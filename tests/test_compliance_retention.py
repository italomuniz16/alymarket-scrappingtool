"""Testes de `compliance/retention.py`: expurgo do cache de enriquecimento por
TTL de retenção (`purge_enrichment_cache`), o job de limpeza (`run_retention_job`),
e o registro correspondente no log de auditoria.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.compliance.audit_log import read_audit_log
from src.compliance.retention import (
    DEFAULT_RETENTION_TTL_DAYS,
    RetentionPurgeResult,
    purge_enrichment_cache,
    run_retention_job,
)
from src.enrichment.client import EnrichmentCache

NOW = datetime(2026, 6, 15, tzinfo=UTC)


def _seed_cache(cache_path: Path, entries: dict[str, datetime]) -> None:
    """Popula o cache com uma entrada por `(chave, created_at)` — TTL de frescor
    bem longo (não deve interferir no expurgo por retenção, que olha só created_at)."""
    with EnrichmentCache(cache_path) as cache:
        for key, created_at in entries.items():
            cache.set(key, {"chave": key}, ttl_seconds=999_999_999, now=created_at.timestamp())


class TestPurgeEnrichmentCache:
    def test_purges_entries_older_than_ttl(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        _seed_cache(
            cache_path,
            {
                "velho": NOW - timedelta(days=200),
                "no-limite": NOW - timedelta(days=181),
                "novo": NOW - timedelta(days=10),
            },
        )

        result = purge_enrichment_cache(
            cache_path,
            ttl_days=180,
            now=NOW,
            audit_log_path=tmp_path / "audit.parquet",
        )

        assert result.n_purged == 2
        with EnrichmentCache(cache_path) as cache:
            assert cache.get("velho", now=NOW.timestamp()) is None
            assert cache.get("no-limite", now=NOW.timestamp()) is None
            assert cache.get("novo", now=NOW.timestamp()) == {"chave": "novo"}

    def test_nothing_to_purge_returns_zero(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        _seed_cache(cache_path, {"recente": NOW - timedelta(days=1)})

        result = purge_enrichment_cache(
            cache_path, ttl_days=180, now=NOW, audit_log_path=tmp_path / "audit.parquet"
        )

        assert result.n_purged == 0

    def test_cutoff_computed_from_ttl_days(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        result = purge_enrichment_cache(
            cache_path, ttl_days=30, now=NOW, audit_log_path=tmp_path / "audit.parquet"
        )
        assert result.cutoff == NOW - timedelta(days=30)

    def test_default_ttl_matches_env_example(self) -> None:
        assert DEFAULT_RETENTION_TTL_DAYS == 180

    def test_returns_retention_purge_result_with_cache_path(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        result = purge_enrichment_cache(
            cache_path, now=NOW, audit_log_path=tmp_path / "audit.parquet"
        )
        assert isinstance(result, RetentionPurgeResult)
        assert result.cache_path == cache_path

    def test_missing_cache_file_purges_nothing_without_error(self, tmp_path: Path) -> None:
        result = purge_enrichment_cache(
            tmp_path / "nao-existe.sqlite", now=NOW, audit_log_path=tmp_path / "audit.parquet"
        )
        assert result.n_purged == 0


class TestPurgeEnrichmentCacheAudit:
    def test_records_audit_event(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        audit_log_path = tmp_path / "audit.parquet"
        _seed_cache(cache_path, {"velho": NOW - timedelta(days=200)})

        purge_enrichment_cache(
            cache_path,
            ttl_days=180,
            now=NOW,
            audit_log_path=audit_log_path,
            usuario="italo",
        )

        df = read_audit_log(audit_log_path)
        assert df.height == 1
        row = df.to_dicts()[0]
        assert row["operacao"] == "retention_purge"
        assert row["usuario"] == "italo"
        assert row["n_registros"] == 1

        filtros = json.loads(row["filtros"])
        assert filtros["ttl_dias"] == 180

    def test_records_audit_event_even_when_nothing_purged(self, tmp_path: Path) -> None:
        """O expurgo é uma operação sensível registrada sempre, mesmo sem nada pra
        remover -- mesma filosofia de export/enrich/query (ver CLAUDE.md)."""
        audit_log_path = tmp_path / "audit.parquet"

        purge_enrichment_cache(tmp_path / "cache.sqlite", now=NOW, audit_log_path=audit_log_path)

        df = read_audit_log(audit_log_path)
        assert df.height == 1
        assert df.to_dicts()[0]["n_registros"] == 0


class TestRunRetentionJob:
    def test_delegates_to_purge_with_scheduler_usuario(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.sqlite"
        audit_log_path = tmp_path / "audit.parquet"
        _seed_cache(cache_path, {"velho": datetime.now(UTC) - timedelta(days=200)})

        result = run_retention_job(
            cache_path=cache_path, ttl_days=180, audit_log_path=audit_log_path
        )

        assert result.n_purged == 1
        row = read_audit_log(audit_log_path).to_dicts()[0]
        assert row["usuario"] == "scheduler"
