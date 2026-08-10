"""Testes de `etl/loader.py`, sobre uma base pequena (os fixtures de 3 linhas de
EMPRESAS/ESTABELECIMENTOS/SIMPLES já usados pelos testes do parser br_receita —
sem criar fixtures novos).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from src.etl.loader import (
    LoaderError,
    csv_to_parquet,
    distinct_value_counts,
    duplicate_key_count,
    load_parquet_to_table,
    load_staging_directory,
    null_counts,
    open_warehouse,
    sanity_check,
    table_row_count,
)

FIXTURES = Path(__file__).parent / "fixtures"
EMPRESAS_CSV = FIXTURES / "br_receita_empresas_sample.csv"
ESTABELECIMENTOS_CSV = FIXTURES / "br_receita_estabelecimentos_sample.csv"
SIMPLES_CSV = FIXTURES / "br_receita_simples_sample.csv"

Con = duckdb.DuckDBPyConnection


class TestCsvToParquet:
    def test_preserves_shape_and_accents(self, tmp_path: Path) -> None:
        parquet_path = csv_to_parquet(ESTABELECIMENTOS_CSV, "ESTABELECIMENTOS", tmp_path)

        assert parquet_path.exists()
        with duckdb.connect(":memory:") as con:
            rel = con.sql(f"SELECT * FROM read_parquet('{parquet_path.as_posix()}')")
            assert len(rel.columns) == 30
            rows = rel.fetchall()
            assert len(rows) == 3
            nomes_fantasia = [row[4] for row in rows]
            assert "PADARIA SÃO JOÃO" in nomes_fantasia
            assert "COMÉRCIO DE CONFECÇÕES" in nomes_fantasia

    def test_unknown_entity_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LoaderError):
            csv_to_parquet(EMPRESAS_CSV, "SOCIOS", tmp_path)


class TestLoadParquetToTable:
    def test_merges_multiple_files(self, tmp_path: Path) -> None:
        parquet_a = csv_to_parquet(EMPRESAS_CSV, "EMPRESAS", tmp_path / "pq")
        parquet_b = csv_to_parquet(EMPRESAS_CSV, "EMPRESAS", tmp_path / "pq2")

        with duckdb.connect(":memory:") as con:
            n = load_parquet_to_table(con, [parquet_a, parquet_b], "staging_empresas")
            assert n == 6
            assert table_row_count(con, "staging_empresas") == 6

    def test_replace_true_overwrites_existing_table(self, tmp_path: Path) -> None:
        parquet_path = csv_to_parquet(EMPRESAS_CSV, "EMPRESAS", tmp_path)

        with duckdb.connect(":memory:") as con:
            load_parquet_to_table(con, [parquet_path], "staging_empresas")
            n = load_parquet_to_table(con, [parquet_path], "staging_empresas", replace=True)
            assert n == 3

    def test_empty_list_raises(self) -> None:
        with duckdb.connect(":memory:") as con, pytest.raises(LoaderError):
            load_parquet_to_table(con, [], "staging_empresas")


def _copy_fixtures_with_rf_names(tmp_path: Path) -> Path:
    """Copia os 3 fixtures + 1 arquivo não reconhecido para tmp_path, com nomes no
    padrão real de extração da Receita (sufixo EMPRECSV/ESTABELE/SIMPLES/SOCIOCSV)."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "K3241.K03200Y0.D50812.EMPRECSV").write_bytes(EMPRESAS_CSV.read_bytes())
    (staging_dir / "K3241.K03200Y1.D50812.ESTABELE").write_bytes(
        ESTABELECIMENTOS_CSV.read_bytes()
    )
    (staging_dir / "K3241.K03200Y2.D50812.SIMPLES.CSV").write_bytes(SIMPLES_CSV.read_bytes())
    (staging_dir / "K3241.K03200Y3.D50812.SOCIOCSV").write_text(
        "lixo;nao;reconhecido\n", encoding="latin-1"
    )
    return staging_dir


