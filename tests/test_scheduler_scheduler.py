"""Testes de `scheduler/scheduler.py`: cadência dos jobs (mensal pra BR_RECEITA,
diário/semanal pra FR_SIRENE) e que cada job de fato delega pra
`run_pipeline_if_new` — tudo verificado via `CronTrigger.get_next_fire_time` com um
instante de referência fixo, nunca esperando o tempo real (`scheduler.start()`
nunca é chamado aqui).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.scheduler.pipeline_runner import IngestionPipeline, PipelineStepResult
from src.scheduler.scheduler import (
    DEFAULT_BR_RECEITA_CRON,
    DEFAULT_FR_SIRENE_CRON,
    JOB_ID_BR_RECEITA,
    JOB_ID_FR_SIRENE,
    _run_job,
    build_scheduler,
)


def _fake_pipeline(fonte: str) -> IngestionPipeline:
    return IngestionPipeline(
        fonte=fonte,
        check_latest=lambda: "2026-08",
        run=lambda comp: PipelineStepResult(n_rows_written=1, activated=True),
    )


class TestBuildSchedulerJobs:
    def test_registers_one_job_per_source(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids == {JOB_ID_BR_RECEITA, JOB_ID_FR_SIRENE}

    def test_does_not_start_the_scheduler(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        assert scheduler.running is False

    def test_jobs_target_run_job_with_the_right_pipeline(self) -> None:
        br_pipeline = _fake_pipeline("BR_RECEITA")
        fr_pipeline = _fake_pipeline("FR_SIRENE")
        scheduler = build_scheduler(br_pipeline, fr_pipeline)

        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert jobs[JOB_ID_BR_RECEITA].func is _run_job
        assert jobs[JOB_ID_BR_RECEITA].kwargs["pipeline"] is br_pipeline
        assert jobs[JOB_ID_FR_SIRENE].kwargs["pipeline"] is fr_pipeline

    def test_state_path_threaded_into_both_jobs(self, tmp_path: Path) -> None:
        state_path = tmp_path / "estado.json"
        scheduler = build_scheduler(
            _fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"), state_path=state_path
        )

        for job in scheduler.get_jobs():
            assert job.kwargs["state_path"] == state_path


class TestBrReceitaCadenceIsMonthly:
    def test_default_cron_fields_are_monthly(self) -> None:
        assert DEFAULT_BR_RECEITA_CRON["day"] == 5
        # "day" sozinho (sem "month") -> dispara todo mês, não uma vez só.

    def test_next_fire_time_is_next_month_when_past_the_day(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_BR_RECEITA)

        reference = datetime(2026, 8, 10, tzinfo=UTC)  # depois do dia 5 de agosto
        next_fire = job.trigger.get_next_fire_time(None, reference)

        assert next_fire is not None
        assert (next_fire.year, next_fire.month, next_fire.day) == (2026, 9, 5)

    def test_next_fire_time_is_this_month_when_before_the_day(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_BR_RECEITA)

        reference = datetime(2026, 8, 1, tzinfo=UTC)  # antes do dia 5
        next_fire = job.trigger.get_next_fire_time(None, reference)

        assert next_fire is not None
        assert (next_fire.year, next_fire.month, next_fire.day) == (2026, 8, 5)

    def test_fires_again_a_month_later_from_its_own_previous_fire(self) -> None:
        """Prova que a cadência é mensal de verdade (não só "um disparo só"):
        passando o último disparo como referência, o próximo cai um mês depois."""
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_BR_RECEITA)

        first_fire = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)
        second_fire = job.trigger.get_next_fire_time(first_fire, first_fire)

        assert second_fire is not None
        assert (second_fire.year, second_fire.month, second_fire.day) == (2026, 9, 5)

    def test_custom_cron_overrides_default(self) -> None:
        scheduler = build_scheduler(
            _fake_pipeline("BR_RECEITA"),
            _fake_pipeline("FR_SIRENE"),
            br_receita_cron={"day": 1, "hour": 0, "minute": 0},
        )
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_BR_RECEITA)

        reference = datetime(2026, 8, 15, tzinfo=UTC)
        next_fire = job.trigger.get_next_fire_time(None, reference)

        assert next_fire is not None
        assert (next_fire.year, next_fire.month, next_fire.day) == (2026, 9, 1)


class TestFrSireneCadenceIsDailyOrWeekly:
    def test_default_cron_fields_have_no_day_restriction(self) -> None:
        """Sem "day"/"day_of_week" explícito no default -> dispara todo dia
        (diário), a cadência mais frequente das duas permitidas pelo enunciado."""
        assert "day" not in DEFAULT_FR_SIRENE_CRON
        assert "day_of_week" not in DEFAULT_FR_SIRENE_CRON

    def test_next_fire_time_is_the_next_day(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_FR_SIRENE)

        reference = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)  # depois das 4h de hoje
        next_fire = job.trigger.get_next_fire_time(None, reference)

        assert next_fire is not None
        assert (next_fire.year, next_fire.month, next_fire.day) == (2026, 8, 11)

    def test_fires_every_day_consecutively(self) -> None:
        scheduler = build_scheduler(_fake_pipeline("BR_RECEITA"), _fake_pipeline("FR_SIRENE"))
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_FR_SIRENE)

        first_fire = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)
        second_fire = job.trigger.get_next_fire_time(first_fire, first_fire)

        assert second_fire is not None
        assert (second_fire - first_fire).days == 1

    def test_can_be_configured_as_weekly_instead(self) -> None:
        """O enunciado permite diário OU semanal -- confirma que a cadência semanal
        também é suportada via override, não só o default diário."""
        scheduler = build_scheduler(
            _fake_pipeline("BR_RECEITA"),
            _fake_pipeline("FR_SIRENE"),
            fr_sirene_cron={"day_of_week": "mon", "hour": 4, "minute": 0},
        )
        job = next(j for j in scheduler.get_jobs() if j.id == JOB_ID_FR_SIRENE)

        first_fire = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)  # é uma segunda-feira
        second_fire = job.trigger.get_next_fire_time(first_fire, first_fire)

        assert second_fire is not None
        assert (second_fire - first_fire).days == 7


class TestRunJob:
    def test_delegates_to_run_pipeline_if_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[IngestionPipeline, Path]] = []

        def fake_run_pipeline_if_new(pipeline: IngestionPipeline, *, state_path: Path) -> None:
            calls.append((pipeline, state_path))

        monkeypatch.setattr("src.scheduler.scheduler.run_pipeline_if_new", fake_run_pipeline_if_new)

        pipeline = _fake_pipeline("BR_RECEITA")
        state_path = tmp_path / "state.json"
        _run_job(pipeline, state_path=state_path)

        assert calls == [(pipeline, state_path)]
