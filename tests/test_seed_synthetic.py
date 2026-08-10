"""Testes de `seed/synthetic.py`: geração de leads sintéticos (BR/FR), respeito às
proporções configuráveis, isolamento físico da gravação, e — o requisito central —
que 100% dos registros gerados saem com `is_synthetic=True`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

from src.etl.transform import new_version_dir
from src.seed.synthetic import (
    DEFAULT_DEMO_DIR,
    FONTE_DEMO,
    ProportionSpec,
    SyntheticLeadGenerator,
    build_arg_parser,
    gerar_leads_sinteticos,
    load_proportions,
    main,
    write_demo_leads,
)


class TestLoadProportions:
    def test_groups_by_dimension(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "proporcoes.csv"
        csv_path.write_text(
            "dimensao,valor,peso\n"
            "atividade,4721102,35\n"
            "atividade,8630501,20\n"
            "regiao,SP,40\n"
            "regiao,RJ,20\n",
            encoding="utf-8",
        )
        result = load_proportions(csv_path)

        expected_atividade = ProportionSpec(valores=("4721102", "8630501"), pesos=(35.0, 20.0))
        expected_regiao = ProportionSpec(valores=("SP", "RJ"), pesos=(40.0, 20.0))
        assert result["atividade"] == expected_atividade
        assert result["regiao"] == expected_regiao

    def test_blank_rows_ignored(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "proporcoes.csv"
        csv_path.write_text("dimensao,valor,peso\n,,\natividade,4721102,10\n", encoding="utf-8")
        result = load_proportions(csv_path)
        assert list(result.keys()) == ["atividade"]


class TestSyntheticLeadGeneratorConstruction:
    def test_invalid_pais_raises(self) -> None:
        with pytest.raises(ValueError):
            SyntheticLeadGenerator("US")


class TestGerarUmBR:
    def test_ids_are_consistent_cnpj(self) -> None:
        gerador = SyntheticLeadGenerator("BR", seed=1)
        lead = gerador.gerar_um()

        assert lead["pais"] == "BR"
        assert len(lead["id_legal"]) == 8
        assert len(lead["id_estab"]) == 14
        assert lead["id_estab"].startswith(lead["id_legal"])
        assert lead["id_legal"].isdigit()
        assert lead["id_estab"].isdigit()

    def test_is_synthetic_and_fonte(self) -> None:
        lead = SyntheticLeadGenerator("BR", seed=1).gerar_um()
        assert lead["is_synthetic"] is True
        assert lead["fonte"] == FONTE_DEMO == "DEMO"
        assert lead["flag_difusao_restrita"] is False

    def test_telefone_is_digits_only(self) -> None:
        lead = SyntheticLeadGenerator("BR", seed=1).gerar_um()
        assert lead["telefone"].isdigit()

    def test_email_looks_valid(self) -> None:
        lead = SyntheticLeadGenerator("BR", seed=1).gerar_um()
        assert "@" in lead["email"]

    def test_situacao_and_porte_from_default_pools(self) -> None:
        lead = SyntheticLeadGenerator("BR", seed=1).gerar_um()
        assert lead["situacao"] in {"ATIVA", "BAIXADA", "SUSPENSA"}
        assert lead["porte"] in {"MICRO EMPRESA", "EMPRESA DE PEQUENO PORTE", "DEMAIS"}


class TestGerarUmFR:
    def test_ids_are_consistent_siren_siret(self) -> None:
        gerador = SyntheticLeadGenerator("FR", seed=1)
        lead = gerador.gerar_um()

        assert lead["pais"] == "FR"
        assert len(lead["id_legal"]) == 9
        assert len(lead["id_estab"]) == 14
        assert lead["id_estab"].startswith(lead["id_legal"])

    def test_is_synthetic_and_fonte(self) -> None:
        lead = SyntheticLeadGenerator("FR", seed=1).gerar_um()
        assert lead["is_synthetic"] is True
        assert lead["fonte"] == "DEMO"
        assert lead["flag_difusao_restrita"] is False


class TestReprodutibilidade:
    def test_same_seed_produces_same_lead(self) -> None:
        lead_a = SyntheticLeadGenerator("BR", seed=42).gerar_um()
        lead_b = SyntheticLeadGenerator("BR", seed=42).gerar_um()
        assert lead_a == lead_b

    def test_different_seeds_produce_different_ids(self) -> None:
        lead_a = SyntheticLeadGenerator("BR", seed=1).gerar_um()
        lead_b = SyntheticLeadGenerator("BR", seed=2).gerar_um()
        assert lead_a["id_legal"] != lead_b["id_legal"]


class TestGerarMuitos:
    def test_yields_exactly_n(self) -> None:
        leads = list(SyntheticLeadGenerator("BR", seed=1).gerar_muitos(10))
        assert len(leads) == 10

    def test_each_lead_has_all_canonical_fields(self) -> None:
        from src.ingestion.base import CanonicalLead

        leads = list(SyntheticLeadGenerator("FR", seed=1).gerar_muitos(5))
        expected_fields = set(CanonicalLead.model_fields)
        for lead in leads:
            assert set(lead) == expected_fields


class TestProporcoesConfiguraveis:
    def test_atividade_forcada_por_proporcoes(self) -> None:
        # Um único valor com todo o peso -> toda amostra tem que sair ele, sempre
        # (sem depender de estatística/flakiness).
        proporcoes = {"atividade": ProportionSpec(valores=("9999999",), pesos=(1.0,))}
        gerador = SyntheticLeadGenerator("BR", proporcoes=proporcoes, seed=1)

        leads = list(gerador.gerar_muitos(20))
        assert all(lead["cod_atividade"] == "9999999" for lead in leads)

    def test_regiao_forcada_por_proporcoes(self) -> None:
        proporcoes = {"regiao": ProportionSpec(valores=("XX",), pesos=(1.0,))}
        gerador = SyntheticLeadGenerator("BR", proporcoes=proporcoes, seed=1)

        leads = list(gerador.gerar_muitos(20))
        assert all(lead["regiao"] == "XX" for lead in leads)

    def test_sem_proporcoes_usa_pool_default(self) -> None:
        from src.seed.synthetic import _DEFAULT_ATIVIDADES

        leads = list(SyntheticLeadGenerator("BR", seed=1).gerar_muitos(20))
        assert all(lead["cod_atividade"] in _DEFAULT_ATIVIDADES["BR"] for lead in leads)


class TestGerarLeadsSinteticos:
    def test_returns_n_leads(self) -> None:
        leads = gerar_leads_sinteticos(7, "BR", seed=1)
        assert len(leads) == 7

    def test_loads_proportions_from_path(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "proporcoes.csv"
        csv_path.write_text("dimensao,valor,peso\natividade,1234567,1\n", encoding="utf-8")

        leads = gerar_leads_sinteticos(5, "BR", proporcoes_path=csv_path, seed=1)
        assert all(lead["cod_atividade"] == "1234567" for lead in leads)


# -- O requisito central: 100% dos registros gerados são is_synthetic=True ----------


class TestCemPorCentoSintetico:
    @pytest.mark.parametrize("pais", ["BR", "FR"])
    def test_todos_os_registros_sao_is_synthetic(self, pais: str) -> None:
        leads = gerar_leads_sinteticos(100, pais, seed=123)
        assert len(leads) == 100
        assert all(lead["is_synthetic"] is True for lead in leads)
        assert all(lead["fonte"] == "DEMO" for lead in leads)

    def test_nenhum_registro_tem_flag_difusao_restrita(self) -> None:
        leads = gerar_leads_sinteticos(50, "FR", seed=1)
        assert all(lead["flag_difusao_restrita"] is False for lead in leads)


class TestWriteDemoLeads:
    def test_writes_to_isolated_partition(self, tmp_path: Path) -> None:
        leads = gerar_leads_sinteticos(5, "BR", seed=1)
        demo_dir = tmp_path / "demo"

        path = write_demo_leads(leads, demo_dir, pais="BR")

        assert path == demo_dir / "pais=BR" / "demo.parquet"
        df = pl.read_parquet(path)
        assert df.height == 5
        assert set(df["is_synthetic"].unique().to_list()) == {True}

    def test_raises_if_any_record_is_not_synthetic(self, tmp_path: Path) -> None:
        leads = gerar_leads_sinteticos(3, "BR", seed=1)
        leads[1] = {**leads[1], "is_synthetic": False}

        with pytest.raises(ValueError):
            write_demo_leads(leads, tmp_path / "demo", pais="BR")

    def test_never_shares_a_subtree_with_real_data_versions(self, tmp_path: Path) -> None:
        """Mesmo usando o MESMO diretório-base que o warehouse real, a partição demo
        e a árvore de versões reais (etl/transform.py) nunca se sobrepõem."""
        warehouse_dir = tmp_path / "warehouse"
        leads = gerar_leads_sinteticos(3, "BR", seed=1)

        demo_path = write_demo_leads(leads, warehouse_dir, pais="BR")
        real_version_dir = new_version_dir(warehouse_dir)

        assert "versions" not in demo_path.parts
        assert not str(real_version_dir).startswith(str(demo_path.parent))
        assert not str(demo_path).startswith(str(real_version_dir))


class TestCli:
    def test_build_arg_parser_requires_gerar_and_pais(self) -> None:
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_rejects_invalid_pais(self) -> None:
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--gerar", "5", "--pais", "US"])

    def test_parses_valid_args(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--gerar", "10", "--pais", "FR", "--seed", "7"])
        assert args.n == 10
        assert args.pais == "FR"
        assert args.seed == 7
        assert args.demo_dir == DEFAULT_DEMO_DIR

    def test_main_generates_and_writes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        demo_dir = tmp_path / "demo"
        argv = ["--gerar", "5", "--pais", "BR", "--demo-dir", str(demo_dir), "--seed", "1"]
        exit_code = main(argv)

        assert exit_code == 0
        assert "5 lead(s) sintético(s)" in capsys.readouterr().out
        df = pl.read_parquet(demo_dir / "pais=BR" / "demo.parquet")
        assert df.height == 5
        assert set(df["is_synthetic"].unique().to_list()) == {True}

    def test_runs_as_a_real_subprocess_script(self, tmp_path: Path) -> None:
        """Roda `python -m src.seed.synthetic --gerar N --pais BR` de verdade, o
        exemplo exato do docstring do módulo."""
        demo_dir = tmp_path / "demo"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.seed.synthetic",
                "--gerar",
                "10",
                "--pais",
                "BR",
                "--demo-dir",
                str(demo_dir),
                "--seed",
                "1",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=Path(__file__).parent.parent,
        )

        assert result.returncode == 0, result.stderr
        assert "10 lead(s) sintético(s)" in result.stdout
        df = pl.read_parquet(demo_dir / "pais=BR" / "demo.parquet")
        assert set(df["is_synthetic"].unique().to_list()) == {True}
