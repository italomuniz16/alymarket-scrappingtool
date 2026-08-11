"""Testes de `dashboard/data.py`: camada de dados do dashboard, sem depender do
runtime do Streamlit. Cobre preview/TAM, gráficos, painel de compliance, o portão de
exportação (nunca em modo demonstração) e a sincronização da view `leads` com a
versão ativa do warehouse.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl
import pytest

from src.dashboard.data import (
    KNOWN_FONTES,
    DemoExportBlockedError,
    chart_counts_by_atividade,
    chart_counts_by_regiao,
    compute_compliance_panel,
    criteria_to_filtros_dict,
    run_export,
    run_preview,
    scheduler_status_rows,
    sync_leads_view,
)
from src.etl.transform import CANONICAL_PARQUET_SCHEMA, activate_version, new_version_dir
from src.scheduler.state import mark_processed
from src.segmentation.filters import ICPCriteria
from src.segmentation.suppression import SuppressionList

Con = duckdb.DuckDBPyConnection

ROW_TEMPLATE: dict[str, object] = {
    "pais": "BR",
    "id_legal": "1",
    "id_estab": "1",
    "razao_social": "X",
    "nome_fantasia": None,
    "cod_atividade": "8630501",
    "situacao": "ATIVA",
    "regiao": "SP",
    "municipio": None,
    "cep": None,
    "telefone": None,
    "email": None,
    "data_inicio_atividade": None,
    "porte": None,
    "capital_social": None,
    "natureza_juridica": None,
    "score_icp": None,
    "fonte": "BR_RECEITA",
    "enriquecido_em": None,
    "is_synthetic": False,
    "flag_difusao_restrita": False,
}


def _row(**overrides: object) -> dict[str, object]:
    return {**ROW_TEMPLATE, **overrides}


@pytest.fixture
def leads_con() -> Con:
    """Conexão DuckDB com uma tabela `leads` pequena, criada direto por SQL (não via
    Parquet) -- `data.py` é agnóstico à origem, só importa que `source="leads"` seja
    consultável."""
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE leads (
            pais VARCHAR, id_legal VARCHAR, id_estab VARCHAR, razao_social VARCHAR,
            nome_fantasia VARCHAR, cod_atividade VARCHAR, situacao VARCHAR,
            regiao VARCHAR, municipio VARCHAR, cep VARCHAR, telefone VARCHAR,
            email VARCHAR, data_inicio_atividade DATE, porte VARCHAR,
            capital_social DOUBLE, natureza_juridica VARCHAR, score_icp DOUBLE,
            fonte VARCHAR, enriquecido_em TIMESTAMP, is_synthetic BOOLEAN,
            flag_difusao_restrita BOOLEAN
        )
        """
    )
    rows = [
        _row(id_estab="1", regiao="SP", cod_atividade="8630501", email="a@x.com"),
        _row(id_estab="2", regiao="RJ", cod_atividade="8630501"),
        _row(id_estab="3", regiao="SP", cod_atividade="4721102", situacao="BAIXADA"),
        _row(id_estab="4", regiao="SP", cod_atividade="8630501", is_synthetic=True),
        _row(id_estab="5", pais="FR", regiao="IDF", flag_difusao_restrita=True),
    ]
    columns = list(ROW_TEMPLATE.keys())
    placeholders = ", ".join("?" for _ in columns)
    for row in rows:
        con.execute(f"INSERT INTO leads VALUES ({placeholders})", [row[c] for c in columns])
    return con


class TestSyncLeadsView:
    def test_no_active_version_returns_false(self, tmp_path: Path) -> None:
        con = duckdb.connect(":memory:")
        assert sync_leads_view(con, tmp_path / "warehouse") is False

    def test_active_version_returns_true_and_view_is_queryable(self, tmp_path: Path) -> None:
        warehouse_dir = tmp_path / "warehouse"
        version_dir = new_version_dir(warehouse_dir)
        partition_dir = version_dir / "pais=BR"
        partition_dir.mkdir(parents=True)
        pl.DataFrame([_row()], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            partition_dir / "part-00000.parquet"
        )
        activate_version(warehouse_dir, version_dir)

        con = duckdb.connect(":memory:")
        assert sync_leads_view(con, warehouse_dir) is True
        assert con.execute("SELECT count(*) FROM leads").fetchone() == (1,)


class TestRunPreview:
    def test_tam_and_rows_exclude_synthetic_and_restricted_by_default(self, leads_con: Con) -> None:
        result = run_preview(leads_con, ICPCriteria())
        assert result.tam == 3  # exclui id 4 (synthetic) e id 5 (difusao restrita)
        assert len(result.rows) == 3
        assert result.demo is False

    def test_demo_mode_includes_synthetic(self, leads_con: Con) -> None:
        result = run_preview(leads_con, ICPCriteria(), demo=True)
        assert result.tam == 4  # inclui synthetic, continua sem difusao restrita
        assert result.demo is True

    def test_limit_caps_rows_but_not_tam(self, leads_con: Con) -> None:
        result = run_preview(leads_con, ICPCriteria(), limit=1)
        assert len(result.rows) == 1
        assert result.tam == 3

    def test_criteria_filters_applied(self, leads_con: Con) -> None:
        result = run_preview(leads_con, ICPCriteria(regiao="SP", situacao="ATIVA"))
        assert result.tam == 1


