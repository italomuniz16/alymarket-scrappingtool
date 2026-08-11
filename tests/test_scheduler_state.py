"""Testes de `scheduler/state.py`: persistência do estado de idempotência (última
competência processada com sucesso, por fonte)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.scheduler.state import (
    SourceState,
    already_processed,
    load_state,
    mark_processed,
    save_state,
)


class TestLoadState:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        assert load_state(tmp_path / "nao-existe.json") == {}

    def test_loads_persisted_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=path)

        state = load_state(path)

        assert state.keys() == {"BR_RECEITA"}
        assert state["BR_RECEITA"].last_competencia == "2026-08"


class TestMarkProcessed:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        assert not path.exists()

        mark_processed("BR_RECEITA", "2026-08", path=path)

        assert path.is_file()

    def test_records_last_run_at(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        now = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)

        mark_processed("BR_RECEITA", "2026-08", path=path, now=now)

        assert load_state(path)["BR_RECEITA"].last_run_at == now.isoformat()

    def test_updates_existing_source_without_touching_others(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-07", path=path)
        mark_processed("FR_SIRENE", "2026-08-01", path=path)

        mark_processed("BR_RECEITA", "2026-08", path=path)

        state = load_state(path)
        assert state["BR_RECEITA"].last_competencia == "2026-08"
        assert state["FR_SIRENE"].last_competencia == "2026-08-01"

    def test_overwrites_previous_competencia_for_same_source(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-07", path=path)
        mark_processed("BR_RECEITA", "2026-08", path=path)

        state = load_state(path)
        assert state["BR_RECEITA"].last_competencia == "2026-08"
        assert len(state) == 1


class TestAlreadyProcessed:
    def test_false_when_never_processed(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        assert already_processed("BR_RECEITA", "2026-08", path=path) is False

    def test_true_when_competencia_matches_last_processed(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=path)

        assert already_processed("BR_RECEITA", "2026-08", path=path) is True

    def test_false_when_competencia_differs(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=path)

        assert already_processed("BR_RECEITA", "2026-09", path=path) is False

    def test_false_for_a_different_fonte(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        mark_processed("BR_RECEITA", "2026-08", path=path)

        assert already_processed("FR_SIRENE", "2026-08", path=path) is False


class TestSaveState:
    def test_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = {
            "BR_RECEITA": SourceState(
                last_competencia="2026-08", last_run_at="2026-08-05T03:00:00"
            ),
            "FR_SIRENE": SourceState(
                last_competencia="2026-08-01", last_run_at="2026-08-01T04:00:00"
            ),
        }

        save_state(state, path)

        assert load_state(path) == state
