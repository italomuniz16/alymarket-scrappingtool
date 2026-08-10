"""Join estabelecimento + empresa + simples + lookups, materialização da tabela
`leads` em Parquet particionado por `pais`, validação de qualidade pós-carga e troca
de versão blue/green.

## Pipeline

1. `build_joined_relation`/`_JOIN_SQL`: join SQL (DuckDB) entre as tabelas de staging
   criadas por `etl/loader.py` (`staging_estabelecimentos`, `staging_empresas`,
   `staging_simples`) e as tabelas de lookup carregadas aqui mesmo (`lookup_municipio`,
   `lookup_natureza_juridica` — pequenas o suficiente para não precisar do tratamento
   de `loader.py`), já com datas/decimais convertidos via `TRY_STRPTIME`/`TRY_CAST`.
2. `materialize_leads`: lê o resultado do join em lotes (`.fetchmany`, não um
   `.fetchall()` único) e mapeia cada linha via `etl/canonical.map_estabelecimento_to_canonical`,
   gravando cada lote como um arquivo Parquet (`part-NNNNN.parquet`) sob
   `{versao}/pais=BR/` — mantém o mapeamento canônico como fonte única da verdade
   (mesma função usada nos testes unitários de `canonical.py`), sem duplicar a lógica
   em SQL. Linhas que falham a validação do schema canônico são puladas e contadas,
   não interrompem a materialização (mesmo padrão de `parser.py` para CSV malformado).
3. `run_quality_checks`: roda contra o Parquet recém-gravado (pós-carga, não contra o
   join em memória): contagem de linhas, % de nulos por coluna, distribuição de
   `situacao` (flagando valores fora do domínio oficial), e duplicidade de `id_estab`.
4. `run_transform_pipeline`: orquestra os passos acima numa versão nova
   (`versions/{timestamp}/`) e só chama `activate_version` (troca do ponteiro
   `active_version.txt`, escrita atômica) se a validação passar — blue/green: a
   versão anterior continua ativa e intacta até a nova ser aprovada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from pydantic import ValidationError

from src.etl.canonical import SITUACAO_CADASTRAL_LABELS, map_estabelecimento_to_canonical
from src.etl.loader import table_row_count

logger = logging.getLogger(__name__)

STAGING_ESTABELECIMENTOS = "staging_estabelecimentos"
STAGING_EMPRESAS = "staging_empresas"
STAGING_SIMPLES = "staging_simples"
LOOKUP_MUNICIPIO = "lookup_municipio"
LOOKUP_NATUREZA_JURIDICA = "lookup_natureza_juridica"

_JOIN_SQL = f"""
    SELECT
        e.cnpj_basico,
        e.cnpj_basico || e.cnpj_ordem || e.cnpj_dv AS cnpj_completo,
        NULLIF(e.nome_fantasia, '') AS nome_fantasia,
        NULLIF(e.situacao_cadastral, '') AS situacao_cadastral,
        CASE
            WHEN e.data_inicio_atividade IS NULL
                 OR e.data_inicio_atividade IN ('0', '00000000', '')
            THEN NULL
            ELSE TRY_STRPTIME(e.data_inicio_atividade, '%Y%m%d')::DATE
        END AS data_inicio_atividade,
        NULLIF(e.cnae_fiscal_principal, '') AS cnae_fiscal_principal,
        NULLIF(e.uf, '') AS uf,
        NULLIF(e.municipio, '') AS municipio_codigo,
        mun.descricao AS municipio_descricao,
        NULLIF(e.cep, '') AS cep,
        NULLIF(e.ddd_1, '') AS ddd_1,
        NULLIF(e.telefone_1, '') AS telefone_1,
        NULLIF(e.correio_eletronico, '') AS correio_eletronico,
        NULLIF(em.razao_social, '') AS razao_social,
        NULLIF(em.natureza_juridica, '') AS natureza_juridica_codigo,
        nat.descricao AS natureza_juridica_descricao,
        TRY_CAST(REPLACE(NULLIF(em.capital_social, ''), ',', '.') AS DECIMAL(18, 2))
            AS capital_social,
        NULLIF(em.porte_empresa, '') AS porte_empresa,
        NULLIF(s.opcao_pelo_simples, '') AS opcao_pelo_simples,
        NULLIF(s.opcao_pelo_mei, '') AS opcao_pelo_mei
    FROM {STAGING_ESTABELECIMENTOS} e
    LEFT JOIN {STAGING_EMPRESAS} em ON e.cnpj_basico = em.cnpj_basico
    LEFT JOIN {STAGING_SIMPLES} s ON e.cnpj_basico = s.cnpj_basico
    LEFT JOIN {LOOKUP_MUNICIPIO} mun ON e.municipio = mun.codigo
    LEFT JOIN {LOOKUP_NATUREZA_JURIDICA} nat ON em.natureza_juridica = nat.codigo
