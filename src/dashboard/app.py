"""Dashboard MVP (Streamlit): filtros ICP, preview + TAM, gráficos por região/
atividade, exportação (sempre via portão de supressão) e painel de compliance.

Rode com: `streamlit run src/dashboard/app.py`

A lógica de dados (consultas, TAM, gráficos, compliance, exportação, coleta) fica em
`dashboard/data.py`, testável sem depender do runtime do Streamlit — este arquivo só
desenha os widgets e chama aquelas funções.

## Modo demonstração

O seletor "Modo demonstração" mostra dados sintéticos (`is_synthetic=true`) junto dos
reais, claramente rotulados (`DEMO_LABEL`) — mas **nunca** habilita nenhum botão de
exportar (nem o em massa, nem o de uma única empresa): exportação exige o modo
desligado (ver `dashboard/data.run_export`/`run_export_one`, que recusam rodar com
`demo=True` mesmo se chamadas por engano).

## Coletar leads pela interface

O painel "Coletar leads" roda a ingestão (fonte: OpenCNPJ — ver
`src/ingestion/br_opencnpj/`) direto do servidor onde o Streamlit está rodando,
sem precisar de acesso à `cli.py`. Importante no deploy hospedado: o warehouse
populado assim é efêmero (perdido a cada reinício/redeploy do container), mas é o
único jeito de o site publicado ter dados reais sem versionar dados no git (ver
CLAUDE.md/.gitignore).
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
    run_export_one,
    run_ingest_opencnpj,
    run_preview,
    scheduler_status_rows,
    sync_leads_view,
)
from src.etl.transform import get_active_leads_dir
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

# Content-Type de cada formato de exportação, pro botão "Baixar" do navegador (ver
# _render_preview_and_tam) — sem isso o navegador não sabe que tipo de arquivo é.
_EXPORT_MIME_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
}

# Tokens de cor/tipografia reutilizados no CSS abaixo e nos gráficos.
_ACCENT_LIGHT = "#2a78d6"

# Polimento visual leve: mantém a estrutura nativa do Streamlit (mesmas seções,
# mesmos widgets), só aplica cor/tipografia/espaçamento consistentes via CSS —
# cards para os `st.metric`, hierarquia de título mais clara. Tema fixo claro
# (ver .streamlit/config.toml, base="light") de propósito: o Streamlit não expõe
# um hook de tema pro CSS injetado reagir junto com o dele, então um dark mode só
# parcial (cor de fundo escura, texto do Streamlit continua fixo escuro) rendeu
# texto ilegível — mais seguro manter os dois (tema nativo + CSS) sempre em claro,
# coerentes entre si, do que arriscar um dark mode inconsistente.
_CUSTOM_CSS = f"""
<style>
:root {{
    --am-surface: #fcfcfb;
    --am-page: #f9f9f7;
    --am-ink: #0b0b0b;
    --am-ink-secondary: #52514e;
    --am-border: rgba(11, 11, 11, 0.10);
    --am-accent: {_ACCENT_LIGHT};
}}

.block-container {{
    /* Antes: `max-width: 1200px` centralizava o conteúdo numa coluna estreita
       em telas largas, deixando uma faixa clara de sobra visível dos dois
       lados (o fundo claro do tema, `--am-page`, por trás) -- removido: com
       `layout="wide"` (já configurado), o conteúdo ocupa a largura toda.
       Isso sozinho não bastava: mesmo em `layout="wide"`, o Streamlit ainda
       aplica um padding padrão de fábrica (`96px/80px/160px` em cima/lados/
       embaixo) no `.block-container` -- sobrava como moldura clara visível
       nas laterais e embaixo. Reduzido pra um respiro modesto e simétrico
       (widgets nativos do Streamlit ainda precisam de alguma margem da
       borda pra não parecer quebrado, mas nada como os valores de fábrica). */
    padding: 1.25rem 1.5rem 2rem !important;
    max-width: 100% !important;
}}

h1 {{
    font-weight: 700 !important;
    letter-spacing: -0.02em;
}}
h2, h3 {{
    font-weight: 600 !important;
    margin-top: 1.75rem !important;
}}

