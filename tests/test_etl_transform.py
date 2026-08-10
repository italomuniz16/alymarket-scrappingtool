"""Testes de `etl/transform.py`: join, materialização em Parquet particionado,
validação de qualidade pós-carga e troca de versão blue/green.

Reaproveita os fixtures já existentes de EMPRESAS/ESTABELECIMENTOS/SIMPLES/lookups
(os mesmos usados pelos testes do parser br_receita e do etl/loader) — sem criar
fixtures novos.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl
import pytest

from src.etl.loader import load_staging_directory
from src.etl.transform import (
    CANONICAL_PARQUET_SCHEMA,
    QualityThresholds,
    activate_version,
    build_joined_relation,
    get_active_leads_dir,
    get_active_version,
    load_lookup_table_into_duckdb,
    materialize_leads,
    new_version_dir,
    run_quality_checks,
    run_transform_pipeline,
)
from src.ingestion.br_receita.parser import ESTABELECIMENTOS_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures"
EMPRESAS_CSV = FIXTURES / "br_receita_empresas_sample.csv"
ESTABELECIMENTOS_CSV = FIXTURES / "br_receita_estabelecimentos_sample.csv"
SIMPLES_CSV = FIXTURES / "br_receita_simples_sample.csv"
MUNICIPIO_LOOKUP_CSV = FIXTURES / "br_receita_municipio_lookup_sample.csv"
NATUREZA_JURIDICA_LOOKUP_CSV = FIXTURES / "br_receita_natureza_juridica_lookup_sample.csv"

Con = duckdb.DuckDBPyConnection


def _staging_dir(tmp_path: Path, *, extra_estabelecimento_row: str | None = None) -> Path:
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "K1.EMPRECSV").write_bytes(EMPRESAS_CSV.read_bytes())
    (staging_dir / "K1.ESTABELE").write_bytes(ESTABELECIMENTOS_CSV.read_bytes())
    (staging_dir / "K1.SIMPLES").write_bytes(SIMPLES_CSV.read_bytes())
    if extra_estabelecimento_row is not None:
        (staging_dir / "K1.ESTABELE.extra").write_text(
            extra_estabelecimento_row + "\n", encoding="latin-1"
        )
    return staging_dir


def _load_con(tmp_path: Path, *, extra_estabelecimento_row: str | None = None) -> Con:
    con = duckdb.connect(":memory:")
    staging_dir = _staging_dir(tmp_path, extra_estabelecimento_row=extra_estabelecimento_row)
    load_staging_directory(con, staging_dir, tmp_path / "parquet")
    load_lookup_table_into_duckdb(con, MUNICIPIO_LOOKUP_CSV, "lookup_municipio")
    load_lookup_table_into_duckdb(con, NATUREZA_JURIDICA_LOOKUP_CSV, "lookup_natureza_juridica")
    return con


def _joined_rows_by_cnpj(con: Con) -> dict[str, dict[str, object]]:
    rel = build_joined_relation(con)
    columns = rel.columns
    return {row[0]: dict(zip(columns, row, strict=True)) for row in rel.fetchall()}


class TestBuildJoinedRelation:
    def test_join_row_count_and_computed_cnpj_completo(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        rows = _joined_rows_by_cnpj(con)
        assert len(rows) == 3
        assert rows["11111111"]["cnpj_completo"] == "11111111000191"

    def test_join_casts_date_and_decimal(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        row1 = _joined_rows_by_cnpj(con)["11111111"]
        assert row1["data_inicio_atividade"] == date(2020, 1, 1)
        assert row1["capital_social"] == Decimal("150000.00")

    def test_join_resolves_lookups(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        row1 = _joined_rows_by_cnpj(con)["11111111"]
        assert row1["municipio_descricao"] == "SAO PAULO"
        assert row1["natureza_juridica_descricao"] == "SOCIEDADE EMPRESARIA LIMITADA"

    def test_join_includes_simples_fields(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        rows = _joined_rows_by_cnpj(con)
        assert rows["11111111"]["opcao_pelo_simples"] == "S"
        assert rows["22222222"]["opcao_pelo_mei"] == "S"


class TestMaterializeLeads:
    def test_writes_partitioned_parquet_with_canonical_schema(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads(con, version_dir, batch_size=100)

        assert result.n_rows_written == 3
        assert result.n_rows_skipped == 0
        partition_dir = version_dir / "pais=BR"
        assert partition_dir.is_dir()
        assert len(result.part_files) == 1  # 3 linhas cabem num lote de 100

        df = pl.read_parquet(partition_dir / "*.parquet")
        assert df.height == 3
        assert set(df.columns) == set(CANONICAL_PARQUET_SCHEMA)
        assert set(df["fonte"].unique().to_list()) == {"BR_RECEITA"}
        assert set(df["is_synthetic"].unique().to_list()) == {False}
        assert set(df["pais"].unique().to_list()) == {"BR"}

    def test_small_batch_size_creates_multiple_part_files(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads(con, version_dir, batch_size=1)

        assert result.n_rows_written == 3
        assert len(result.part_files) == 3

        df = pl.read_parquet((version_dir / "pais=BR").as_posix() + "/*.parquet")
        assert df.height == 3

    def test_skips_orphan_estabelecimento_without_empresa(self, tmp_path: Path) -> None:
        orphan_values = {
            "cnpj_basico": "99999999",
            "cnpj_ordem": "0001",
            "cnpj_dv": "00",
            "identificador_matriz_filial": "1",
            "nome_fantasia": "ORFAO",
            "situacao_cadastral": "02",
            "data_situacao_cadastral": "20200101",
            "motivo_situacao_cadastral": "00",
            "data_inicio_atividade": "20200101",
            "cnae_fiscal_principal": "0000000",
            "tipo_logradouro": "RUA",
            "logradouro": "RUA X",
            "numero": "1",
            "bairro": "B",
            "cep": "00000000",
            "uf": "SP",
            "municipio": "9999",
        }
        orphan_row = ";".join(orphan_values.get(col, "") for col in ESTABELECIMENTOS_COLUMNS)
        assert orphan_row.count(";") == len(ESTABELECIMENTOS_COLUMNS) - 1

        con = _load_con(tmp_path, extra_estabelecimento_row=orphan_row)
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads(con, version_dir, batch_size=100)

        # 3 estabelecimentos originais + 1 órfão sem empresa correspondente
        # (razao_social ausente -> falha na validação canônica -> pulado).
        assert result.n_rows_written == 3
        assert result.n_rows_skipped == 1

    def test_batch_entirely_skipped_writes_no_part_file_for_it(self, tmp_path: Path) -> None:
        orphan_values = {"cnpj_basico": "99999999", "cnpj_ordem": "0001", "cnpj_dv": "00"}
        orphan_row = ";".join(orphan_values.get(col, "") for col in ESTABELECIMENTOS_COLUMNS)

        con = _load_con(tmp_path, extra_estabelecimento_row=orphan_row)
        version_dir = tmp_path / "versions" / "v1"

        # batch_size=1 -> a linha órfã cai sozinha num lote, que fica inteiramente
        # vazio depois da validação canônica (nenhum part file deve ser gerado pra ele).
        result = materialize_leads(con, version_dir, batch_size=1)

        assert result.n_rows_written == 3
        assert result.n_rows_skipped == 1
        assert len(result.part_files) == 3  # não 4: o lote vazio não gera arquivo


class TestRunQualityChecks:
    def test_passes_on_good_data(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads(con, version_dir)

        report = run_quality_checks(con, version_dir)

        assert report.passed
        assert report.n_rows == 3
        assert report.duplicate_id_estab == 0
        assert report.failures == []

    def test_situacao_distribution_reported(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads(con, version_dir)

        report = run_quality_checks(con, version_dir)

        assert report.situacao_distribution == {"ATIVA": 1, "BAIXADA": 1, "SUSPENSA": 1}

    def test_fails_when_below_min_rows(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads(con, version_dir)

        report = run_quality_checks(con, version_dir, thresholds=QualityThresholds(min_rows=10))

        assert not report.passed
        assert any("abaixo do mínimo" in f for f in report.failures)

    def test_fails_when_null_ratio_exceeds_threshold(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads(con, version_dir)

        # No fixture, 2 das 3 linhas (cnpj 22222222 e 33333333) não têm e-mail.
        thresholds = QualityThresholds(max_null_ratio={"email": 0.1})
        report = run_quality_checks(con, version_dir, thresholds=thresholds)

        assert not report.passed
        assert report.null_ratios["email"] == pytest.approx(2 / 3)
        assert any("email" in f for f in report.failures)

    def test_passes_when_null_ratio_within_threshold(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        version_dir = tmp_path / "versions" / "v1"
        materialize_leads(con, version_dir)

        thresholds = QualityThresholds(max_null_ratio={"cep": 0.0})
        report = run_quality_checks(con, version_dir, thresholds=thresholds)

        # Todas as 3 linhas do fixture têm CEP preenchido -> não deveria reprovar aqui;
        # confere o caminho contrário: threshold impossível (0 nulos tolerados) mas
        # sem nenhum nulo de fato -> continua passando.
        assert report.null_ratios["cep"] == 0.0
        assert report.passed

    def test_detects_unknown_situacao_value(self, tmp_path: Path) -> None:
        con = duckdb.connect(":memory:")
        version_dir = tmp_path / "versions" / "v1"
        partition_dir = version_dir / "pais=BR"
        partition_dir.mkdir(parents=True)

        row = {
            "pais": "BR",
            "id_legal": "1",
            "id_estab": "1",
            "razao_social": "X",
            "nome_fantasia": None,
            "cod_atividade": None,
            "situacao": "CODIGO-DESCONHECIDO",
            "regiao": None,
            "municipio": None,
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
        pl.DataFrame([row], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            partition_dir / "part-00000.parquet"
        )

        report = run_quality_checks(con, version_dir)

        assert not report.passed
        assert any("fora do domínio" in f for f in report.failures)

    def test_detects_duplicate_id_estab(self, tmp_path: Path) -> None:
        con = duckdb.connect(":memory:")
        version_dir = tmp_path / "versions" / "v1"
        partition_dir = version_dir / "pais=BR"
        partition_dir.mkdir(parents=True)

        base_row = {
            "pais": "BR",
            "id_legal": "1",
            "id_estab": "DUPLICADO",
            "razao_social": "X",
            "nome_fantasia": None,
            "cod_atividade": None,
            "situacao": "ATIVA",
            "regiao": None,
            "municipio": None,
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
        pl.DataFrame([base_row, base_row], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            partition_dir / "part-00000.parquet"
        )

        report = run_quality_checks(con, version_dir)

        assert not report.passed
        assert report.duplicate_id_estab == 1
        assert any("duplicado" in f for f in report.failures)


class TestBlueGreenVersioning:
    def test_get_active_version_none_when_never_activated(self, tmp_path: Path) -> None:
        assert get_active_version(tmp_path) is None
        assert get_active_leads_dir(tmp_path) is None

    def test_activate_version_updates_pointer(self, tmp_path: Path) -> None:
        version_dir = new_version_dir(tmp_path)
        version_dir.mkdir(parents=True)

        activate_version(tmp_path, version_dir)

        assert get_active_version(tmp_path) == version_dir.name
        assert get_active_leads_dir(tmp_path) == version_dir

    def test_new_version_dir_produces_unique_names(self, tmp_path: Path) -> None:
        v1 = new_version_dir(tmp_path)
        v2 = new_version_dir(tmp_path)
        assert v1 != v2


class TestRunTransformPipeline:
    def test_activates_on_success(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline(
            con,
            warehouse_dir,
            municipio_lookup_csv=MUNICIPIO_LOOKUP_CSV,
            natureza_juridica_lookup_csv=NATUREZA_JURIDICA_LOOKUP_CSV,
        )

        assert result.activated
        assert result.quality_report.passed
        assert result.materialize_result.n_rows_written == 3
        assert get_active_version(warehouse_dir) == result.version_dir.name

        active_dir = get_active_leads_dir(warehouse_dir)
        assert active_dir is not None
        df = pl.read_parquet((active_dir / "pais=BR").as_posix() + "/*.parquet")
        assert df.height == 3

    def test_does_not_activate_on_quality_failure(self, tmp_path: Path) -> None:
        con = _load_con(tmp_path)
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline(
            con,
            warehouse_dir,
            municipio_lookup_csv=MUNICIPIO_LOOKUP_CSV,
            natureza_juridica_lookup_csv=NATUREZA_JURIDICA_LOOKUP_CSV,
            thresholds=QualityThresholds(min_rows=100),
        )

        assert not result.activated
        assert not result.quality_report.passed
        # A versão foi materializada em disco (para inspeção), mas não ativada.
        assert result.version_dir.exists()
        assert get_active_version(warehouse_dir) is None

    def test_second_successful_run_replaces_active_pointer_without_deleting_first(
        self, tmp_path: Path
    ) -> None:
        con = _load_con(tmp_path)
        warehouse_dir = tmp_path / "warehouse"

        first = run_transform_pipeline(
            con,
            warehouse_dir,
            municipio_lookup_csv=MUNICIPIO_LOOKUP_CSV,
            natureza_juridica_lookup_csv=NATUREZA_JURIDICA_LOOKUP_CSV,
        )
        second = run_transform_pipeline(
            con,
            warehouse_dir,
            municipio_lookup_csv=MUNICIPIO_LOOKUP_CSV,
            natureza_juridica_lookup_csv=NATUREZA_JURIDICA_LOOKUP_CSV,
        )

        assert first.version_dir != second.version_dir
        assert get_active_version(warehouse_dir) == second.version_dir.name
        # Versão antiga continua intacta no disco (blue/green não apaga).
        assert first.version_dir.exists()
        assert (first.version_dir / "pais=BR").exists()
