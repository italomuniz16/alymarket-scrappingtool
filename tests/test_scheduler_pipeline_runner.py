"""Testes de `scheduler/pipeline_runner.py`: idempotência (competência já
processada não reprocessa) e notificação de sucesso/falha em log — com pipelines
falsos (sem rede/disco pesado, sem esperar o tempo real).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.scheduler.pipeline_runner import (
    IngestionPipeline,
    PipelineStepResult,
    run_pipeline_if_new,
)
from src.scheduler.state import already_processed


def _fake_pipeline(
    fonte: str,
    *,
    competencia: str,
    run_result: PipelineStepResult | None = None,
    fails: bool = False,
) -> tuple[IngestionPipeline, list[str]]:
    """Um `IngestionPipeline` falso: `check_latest` sempre devolve `competencia`;
    `run` grava cada chamada em `calls` (pra provar quantas vezes rodou) e devolve
    `run_result` ou levanta `RuntimeError` se `fails=True`."""
    calls: list[str] = []

    def check_latest() -> str:
        return competencia

    def run(comp: str) -> PipelineStepResult:
        calls.append(comp)
        if fails:
            raise RuntimeError("falha simulada no pipeline")
        return run_result or PipelineStepResult(n_rows_written=10, activated=True)

    return IngestionPipeline(fonte=fonte, check_latest=check_latest, run=run), calls


class TestIdempotency:
    def test_new_competencia_runs_the_pipeline(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline, calls = _fake_pipeline("BR_RECEITA", competencia="2026-08")

        outcome = run_pipeline_if_new(pipeline, state_path=state_path)

        assert calls == ["2026-08"]
        assert outcome.skipped is False
        assert outcome.success is True
        assert outcome.competencia == "2026-08"

    def test_marks_competencia_as_processed_after_success(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08")

        run_pipeline_if_new(pipeline, state_path=state_path)

        assert already_processed("BR_RECEITA", "2026-08", path=state_path) is True

    def test_same_competencia_again_does_not_rerun(self, tmp_path: Path) -> None:
        """O caso central pedido: competência já processada não reprocessa."""
        state_path = tmp_path / "state.json"
        pipeline, calls = _fake_pipeline("BR_RECEITA", competencia="2026-08")

        run_pipeline_if_new(pipeline, state_path=state_path)
        outcome = run_pipeline_if_new(pipeline, state_path=state_path)

        assert calls == ["2026-08"]  # só rodou uma vez, não duas
        assert outcome.skipped is True
        assert outcome.success is True

    def test_new_competencia_after_previous_one_runs_again(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline1, calls1 = _fake_pipeline("BR_RECEITA", competencia="2026-07")
        run_pipeline_if_new(pipeline1, state_path=state_path)

        pipeline2, calls2 = _fake_pipeline("BR_RECEITA", competencia="2026-08")
        outcome = run_pipeline_if_new(pipeline2, state_path=state_path)

        assert calls2 == ["2026-08"]
        assert outcome.skipped is False

    def test_different_fontes_are_independent(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        br_pipeline, br_calls = _fake_pipeline("BR_RECEITA", competencia="2026-08")
        fr_pipeline, fr_calls = _fake_pipeline("FR_SIRENE", competencia="2026-08")

        run_pipeline_if_new(br_pipeline, state_path=state_path)
        outcome_fr = run_pipeline_if_new(fr_pipeline, state_path=state_path)

        # Mesma competência "2026-08", mas fonte diferente -- não é considerado
        # "já processado" (estado é por fonte).
        assert fr_calls == ["2026-08"]
        assert outcome_fr.skipped is False

    def test_failed_run_does_not_mark_as_processed(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08", fails=True)

        run_pipeline_if_new(pipeline, state_path=state_path)

        assert already_processed("BR_RECEITA", "2026-08", path=state_path) is False

    def test_failed_run_is_retried_on_next_call(self, tmp_path: Path) -> None:
        """Uma falha não fica "presa": a próxima rodada agendada tenta de novo,
        já que a competência nunca foi marcada como processada."""
        state_path = tmp_path / "state.json"
        failing_pipeline, failing_calls = _fake_pipeline(
            "BR_RECEITA", competencia="2026-08", fails=True
        )
        run_pipeline_if_new(failing_pipeline, state_path=state_path)

        recovering_pipeline, recovering_calls = _fake_pipeline("BR_RECEITA", competencia="2026-08")
        outcome = run_pipeline_if_new(recovering_pipeline, state_path=state_path)

        assert failing_calls == ["2026-08"]
        assert recovering_calls == ["2026-08"]  # tentou de novo, não pulou
        assert outcome.success is True
        assert already_processed("BR_RECEITA", "2026-08", path=state_path) is True


class TestOutcome:
    def test_success_outcome_carries_the_result(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        result = PipelineStepResult(n_rows_written=42, activated=True, details={"x": 1})
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08", run_result=result)

        outcome = run_pipeline_if_new(pipeline, state_path=state_path)

        assert outcome.result == result
        assert outcome.error is None

    def test_failure_outcome_carries_the_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08", fails=True)

        outcome = run_pipeline_if_new(pipeline, state_path=state_path)

        assert outcome.success is False
        assert outcome.result is None
        assert outcome.error == "falha simulada no pipeline"

    def test_skipped_outcome_has_no_result(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08")
        run_pipeline_if_new(pipeline, state_path=state_path)

        outcome = run_pipeline_if_new(pipeline, state_path=state_path)

        assert outcome.skipped is True
        assert outcome.result is None


class TestNotificationLogging:
    """Notificação de sucesso/falha é em log (ver CLAUDE.md/docstring do módulo) --
    testado via `caplog`, sem mock nenhum de sistema de notificação externo."""

    def test_logs_info_on_success(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08")

        with caplog.at_level(logging.INFO, logger="src.scheduler.pipeline_runner"):
            run_pipeline_if_new(pipeline, state_path=state_path)

        messages = [r.message for r in caplog.records]
        assert any("SUCESSO" in m and "2026-08" in m for m in messages)

    def test_logs_error_on_failure(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08", fails=True)

        with caplog.at_level(logging.INFO, logger="src.scheduler.pipeline_runner"):
            run_pipeline_if_new(pipeline, state_path=state_path)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "FALHA" in error_records[0].message
        assert "2026-08" in error_records[0].message
        assert error_records[0].exc_info is not None  # rastreável (traceback preservado)

    def test_logs_info_on_skip(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08")
        run_pipeline_if_new(pipeline, state_path=state_path)

        with caplog.at_level(logging.INFO, logger="src.scheduler.pipeline_runner"):
            run_pipeline_if_new(pipeline, state_path=state_path)

        messages = [r.message for r in caplog.records]
        assert any("já processada" in m for m in messages)

    def test_no_error_logged_on_success(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08")

        with caplog.at_level(logging.INFO, logger="src.scheduler.pipeline_runner"):
            run_pipeline_if_new(pipeline, state_path=state_path)

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestNeverRaises:
    def test_run_pipeline_if_new_never_raises_even_on_pipeline_failure(
        self, tmp_path: Path
    ) -> None:
        """Garante que uma falha real de pipeline não derruba quem chamou (ex.: o
        job do scheduler) -- essencial pra próxima rodada agendada continuar
        acontecendo."""
        state_path = tmp_path / "state.json"
        pipeline, _ = _fake_pipeline("BR_RECEITA", competencia="2026-08", fails=True)

        # Não deve levantar.
        outcome = run_pipeline_if_new(pipeline, state_path=state_path)
        assert outcome.success is False