[data-testid="stMetric"] {{
    background: var(--am-surface);
    border: 1px solid var(--am-border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
}}
[data-testid="stMetricLabel"] {{
    color: var(--am-ink-secondary) !important;
}}

[data-testid="stExpander"], [data-testid="stVerticalBlockBorderWrapper"] {{
    border-radius: 10px;
}}

hr {{
    margin: 1.75rem 0 !important;
    border-color: var(--am-border) !important;
}}

[data-testid="stSidebar"] {{
    border-right: 1px solid var(--am-border);
}}

div[data-testid="stAlert"] {{
    border-radius: 8px;
}}
</style>
"""


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


def _render_ingest_panel(warehouse_dir: Path) -> None:
    """Painel "Coletar leads": roda a ingestão via OpenCNPJ direto da interface —
    ver docstring do módulo sobre por que isso importa no deploy hospedado."""
    has_data = get_active_leads_dir(warehouse_dir) is not None

    with st.expander("🔄 Coletar leads (fonte: OpenCNPJ)", expanded=not has_data):
        st.caption(
            "Busca empresas reais via sitemap público (cnpja.com) + API aberta e "
            "gratuita do OpenCNPJ (sem autenticação, dados oficiais da Receita "
            "Federal) — fonte alternativa enquanto o conector oficial da Receita "
            "Federal (Dados Abertos CNPJ) está desativado."
        )
        col_n, col_btn = st.columns([2, 1])
        n = col_n.number_input(
            "Quantidade de CNPJs", min_value=1, max_value=500, value=40, step=10
        )
        col_btn.write("")
        if col_btn.button("Coletar leads", type="primary", width="stretch"):
            with st.spinner(f"Coletando até {int(n)} lead(s)... pode levar alguns minutos."):
                try:
                    result = run_ingest_opencnpj(warehouse_dir, n=int(n))
                except Exception as exc:
                    st.error(f"Falha ao coletar leads: {exc}")
                    return

            if result.activated:
                st.success(
                    f"{result.materialize_result.n_rows_written} lead(s) novo(s) coletado(s) "
                    f"— {result.quality_report.n_rows} no total ativado(s)."
                )
                st.rerun()
            else:
                st.error(f"Validação de qualidade reprovada: {result.quality_report.failures}")


def _render_preview_and_tam(
    con: duckdb.DuckDBPyConnection,
    criteria: ICPCriteria,
    demo_mode: bool,
    limit: int,
    *,
    suppression: SuppressionList,
) -> None:
    preview = run_preview(con, criteria, demo=demo_mode, limit=limit)

    col_tam, col_preview = st.columns(2)
    col_tam.metric("TAM (tamanho do mercado endereçável)", preview.tam)
    col_preview.metric("Linhas no preview", len(preview.rows))

    st.subheader("Preview")
    st.caption("Selecione uma linha na tabela pra exportar só aquela empresa.")
    event = st.dataframe(
        preview.rows,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="preview_table",
    )

    selected_rows: list[int] = []
    if isinstance(event, dict):
        selected_rows = event.get("selection", {}).get("rows", [])
    if not selected_rows:
        return

    selected = preview.rows[selected_rows[0]]
    with st.container(border=True):
        st.markdown(f"**Selecionada:** {selected['razao_social']} · `{selected['id_estab']}`")

        if demo_mode:
            st.caption("Exportação desabilitada em modo demonstração.")
            return

        col_fmt, col_btn = st.columns([2, 1])
        formato_one = col_fmt.radio(
            "Formato", ["csv", "xlsx", "txt"], horizontal=True, key="formato_one"
        )
        col_btn.write("")
        if col_btn.button("Exportar esta empresa", key="export_one_btn", width="stretch"):
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            dest = EXPORT_DIR / f"lead_{selected['id_estab']}_{timestamp}.{formato_one}"
            try:
                result = run_export_one(
                    con,
                    criteria,
                    id_estab=selected["id_estab"],
                    suppression=suppression,
                    dest=dest,
                    formato=formato_one,
                    demo=demo_mode,
                    audit_log_path=AUDIT_LOG_PATH,
                )
            except (DemoExportBlockedError, ValueError) as exc:
                st.error(str(exc))
            else:
                if result.n_exported == 0:
                    st.warning("Empresa suprimida (opt-out/duplicata) — nada exportado.")
                else:
                    st.success(f"{result.n_exported} lead exportado — arquivo pronto abaixo.")
                    # `result.path` fica no disco do servidor (efêmero, especialmente
                    # no deploy hospedado — inacessível pro usuário). O download real
                    # é este botão: entrega os bytes pro navegador na hora. Não dá
                    # pra automatizar num clique só sem reexportar (e gravar de novo
                    # no audit_log) a cada rerun do app — st.download_button precisa
                    # dos bytes já prontos ANTES de qualquer clique, então o gatilho
                    # tem que continuar sendo o clique em "Exportar esta empresa".
                    st.download_button(
                        "⬇️ Baixar arquivo",
                        data=result.path.read_bytes(),
                        file_name=result.path.name,
                        mime=_EXPORT_MIME_TYPES[formato_one],
                        key="download_one_btn",
                        width="stretch",
                    )


def _render_charts(con: duckdb.DuckDBPyConnection, criteria: ICPCriteria, demo_mode: bool) -> None:
    st.subheader("Distribuição")
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.caption("Por região")
        st.bar_chart(chart_counts_by_regiao(con, criteria, demo=demo_mode), color=_ACCENT_LIGHT)
    with chart_col2:
        st.caption("Por atividade")
        st.bar_chart(
            chart_counts_by_atividade(con, criteria, demo=demo_mode), color=_ACCENT_LIGHT
        )


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

    formato = st.radio("Formato", ["csv", "xlsx", "txt"], horizontal=True)
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
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)
    st.title("alymarket — geração de leads B2B")

    con = get_connection()
    criteria, demo_mode, limit = _sidebar_filters()

    if demo_mode:
        st.warning(f"⚠️ {DEMO_LABEL} — os resultados abaixo podem incluir dados fictícios.")

    _render_scheduler_panel()
    st.divider()
    _render_ingest_panel(WAREHOUSE_DIR)
    st.divider()

    if not sync_leads_view(con, WAREHOUSE_DIR):
        st.info(
            "Nenhuma versão de `leads` está ativa ainda. Use o painel "
            "\"🔄 Coletar leads\" acima pra popular o warehouse."
        )
        return

    suppression = get_suppression_list(str(SUPPRESSION_LIST_PATH))

    _render_preview_and_tam(con, criteria, demo_mode, limit, suppression=suppression)
    _render_charts(con, criteria, demo_mode)
    _render_compliance_panel(con, criteria, suppression)
    _render_export(con, criteria, suppression, demo_mode)


main()
