"""CLI de consulta e operação da plataforma alymarket.

Comando `query`: consulta a tabela `leads` (a versão ativa do warehouse — ver
`src/etl/transform.get_active_leads_dir`) com filtros por ICP, imprime uma tabela no
terminal e a contagem total de linhas que batem com o filtro.

Exemplo:
    python cli.py query --pais BR --regiao SP --cod-atividade 8630501 \\
        --situacao ATIVA --com-email --limit 50

Comando `optout`: registra (ou remove, com `--remove`) um opt-out na lista de
supressão persistida (ver `segmentation/suppression.py`) — é o "endpoint" deste
projeto pra atender a um pedido de opt-out; não há API HTTP, a interface
operacional é a CLI.

Exemplo:
    python cli.py optout --id-estab 11111111000191 --motivo "solicitação do titular"
    python cli.py optout --email contato@empresa.com --remove

Comando `retention-purge`: roda o job de limpeza de retenção (ver
`compliance/retention.py`) — expurga do cache de enriquecimento entradas mais
antigas que o TTL configurado.

Exemplo:
    python cli.py retention-purge --ttl-days 180

Comando `ingest`: coleta leads reais de uma fonte externa e ativa uma nova versão
do warehouse (blue/green — ver `etl/transform.py`). Por enquanto só suporta
`--fonte opencnpj` (default): descoberta de CNPJs via sitemap público do cnpja.com
(`ingestion/br_opencnpj/discovery.py`, permitido pelo `robots.txt`) + busca de cada
um na API aberta e sem autenticação do OpenCNPJ (`ingestion/br_opencnpj/client.py`,
dados oficiais da Receita Federal) — fonte alternativa enquanto
`ingestion/br_receita/downloader.py` (URL oficial original) está desativado (portal
migrou de estrutura).

Exemplo:
    python cli.py ingest --fonte opencnpj --n 100

## Compliance (ver CLAUDE.md)

`is_synthetic = false` e `flag_difusao_restrita = false` são filtros *hard*,
sempre aplicados, sem flag para desativá-los: dado sintético nunca deve aparecer como
lead real, e registros de difusão restrita (França) nunca podem entrar em lista de
prospecção. Isso não é configurável por design.

`query`, `optout` e `retention-purge` são operações sensíveis (ver CLAUDE.md/
`compliance/audit_log.py`): cada uma registra um evento no log de auditoria, sempre,
sem flag pra desativar (mesma filosofia de `export/exporters.py`).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

from src.compliance.audit_log import DEFAULT_AUDIT_LOG_PATH, new_event, record_event
from src.compliance.retention import DEFAULT_RETENTION_TTL_DAYS, run_retention_job
from src.enrichment.client import DEFAULT_CACHE_PATH
from src.etl.transform import get_active_leads_dir, run_transform_pipeline_opencnpj
from src.ingestion.br_opencnpj.client import OpenCnpjClient
from src.ingestion.br_opencnpj.discovery import SitemapCnpjDiscovery
from src.segmentation.suppression import (
    DEFAULT_SUPPRESSION_LIST_PATH,
    add_to_suppression_list,
    remove_from_suppression_list,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
DEFAULT_WAREHOUSE_DIR = "./data/warehouse"


@dataclass(frozen=True)
class QueryFilters:
    """Filtros aceitos pelo comando `query`, já normalizados (não o `argparse.Namespace` bruto)."""

    pais: str | None = None
    regiao: str | None = None
    cod_atividade: str | None = None
    situacao: str | None = None
    porte: str | None = None
    aberta_apos: date | None = None
    com_email: bool = False
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class BuiltQuery:
    """SQL montado para o comando `query`: consulta paginada + contagem total (sem LIMIT)."""

    select_sql: str
    count_sql: str
    params: list[Any] = field(default_factory=list)


def build_query_sql(filters: QueryFilters, *, leads_glob: str) -> BuiltQuery:
    """Monta o SQL de consulta (com filtros + `ORDER BY` + `LIMIT`) e o de contagem
    total (mesmos filtros, sem `LIMIT`), ambos sobre `read_parquet(leads_glob)`.

    `is_synthetic = false` e `flag_difusao_restrita = false` são sempre incluídos,
    incondicionalmente — não fazem parte de `QueryFilters` porque não são
    configuráveis (ver docstring do módulo).

    Args:
        filters: filtros já normalizados (ver `filters_from_namespace`).
        leads_glob: caminho glob para os arquivos Parquet da versão ativa (ex.:
            `{versao}/pais=*/*.parquet`).

    Returns:
        `BuiltQuery` com o SQL parametrizado (`?`) e a lista de parâmetros, na ordem
        em que aparecem no SQL — use com `con.sql(sql, params=params)` /
        `con.execute(sql, params)`.
    """
    where_clauses = ["is_synthetic = false", "flag_difusao_restrita = false"]
    params: list[Any] = []

    if filters.pais:
        where_clauses.append("pais = ?")
        params.append(filters.pais.upper())
    if filters.regiao:
        where_clauses.append("regiao = ?")
        params.append(filters.regiao.upper())
    if filters.cod_atividade:
        where_clauses.append("cod_atividade = ?")
        params.append(filters.cod_atividade)
    if filters.situacao:
        where_clauses.append("situacao = ?")
        params.append(filters.situacao.upper())
    if filters.porte:
        where_clauses.append("porte = ?")
        params.append(filters.porte.upper())
    if filters.aberta_apos:
        where_clauses.append("data_inicio_atividade >= ?")
        params.append(filters.aberta_apos)
    if filters.com_email:
        where_clauses.append("email IS NOT NULL")

    escaped_glob = leads_glob.replace("'", "''")
    where_sql = " AND ".join(where_clauses)
    from_where = f"FROM read_parquet('{escaped_glob}') WHERE {where_sql}"

    select_sql = f"SELECT * {from_where} ORDER BY razao_social LIMIT {int(filters.limit)}"
    count_sql = f"SELECT count(*) {from_where}"

    return BuiltQuery(select_sql=select_sql, count_sql=count_sql, params=params)


def _parse_date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Data inválida (use AAAA-MM-DD): {value!r}") from exc


def build_arg_parser() -> argparse.ArgumentParser:
    """Constrói o parser de argumentos do CLI (comando `query` como subcomando)."""
    parser = argparse.ArgumentParser(prog="cli.py", description="CLI da plataforma alymarket.")
    parser.add_argument(
        "--warehouse-dir",
        type=Path,
        default=Path(os.environ.get("DATA_WAREHOUSE_DIR", DEFAULT_WAREHOUSE_DIR)),
        help=(
            "Diretório do warehouse (contém versions/ e active_version.txt). "
            f"Default: $DATA_WAREHOUSE_DIR ou {DEFAULT_WAREHOUSE_DIR!r}."
        ),
    )
    parser.add_argument(
        "--audit-log-path",
        dest="audit_log_path",
        type=Path,
        default=Path(os.environ.get("AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))),
        help=(
            "Onde registrar o log de auditoria (ver compliance/audit_log.py). "
            f"Default: $AUDIT_LOG_PATH ou {str(DEFAULT_AUDIT_LOG_PATH)!r}."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    query_parser = subparsers.add_parser(
        "query", help="Consulta a tabela `leads` (versão ativa) com filtros."
    )
    query_parser.add_argument("--pais", help="Código do país (ex.: BR, FR).")
    query_parser.add_argument("--regiao", help="UF (BR) ou região/département (FR).")
    query_parser.add_argument(
        "--cod-atividade", dest="cod_atividade", help="CNAE (BR) ou NAF/APE (FR)."
    )
    query_parser.add_argument("--situacao", help="Situação cadastral (ex.: ATIVA, BAIXADA).")
    query_parser.add_argument("--porte", help="Porte da empresa (ex.: MICRO EMPRESA).")
    query_parser.add_argument(
        "--aberta-apos",
        dest="aberta_apos",
        type=_parse_date_arg,
        help="Só empresas com início de atividade a partir desta data (AAAA-MM-DD).",
    )
    query_parser.add_argument(
        "--com-email", dest="com_email", action="store_true", help="Só leads com e-mail preenchido."
    )
    query_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Máximo de linhas retornadas (default: {DEFAULT_LIMIT}).",
    )

    optout_parser = subparsers.add_parser(
        "optout", help="Registra (ou remove, com --remove) um opt-out na lista de supressão."
    )
    optout_parser.add_argument(
        "--suppression-list-path",
        dest="suppression_list_path",
        type=Path,
        default=Path(os.environ.get("SUPPRESSION_LIST_PATH", str(DEFAULT_SUPPRESSION_LIST_PATH))),
        help=(
            "Onde a lista de supressão está persistida. "
            f"Default: $SUPPRESSION_LIST_PATH ou {str(DEFAULT_SUPPRESSION_LIST_PATH)!r}."
        ),
    )
    optout_parser.add_argument(
        "--id-estab", dest="id_estab", help="Identificador do estabelecimento."
    )
    optout_parser.add_argument("--email", help="E-mail a suprimir.")
    optout_parser.add_argument(
        "--motivo", help="Texto livre opcional (só para auditoria/rastreabilidade)."
    )
    optout_parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove o opt-out em vez de registrá-lo (default: registra).",
    )

    retention_parser = subparsers.add_parser(
        "retention-purge", help="Roda o job de limpeza de retenção (compliance/retention.py)."
    )
    retention_parser.add_argument(
        "--cache-path",
        dest="cache_path",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Cache de enriquecimento a expurgar. Default: {str(DEFAULT_CACHE_PATH)!r}.",
    )
    retention_parser.add_argument(
        "--ttl-days",
        dest="ttl_days",
        type=int,
        default=int(os.environ.get("RETENTION_TTL_DAYS", str(DEFAULT_RETENTION_TTL_DAYS))),
        help=(
            "Dias de retenção antes do expurgo. "
            f"Default: $RETENTION_TTL_DAYS ou {DEFAULT_RETENTION_TTL_DAYS}."
        ),
    )

    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Coleta leads reais de uma fonte externa e ativa uma nova versão do warehouse.",
    )
    ingest_parser.add_argument(
        "--fonte",
        choices=["opencnpj"],
        default="opencnpj",
        help=(
            "Fonte de ingestão. Por enquanto só 'opencnpj' (default) -- ver CLAUDE.md "
            "sobre o conector oficial da Receita Federal estar desativado."
        ),
    )
    ingest_parser.add_argument(
        "--n", type=int, default=40, help="Quantidade de CNPJs a buscar (default: 40)."
    )

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Faz o parsing dos argumentos de linha de comando."""
    return build_arg_parser().parse_args(argv)