"""

# Schema explícito (não inferido) para o Parquet: garante colunas/tipos idênticos em
# todo lote/arquivo, mesmo quando um lote inteiro tem uma coluna sempre nula.
CANONICAL_PARQUET_SCHEMA: dict[str, Any] = {
    "pais": pl.Utf8,
    "id_legal": pl.Utf8,
    "id_estab": pl.Utf8,
    "razao_social": pl.Utf8,
    "nome_fantasia": pl.Utf8,
    "cod_atividade": pl.Utf8,
    "situacao": pl.Utf8,
    "regiao": pl.Utf8,
    "municipio": pl.Utf8,
    "cep": pl.Utf8,
    "telefone": pl.Utf8,
    "email": pl.Utf8,
    "data_inicio_atividade": pl.Date,
    "porte": pl.Utf8,
    "capital_social": pl.Float64,
    "natureza_juridica": pl.Utf8,
    "score_icp": pl.Float64,
    "fonte": pl.Utf8,
    "enriquecido_em": pl.Datetime,
    "is_synthetic": pl.Boolean,
    "flag_difusao_restrita": pl.Boolean,
}


def load_lookup_table_into_duckdb(
    con: duckdb.DuckDBPyConnection, csv_path: Path, table_name: str
) -> int:
    """Carrega uma tabela auxiliar (`codigo;descricao`, latin-1, sem cabeçalho — ver
    `ingestion/br_receita/parser.load_lookup_table`) como tabela DuckDB.

    Returns:
        Número de linhas carregadas.
    """
    escaped = csv_path.as_posix().replace("'", "''")
    con.execute(
        f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_csv("
        f"'{escaped}', columns={{'codigo': 'VARCHAR', 'descricao': 'VARCHAR'}}, "
        f"header=false, sep=';', quote='\"', encoding='latin-1')"
    )
    return table_row_count(con, table_name)


def build_joined_relation(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyRelation:
    """Retorna a relação (não materializada) do join estabelecimento+empresa+simples+lookups."""
    return con.sql(_JOIN_SQL)


@dataclass(frozen=True)
class MaterializeResult:
    """Resultado da materialização da tabela `leads` numa versão."""

    n_rows_written: int
    n_rows_skipped: int
    part_files: list[Path]


def materialize_leads(
    con: duckdb.DuckDBPyConnection,
    version_dir: Path,
    *,
    batch_size: int = 5_000,
) -> MaterializeResult:
    """Executa o join e grava a tabela `leads` em Parquet sob `version_dir/pais=BR/`.

    Lê o resultado do join em lotes de `batch_size` linhas (não um `.fetchall()`
    único) para não acumular o resultado mapeado inteiro em memória de uma vez; cada
    lote vira seu próprio arquivo Parquet, com schema explícito e uniforme
    (`CANONICAL_PARQUET_SCHEMA`) para que todos os arquivos da partição sejam
    consistentes entre si.

    Args:
        con: conexão DuckDB com as tabelas de staging e de lookup já carregadas.
        version_dir: diretório da versão (ver `new_version_dir`); `pais=BR/` é criado
            dentro dele.
        batch_size: linhas por lote/arquivo Parquet.

    Returns:
        `MaterializeResult` com contagem de linhas gravadas/puladas e os arquivos gerados.
    """
    partition_dir = version_dir / "pais=BR"
    partition_dir.mkdir(parents=True, exist_ok=True)

    cursor = con.execute(_JOIN_SQL)
    columns = [d[0] for d in cursor.description]

    n_written = 0
    n_skipped = 0
    part_files: list[Path] = []
    batch_index = 0

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break

        mapped: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(columns, row, strict=True))
            try:
                canonical = map_estabelecimento_to_canonical(record)
            except ValidationError as exc:
                n_skipped += 1
                logger.warning(
                    "Registro pulado (schema canônico inválido) para cnpj_basico=%r: %s",
                    record.get("cnpj_basico"),
                    exc,
                )
                continue
            if canonical["capital_social"] is not None:
                canonical["capital_social"] = float(canonical["capital_social"])
            mapped.append(canonical)

        if not mapped:
            continue

        part_path = partition_dir / f"part-{batch_index:05d}.parquet"
        pl.DataFrame(mapped, schema=CANONICAL_PARQUET_SCHEMA).write_parquet(part_path)
        part_files.append(part_path)
        n_written += len(mapped)
        batch_index += 1

    return MaterializeResult(
        n_rows_written=n_written, n_rows_skipped=n_skipped, part_files=part_files
    )


# -- Validação de qualidade pós-carga -----------------------------------------------

_DEFAULT_NULL_CHECK_COLUMNS = (
    "id_legal",
    "id_estab",
    "razao_social",
    "situacao",
    "municipio",
    "cod_atividade",
)


@dataclass(frozen=True)
class QualityThresholds:
    """Limites que definem se uma versão recém-materializada pode ser ativada."""

    min_rows: int = 1
    max_null_ratio: dict[str, float] = field(default_factory=dict)
    known_situacao_values: frozenset[str] = frozenset(SITUACAO_CADASTRAL_LABELS.values())


@dataclass(frozen=True)
class QualityReport:
    """Relatório de qualidade pós-carga de uma versão da tabela `leads`."""

    n_rows: int
    null_ratios: dict[str, float]
    situacao_distribution: dict[str, int]
    duplicate_id_estab: int
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


def run_quality_checks(
    con: duckdb.DuckDBPyConnection,
    version_dir: Path,
    *,
    thresholds: QualityThresholds | None = None,
) -> QualityReport:
    """Roda as checagens de sanidade pós-carga contra o Parquet de `version_dir`
    (não contra o join em memória) — contagem, % de nulos, distribuição de
    `situacao` (valores fora do domínio oficial reprovam) e `id_estab` duplicado.
    """
    thresholds = thresholds or QualityThresholds()
    glob = (version_dir / "pais=*" / "*.parquet").as_posix()

    n_rows_row = con.execute(f"SELECT count(*) FROM read_parquet('{glob}')").fetchone()
    assert n_rows_row is not None
    n_rows = n_rows_row[0]

    failures: list[str] = []
    if n_rows < thresholds.min_rows:
        failures.append(f"n_rows={n_rows} abaixo do mínimo exigido ({thresholds.min_rows})")

    null_ratios: dict[str, float] = {}
    situacao_distribution: dict[str, int] = {}
    duplicate_id_estab = 0

    if n_rows > 0:
        check_columns = list(thresholds.max_null_ratio) or list(_DEFAULT_NULL_CHECK_COLUMNS)
        exprs = ", ".join(
            f"sum(CASE WHEN \"{c}\" IS NULL THEN 1 ELSE 0 END)::DOUBLE / count(*) AS \"{c}\""
            for c in check_columns
        )
        row = con.execute(f"SELECT {exprs} FROM read_parquet('{glob}')").fetchone()
        assert row is not None
        null_ratios = dict(zip(check_columns, row, strict=True))
        for col, ratio in null_ratios.items():
            max_ratio = thresholds.max_null_ratio.get(col)
            if max_ratio is not None and ratio > max_ratio:
                failures.append(f"coluna {col!r}: {ratio:.1%} nulos (máx {max_ratio:.1%})")

        dist_rows = con.execute(
            f"SELECT situacao, count(*) FROM read_parquet('{glob}') GROUP BY situacao"
        ).fetchall()
        situacao_distribution = {
            (value if value is not None else "(nulo)"): count for value, count in dist_rows
        }
        unknown = set(situacao_distribution) - set(thresholds.known_situacao_values) - {"(nulo)"}
        if unknown:
            failures.append(f"valores de situação fora do domínio esperado: {sorted(unknown)}")

        dup_row = con.execute(
            f"SELECT count(*) FROM (SELECT id_estab FROM read_parquet('{glob}') "
            f"GROUP BY id_estab HAVING count(*) > 1) AS dup"
        ).fetchone()
        assert dup_row is not None
        duplicate_id_estab = dup_row[0]
        if duplicate_id_estab > 0:
            failures.append(f"{duplicate_id_estab} id_estab duplicado(s)")

    return QualityReport(
        n_rows=n_rows,
        null_ratios=null_ratios,
        situacao_distribution=situacao_distribution,
        duplicate_id_estab=duplicate_id_estab,
        failures=failures,
    )


# -- Versionamento blue/green ---------------------------------------------------

_ACTIVE_POINTER_NAME = "active_version.txt"


def new_version_dir(warehouse_dir: Path) -> Path:
    """Gera um novo diretório de versão (`versions/{timestamp UTC}/`), ainda não ativo."""
    version_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return warehouse_dir / "versions" / version_name


def get_active_version(warehouse_dir: Path) -> str | None:
    """Nome da versão atualmente ativa, ou `None` se nenhuma foi ativada ainda."""
    pointer = warehouse_dir / _ACTIVE_POINTER_NAME
    if not pointer.is_file():
        return None
    content = pointer.read_text(encoding="utf-8").strip()
    return content or None


def get_active_leads_dir(warehouse_dir: Path) -> Path | None:
    """Diretório da versão ativa (contendo `pais=BR/...`), ou `None` se nenhuma ativa."""
    version = get_active_version(warehouse_dir)
    return (warehouse_dir / "versions" / version) if version else None


def activate_version(warehouse_dir: Path, version_dir: Path) -> None:
    """Troca a versão ativa de forma atômica (grava um `.tmp` e faz replace).

    Blue/green: a versão anterior nunca é apagada nem sobrescrita por isto — só o
    ponteiro muda. Chame só depois de `run_quality_checks` aprovar `version_dir`.
    """
    pointer = warehouse_dir / _ACTIVE_POINTER_NAME
    tmp_pointer = pointer.with_suffix(".tmp")
    tmp_pointer.write_text(version_dir.name, encoding="utf-8")
    tmp_pointer.replace(pointer)
    logger.info("Versão ativada: %s", version_dir.name)


@dataclass(frozen=True)
class PipelineResult:
    """Resultado de uma execução completa do pipeline de transform."""

    version_dir: Path
    materialize_result: MaterializeResult
    quality_report: QualityReport
    activated: bool


def run_transform_pipeline(
    con: duckdb.DuckDBPyConnection,
    warehouse_dir: Path,
    *,
    municipio_lookup_csv: Path,
    natureza_juridica_lookup_csv: Path,
    batch_size: int = 5_000,
    thresholds: QualityThresholds | None = None,
) -> PipelineResult:
    """Orquestra o pipeline completo: carrega lookups -> join -> materializa numa
    versão nova -> valida qualidade -> ativa (blue/green) só se aprovado.

    Pressupõe que `con` já tem `staging_estabelecimentos`/`staging_empresas`/
    `staging_simples` carregadas (ver `etl/loader.load_staging_directory`).
    """
    load_lookup_table_into_duckdb(con, municipio_lookup_csv, LOOKUP_MUNICIPIO)
    load_lookup_table_into_duckdb(con, natureza_juridica_lookup_csv, LOOKUP_NATUREZA_JURIDICA)

    version_dir = new_version_dir(warehouse_dir)
    materialize_result = materialize_leads(con, version_dir, batch_size=batch_size)
    quality_report = run_quality_checks(con, version_dir, thresholds=thresholds)

    activated = False
    if quality_report.passed:
        activate_version(warehouse_dir, version_dir)
        activated = True
    else:
        logger.warning(
            "Validação de qualidade reprovada para %s, versão não ativada: %s",
            version_dir.name,
            quality_report.failures,
        )

    return PipelineResult(
        version_dir=version_dir,
        materialize_result=materialize_result,
        quality_report=quality_report,
        activated=activated,
    )
