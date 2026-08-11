"""Testes de `etl/transform.materialize_leads_opencnpj`/`run_transform_pipeline_opencnpj`:
materialização em Parquet particionado (`pais=BR/`) a partir de registros já
buscados na API do OpenCNPJ (sem I/O de arquivo — ao contrário do lado FR, aqui os
registros já chegam em memória)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from src.etl.transform import (
    CANONICAL_PARQUET_SCHEMA,
    QualityThresholds,
    activate_version,
    get_active_version,
    materialize_leads_opencnpj,
    new_version_dir,
    run_transform_pipeline_opencnpj,
)

RECORD_1 = {
    "cnpj": "00000000083208",
    "situacaoCadastral": "Ativa",
    "razaoSocial": "BANCO DO BRASIL SA",
    "nomeFantasia": "PARAISO - SAO PAULO (SP)",
    "dataInicioAtividades": "26/09/1974",
    "naturezaJuridica": "Sociedade de Economia Mista (2038)",
    "capitalSocial": 120000000000,
    "email": "AGE1189@BB.COM.BR",
    "telefone": "(11) 35550400",
    "municipio": "SAO PAULO",
    "uf": "SP",
    "cep": "04004-040",
    "cnaes": [{"cnae": "64.22-1-00", "descricao": "Bancos"}],
}
RECORD_2 = {
    "cnpj": "11111111000111",
    "situacaoCadastral": "Ativa",
    "razaoSocial": "EMPRESA DOIS LTDA",
    "nomeFantasia": None,
    "dataInicioAtividades": "01/01/2020",
    "naturezaJuridica": "Sociedade Empresaria Limitada (2062)",
    "capitalSocial": 50000,
    "email": None,
    "telefone": None,
    "municipio": "CAMPINAS",
    "uf": "SP",
    "cep": "13000-000",
    "cnaes": [{"cnae": "47.11-3-02", "descricao": "Comercio"}],
}
INVALID_RECORD = {**RECORD_2, "cnpj": "22222222000122", "razaoSocial": None}


class TestMaterializeLeadsOpencnpj:
    def test_writes_partitioned_parquet_with_canonical_schema(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads_opencnpj([RECORD_1, RECORD_2], version_dir)

        assert result.n_rows_written == 2
        assert result.n_rows_skipped == 0
        partition_dir = version_dir / "pais=BR"
        assert partition_dir.is_dir()

        df = pl.read_parquet(partition_dir / "*.parquet")
        assert df.height == 2
        assert set(df.columns) == set(CANONICAL_PARQUET_SCHEMA)
        assert set(df["fonte"].unique().to_list()) == {"BR_OPENCNPJ"}
        assert set(df["is_synthetic"].unique().to_list()) == {False}
        assert set(df["pais"].unique().to_list()) == {"BR"}

    def test_invalid_record_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"

        result = materialize_leads_opencnpj([RECORD_1, INVALID_RECORD], version_dir)

        assert result.n_rows_written == 1
        assert result.n_rows_skipped == 1

    def test_small_batch_size_creates_multiple_part_files(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"
        result = materialize_leads_opencnpj([RECORD_1, RECORD_2], version_dir, batch_size=1)
        assert result.n_rows_written == 2
        assert len(result.part_files) == 2

    def test_empty_input_writes_nothing(self, tmp_path: Path) -> None:
        version_dir = tmp_path / "versions" / "v1"
        result = materialize_leads_opencnpj([], version_dir)
        assert result.n_rows_written == 0
        assert result.part_files == []


class TestRunTransformPipelineOpencnpj:
    def test_activates_on_success(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline_opencnpj([RECORD_1, RECORD_2], warehouse_dir)

        assert result.activated
        assert result.quality_report.passed
        assert result.materialize_result.n_rows_written == 2
        assert get_active_version(warehouse_dir) == result.version_dir.name

        df = pl.read_parquet((result.version_dir / "pais=BR").as_posix() + "/*.parquet")
        assert df.height == 2
        assert set(df["fonte"].unique().to_list()) == {"BR_OPENCNPJ"}

    def test_does_not_activate_on_quality_failure(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        result = run_transform_pipeline_opencnpj(
            [RECORD_1], warehouse_dir, thresholds=QualityThresholds(min_rows=100)
        )

        assert not result.activated
        assert not result.quality_report.passed
        assert get_active_version(warehouse_dir) is None

    def test_preserves_existing_fr_partition(self, tmp_path: Path) -> None:
        """Espelho do teste equivalente do lado FR: rodar o pipeline OpenCNPJ (BR)
        não pode apagar dados FR de uma rodada agendada separadamente."""
        warehouse_dir = tmp_path / "warehouse"
        fr_row: dict[str, object] = {
            "pais": "FR",
            "id_legal": "123456789",
            "id_estab": "12345678900015",
            "razao_social": "EMPRESA FRANCESA",
            "nome_fantasia": None,
            "cod_atividade": "62.01Z",
            "situacao": "ATIVA",
            "regiao": "75",
            "municipio": "PARIS",
            "cep": None,
            "telefone": None,
            "email": None,
            "data_inicio_atividade": None,
            "porte": None,
            "capital_social": None,
            "natureza_juridica": None,
            "score_icp": None,
            "fonte": "FR_SIRENE",
            "enriquecido_em": None,
            "is_synthetic": False,
            "flag_difusao_restrita": False,
        }
        pre_existing = new_version_dir(warehouse_dir)
        fr_partition = pre_existing / "pais=FR"
        fr_partition.mkdir(parents=True)
        pl.DataFrame([fr_row], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            fr_partition / "part-00000.parquet"
        )
        activate_version(warehouse_dir, pre_existing)

        result = run_transform_pipeline_opencnpj([RECORD_1], warehouse_dir)

        assert result.activated
        assert (result.version_dir / "pais=BR").exists()
        df = pl.read_parquet((result.version_dir / "pais=FR").as_posix() + "/*.parquet")
        assert df.height == 1
        assert df["id_estab"].to_list() == ["12345678900015"]

    def test_second_run_replaces_pointer_without_deleting_first(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"

        first = run_transform_pipeline_opencnpj([RECORD_1], warehouse_dir)
        second = run_transform_pipeline_opencnpj([RECORD_2], warehouse_dir)

        assert first.version_dir != second.version_dir
        assert get_active_version(warehouse_dir) == second.version_dir.name
        assert first.version_dir.exists()