def filters_from_namespace(args: argparse.Namespace) -> QueryFilters:
    """Converte o `argparse.Namespace` do subcomando `query` em `QueryFilters`."""
    return QueryFilters(
        pais=args.pais,
        regiao=args.regiao,
        cod_atividade=args.cod_atividade,
        situacao=args.situacao,
        porte=args.porte,
        aberta_apos=args.aberta_apos,
        com_email=args.com_email,
        limit=args.limit,
    )


def run_query_command(
    warehouse_dir: Path,
    filters: QueryFilters,
    *,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    usuario: str | None = None,
) -> int:
    """Executa o comando `query`: resolve a versão ativa, roda a consulta e imprime
    a tabela + contagem total no terminal.

    "Geração de lista" é uma operação sensível (ver CLAUDE.md/
    `compliance/audit_log.py`): todo comando bem-sucedido (versão ativa encontrada)
    registra os filtros usados e a contagem total resultante, sempre — sem parâmetro
    pra pular essa etapa. A ausência de versão ativa (erro, `return 1` abaixo) não
    gera evento: não houve consulta de verdade a registrar.

    Returns:
        Código de saída (`0` em sucesso, `1` se não houver versão ativa).
    """
    active_dir = get_active_leads_dir(warehouse_dir)
    if active_dir is None:
        print(
            f"Erro: nenhuma versão ativa de `leads` em {warehouse_dir} "
            "(rode o pipeline de transform primeiro).",
            file=sys.stderr,
        )
        return 1

    leads_glob = (active_dir / "pais=*" / "*.parquet").as_posix()
    built = build_query_sql(filters, leads_glob=leads_glob)

    with duckdb.connect(":memory:") as con:
        con.sql(built.select_sql, params=built.params).show()
        count_row = con.execute(built.count_sql, built.params).fetchone()

    assert count_row is not None
    n_registros = count_row[0]
    print(f"Total: {n_registros} lead(s)")

    record_event(
        new_event(
            "query",
            usuario=usuario,
            filtros=dataclasses.asdict(filters),
            n_registros=n_registros,
        ),
        audit_log_path,
    )

    return 0


