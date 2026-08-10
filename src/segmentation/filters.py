"""Construtor componível de query ICP sobre a tabela `leads`.

`ICPCriteria` descreve um perfil de cliente ideal (país, atividade, região, porte,
faixa de capital social, situação cadastral, recência de abertura, presença de
e-mail/telefone). Cada critério tem sua própria função `filter_*` (testável
isoladamente), que devolve uma `FilterClause` (SQL parametrizado + valores) ou `None`
se o critério não foi informado. `build_where_clauses` compõe todas elas — inclusive
as duas exclusões obrigatórias — numa lista só; `build_leads_sql`/`build_export_query`
montam o SQL final a partir dessa lista.

## Exclusões obrigatórias (ver CLAUDE.md)

`flag_difusao_restrita = false` é injetado **sempre**, incondicionalmente — não há
parâmetro em módulo nenhum aqui que desative isso: é o filtro hard de difusão restrita
(França), que a lei proíbe usar para prospecção.

`is_synthetic = false` também é injetado sempre, **exceto** quando `demo=True` é
passado explicitamente a `build_leads_sql` — modo usado só para o dashboard não ficar
vazio antes de haver dado real carregado (ver docs/PRD.md §1.4). Mesmo assim, o modo
demo nunca deve alcançar exportação/outreach: `build_export_query` existe
especificamente para esses caminhos e **não tem parâmetro `demo`** — não é possível
ativar o modo demo através dela nem por engano, a garantia é na assinatura, não numa
checagem em runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

DEFAULT_SOURCE = "leads"
DEFAULT_ORDER_BY = "razao_social"


@dataclass(frozen=True)
class ICPCriteria:
    """Critérios de segmentação ICP. Todos opcionais — nenhum informado = base toda
    (menos as exclusões obrigatórias). `pais`/`cod_atividade`/`regiao`/`porte`/
    `situacao` aceitam um valor único ou uma lista (`OR` entre os valores da lista).
    """

    pais: str | Sequence[str] | None = None
    cod_atividade: str | Sequence[str] | None = None
    regiao: str | Sequence[str] | None = None
    porte: str | Sequence[str] | None = None
    situacao: str | Sequence[str] | None = None
    capital_social_min: Decimal | None = None
    capital_social_max: Decimal | None = None
    aberta_apos: date | None = None
    com_email: bool = False
    com_telefone: bool = False


@dataclass(frozen=True)
class FilterClause:
    """Um fragmento de `WHERE` parametrizado (`?`) e seus valores, na ordem."""

    sql: str
    params: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class BuiltQuery:
    """SQL montado: consulta (com `ORDER BY`/`LIMIT` opcionais) + contagem total."""

    select_sql: str
    count_sql: str
    params: list[Any] = field(default_factory=list)


# -- Filtros individuais (cada um testável isoladamente) -----------------------------


def _eq_or_in(
    column: str, value: str | Sequence[str] | None, *, upper: bool
) -> FilterClause | None:
    if value is None:
        return None

    values = [value] if isinstance(value, str) else list(value)
    if not values:
        return None

    normalized = [v.upper() if upper else v for v in values]
    if len(normalized) == 1:
        return FilterClause(f"{column} = ?", [normalized[0]])

    placeholders = ", ".join("?" for _ in normalized)
    return FilterClause(f"{column} IN ({placeholders})", normalized)


def filter_exclude_synthetic() -> FilterClause:
    """Exclusão obrigatória (default): nunca deixa dado sintético passar por lead real."""
    return FilterClause("is_synthetic = false")


def filter_exclude_difusao_restrita() -> FilterClause:
    """Exclusão obrigatória (sempre, sem exceção): filtro hard de difusão restrita (França)."""
    return FilterClause("flag_difusao_restrita = false")


def filter_pais(pais: str | Sequence[str] | None) -> FilterClause | None:
    return _eq_or_in("pais", pais, upper=True)


def filter_cod_atividade(cod_atividade: str | Sequence[str] | None) -> FilterClause | None:
    # CNAE/NAF são códigos numéricos/alfanuméricos sem conceito de caixa -- não normaliza.
    return _eq_or_in("cod_atividade", cod_atividade, upper=False)


def filter_regiao(regiao: str | Sequence[str] | None) -> FilterClause | None:
    return _eq_or_in("regiao", regiao, upper=True)


def filter_porte(porte: str | Sequence[str] | None) -> FilterClause | None:
    return _eq_or_in("porte", porte, upper=True)


def filter_situacao(situacao: str | Sequence[str] | None) -> FilterClause | None:
    return _eq_or_in("situacao", situacao, upper=True)


def filter_capital_social_range(
    minimo: Decimal | None, maximo: Decimal | None
) -> list[FilterClause]:
    """Faixa de capital social; cada ponta é opcional e independente (`>=`/`<=`)."""
    clauses: list[FilterClause] = []
    if minimo is not None:
        clauses.append(FilterClause("capital_social >= ?", [minimo]))
    if maximo is not None:
        clauses.append(FilterClause("capital_social <= ?", [maximo]))
    return clauses


def filter_aberta_apos(aberta_apos: date | None) -> FilterClause | None:
    """Recência de abertura: só empresas com início de atividade a partir desta data."""
    if aberta_apos is None:
        return None
    return FilterClause("data_inicio_atividade >= ?", [aberta_apos])


def filter_com_email(com_email: bool) -> FilterClause | None:
    return FilterClause("email IS NOT NULL") if com_email else None


def filter_com_telefone(com_telefone: bool) -> FilterClause | None:
    return FilterClause("telefone IS NOT NULL") if com_telefone else None


# -- Composição -----------------------------------------------------------------


def build_where_clauses(criteria: ICPCriteria, *, demo: bool = False) -> list[FilterClause]:
    """Compõe a lista completa de cláusulas: exclusões obrigatórias + critérios ICP
    informados em `criteria`, cada uma vinda de uma função `filter_*` independente.
    """
    clauses: list[FilterClause] = [filter_exclude_difusao_restrita()]
    if not demo:
        clauses.append(filter_exclude_synthetic())

    optional_clauses = (
        filter_pais(criteria.pais),
        filter_cod_atividade(criteria.cod_atividade),
        filter_regiao(criteria.regiao),
        filter_porte(criteria.porte),
        filter_situacao(criteria.situacao),
        filter_aberta_apos(criteria.aberta_apos),
        filter_com_email(criteria.com_email),
        filter_com_telefone(criteria.com_telefone),
    )
    clauses.extend(clause for clause in optional_clauses if clause is not None)
    clauses.extend(
        filter_capital_social_range(criteria.capital_social_min, criteria.capital_social_max)
    )

    return clauses


def build_leads_sql(
    criteria: ICPCriteria,
    *,
    source: str = DEFAULT_SOURCE,
    demo: bool = False,
    order_by: str | None = DEFAULT_ORDER_BY,
    limit: int | None = None,
) -> BuiltQuery:
    """Monta a query sobre `leads` (ou `source`, se for outra relação/expressão SQL —
    ex.: `"read_parquet('.../*.parquet')"`) a partir de `criteria`.

    Args:
        criteria: critérios ICP (ver `ICPCriteria`).
        source: relação/expressão SQL a consultar (`FROM {source}`). Default: `"leads"`.
        demo: se `True`, **não** exclui `is_synthetic` (ver docstring do módulo). Use
            só para popular dashboard/UI de demonstração — nunca para exportação
            (use `build_export_query`, que não tem este parâmetro).
        order_by: coluna(s) para `ORDER BY`; `None` omite a cláusula.
        limit: se dado, adiciona `LIMIT` só na consulta (não na contagem).

    Returns:
        `BuiltQuery` com o SQL parametrizado (`?`) e os valores, na ordem do SQL.
    """
    clauses = build_where_clauses(criteria, demo=demo)
    where_sql = " AND ".join(clause.sql for clause in clauses)
    params: list[Any] = [value for clause in clauses for value in clause.params]

    from_where = f"FROM {source} WHERE {where_sql}"

    select_sql = f"SELECT * {from_where}"
    if order_by:
        select_sql += f" ORDER BY {order_by}"
    if limit is not None:
        select_sql += f" LIMIT {int(limit)}"

    count_sql = f"SELECT count(*) {from_where}"

    return BuiltQuery(select_sql=select_sql, count_sql=count_sql, params=params)


def build_export_query(
    criteria: ICPCriteria,
    *,
    source: str = DEFAULT_SOURCE,
    order_by: str | None = DEFAULT_ORDER_BY,
    limit: int | None = None,
) -> BuiltQuery:
    """Monta a query para exportação/outreach: sempre com as duas exclusões
    obrigatórias, sem exceção. Não tem parâmetro `demo` de propósito — ver docstring
    do módulo: a garantia de que dado sintético nunca chega em exportação está na
    assinatura desta função, não numa checagem que dependa de alguém lembrar de
    passar o argumento certo.
    """
    return build_leads_sql(criteria, source=source, demo=False, order_by=order_by, limit=limit)
