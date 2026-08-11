"""Dashboard MVP (Streamlit): filtros ICP, preview + TAM, gráficos por região/
atividade, exportação (sempre via portão de supressão) e painel de compliance.

Rode com: `streamlit run src/dashboard/app.py`

A lógica de dados (consultas, TAM, gráficos, compliance, exportação) fica em
`dashboard/data.py`, testável sem depender do runtime do Streamlit — este arquivo só
desenha os widgets e chama aquelas funções.

## Modo demonstração

O seletor "Modo demonstração" mostra dados sintéticos (`is_synthetic=true`) junto dos
reais, claramente rotulados (`DEMO_LABEL`) — mas **nunca** habilita o botão de
exportar: exportação exige o modo desligado (ver `dashboard/data.run_export`, que
recusa rodar com `demo=True` mesmo se chamada por engano).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import duckdb
import streamlit as st

from src.dashboard.data import (
    DEMO_LABEL,
    DemoExportBlockedError,
    chart_counts_by_atividade,
    chart_counts_by_regiao,
    compute_compliance_panel,
    run_export,
    run_preview,
    scheduler_status_rows,
    sync_leads_view,
)
from src.scheduler.state import DEFAULT_STATE_PATH
from src.segmentation.filters import ICPCriteria
from src.segmentation.suppression import SuppressionList, load_suppression_list

WAREHOUSE_DIR = Path(os.environ.get("DATA_WAREHOUSE_DIR", "./data/warehouse"))
SUPPRESSION_LIST_PATH = Path(
    os.environ.get("SUPPRESSION_LIST_PATH", "./data/warehouse/suppression_list.csv")
)
EXPORT_DIR = Path(os.environ.get("EXPORTS_DIR", "./data/exports"))
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "./data/warehouse/audit_log.parquet"))
SCHEDULER_STATE_PATH = Path(os.environ.get("SCHEDULER_STATE_PATH", str(DEFAULT_STATE_PATH)))

SITUACOES = ("", "ATIVA", "BAIXADA", "SUSPENSA", "INAPTA", "NULA")


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


@st.cache_data
def get_suppression_list(path_str: str) -> SuppressionList:
    return load_suppression_list(Path(path_str))


def _split_csv_field(raw: str) -> list[str] | None:
    values = [v.strip() for v in raw.split(",") if v.strip()]
    return values or None


def _sidebar_filters() -> tuple[ICPCriteria, bool, int]:
    with st.sidebar:
        st.header("Filtros ICP")
        pais = st.selectbox("País", ["BR", "FR"])
        cod_atividade_raw = st.text_input("Atividade (CNAE/NAF, separados por vírgula)")
        regiao_raw = st.text_input("Região (UF/département, separados por vírgula)")
        porte_raw = st.text_input("Porte (separados por vírgula)")
        situacao = st.selectbox("Situação", SITUACOES)
        aberta_apos = st.date_input("Aberta a partir de", value=None)
        com_email = st.checkbox("Só com e-mail")
        limit = st.slider("Linhas no preview", min_value=10, max_value=500, value=100, step=10)

        st.divider()
        demo_mode = st.toggle(
            "Modo demonstração",
            value=False,
            help="Mostra também dados fictícios (Faker) além dos reais. Nunca habilita exportação.",
        )

    criteria = ICPCriteria(
        pais=pais,
        cod_atividade=_split_csv_field(cod_atividade_raw),
        regiao=_split_csv_field(regiao_raw),
        porte=_split_csv_field(porte_raw),
        situacao=situacao or None,
        aberta_apos=aberta_apos,
        com_email=com_email,
    )
    return criteria, demo_mode, limit


def _format_timestamp(value: str | None) -> str:
    """`"2026-08-05T03:00:12.345678+00:00"` -> `"2026-08-05 03:00:12"` (mais legível
    no painel; `None` vira `"—"`)."""
    if not value:
        return "—"
    return value[:19].replace("T", " ")


def _render_scheduler_panel() -> None:
    st.subheader("Scheduler — última rodada por fonte")
    rows = scheduler_status_rows(SCHEDULER_STATE_PATH)

    cols = st.columns(len(rows))
    for col, row in zip(cols, rows, strict=True):
        with col:
            st.metric(row["fonte"], row["ultima_competencia"] or "nunca rodou")
            st.caption(f"Última execução: {_format_timestamp(row['ultima_execucao'])}")


def _render_preview_and_tam(
    con: duckdb.DuckDBPyConnection, criteria: ICPCriteria, demo_mode: bool, limit: int
) -> None:
    preview = run_preview(con, criteria, demo=demo_mode, limit=limit)

    col_tam, col_preview = st.columns(2)
    col_tam.metric("TAM (tamanho do mercado endereçável)", preview.tam)
    col_preview.metric("Linhas no preview", len(preview.rows))

    st.subheader("Preview")
    st.dataframe(preview.rows, width="stretch")


def _render_charts(con: duckdb.DuckDBPyConnection, criteria: ICPCriteria, demo_mode: bool) -> None:
    st.subheader("Distribuição")
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.caption("Por região")
        st.bar_chart(chart_counts_by_regiao(con, criteria, demo=demo_mode))
    with chart_col2:
        st.caption("Por atividade")
        st.bar_chart(chart_counts_by_atividade(con, criteria, demo=demo_mode))


def _render_compliance_panel(
    con: duckdb.DuckDBPyConnection, criteria: ICPCriteria, suppression: SuppressionList
) -> None:
    st.subheader("Compliance")
    panel = compute_compliance_panel(con, criteria, suppression=suppression)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sintéticos excluídos", panel.n_sinteticos)
    c2.metric("Difusão restrita excluídos", panel.n_difusao_restrita)
    c3.metric("Duplicados removidos", panel.n_duplicados)
    c4.metric("Opt-out removidos", panel.n_opt_out)
    st.caption(
        f"De {panel.total_bruto} registro(s) no filtro, **{panel.total_exportavel}** "
        "ficariam disponíveis para exportação."
    )


def _render_export(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    suppression: SuppressionList,
    demo_mode: bool,
) -> None:
    st.subheader("Exportar")

    if demo_mode:
        st.button("Exportar", disabled=True)
        st.caption("Exportação desabilitada em modo demonstração — desligue o modo para exportar.")
        return

    formato = st.radio("Formato", ["csv", "xlsx"], horizontal=True)
    usuario = st.text_input("Usuário (para auditoria)", value="")

    if st.button("Exportar"):
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = EXPORT_DIR / f"leads_{timestamp}.{formato}"
        try:
            result = run_export(
                con,
                criteria,
                suppression=suppression,
                dest=dest,
                formato=formato,
                demo=demo_mode,
                usuario=usuario or None,
                audit_log_path=AUDIT_LOG_PATH,
            )
        except DemoExportBlockedError as exc:
            st.error(str(exc))
        else:
            st.success(f"{result.n_exported} lead(s) exportado(s) para `{result.path}`.")


def main() -> None:
    st.set_page_config(page_title="alymarket — leads", layout="wide")
    st.title("alymarket — geração de leads B2B")

    con = get_connection()
    criteria, demo_mode, limit = _sidebar_filters()

    if demo_mode:
        st.warning(f"⚠️ {DEMO_LABEL} — os resultados abaixo podem incluir dados fictícios.")

    _render_scheduler_panel()
    st.divider()

    if not sync_leads_view(con, WAREHOUSE_DIR):
        st.error(
            f"Nenhuma versão de `leads` está ativa em `{WAREHOUSE_DIR}`. "
            "Rode o pipeline de transform (`etl/transform.run_transform_pipeline`) primeiro."
        )
        return

    _render_preview_and_tam(con, criteria, demo_mode, limit)
    _render_charts(con, criteria, demo_mode)

    suppression = get_suppression_list(str(SUPPRESSION_LIST_PATH))
    _render_compliance_panel(con, criteria, suppression)
    _render_export(con, criteria, suppression, demo_mode)


main()
