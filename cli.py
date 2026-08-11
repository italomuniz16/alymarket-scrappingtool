"""CLI de consulta e operação da plataforma alymarket.

Comando `query`: consulta a tabela `leads` (a versão ativa do warehouse — ver
`src/etl/transform.get_active_leads_dir`) com filtros por ICP, imprime uma tabela no
terminal e a contagem total de linhas que batem com o filtro.

Exemplo:
    python cli.py query --pais BR --regiao SP --cod-atividade 8630501 \\
        --situacao ATIVA --com-email --limit 50

## Compliance (ver CLAUDE.md)

`is_synthetic = false` e `flag_difusao_restrita = false` são filtros *hard*,
sempre aplicados, sem flag para desativá-los: dado sintético nunca deve aparecer como
lead real, e registros de difusão restrita (França) nunca podem entrar em lista de
prospecção. Isso não é configurável por design.

`query` é "geração de lista" — uma operação sensível (ver CLAUDE.md/
`compliance/audit_log.py`): todo comando registra os filtros usados e a contagem
total resultante no log de auditoria, sempre, sem flag pra desativar (mesma
filosofia de `export/exporters.py`).
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
from src.etl.transform import get_active_leads_dir

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


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada de linha de comando."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()
    args = parse_args(argv)

    if args.command == "query":
        filters = filters_from_namespace(args)
        return run_query_command(args.warehouse_dir, filters, audit_log_path=args.audit_log_path)

    build_arg_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
