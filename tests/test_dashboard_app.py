"""Teste de fumaça de `dashboard/app.py`: roda a página de verdade via
`streamlit.testing.v1.AppTest` (não só a camada de dados) para provar que ela
carrega sem exceção, com e sem versão ativa, em modo normal e em modo demonstração,
e que o botão de exportar de fato funciona (e fica desabilitado em modo demo).

A validação visual (layout, cores, etc.) continua manual, como o enunciado prevê —
isto cobre "roda sem quebrar e faz o que deveria", não aparência.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from streamlit.testing.v1 import AppTest

from src.dashboard.data import DEMO_LABEL
from src.etl.transform import CANONICAL_PARQUET_SCHEMA, activate_version, new_version_dir
from src.scheduler.state import mark_processed

APP_PATH = str(Path(__file__).parent.parent / "src" / "dashboard" / "app.py")

ROW: dict[str, object] = {
    "pais": "BR",
    "id_legal": "1",
    "id_estab": "1",
    "razao_social": "EMPRESA TESTE LTDA",
    "nome_fantasia": None,
    "cod_atividade": "8630501",
    "situacao": "ATIVA",
    "regiao": "SP",
    "municipio": None,
    "cep": None,
    "telefone": None,
    "email": "contato@empresateste.com.br",
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


def _populate_active_warehouse(warehouse_dir: Path) -> None:
    version_dir = new_version_dir(warehouse_dir)
    partition_dir = version_dir / "pais=BR"
    partition_dir.mkdir(parents=True)
    pl.DataFrame([ROW], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
        partition_dir / "part-00000.parquet"
    )
    activate_version(warehouse_dir, version_dir)


def _configured_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, populated: bool) -> Path:
    warehouse_dir = tmp_path / "warehouse"
    if populated:
        _populate_active_warehouse(warehouse_dir)
    monkeypatch.setenv("DATA_WAREHOUSE_DIR", str(warehouse_dir))
    monkeypatch.setenv("SUPPRESSION_LIST_PATH", str(tmp_path / "nao-existe-supressao.csv"))
    monkeypatch.setenv("EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.parquet"))
    monkeypatch.setenv("SCHEDULER_STATE_PATH", str(tmp_path / "scheduler_state.json"))
    return warehouse_dir


class TestNoActiveVersion:
    def test_shows_guidance_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem versão ativa, a página orienta pro painel "Coletar leads" (`st.info`,
        não mais `st.error` -- agora há uma ação concreta na própria página)."""
        _configured_env(monkeypatch, tmp_path, populated=False)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        assert not at.exception
        assert any("Nenhuma versão de" in str(i.value) for i in at.info)

    def test_ingest_panel_expanded_when_no_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=False)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        assert not at.exception
        collect_buttons = [b for b in at.button if b.label == "Coletar leads"]
        assert len(collect_buttons) == 1

    def test_scheduler_panel_still_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """O painel do scheduler ajuda a explicar POR QUE não há dados ainda --
        precisa aparecer mesmo sem versão ativa de `leads`."""
        _configured_env(monkeypatch, tmp_path, populated=False)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        assert not at.exception
        metrics = {m.label: m.value for m in at.metric}
        assert metrics["BR_RECEITA"] == "nunca rodou"
        assert metrics["FR_SIRENE"] == "nunca rodou"


class TestHappyPath:
    def test_renders_without_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        assert not at.exception
        assert at.error == []

    def test_metrics_reflect_the_seeded_lead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        metrics = {m.label: m.value for m in at.metric}
        assert metrics["TAM (tamanho do mercado endereçável)"] == "1"
        assert metrics["Sintéticos excluídos"] == "0"
        assert metrics["Difusão restrita excluídos"] == "0"

    def test_export_button_enabled_outside_demo_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        export_buttons = [b for b in at.button if b.label == "Exportar"]
        assert len(export_buttons) == 1
        assert export_buttons[0].disabled is False

    def test_export_button_click_writes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        export_button = next(b for b in at.button if b.label == "Exportar")
        export_button.click().run(timeout=30)

        assert not at.exception
        assert len(at.success) == 1
        exported_files = list((tmp_path / "exports").glob("leads_*.csv"))
        assert len(exported_files) == 1


class TestDemoMode:
    def test_shows_demo_label_banner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        at.sidebar.toggle[0].set_value(True)
        at.run(timeout=30)

        assert not at.exception
        assert any(DEMO_LABEL in str(w.value) for w in at.warning)

    def test_export_button_disabled_in_demo_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        at.sidebar.toggle[0].set_value(True)
        at.run(timeout=30)

        export_buttons = [b for b in at.button if b.label == "Exportar"]
        assert len(export_buttons) == 1
        assert export_buttons[0].disabled is True

    def test_tam_includes_synthetic_leads_in_demo_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warehouse_dir = _configured_env(monkeypatch, tmp_path, populated=False)
        version_dir = new_version_dir(warehouse_dir)
        partition_dir = version_dir / "pais=BR"
        partition_dir.mkdir(parents=True)
        synthetic_row = {**ROW, "id_estab": "2", "is_synthetic": True}
        pl.DataFrame([ROW, synthetic_row], schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
            partition_dir / "part-00000.parquet"
        )
        activate_version(warehouse_dir, version_dir)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        metrics_off = {m.label: m.value for m in at.metric}

        at.sidebar.toggle[0].set_value(True)
        at.run(timeout=30)
        metrics_on = {m.label: m.value for m in at.metric}

        assert metrics_off["TAM (tamanho do mercado endereçável)"] == "1"
        assert metrics_on["TAM (tamanho do mercado endereçável)"] == "2"


class TestSchedulerPanel:
    def test_shows_never_run_when_state_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        metrics = {m.label: m.value for m in at.metric}
        assert metrics["BR_RECEITA"] == "nunca rodou"
        assert metrics["FR_SIRENE"] == "nunca rodou"
        captions = [c.value for c in at.caption]
        assert any("—" in c for c in captions if "Última execução" in c)

    def test_shows_last_competencia_when_processed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configured_env(monkeypatch, tmp_path, populated=True)
        mark_processed("BR_RECEITA", "2026-08", path=tmp_path / "scheduler_state.json")

        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)

        metrics = {m.label: m.value for m in at.metric}
        assert metrics["BR_RECEITA"] == "2026-08"
        assert metrics["FR_SIRENE"] == "nunca rodou"
        captions = [c.value for c in at.caption]
        assert any("Última execução: 20" in c for c in captions)  # timestamp real, não "—"
