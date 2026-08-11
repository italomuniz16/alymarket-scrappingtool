"""Camada de dados do dashboard (`app.py`): funções puras, sem `import streamlit`,
testáveis com pytest normal — `app.py` só desenha widgets e chama estas funções.

## Modo demonstração vs. exportação real

`run_preview`/`compute_compliance_panel` aceitam `demo=True` (via
`segmentation.filters.build_leads_sql`), que mostra dados sintéticos claramente —
`app.py` rotula esses resultados como `DEMO_LABEL`. `run_export`, porém, **nunca**
aceita esse modo: chama `segmentation.filters.build_export_query` (que não tem
parâmetro `demo` — ver aquele módulo) e levanta `DemoExportBlockedError` se for
chamada com `demo=True`, como segunda camada de defesa além do botão desabilitado na
UI (ver CLAUDE.md: dado sintético nunca alcança exportação).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from src.etl.canonical import FONTE_BR_RECEITA, FONTE_FR_SIRENE
from src.etl.transform import PipelineResult, get_active_leads_dir
from src.export.exporters import ExportResult, export_csv, export_txt, export_xlsx
from src.scheduler.pipelines import collect_opencnpj_leads
from src.scheduler.state import DEFAULT_STATE_PATH, load_state
from src.segmentation.filters import (
    FilterClause,
    ICPCriteria,
    build_export_query,
    build_leads_sql,
    build_where_clauses,
    filter_aberta_apos,
    filter_capital_social_range,
    filter_cod_atividade,
    filter_com_email,
    filter_com_telefone,
    filter_pais,
    filter_porte,
    filter_regiao,
    filter_situacao,
)
from src.segmentation.suppression import SuppressionList, apply_suppression_gate

DEMO_LABEL = "DADOS FICTÍCIOS — DEMONSTRAÇÃO"

# Formatos de exportação suportados pelo dashboard (run_export/run_export_one) --
# "txt" é organizado por bloco (Rótulo: valor), pensado pra leitura humana; ver
# export/exporters.export_txt.
EXPORTERS = {"csv": export_csv, "xlsx": export_xlsx, "txt": export_txt}


class DemoExportBlockedError(RuntimeError):
    """Levantado se algum código tentar exportar em modo demonstração. Não deveria
    acontecer pela UI (o botão de exportar fica desabilitado nesse modo) — isto é
    reforço, não o único ponto de checagem."""


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")


def sync_leads_view(con: duckdb.DuckDBPyConnection, warehouse_dir: Path) -> bool:
    """Cria/atualiza a view `leads` no DuckDB apontando para a versão ativa do
    warehouse (ver `etl.transform.get_active_leads_dir`).

    Returns:
        `True` se havia versão ativa (view criada/atualizada); `False` se não há
        nenhuma versão ativa ainda — `app.py` deve mostrar um aviso e não seguir.
    """
    active_dir = get_active_leads_dir(warehouse_dir)
    if active_dir is None:
        return False
    glob = (active_dir / "pais=*" / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW leads AS SELECT * FROM read_parquet('{_quote_literal(glob)}')"
    )
    return True


def _fetch_dicts(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any]
) -> list[dict[str, Any]]:
    cursor = con.execute(sql, params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


@dataclass(frozen=True)
class PreviewResult:
    """Resultado de uma consulta de preview: linhas (já limitadas por `limit`), TAM
    (contagem total, sem limite), e se o modo demonstração estava ativo."""

    rows: list[dict[str, Any]]
    tam: int
    demo: bool


def run_preview(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    source: str = "leads",
    demo: bool = False,
    limit: int = 200,
) -> PreviewResult:
    """Roda a consulta de preview (com `LIMIT`) e a contagem total (TAM, sem `LIMIT`)
    pros critérios ICP informados."""
    built = build_leads_sql(criteria, source=source, demo=demo, limit=limit)
    rows = _fetch_dicts(con, built.select_sql, built.params)

    tam_row = con.execute(built.count_sql, built.params).fetchone()
    assert tam_row is not None
    return PreviewResult(rows=rows, tam=tam_row[0], demo=demo)


def chart_counts_by_column(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    column: str,
    source: str = "leads",
    demo: bool = False,
    top_n: int = 15,
) -> dict[str, int]:
    """Distribuição de contagem por `column` (ex.: `"regiao"`, `"cod_atividade"`) pros
    critérios atuais — alimenta os gráficos de barra do dashboard."""
    clauses = build_where_clauses(criteria, demo=demo)
    where_sql = " AND ".join(clause.sql for clause in clauses)
    params: list[Any] = [value for clause in clauses for value in clause.params]

    sql = (
        f'SELECT "{column}", count(*) AS n FROM {source} WHERE {where_sql} '
        f'GROUP BY "{column}" ORDER BY n DESC LIMIT {int(top_n)}'
    )
    rows = con.execute(sql, params).fetchall()
    return {(value if value is not None else "(sem valor)"): count for value, count in rows}


def chart_counts_by_regiao(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    source: str = "leads",
    demo: bool = False,
) -> dict[str, int]:
    return chart_counts_by_column(con, criteria, column="regiao", source=source, demo=demo)


def chart_counts_by_atividade(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    source: str = "leads",
    demo: bool = False,
) -> dict[str, int]:
    return chart_counts_by_column(con, criteria, column="cod_atividade", source=source, demo=demo)


@dataclass(frozen=True)
class CompliancePanel:
    """Resumo de exclusões aplicadas pros critérios atuais: quantos registros batem
    no filtro ICP bruto (incluindo sintéticos, ainda sem excluir nada), e quantos são
    cortados por cada regra antes de qualquer exportação."""

    total_bruto: int
    n_sinteticos: int
    n_difusao_restrita: int
    n_duplicados: int
    n_opt_out: int
    total_exportavel: int


def _icp_only_clauses(criteria: ICPCriteria) -> list[FilterClause]:
    """Cláusulas só dos critérios ICP, SEM as exclusões obrigatórias.

    Uso restrito ao painel de compliance: `build_leads_sql`/`build_where_clauses`
    (com `demo=True`) ainda assim nunca deixam passar `flag_difusao_restrita=true`
    -- é um filtro hard, de propósito, que nenhum modo desativa (ver
    `segmentation/filters.py`). Isso é exatamente certo pra preview/exportação, mas
    significa que não dá pra usá-los aqui: o painel de compliance existe justamente
    pra *contar* quantos registros de difusão restrita/sintéticos foram cortados, e
    pra isso precisa enxergá-los antes de qualquer exclusão. O resultado desta
    função nunca deve alimentar preview nem exportação -- só contagens agregadas.
    """
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
    clauses = [clause for clause in optional_clauses if clause is not None]
    clauses.extend(
        filter_capital_social_range(criteria.capital_social_min, criteria.capital_social_max)
    )
    return clauses


def compute_compliance_panel(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    suppression: SuppressionList,
    source: str = "leads",
) -> CompliancePanel:
    """Roda só os critérios ICP (sem nenhuma exclusão, nem sintético nem difusão
    restrita -- ver `_icp_only_clauses`) e depois roda `apply_suppression_gate` sobre
    esse conjunto bruto, pra contar quanto cada regra corta — é o que o painel de
    compliance do dashboard mostra. As linhas em si nunca são expostas fora desta
    função, só as contagens agregadas.
    """
    clauses = _icp_only_clauses(criteria)
    if clauses:
        where_sql = " AND ".join(clause.sql for clause in clauses)
        params: list[Any] = [value for clause in clauses for value in clause.params]
        sql = f"SELECT * FROM {source} WHERE {where_sql}"
    else:
        sql = f"SELECT * FROM {source}"
        params = []
    rows = _fetch_dicts(con, sql, params)

    n_sinteticos = sum(1 for row in rows if row.get("is_synthetic"))
    n_difusao_restrita = sum(1 for row in rows if row.get("flag_difusao_restrita"))

    _final, report = apply_suppression_gate(rows, suppression)

    return CompliancePanel(
        total_bruto=len(rows),
        n_sinteticos=n_sinteticos,
        n_difusao_restrita=n_difusao_restrita,
        n_duplicados=report.n_deduped_id_estab + report.n_deduped_email_domain,
        n_opt_out=report.n_suppressed,
        total_exportavel=report.n_out,
    )


def criteria_to_filtros_dict(criteria: ICPCriteria) -> dict[str, Any]:
    """Converte `criteria` num dict simples (só campos preenchidos), pro `audit_log`
    registrar quais filtros geraram a exportação."""
    return {k: v for k, v in dataclasses.asdict(criteria).items() if v is not None}


# -- Painel do scheduler --------------------------------------------------------

# Fontes conhecidas do scheduler (ver `src/scheduler/pipelines.py`) -- mostradas
# sempre no painel, mesmo as que nunca rodaram ainda (mais informativo que omitir
# silenciosamente uma fonte sem estado).
KNOWN_FONTES: tuple[str, ...] = (FONTE_BR_RECEITA, FONTE_FR_SIRENE)


def scheduler_status_rows(state_path: Path | str = DEFAULT_STATE_PATH) -> list[dict[str, Any]]:
    """Uma linha por fonte conhecida (`KNOWN_FONTES`) com a última competência
    processada com sucesso e quando (ver `scheduler/state.py`) -- alimenta o painel
    "última rodada por fonte" do dashboard.

    Uma fonte que ainda nunca rodou aparece com `ultima_competencia`/
    `ultima_execucao` como `None`, não fica ausente da lista.
    """
    state = load_state(state_path)
    rows: list[dict[str, Any]] = []
    for fonte in KNOWN_FONTES:
        source_state = state.get(fonte)
        rows.append(
            {
                "fonte": fonte,
                "ultima_competencia": source_state.last_competencia if source_state else None,
                "ultima_execucao": source_state.last_run_at if source_state else None,
            }
        )
    return rows


def run_export(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    suppression: SuppressionList,
    dest: Path,
    formato: str,
    demo: bool,
    source: str = "leads",
    usuario: str | None = None,
    audit_log_path: Path | str | None = None,
) -> ExportResult:
    """Exporta os leads que batem em `criteria` para `dest`, no formato `formato`
    (`"csv"` ou `"xlsx"`).

    Raises:
        DemoExportBlockedError: se `demo=True` — exportação nunca roda em modo
            demonstração (a UI já desabilita o botão nesse caso; isto é reforço).
        ValueError: `formato` fora de `EXPORTERS` (`"csv"`/`"xlsx"`/`"txt"`).
    """
    if demo:
        raise DemoExportBlockedError(
            "Exportação bloqueada: modo demonstração está ativo. "
            "Desative o modo demonstração para exportar dados reais."
        )
    if formato not in EXPORTERS:
        raise ValueError(f"Formato de exportação desconhecido: {formato!r}")

    built = build_export_query(criteria, source=source, order_by=None, limit=None)
    rows = _fetch_dicts(con, built.select_sql, built.params)
    filtros = criteria_to_filtros_dict(criteria)

    exportar = EXPORTERS[formato]
    kwargs: dict[str, Any] = {
        "suppression": suppression,
        "filtros": filtros,
        "usuario": usuario,
    }
    if audit_log_path is not None:
        kwargs["audit_log_path"] = audit_log_path
    return exportar(rows, dest, **kwargs)


def run_export_one(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    *,
    id_estab: str,
    suppression: SuppressionList,
    dest: Path,
    formato: str,
    demo: bool,
    source: str = "leads",
    usuario: str | None = None,
    audit_log_path: Path | str | None = None,
) -> ExportResult:
    """Exporta um único lead (`id_estab`) dentre os que batem em `criteria` —
    mesmas garantias de `run_export` (bloqueio em modo demo, portão de supressão
    sempre aplicado dentro de `export_csv`/`export_xlsx`), usado pela ação
    "Exportar esta empresa" de cada linha do preview do dashboard.

    A consulta reaproveita `build_export_query` inteira (mesmas exclusões
    obrigatórias que `run_export`) envolvida numa subquery com `id_estab = ?` — não
    reimplementa o WHERE nem dá um jeito de pular as exclusões.

    Raises:
        DemoExportBlockedError: mesma regra de `run_export`.
        ValueError: `formato` fora de `EXPORTERS` (`"csv"`/`"xlsx"`/`"txt"`), ou
            `id_estab` não encontrado sob `criteria` (ex.: a linha saiu do
            conjunto por uma mudança de filtro entre o preview e o clique — erro
            de uso, não de sistema).
    """
    if demo:
        raise DemoExportBlockedError(
            "Exportação bloqueada: modo demonstração está ativo. "
            "Desative o modo demonstração para exportar dados reais."
        )
    if formato not in EXPORTERS:
        raise ValueError(f"Formato de exportação desconhecido: {formato!r}")

    built = build_export_query(criteria, source=source, order_by=None, limit=None)
    wrapped_sql = f"SELECT * FROM ({built.select_sql}) AS _one WHERE id_estab = ?"
    rows = _fetch_dicts(con, wrapped_sql, [*built.params, id_estab])
    if not rows:
        raise ValueError(f"Lead {id_estab!r} não encontrado sob os filtros atuais.")

    filtros = criteria_to_filtros_dict(criteria)
    filtros["id_estab"] = id_estab

    exportar = EXPORTERS[formato]
    kwargs: dict[str, Any] = {
        "suppression": suppression,
        "filtros": filtros,
        "usuario": usuario,
    }
    if audit_log_path is not None:
        kwargs["audit_log_path"] = audit_log_path
    return exportar(rows, dest, **kwargs)


def run_ingest_opencnpj(warehouse_dir: Path, *, n: int) -> PipelineResult:
    """Coleta `n` leads reais via OpenCNPJ e ativa uma nova versão do warehouse
    (ver `scheduler.pipelines.collect_opencnpj_leads`) — usado pelo botão "Coletar
    leads" do dashboard, útil sobretudo no deploy hospedado (sem acesso ao
    `cli.py ingest` local): permite popular o warehouse (efêmero nesse ambiente,
    perdido a cada reinício/redeploy do container) direto pela interface."""
    return collect_opencnpj_leads(warehouse_dir, n=n)