def run_optout_command(args: argparse.Namespace) -> int:
    """Executa o comando `optout`: registra ou remove (`--remove`) um opt-out na
    lista de supressão persistida, e imprime o resultado no terminal.

    Returns:
        Código de saída (`0` em sucesso, `1` se nem `--id-estab` nem `--email`
        foram informados, ou se `--remove` não encontrou nada pra remover).
    """
    if not args.id_estab and not args.email:
        print("Erro: informe --id-estab e/ou --email.", file=sys.stderr)
        return 1

    if args.remove:
        n_removed = remove_from_suppression_list(
            args.suppression_list_path, id_estab=args.id_estab, email=args.email
        )
        if n_removed == 0:
            print("Nada a remover: opt-out não encontrado na lista.")
            return 1
        print(f"{n_removed} opt-out(s) removido(s) de {args.suppression_list_path}.")
        return 0

    added = add_to_suppression_list(
        args.suppression_list_path,
        id_estab=args.id_estab,
        email=args.email,
        motivo=args.motivo,
    )
    if added:
        print(f"Opt-out registrado em {args.suppression_list_path}.")
    else:
        print("Opt-out já estava registrado; nada a fazer.")
    return 0


def run_retention_purge_command(args: argparse.Namespace) -> int:
    """Executa o comando `retention-purge`: roda o job de limpeza de retenção e
    imprime quantas entradas foram expurgadas."""
    result = run_retention_job(
        cache_path=args.cache_path, ttl_days=args.ttl_days, audit_log_path=args.audit_log_path
    )
    print(
        f"{result.n_purged} entrada(s) expurgada(s) de {result.cache_path} "
        f"(corte: {result.cutoff.isoformat()})."
    )
    return 0


