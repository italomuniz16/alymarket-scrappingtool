"""Scheduler (APScheduler) que roda o pipeline de ingestão de cada fonte na
cadência certa: BR_RECEITA mensal (a Receita só publica um novo stock uma vez por
mês) e FR_SIRENE diário/semanal (configurável — idempotência faz uma checagem a
mais ser barata: só baixa/materializa de novo se `check_latest()` de fato mudou
desde a última rodada, ver `pipeline_runner.py`).

Cada job só chama `run_pipeline_if_new`, que já cuida de idempotência e notificação
(sucesso/pulo/falha em log) — este módulo monta os triggers cron e registra os
jobs, nada mais. Uma falha num job nunca derruba o scheduler nem cancela os
próximos disparos (ver docstring de `run_pipeline_if_new`).

Uso típico (produção)::

    python -m src.scheduler.scheduler

Lê `BR_MUNICIPIO_LOOKUP_CSV`/`BR_NATUREZA_JURIDICA_LOOKUP_CSV` do ambiente (ver
`.env.example`) pra montar o pipeline BR real e inicia o scheduler (bloqueante).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from src.scheduler.pipeline_runner import IngestionPipeline, run_pipeline_if_new
from src.scheduler.state import DEFAULT_STATE_PATH

logger = logging.getLogger(__name__)

# BR: a Receita publica um novo stock mensalmente -- todo dia 5 (dá margem pro
# arquivo do mês já estar publicado) às 3h (fora do horário comercial).
DEFAULT_BR_RECEITA_CRON: dict[str, object] = {"day": 5, "hour": 3, "minute": 0}
# FR: verificado com mais frequência que o BR de propósito -- o dataset SIRENE é
# atualizado com cadência que varia, e idempotência torna uma checagem extra
# barata (só reprocessa se check_latest() de fato mudou). Default diário às 4h.
DEFAULT_FR_SIRENE_CRON: dict[str, object] = {"hour": 4, "minute": 0}

JOB_ID_BR_RECEITA = "ingest_br_receita"
JOB_ID_FR_SIRENE = "ingest_fr_sirene"


def _run_job(pipeline: IngestionPipeline, *, state_path: Path | str) -> None:
    """Alvo de cada job do APScheduler -- delega inteiramente pra
    `run_pipeline_if_new` (idempotência + notificação já ficam por conta dela)."""
    run_pipeline_if_new(pipeline, state_path=state_path)


def build_scheduler(
    br_receita_pipeline: IngestionPipeline,
    fr_sirene_pipeline: IngestionPipeline,
    *,
    br_receita_cron: dict[str, object] | None = None,
    fr_sirene_cron: dict[str, object] | None = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
    timezone: str = "UTC",
) -> BlockingScheduler:
    """Monta (mas não inicia) um `BlockingScheduler` com um job por fonte.

    Não constrói os pipelines aqui de propósito — recebe-os prontos, pra este
    módulo não precisar saber de nada específico de BR/FR (nem para onde apontam
    os lookups da Receita, por exemplo) e pra ficar trivial testar com pipelines
    falsos (ver `tests/test_scheduler_scheduler.py`).

    Args:
        br_receita_pipeline/fr_sirene_pipeline: pipelines já construídos (ver
            `pipelines.build_br_receita_pipeline`/`build_fr_sirene_pipeline`).
        br_receita_cron/fr_sirene_cron: kwargs de `apscheduler.triggers.cron.
            CronTrigger` (ex.: `{"day": 5, "hour": 3}`); default:
            `DEFAULT_BR_RECEITA_CRON`/`DEFAULT_FR_SIRENE_CRON`.
        state_path: onde persiste o estado de idempotência (ver `state.py`).
        timezone: fuso horário dos triggers cron.

    Returns:
        Scheduler pronto pra `.start()` (bloqueante) — não iniciado aqui, pra quem
        chama poder inspecionar `.get_jobs()` antes de rodar de verdade (é assim
        que os testes verificam a cadência sem esperar o tempo real).
    """
    scheduler = BlockingScheduler()

    scheduler.add_job(
        _run_job,
        trigger=CronTrigger(**(br_receita_cron or DEFAULT_BR_RECEITA_CRON), timezone=timezone),
        id=JOB_ID_BR_RECEITA,
        name=f"Ingestão {br_receita_pipeline.fonte} (mensal)",
        kwargs={"pipeline": br_receita_pipeline, "state_path": state_path},
    )
    scheduler.add_job(
        _run_job,
        trigger=CronTrigger(**(fr_sirene_cron or DEFAULT_FR_SIRENE_CRON), timezone=timezone),
        id=JOB_ID_FR_SIRENE,
        name=f"Ingestão {fr_sirene_pipeline.fonte} (diário/semanal)",
        kwargs={"pipeline": fr_sirene_pipeline, "state_path": state_path},
    )

    return scheduler


def main() -> None:
    """Ponto de entrada de produção: monta os pipelines reais a partir de
    variáveis de ambiente e inicia o scheduler (bloqueante — roda até Ctrl+C/sinal).
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    # Import tardio: build_br_receita_pipeline/build_fr_sirene_pipeline puxam
    # httpx/duckdb/etc. -- não vale pagar esse custo só pra importar build_scheduler
    # em testes que injetam pipelines falsos.
    from src.scheduler.pipelines import build_br_receita_pipeline, build_fr_sirene_pipeline

    municipio_csv = Path(os.environ["BR_MUNICIPIO_LOOKUP_CSV"])
    natureza_csv = Path(os.environ["BR_NATUREZA_JURIDICA_LOOKUP_CSV"])

    scheduler = build_scheduler(
        build_br_receita_pipeline(
            municipio_lookup_csv=municipio_csv,
            natureza_juridica_lookup_csv=natureza_csv,
        ),
        build_fr_sirene_pipeline(),
    )

    logger.info("Scheduler iniciado. Jobs agendados: %s", [j.id for j in scheduler.get_jobs()])
    scheduler.start()


if __name__ == "__main__":
    main()