class TestChartCounts:
    def test_by_regiao(self, leads_con: Con) -> None:
        counts = chart_counts_by_regiao(leads_con, ICPCriteria())
        assert counts == {"SP": 2, "RJ": 1}

    def test_by_atividade(self, leads_con: Con) -> None:
        counts = chart_counts_by_atividade(leads_con, ICPCriteria())
        assert counts == {"8630501": 2, "4721102": 1}

    def test_demo_mode_includes_synthetic_in_counts(self, leads_con: Con) -> None:
        counts = chart_counts_by_regiao(leads_con, ICPCriteria(), demo=True)
        assert counts["SP"] == 3  # 3 leads SP reais + o synthetic tambem eh SP


class TestComputeCompliancePanel:
    def test_panel_counts(self, leads_con: Con) -> None:
        suppression = SuppressionList(ids_estab=frozenset({"2"}))
        panel = compute_compliance_panel(leads_con, ICPCriteria(), suppression=suppression)

        assert panel.total_bruto == 5
        assert panel.n_sinteticos == 1
        assert panel.n_difusao_restrita == 1
        assert panel.n_opt_out == 1
        assert panel.total_exportavel == 2  # ids 1 e 3 (2=suprimido, 4=synth, 5=FR)

    def test_no_suppression_list_still_reports_hard_exclusions(self, leads_con: Con) -> None:
        panel = compute_compliance_panel(leads_con, ICPCriteria(), suppression=SuppressionList())
        assert panel.n_sinteticos == 1
        assert panel.n_difusao_restrita == 1
        assert panel.n_opt_out == 0

    def test_panel_respects_icp_criteria(self, leads_con: Con) -> None:
        # Só regiao=SP: ids 1, 3, 4 (synthetic) -- exclui 2 (RJ) e 5 (FR/IDF).
        panel = compute_compliance_panel(
            leads_con, ICPCriteria(regiao="SP"), suppression=SuppressionList()
        )
        assert panel.total_bruto == 3
        assert panel.n_sinteticos == 1
        assert panel.n_difusao_restrita == 0
        assert panel.total_exportavel == 2


class TestCriteriaToFiltrosDict:
    def test_only_non_none_fields(self) -> None:
        criteria = ICPCriteria(pais="BR", regiao="SP")
        result = criteria_to_filtros_dict(criteria)
        assert result == {"pais": "BR", "regiao": "SP", "com_email": False, "com_telefone": False}


class TestRunExport:
    def test_raises_when_demo_true(self, leads_con: Con, tmp_path: Path) -> None:
        with pytest.raises(DemoExportBlockedError):
            run_export(
                leads_con,
                ICPCriteria(),
                suppression=SuppressionList(),
                dest=tmp_path / "out.csv",
                formato="csv",
                demo=True,
            )
        assert not (tmp_path / "out.csv").exists()

    def test_raises_on_unknown_formato(self, leads_con: Con, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Formato"):
            run_export(
                leads_con,
                ICPCriteria(),
                suppression=SuppressionList(),
                dest=tmp_path / "out.pdf",
                formato="pdf",
                demo=False,
            )

    def test_exports_csv_respecting_suppression_gate(self, leads_con: Con, tmp_path: Path) -> None:
        dest = tmp_path / "out.csv"
        audit_path = tmp_path / "audit.parquet"

        result = run_export(
            leads_con,
            ICPCriteria(),
            suppression=SuppressionList(),
            dest=dest,
            formato="csv",
            demo=False,
            usuario="italo",
            audit_log_path=audit_path,
        )

        assert result.path == dest
        assert result.n_exported == 3  # exclui synthetic e difusao restrita
        assert dest.exists()
        assert audit_path.exists()

    def test_exports_xlsx(self, leads_con: Con, tmp_path: Path) -> None:
        dest = tmp_path / "out.xlsx"
        result = run_export(
            leads_con,
            ICPCriteria(regiao="SP"),
            suppression=SuppressionList(),
            dest=dest,
            formato="xlsx",
            demo=False,
            audit_log_path=tmp_path / "audit.parquet",
        )
        assert dest.exists()
        assert result.n_exported == 2  # ids 1 e 3 (regiao SP, exclui o synthetic)


class TestSchedulerStatusRows:
    def test_missing_state_file_shows_every_known_fonte_as_never_run(self, tmp_path: Path) -> None:
        rows = scheduler_status_rows(tmp_path / "nao-existe.json")

        assert [row["fonte"] for row in rows] == list(KNOWN_FONTES)
        assert all(row["ultima_competencia"] is None for row in rows)
        assert all(row["ultima_execucao"] is None for row in rows)

    def test_reflects_processed_fonte(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=state_path)

        rows = {row["fonte"]: row for row in scheduler_status_rows(state_path)}

        assert rows["BR_RECEITA"]["ultima_competencia"] == "2026-08"
        assert rows["BR_RECEITA"]["ultima_execucao"] is not None
        # FR_SIRENE nunca rodou nesta base de estado -- continua presente, mas vazio.
        assert rows["FR_SIRENE"]["ultima_competencia"] is None

    def test_reflects_both_fontes_independently(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=state_path)
        mark_processed("FR_SIRENE", "2026-08-01", path=state_path)

        rows = {row["fonte"]: row for row in scheduler_status_rows(state_path)}

        assert rows["BR_RECEITA"]["ultima_competencia"] == "2026-08"
        assert rows["FR_SIRENE"]["ultima_competencia"] == "2026-08-01"

    def test_row_order_matches_known_fontes(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        # Registrado fora de ordem de propósito -- a ordem das linhas não deve
        # depender da ordem em que as fontes foram processadas.
        mark_processed("FR_SIRENE", "2026-08-01", path=state_path)
        mark_processed("BR_RECEITA", "2026-08", path=state_path)

        rows = scheduler_status_rows(state_path)

        assert [row["fonte"] for row in rows] == list(KNOWN_FONTES)
