"""Roda o pipeline de ingestão de UMA fonte só se a competência mais recente mudou
desde a última vez processada com sucesso (idempotência — ver `state.py`), e
notifica sucesso/falha/pulo em log sempre — é a "notificação" deste projeto (ver
CLAUDE.md), não há e-mail/Slack/etc.

`IngestionPipeline` desacopla a lógica de agendamento/idempotência (testável sem
rede/disco pesado, ver `tests/test_scheduler_pipeline_runner.py`, que injeta
pipelines falsos) da execução real do pipeline (download -> extração/parsing ->
join -> blue/green), que fica em `pipelines.py`, compondo os módulos já existentes
de ingestão/ETL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.scheduler.state import DEFAULT_STATE_PATH, already_processed, mark_processed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineStepResult:
    """Resultado de uma execução completa do pipeline de uma fonte (download até
    blue/green) — o que `IngestionPipeline.run` deve devolver em caso de sucesso."""

    n_rows_written: int
    activated: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionPipeline:
    """Uma fonte de ingestão agendável: como descobrir a competência mais recente
    (`check_latest`) e como rodar o pipeline completo pra uma competência dada
    (`run`) — ver `pipelines.build_br_receita_pipeline`/`build_fr_sirene_pipeline`
    para as implementações reais.

    `run` deve levantar em caso de falha (não engolir a exceção) — é
    `run_pipeline_if_new` quem decide o que fazer com ela (capturar, logar, não
    marcar como processada).
    """

    fonte: str
    check_latest: Callable[[], str]
    run: Callable[[str], PipelineStepResult]


@dataclass(frozen=True)
class RunOutcome:
    """Resultado de uma chamada a `run_pipeline_if_new` — pulo, sucesso ou falha,
    sempre com `fonte`/`competencia` preenchidos pra quem consome poder decidir o
    que fazer (ex.: um job do scheduler só loga; um teste inspeciona os campos)."""

    fonte: str
    competencia: str
    skipped: bool
    success: bool
    error: str | None = None
    result: PipelineStepResult | None = None


def run_pipeline_if_new(
    pipeline: IngestionPipeline, *, state_path: Path | str = DEFAULT_STATE_PATH
) -> RunOutcome:
    """Descobre a competência mais recente de `pipeline` e roda o pipeline completo
    só se ela ainda não foi processada com sucesso (idempotência — ver `state.py`).

    Notifica sucesso/pulo/falha em log sempre. Uma falha em `pipeline.run(...)`
    NUNCA sobe daqui — é capturada, logada com `exc_info=True` (rastreável em
    produção), e a competência não é marcada como processada, pra ser retentada na
    próxima rodada agendada. Isso é o que garante que uma falha num job não
    desative o agendamento dos próximos disparos do APScheduler (ver
    `scheduler.py`).

    Args:
        pipeline: fonte a rodar (ver `IngestionPipeline`).
        state_path: onde o estado de idempotência está persistido (ver `state.py`).

    Returns:
        `RunOutcome` — sempre, mesmo em falha (nunca levanta).
    """
    competencia = pipeline.check_latest()

    if already_processed(pipeline.fonte, competencia, path=state_path):
        logger.info(
            "[%s] competência %s já processada; nada a fazer (idempotente).",
            pipeline.fonte,
            competencia,
        )
        return RunOutcome(fonte=pipeline.fonte, competencia=competencia, skipped=True, success=True)

    logger.info(
        "[%s] nova competência detectada: %s — iniciando pipeline.", pipeline.fonte, competencia
    )
    try:
        result = pipeline.run(competencia)
    except Exception as exc:
        logger.error(
            "[%s] FALHA ao processar competência %s: %s",
            pipeline.fonte,
            competencia,
            exc,
            exc_info=True,
        )
        return RunOutcome(
            fonte=pipeline.fonte,
            competencia=competencia,
            skipped=False,
            success=False,
            error=str(exc),
        )

    mark_processed(pipeline.fonte, competencia, path=state_path)
    logger.info(
        "[%s] competência %s processada com SUCESSO: %d lead(s) gravado(s), versão ativada=%s",
        pipeline.fonte,
        competencia,
        result.n_rows_written,
        result.activated,
    )
    return RunOutcome(
        fonte=pipeline.fonte,
        competencia=competencia,
        skipped=False,
        success=True,
        result=result,
    )
