"""Política de retenção/expurgo de dados enriquecidos, com TTL configurável (LGPD/
RGPD — "minimização", "retenção com TTL", ver CLAUDE.md).

## O que é expurgado

O alvo é `enrichment/client.py::EnrichmentCache` — o cache SQLite persistido de
respostas brutas de provedores de enriquecimento (BrasilAPI, Recherche
d'Entreprises, ...), que é o único lugar do projeto onde "dados enriquecidos" ficam
guardados fora da tabela `leads` já versionada/particionada.

Retenção é um conceito DIFERENTE do TTL de frescor que a própria `EnrichmentCache`
já tinha (`expires_at`/`ttl_seconds`, dias — evita rechamar a mesma API rápido
demais). Retenção é sobre por quanto tempo um dado enriquecido pode ficar guardado,
ponto — daí `created_at` (adicionado ao schema da `EnrichmentCache` nesta tarefa) e
`RETENTION_TTL_DAYS` (meses, bem mais longo que o TTL de frescor) serem uma política
separada, independente da entrada ainda estar "fresca" (`expires_at`) ou não.

## Job de limpeza

`purge_enrichment_cache` é a função de expurgo em si; `run_retention_job` é o ponto
de entrada pra rodar como job agendado (ex.: via APScheduler — ver docs/PRD.md — ou
manualmente via `python cli.py retention-purge`). Como toda operação sensível deste
projeto, o expurgo registra um evento em `compliance/audit_log.py`, sempre.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.compliance.audit_log import DEFAULT_AUDIT_LOG_PATH, new_event, record_event
from src.enrichment.client import DEFAULT_CACHE_PATH, EnrichmentCache

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_TTL_DAYS = 180  # mesmo default do .env.example (RETENTION_TTL_DAYS)


@dataclass(frozen=True)
class RetentionPurgeResult:
    """Resultado de uma rodada de expurgo por retenção."""

    n_purged: int
    cutoff: datetime
    cache_path: Path


def purge_enrichment_cache(
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    *,
    ttl_days: int = DEFAULT_RETENTION_TTL_DAYS,
    now: datetime | None = None,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    usuario: str | None = None,
) -> RetentionPurgeResult:
    """Expurga do cache de enriquecimento toda entrada com mais de `ttl_days` dias
    (`created_at`, não `expires_at` — ver docstring do módulo).

    Operação sensível (ver CLAUDE.md): registra sempre um evento em
    `compliance/audit_log.py`, sem parâmetro pra pular essa etapa — mesma filosofia
    de `export/exporters.py`/`enrichment/client.enrich_leads`.

    Args:
        cache_path: onde o cache de enriquecimento está persistido (SQLite).
        ttl_days: quantos dias um dado enriquecido pode ficar retido antes de ser
            expurgado — configurável (ver `RETENTION_TTL_DAYS` no `.env.example`).
        now: "agora" para o cálculo do corte; default: `datetime.now(UTC)`. Parâmetro
            explícito principalmente para testes determinísticos.
        audit_log_path: onde registrar o evento de auditoria.
        usuario: quem/o que disparou o expurgo (ex.: `"scheduler"` pra execução
            automática); default: usuário do SO.

    Returns:
        `RetentionPurgeResult` com quantas entradas foram removidas e o corte usado.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=ttl_days)

    with EnrichmentCache(cache_path) as cache:
        n_purged = cache.purge_created_before(cutoff.timestamp())

    logger.info(
        "Expurgo de retenção: %d entrada(s) removida(s) de %s (corte: %s, ttl=%d dia(s))",
        n_purged,
        cache_path,
        cutoff.isoformat(),
        ttl_days,
    )

    record_event(
        new_event(
            "retention_purge",
            usuario=usuario,
            filtros={"ttl_dias": ttl_days, "cache_path": str(cache_path)},
            n_registros=n_purged,
        ),
        audit_log_path,
    )

    return RetentionPurgeResult(n_purged=n_purged, cutoff=cutoff, cache_path=Path(cache_path))


def run_retention_job(
    *,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    ttl_days: int = DEFAULT_RETENTION_TTL_DAYS,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
) -> RetentionPurgeResult:
    """Ponto de entrada do job de limpeza — pensado pra ser chamado periodicamente
    (ex.: APScheduler, ver docs/PRD.md) ou manualmente (`python cli.py
    retention-purge`). É só um alias documentado de `purge_enrichment_cache` com
    `usuario="scheduler"` fixo, pra distinguir no log de auditoria uma execução
    automática de uma disparada manualmente por uma pessoa via CLI.
    """
    return purge_enrichment_cache(
        cache_path, ttl_days=ttl_days, audit_log_path=audit_log_path, usuario="scheduler"
    )