def run_ingest_command(warehouse_dir: Path, args: argparse.Namespace) -> int:
    """Executa o comando `ingest`: descobre CNPJs reais, busca cada um na fonte
    escolhida, materializa e ativa uma nova versão do warehouse.

    Por enquanto só suporta `--fonte opencnpj` (ver `src/ingestion/br_opencnpj/`):
    descoberta via sitemap público do cnpja.com (permitido pelo robots.txt) + busca
    via API aberta do OpenCNPJ (sem autenticação, dados oficiais da Receita
    Federal) — fonte alternativa enquanto `ingestion/br_receita/downloader.py` (URL
    oficial original) está desativado.

    Returns:
        Código de saída (`0` em sucesso/versão ativada, `1` se a validação de
        qualidade reprovar e a versão não for ativada).
    """
    print(f"Descobrindo até {args.n} CNPJ(s) via sitemap público (cnpja.com)...")
    with SitemapCnpjDiscovery() as discovery:
        cnpjs = discovery.discover(args.n)
    print(f"{len(cnpjs)} CNPJ(s) encontrado(s).")

    with OpenCnpjClient() as client:
        records = list(client.fetch_many(cnpjs))
    print(f"{len(records)} registro(s) obtido(s) da API OpenCNPJ.")

    result = run_transform_pipeline_opencnpj(records, warehouse_dir)
    print(
        f"{result.materialize_result.n_rows_written} lead(s) gravado(s), "
        f"{result.materialize_result.n_rows_skipped} pulado(s) (schema inválido)."
    )

    if not result.activated:
        print(
            f"Validação de qualidade reprovada: {result.quality_report.failures}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Versão ativada: {result.version_dir.name} "
        f"({result.quality_report.n_rows} lead(s) no total)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada de linha de comando."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()
    args = parse_args(argv)

    if args.command == "query":
        filters = filters_from_namespace(args)
        return run_query_command(args.warehouse_dir, filters, audit_log_path=args.audit_log_path)

    if args.command == "optout":
        return run_optout_command(args)

    if args.command == "retention-purge":
        return run_retention_purge_command(args)

    if args.command == "ingest":
        return run_ingest_command(args.warehouse_dir, args)

    build_arg_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