class TestLoadStagingDirectory:
    def test_end_to_end_creates_one_table_per_entity(self, tmp_path: Path) -> None:
        staging_dir = _copy_fixtures_with_rf_names(tmp_path)

        with duckdb.connect(":memory:") as con:
            counts = load_staging_directory(con, staging_dir, tmp_path / "parquet")

            assert counts == {"EMPRESAS": 3, "ESTABELECIMENTOS": 3, "SIMPLES": 3}

            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            assert tables == {"staging_empresas", "staging_estabelecimentos", "staging_simples"}

    def test_only_filters_entities(self, tmp_path: Path) -> None:
        staging_dir = _copy_fixtures_with_rf_names(tmp_path)

        with duckdb.connect(":memory:") as con:
            counts = load_staging_directory(
                con, staging_dir, tmp_path / "parquet", only=["EMPRESAS"]
            )

            assert counts == {"EMPRESAS": 3}
            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            assert tables == {"staging_empresas"}

    def test_ignores_subdirectories(self, tmp_path: Path) -> None:
        staging_dir = _copy_fixtures_with_rf_names(tmp_path)
        (staging_dir / "subpasta_inesperada").mkdir()

        with duckdb.connect(":memory:") as con:
            counts = load_staging_directory(con, staging_dir, tmp_path / "parquet")

            assert counts == {"EMPRESAS": 3, "ESTABELECIMENTOS": 3, "SIMPLES": 3}


class TestOpenWarehouse:
    def test_persists_across_reconnects(self, tmp_path: Path) -> None:
        db_path = tmp_path / "warehouse.duckdb"
        staging_dir = _copy_fixtures_with_rf_names(tmp_path)

        con = open_warehouse(db_path)
        load_staging_directory(con, staging_dir, tmp_path / "parquet", only=["EMPRESAS"])
        con.close()

        assert db_path.exists()

        con2 = open_warehouse(db_path)
        try:
            assert table_row_count(con2, "staging_empresas") == 3
        finally:
            con2.close()


class TestSanityFunctions:
    @pytest.fixture
    def loaded_estabelecimentos(self, tmp_path: Path) -> duckdb.DuckDBPyConnection:
        parquet_path = csv_to_parquet(ESTABELECIMENTOS_CSV, "ESTABELECIMENTOS", tmp_path)
        con = duckdb.connect(":memory:")
        load_parquet_to_table(con, [parquet_path], "staging_estabelecimentos")
        return con

    def test_table_row_count(self, loaded_estabelecimentos: Con) -> None:
        assert table_row_count(loaded_estabelecimentos, "staging_estabelecimentos") == 3

    def test_null_counts_all_empty_column(self, loaded_estabelecimentos: Con) -> None:
        counts = null_counts(
            loaded_estabelecimentos, "staging_estabelecimentos", ["nome_cidade_exterior"]
        )
        assert counts == {"nome_cidade_exterior": 3}

    def test_null_counts_fully_populated_column(self, loaded_estabelecimentos: Con) -> None:
        counts = null_counts(loaded_estabelecimentos, "staging_estabelecimentos", ["cnpj_basico"])
        assert counts == {"cnpj_basico": 0}

    def test_null_counts_defaults_to_all_columns(self, loaded_estabelecimentos: Con) -> None:
        counts = null_counts(loaded_estabelecimentos, "staging_estabelecimentos")
        assert len(counts) == 30

    def test_duplicate_key_count_zero_when_unique(self, loaded_estabelecimentos: Con) -> None:
        n_dup = duplicate_key_count(
            loaded_estabelecimentos, "staging_estabelecimentos", ["cnpj_basico"]
        )
        assert n_dup == 0

    def test_duplicate_key_count_detects_duplicates(self, tmp_path: Path) -> None:
        parquet_path = csv_to_parquet(ESTABELECIMENTOS_CSV, "ESTABELECIMENTOS", tmp_path)
        with duckdb.connect(":memory:") as con:
            # Carrega o mesmo parquet 2x na mesma tabela -> cada cnpj_basico duplicado.
            load_parquet_to_table(con, [parquet_path, parquet_path], "staging_estabelecimentos")
            assert duplicate_key_count(con, "staging_estabelecimentos", ["cnpj_basico"]) == 3

    def test_distinct_value_counts(self, loaded_estabelecimentos: Con) -> None:
        counts = distinct_value_counts(
            loaded_estabelecimentos, "staging_estabelecimentos", "situacao_cadastral"
        )
        assert counts == {"02": 1, "08": 1, "03": 1}

    def test_sanity_check_aggregates_everything(self, loaded_estabelecimentos: Con) -> None:
        report = sanity_check(
            loaded_estabelecimentos,
            "staging_estabelecimentos",
            key_columns=["cnpj_basico"],
            columns=["cnpj_basico", "nome_cidade_exterior"],
        )

        assert report.table_name == "staging_estabelecimentos"
        assert report.n_rows == 3
        assert report.null_counts == {"cnpj_basico": 0, "nome_cidade_exterior": 3}
        assert report.duplicate_keys == 0
