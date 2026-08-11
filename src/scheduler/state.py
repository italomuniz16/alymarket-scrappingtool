"""Estado de idempotência do scheduler de ingestão: qual foi a última competência
processada com SUCESSO, por fonte — persistido em JSON simples.

É o que `pipeline_runner.run_pipeline_if_new` consulta antes de rodar qualquer
coisa: se a competência mais recente encontrada na fonte (`IngestionPipeline.
check_latest()`) já é a última registrada aqui, a rodada é pulada — "competência já
processada não reprocessa". Só é atualizado depois que o pipeline termina com
sucesso; uma falha nunca marca a competência como processada, pra ser retentada na
próxima rodada agendada.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("./data/warehouse/scheduler_state.json")


@dataclass(frozen=True)
class SourceState:
    """Estado de uma fonte: última competência processada com sucesso e quando."""

    last_competencia: str
    last_run_at: str  # ISO 8601 (UTC) -- só registro informativo, não é reparseado


def load_state(path: Path | str = DEFAULT_STATE_PATH) -> dict[str, SourceState]:
    """Carrega o estado persistido. Arquivo ausente = nenhuma fonte processada ainda
    (estado vazio, não um erro) — mesma convenção de `load_suppression_list`."""
    path = Path(path)
    if not path.is_file():
        return {}

    raw = json.loads(path.read_text(encoding="utf-8"))
    return {fonte: SourceState(**data) for fonte, data in raw.items()}


def save_state(state: dict[str, SourceState], path: Path | str = DEFAULT_STATE_PATH) -> None:
    """Persiste `state` inteiro (sobrescreve o arquivo)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {fonte: asdict(source_state) for fonte, source_state in state.items()}
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def already_processed(
    fonte: str, competencia: str, *, path: Path | str = DEFAULT_STATE_PATH
) -> bool:
    """`True` se `competencia` já é a última processada com sucesso para `fonte` —
    a checagem de idempotência em si."""
    source_state = load_state(path).get(fonte)
    return source_state is not None and source_state.last_competencia == competencia


def mark_processed(
    fonte: str,
    competencia: str,
    *,
    path: Path | str = DEFAULT_STATE_PATH,
    now: datetime | None = None,
) -> None:
    """Registra `competencia` como a última processada com sucesso pra `fonte`."""
    now = now or datetime.now(UTC)
    state = load_state(path)
    state[fonte] = SourceState(last_competencia=competencia, last_run_at=now.isoformat())
    save_state(state, path)
    logger.debug("Estado do scheduler atualizado: %s -> %s", fonte, competencia)
