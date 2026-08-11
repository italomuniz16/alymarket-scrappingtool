"""Testes de `etl/transform.materialize_leads_fr`: join em Python (unidade legal <->
estabelecimento, por SIREN) e materialização em Parquet particionado (`pais=FR/`).

Reaproveita os fixtures já existentes de `fr_sirene_unitelegale_sample.csv`/
`fr_sirene_etablissement_sample.csv` (mesmos 3 SIRENs nos dois arquivos: um aberto,
um "diffusion partielle", um com statut_diffusion vazio) — sem criar fixtures novos.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from src.etl.transform import (
    CANONICAL_PARQUET_SCHEMA,
    QualityThresholds,
    activate_version,
    get_active_version,
    materialize_leads_fr,
    new_version_dir,
    run_transform_pipeline_fr,
)

FIXTURES = Path(__file__).parent / "fixtures"
UNITE_LEGALE_CSV = FIXTURES / "fr_sirene_unitelegale_sample.csv"
ETABLISSEMENT_CSV = FIXTURES / "fr_sirene_etablissement_sample.csv"


class TestMaterializeLeadsFr:
    def test_writes_partitioned_parquet_with_canonical_schema(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], version_dir)

        assert result.n_rows_written == 3
        assert result.n_rows_skipped == 0
        partition_dir = version_dir / "pais=FR"
        assert partition_dir.is_dir()

        df = pl.read_parquet(partition_dir / "*.parquet")
        assert df.height == 3
        assert set(df.columns) == set(CANONICAL_PARQUET_SCHEMA)
        assert set(df["fonte"].unique().to_list()) == {"FR_SIRENE"}
        assert set(df["is_synthetic"].unique().to_list()) == {False}
        assert set(df["pais"].unique().to_list()) == {"FR"}

    def test_flag_difusao_restrita_reflects_statut_diffusion_per_row(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], version_dir)

        df = pl.read_parquet(version_dir / "pais=FR" / "*.parquet")
        by_siret = {row["id_estab"]: row["flag_difusao_restrita"] for row in df.to_dicts()}

        # Fixture: siren 123456789 (statut "O"), 987654321 (statut "P"), 111222333
        # (statut vazio) -- ver tests/fixtures/fr_sirene_*_sample.csv.
        assert by_siret["12345678900015"] is False
        assert by_siret["98765432100012"] is True
        assert by_siret["11122233300021"] is True

    def test_small_batch_size_creates_multiple_part_files(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"
        result = materialize_leads_fr(
            [UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], version_dir, batch_size=1
        )
        assert result.n_rows_written == 3
        assert len(result.part_files) == 3

    def test_orphan_etablissement_without_matching_unite_legale_is_skipped(
        self, tmp_path: Path
    ) -> None:
        orphan_csv = tmp_path / "orphan.csv"
        etablissement_lines = ETABLISSEMENT_CSV.read_text(encoding="utf-8").splitlines()
        header, rows = etablissement_lines[0], etablissement_lines[1:]
        orphan_row = (
            rows[0]
            .replace("123456789", "000000000", 1)
            .replace("12345678900015", "00000000000099", 1)
        )
        orphan_csv.write_text("\n".join([header, *rows, orphan_row]) + "\n", encoding="utf-8")

        version_dir = tmp_path / "versions" / "v1"
        result = materialize_leads_fr([UNITE_LEGALE_CSV], [orphan_csv], version_dir)

        assert result.n_rows_written == 3
        assert result.n_rows_skipped == 1

    def test_empty_inputs_write_nothing(self, tmp_path: Path) -> None:
        empty_ul = tmp_path / "empty_ul.csv"
        empty_ul.write_text(
            UNITE_LEGALE_CSV.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8"
        )
        empty_etab = tmp_path / "empty_etab.csv"
        empty_etab.write_text(
            ETABLISSEMENT_CSV.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8"
        )

        version_dir = tmp_path / "versions" / "v1"
        result = materialize_leads_fr([empty_ul], [empty_etab], version_dir)

        assert result.n_rows_written == 0
        assert result.part_files == []


class TestRunTransformPipelineFr:
    def test_activates_on_success(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], warehouse_dir)

        assert result.activated
        assert result.quality_report.passed
        assert result.materialize_result.n_rows_written == 3
        assert get_active_version(warehouse_dir) == result.version_dir.name

        df = pl.read_parquet((result.version_dir / "pais=FR").as_posix() + "/*.parquet")
        assert df.height == 3
        assert set(df["fonte"].unique().to_list()) == {"FR_SIRENE"}

    def test_does_not_activate_on_quality_failure(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline_fr(
            [UNITE_LEGALE_CSV],
            [ETABLISSEMENT_CSV],
            warehouse_dir,
            thresholds=QualityThresholds(min_rows=100),
        )

        assert not result.activated
        assert not result.quality_report.passed
        assert get_active_version(warehouse_dir) is None

    def test_preserves_existing_br_partition(self, tmp_path: Path) -> None:
        """Espelho do teste equivalente do lado BR: rodar o pipeline FR não pode
        apagar dados BR de uma rodada agendada separadamente (ver src/scheduler/)."""
        warehouse_dir = tmp_path / "warehouse"
        br_row: dict[str, object] = {
            "pais": "BR",
            "id_legal": "11111111",
            "id_estab": "11111111000191",
            "razao_social": "EMPRESA BRASILEIRA",
            "nome_fantasia": None,
            "cod_atividade": "8630501",
            "situacao": "ATIVA",
            "regiao": "SP",
            "municipio": "SAO PAULO",
            "cep": None,
            "telefone": None,
            "email": None,
            "data_inicio_atividade": None,
            "porte": None,
            "capital_social": None,
            "natureza_juridica": None,
            "score_icp": None,
            "fonte": "BR_RECEITA",
            "enriquecido_em": None,
            "is_synthetic": False,
            "flag_difusao_restrita": False,
        }
        pre_existing = new_version_dir(warehouse_dir)
        br_partition = pre_existing / "pais=BR"
        br_partition.mkdir(parents=True)
        pl.DataFrame([br_row], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            br_partition / "part-00000.parquet"
        )
        activate_version(warehouse_dir, pre_existing)

        result = run_transform_pipeline_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], warehouse_dir)

        assert result.activated
        assert (result.version_dir / "pais=FR").exists()
        df = pl.read_parquet((result.version_dir / "pais=BR").as_posix() + "/*.parquet")
        assert df.height == 1
        assert df["id_estab"].to_list() == ["11111111000191"]

    def test_second_run_replaces_pointer_without_deleting_first(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        first = run_transform_pipeline_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], warehouse_dir)
        second = run_transform_pipeline_fr([UNITE_LEGALE_CSV], [ETABLISSEMENT_CSV], warehouse_dir)

        assert first.version_dir != second.version_dir
        assert get_active_version(warehouse_dir) == second.version_dir.name
        assert first.version_dir.exists()
