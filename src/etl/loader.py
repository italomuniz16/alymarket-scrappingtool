"""Carga dos CSVs de staging (extraídos por `ingestion/br_receita/extractor.py`) no
DuckDB, de forma rápida e apta a dezenas de milhões de linhas sem estourar memória.

## Por que SQL `read_csv`, não a API Python

A API Python do DuckDB (`duckdb.read_csv(...)` / `con.read_csv(...)`) **não suporta**
encoding não-UTF-8: `BinderException: Copy is only supported for UTF-8 encoded files`.
O SQL `read_csv(...)` nativo (usado aqui via `con.sql`/`con.execute`), porém, aceita
`encoding='latin-1'` normalmente e lê o arquivo em streaming direto do disco — sem
precisar transcodificar o arquivo inteiro em Python primeiro (o que exigiria carregar
o texto inteiro em memória, inviável para os ~30-40 GB reais da base completa).

## Por que um Parquet intermediário

Cada CSV extraído é convertido para Parquet via `COPY (...) TO ... (FORMAT PARQUET)`
— também streaming, o motor do DuckDB nunca materializa o resultado inteiro em
memória Python. O Parquet serve de checkpoint: mais rápido de reler que o CSV bruto,
e é o formato de storage analítico do próprio projeto (ver docs/PRD.md §4.2-4.3).
Só depois o(s) Parquet(s) de uma entidade são carregados numa tabela DuckDB.

## Staging vs. transform

Este módulo carrega dados **brutos**: todas as colunas ficam `VARCHAR`, sem conversão
de tipo (datas continuam `AAAAMMDD`, capital social continua string com vírgula
decimal). É o padrão ELT clássico — normalização de negócio (usando
`ingestion/br_receita/parser.py`, que já faz esse trabalho linha-a-linha) fica para
`etl/transform.py` (Fase 1, ainda não implementado), não para a carga em massa.

## Memória

`load_parquet_to_table`/`load_staging_directory` recebem a conexão DuckDB de fora —
use `open_warehouse()` para abrir um banco **file-backed** (não `:memory:`) em
produção: o motor gerencia seu próprio buffer pool e faz spill a disco, o que é o que
de fato evita estourar a memória do processo com dezenas de milhões de linhas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import duckdb

from src.ingestion.br_receita.parser import (
    EMPRESAS_COLUMNS,
    ESTABELECIMENTOS_COLUMNS,
    SIMPLES_COLUMNS,
    detect_entity,
)

logger = logging.getLogger(__name__)

CSV_ENCODING = "latin-1"
CSV_SEP = ";"
CSV_QUOTE = '"'

_ENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "EMPRESAS": EMPRESAS_COLUMNS,
    "ESTABELECIMENTOS": ESTABELECIMENTOS_COLUMNS,
    "SIMPLES": SIMPLES_COLUMNS,
}

_ENTITY_TABLE_NAMES: dict[str, str] = {
    "EMPRESAS": "staging_empresas",
    "ESTABELECIMENTOS": "staging_estabelecimentos",
    "SIMPLES": "staging_simples",
}


class LoaderError(RuntimeError):
    """Levantado para entidade desconhecida ou lista de arquivos vazia."""


def open_warehouse(db_path: Path | str) -> duckdb.DuckDBPyConnection:
    """Abre (criando se necessário) o banco DuckDB persistente do warehouse.

    Use isto em produção em vez de `duckdb.connect(":memory:")`: um arquivo `.duckdb`
    em disco permite ao motor gerenciar seu próprio buffer pool e fazer spill a disco,
    o que é necessário para não estourar memória com dezenas de milhões de linhas.
    """
    return duckdb.connect(str(db_path))


def _quote_literal(path: Path) -> str:
    """Escapa um caminho para uso como literal string dentro de SQL."""
    return path.as_posix().replace("'", "''")


def _read_csv_sql(csv_path: Path, columns: tuple[str, ...]) -> str:
    """Monta a expressão SQL `read_csv(...)` com o schema e os parâmetros corretos
    (encoding latin-1, separador ';', aspas duplas, sem cabeçalho) para um CSV oficial
    da Receita Federal."""
    columns_sql = ", ".join(f"'{name}': 'VARCHAR'" for name in columns)
    return (
        f"read_csv('{_quote_literal(csv_path)}', "
        f"columns={{{columns_sql}}}, header=false, "
        f"sep='{CSV_SEP}', quote='{CSV_QUOTE}', encoding='{CSV_ENCODING}')"
    )


def csv_to_parquet(csv_path: Path, entity: str, parquet_dir: Path) -> Path:
    """Converte um CSV de staging para Parquet (streaming, via `COPY ... TO`).

    Args:
        csv_path: caminho do CSV extraído (ver `ingestion/br_receita/extractor.py`).
        entity: `"EMPRESAS"`, `"ESTABELECIMENTOS"` ou `"SIMPLES"`.
        parquet_dir: diretório de saída (criado se necessário).

    Returns:
        Caminho do arquivo Parquet gerado.

    Raises:
        LoaderError: se `entity` não for uma das reconhecidas.
    """
    if entity not in _ENTITY_COLUMNS:
        raise LoaderError(f"Entidade desconhecida: {entity!r}")

    parquet_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = parquet_dir / f"{csv_path.name}.parquet"

    select_sql = f"SELECT * FROM {_read_csv_sql(csv_path, _ENTITY_COLUMNS[entity])}"
    with duckdb.connect(":memory:") as con:
        con.execute(f"COPY ({select_sql}) TO '{_quote_literal(parquet_path)}' (FORMAT PARQUET)")

    logger.info("CSV convertido para Parquet: %s -> %s", csv_path.name, parquet_path.name)
    return parquet_path


def load_parquet_to_table(
    con: duckdb.DuckDBPyConnection,
    parquet_paths: list[Path],
    table_name: str,
    *,
    replace: bool = True,
) -> int:
    """Carrega um ou mais Parquets da mesma entidade numa tabela DuckDB.

    Args:
        con: conexão DuckDB de destino (idealmente file-backed — ver `open_warehouse`).
        parquet_paths: um ou mais Parquets da mesma entidade (ex.: as várias "fatias"
            `Estabelecimentos0.zip`..`Estabelecimentos9.zip` viram uma tabela só).
        table_name: nome da tabela de staging a criar.
        replace: se `True` (default), substitui a tabela se já existir.

    Returns:
        Número de linhas carregadas.

    Raises:
        LoaderError: se `parquet_paths` estiver vazia.
    """
    if not parquet_paths:
        raise LoaderError("Nenhum arquivo Parquet informado")

    file_list_sql = ", ".join(f"'{_quote_literal(p)}'" for p in parquet_paths)
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
    con.execute(f"{verb} {table_name} AS SELECT * FROM read_parquet([{file_list_sql}])")

    return table_row_count(con, table_name)


def load_staging_directory(
    con: duckdb.DuckDBPyConnection,
    staging_dir: Path,
    parquet_dir: Path,
    *,
    only: list[str] | None = None,
) -> dict[str, int]:
    """Ponto de entrada principal: descobre os CSVs em `staging_dir`, converte cada
    um para Parquet e carrega em tabelas DuckDB, uma por entidade.

    Args:
        con: conexão DuckDB de destino (ver `open_warehouse`).
        staging_dir: diretório com os CSVs extraídos (`data/staging/{competencia}/`).
        parquet_dir: diretório para os Parquets intermediários.
        only: se dado, carrega só estas entidades (ex.: `["EMPRESAS", "SIMPLES"]`).

    Returns:
        `{entidade: numero_de_linhas_carregadas}`.
    """
    by_entity: dict[str, list[Path]] = {}
    for csv_path in sorted(staging_dir.iterdir()):
        if not csv_path.is_file():
            continue
        entity = detect_entity(csv_path)
        if entity is None:
            logger.info("Arquivo ignorado (entidade não reconhecida): %s", csv_path.name)
            continue
        if only is not None and entity not in only:
            continue
        by_entity.setdefault(entity, []).append(csv_path)

    counts: dict[str, int] = {}
    for entity, csv_paths in by_entity.items():
        parquet_paths = [csv_to_parquet(p, entity, parquet_dir) for p in csv_paths]
        table_name = _ENTITY_TABLE_NAMES[entity]
        counts[entity] = load_parquet_to_table(con, parquet_paths, table_name)

    return counts


# -- Consultas de sanidade ---------------------------------------------------------


def table_row_count(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    """Contagem total de linhas de uma tabela."""
    row = con.execute(f"SELECT count(*) FROM {table_name}").fetchone()
    assert row is not None  # COUNT(*) sempre retorna uma linha
    return row[0]


def _table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    return [row[0] for row in con.execute(f"DESCRIBE {table_name}").fetchall()]


def null_counts(
    con: duckdb.DuckDBPyConnection, table_name: str, columns: list[str] | None = None
) -> dict[str, int]:
    """Contagem de valores ausentes (`NULL` ou string vazia) por coluna.

    Como a carga tipa tudo como `VARCHAR`, um campo vazio no CSV vira `''`, não
    `NULL` automaticamente — por isso os dois casos contam como "ausente" aqui.
    """
    target_columns = columns or _table_columns(con, table_name)
    exprs = ", ".join(
        f"sum(CASE WHEN \"{c}\" IS NULL OR \"{c}\" = '' THEN 1 ELSE 0 END) AS \"{c}\""
        for c in target_columns
    )
    row = con.execute(f"SELECT {exprs} FROM {table_name}").fetchone()
    assert row is not None
    return dict(zip(target_columns, (int(v) for v in row), strict=True))


def duplicate_key_count(
    con: duckdb.DuckDBPyConnection, table_name: str, key_columns: list[str]
) -> int:
    """Número de combinações de `key_columns` (ex.: `["cnpj_basico"]`) que aparecem
    mais de uma vez na tabela."""
    key_expr = ", ".join(f'"{c}"' for c in key_columns)
    row = con.execute(
        f"SELECT count(*) FROM "
        f"(SELECT {key_expr} FROM {table_name} GROUP BY {key_expr} HAVING count(*) > 1) AS dup"
    ).fetchone()
    assert row is not None
    return row[0]


def distinct_value_counts(
    con: duckdb.DuckDBPyConnection, table_name: str, column: str
) -> dict[str, int]:
    """Distribuição de valores distintos de uma coluna (ex.: `situacao_cadastral`),
    útil para conferir que os códigos batem com o domínio esperado do layout oficial."""
    rows = con.execute(
        f'SELECT "{column}", count(*) AS n FROM {table_name} GROUP BY "{column}" ORDER BY n DESC'
    ).fetchall()
    return {str(value): int(n) for value, n in rows}


@dataclass(frozen=True)
class SanityReport:
    """Resumo de sanidade pós-carga de uma tabela de staging."""

    table_name: str
    n_rows: int
    null_counts: dict[str, int]
    duplicate_keys: int


def sanity_check(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    *,
    key_columns: list[str],
    columns: list[str] | None = None,
) -> SanityReport:
    """Roda as checagens de sanidade essenciais (contagem, nulos, chave duplicada) de
    uma vez e retorna um relatório consolidado."""
    return SanityReport(
        table_name=table_name,
        n_rows=table_row_count(con, table_name),
        null_counts=null_counts(con, table_name, columns),
        duplicate_keys=duplicate_key_count(con, table_name, key_columns),
    )
