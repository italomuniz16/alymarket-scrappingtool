"""Testes de `enrichment/client.py`: rate limiter e cache isolados, e o cliente HTTP
completo contra um servidor mockado (`httpx.MockTransport`) cobrindo cache hit/miss,
backoff/retry, e respeito ao rate limit -- sem nenhuma chamada de rede real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

import src.enrichment.client as client_module
from src.enrichment.client import (
    DEFAULT_USER_AGENT,
    EnrichmentCache,
    EnrichmentClient,
    EnrichmentError,
    RateLimiter,
    enrich_leads,
)


class TestRateLimiter:
    def test_min_interval_zero_never_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

        limiter = RateLimiter(min_interval_seconds=0)
        limiter.wait()
        limiter.wait()

        assert sleeps == []

    def test_first_call_never_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(client_module.time, "monotonic", lambda: 42.0)

        RateLimiter(min_interval_seconds=5.0).wait()

        assert sleeps == []

    def test_second_call_sleeps_for_remaining_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
        values = iter([0.0, 0.0, 0.3, 0.3])
        monkeypatch.setattr(client_module.time, "monotonic", lambda: next(values))

        limiter = RateLimiter(min_interval_seconds=1.0)
        limiter.wait()
        limiter.wait()

        assert sleeps == [pytest.approx(0.7)]

    def test_no_sleep_when_enough_time_already_elapsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
        values = iter([0.0, 0.0, 5.0, 5.0])
        monkeypatch.setattr(client_module.time, "monotonic", lambda: next(values))

        limiter = RateLimiter(min_interval_seconds=1.0)
        limiter.wait()
        limiter.wait()

        assert sleeps == []


class TestEnrichmentCache:
    def test_miss_returns_none(self, tmp_path: Path) -> None:
        with EnrichmentCache(tmp_path / "cache.sqlite") as cache:
            assert cache.get("nao-existe") is None

    def test_set_then_get_is_a_hit(self, tmp_path: Path) -> None:
        with EnrichmentCache(tmp_path / "cache.sqlite") as cache:
            cache.set("12345678", {"razao_social": "ACME"}, ttl_seconds=3600)
            assert cache.get("12345678") == {"razao_social": "ACME"}

    def test_expired_entry_is_a_miss(self, tmp_path: Path) -> None:
        with EnrichmentCache(tmp_path / "cache.sqlite") as cache:
            cache.set("12345678", {"a": 1}, ttl_seconds=10, now=1000.0)
            assert cache.get("12345678", now=1011.0) is None

    def test_expired_entry_is_actually_removed(self, tmp_path: Path) -> None:
        with EnrichmentCache(tmp_path / "cache.sqlite") as cache:
            cache.set("x", {"a": 1}, ttl_seconds=10, now=1000.0)
            cache.get("x", now=1011.0)  # dispara a remoção

            row = cache._conn.execute("SELECT 1 FROM cache WHERE key = ?", ("x",)).fetchone()
            assert row is None

    def test_not_yet_expired_is_a_hit(self, tmp_path: Path) -> None:
        with EnrichmentCache(tmp_path / "cache.sqlite") as cache:
            cache.set("12345678", {"a": 1}, ttl_seconds=10, now=1000.0)
            assert cache.get("12345678", now=1005.0) == {"a": 1}

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cache.sqlite"
        with EnrichmentCache(db_path) as cache1:
            cache1.set("12345678", {"razao_social": "ACME"}, ttl_seconds=3600)

        with EnrichmentCache(db_path) as cache2:
            assert cache2.get("12345678") == {"razao_social": "ACME"}

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "dir" / "cache.sqlite"
        with EnrichmentCache(db_path):
            assert db_path.parent.is_dir()


@dataclass
class FakeApiServer:
    """Handler de `httpx.MockTransport`: cada URL tem uma sequência de respostas
    (status, body) — chamadas além do fim da sequência repetem a última."""

    responses: dict[str, list[tuple[int, dict[str, object]]]]
    requests: list[httpx.Request] = field(default_factory=list)
    _call_counts: dict[str, int] = field(default_factory=dict, init=False)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        sequence = self.responses.get(url, [(404, {"error": "not found"})])
        idx = self._call_counts.get(url, 0)
        status, body = sequence[min(idx, len(sequence) - 1)]
        self._call_counts[url] = idx + 1
        return httpx.Response(status, json=body)


def _make_client(server: FakeApiServer, tmp_path: Path, **kwargs: object) -> EnrichmentClient:
    kwargs.setdefault("min_interval_seconds", 0)
    kwargs.setdefault("retry_wait_seconds", 0)
    kwargs.setdefault("cache_path", tmp_path / "cache.sqlite")
    return EnrichmentClient(
        transport=httpx.MockTransport(server),
        **kwargs,  # type: ignore[arg-type]
    )


class TestEnrichmentClientCache:
    def test_cache_miss_makes_a_request(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"razao_social": "ACME"})]})
        with _make_client(server, tmp_path) as client:
            data = client.get_json(url)

        assert data == {"razao_social": "ACME"}
        assert len(server.requests) == 1

    def test_cache_hit_does_not_make_a_second_request(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"razao_social": "ACME"})]})
        with _make_client(server, tmp_path) as client:
            client.get_json(url)
            data = client.get_json(url)

        assert data == {"razao_social": "ACME"}
        assert len(server.requests) == 1

    def test_different_cache_keys_both_hit_the_network(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"razao_social": "ACME"})]})
        with _make_client(server, tmp_path) as client:
            client.get_json(url, cache_key="a")
            client.get_json(url, cache_key="b")

        assert len(server.requests) == 2

    def test_cache_persists_across_client_instances(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        cache_path = tmp_path / "cache.sqlite"
        server = FakeApiServer(responses={url: [(200, {"razao_social": "ACME"})]})

        with _make_client(server, tmp_path, cache_path=cache_path) as client:
            client.get_json(url)

        server2 = FakeApiServer(responses={url: [(200, {"razao_social": "OUTRO"})]})
        with _make_client(server2, tmp_path, cache_path=cache_path) as client2:
            data = client2.get_json(url)

        assert data == {"razao_social": "ACME"}  # veio do cache, não do server2
        assert len(server2.requests) == 0


class TestEnrichmentClientRetry:
    def test_retries_and_succeeds_after_transient_failures(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(
            responses={url: [(500, {}), (500, {}), (200, {"razao_social": "ACME"})]}
        )
        with _make_client(server, tmp_path, max_attempts=5) as client:
            data = client.get_json(url)

        assert data == {"razao_social": "ACME"}
        assert len(server.requests) == 3

    def test_gives_up_after_max_attempts(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(500, {})]})
        with _make_client(server, tmp_path, max_attempts=3) as client, pytest.raises(
            httpx.HTTPStatusError
        ):
            client.get_json(url)

        assert len(server.requests) == 3

    def test_failed_request_is_not_cached(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(500, {})]})
        with _make_client(server, tmp_path, max_attempts=2) as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.get_json(url)
            assert client._cache.get(url) is None


class TestEnrichmentClientRateLimit:
    def test_sleeps_between_two_network_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Só mocka `sleep`, não `monotonic`: o tenacity também lê time.monotonic()
        # internamente (é o mesmo módulo global) -- mockar o relógio junto
        # interferiria com a contabilidade dele. Como não há retry aqui (resposta
        # 200 na primeira tentativa), tenacity nunca chama sleep -- só o
        # RateLimiter chamaria, e usar o relógio real com um intervalo pequeno é
        # suficiente pra provar que o sleep aconteceu com um valor plausível.
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"ok": True})]})

        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

        with _make_client(server, tmp_path, min_interval_seconds=0.05) as client:
            client.get_json(url, cache_key="a")
            client.get_json(url, cache_key="b")

        assert len(sleeps) == 1
        assert 0 < sleeps[0] <= 0.05

    def test_no_rate_limit_wait_on_cache_hit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"ok": True})]})

        sleeps: list[float] = []
        monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

        with _make_client(server, tmp_path, min_interval_seconds=1.0) as client:
            client.get_json(url)  # miss: primeira chamada, RateLimiter não dorme
            client.get_json(url)  # hit: nem chega a chamar o rate limiter

        assert sleeps == []


class TestEnrichmentClientUserAgent:
    def test_sends_configured_user_agent(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"ok": True})]})
        with _make_client(server, tmp_path, user_agent="meu-bot/1.0") as client:
            client.get_json(url)

        assert server.requests[0].headers["user-agent"] == "meu-bot/1.0"

    def test_default_user_agent_is_identified(self, tmp_path: Path) -> None:
        url = "https://api.test/cnpj/1"
        server = FakeApiServer(responses={url: [(200, {"ok": True})]})
        with _make_client(server, tmp_path) as client:
            client.get_json(url)

        assert server.requests[0].headers["user-agent"] == DEFAULT_USER_AGENT
        assert "alymarket" in DEFAULT_USER_AGENT


class TestEnrichLeads:
    def test_enriches_explicit_subset(self, tmp_path: Path) -> None:
        server = FakeApiServer(
            responses={
                "https://api.test/cnpj/AAA": [(200, {"razao_social": "A"})],
                "https://api.test/cnpj/BBB": [(200, {"razao_social": "B"})],
            }
        )
        with _make_client(server, tmp_path) as client:
            resultado = enrich_leads(
                client, ["AAA", "BBB"], url_template="https://api.test/cnpj/{id}"
            )

        assert resultado == {
            "AAA": {"razao_social": "A"},
            "BBB": {"razao_social": "B"},
        }

    def test_empty_list_raises_without_requesting(self, tmp_path: Path) -> None:
        server = FakeApiServer(responses={})
        with _make_client(server, tmp_path) as client, pytest.raises(EnrichmentError):
            enrich_leads(client, [], url_template="https://api.test/cnpj/{id}")
        assert server.requests == []

    def test_exceeds_max_batch_size_raises_without_requesting(self, tmp_path: Path) -> None:
        server = FakeApiServer(responses={})
        with (
            _make_client(server, tmp_path) as client,
            pytest.raises(EnrichmentError, match="limite"),
        ):
            enrich_leads(
                client,
                ["A", "B", "C"],
                url_template="https://api.test/cnpj/{id}",
                max_batch_size=2,
            )
        assert server.requests == []

    def test_one_failure_does_not_stop_the_rest(self, tmp_path: Path) -> None:
        server = FakeApiServer(
            responses={
                "https://api.test/cnpj/OK": [(200, {"razao_social": "OK LTDA"})],
                "https://api.test/cnpj/RUIM": [(500, {})],
            }
        )
        with _make_client(server, tmp_path, max_attempts=1) as client:
            resultado = enrich_leads(
                client, ["OK", "RUIM"], url_template="https://api.test/cnpj/{id}"
            )

        assert resultado["OK"] == {"razao_social": "OK LTDA"}
        assert resultado["RUIM"] is None
