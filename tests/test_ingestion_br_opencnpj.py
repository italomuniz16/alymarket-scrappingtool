"""Testes de `ingestion/br_opencnpj/` (descoberta via sitemap + cliente da API do
OpenCNPJ) com servidor HTTP mockado (`httpx.MockTransport`) — nenhuma chamada de
rede real é feita, mesma abordagem de `test_br_receita_downloader.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from src.ingestion.br_opencnpj.client import OpenCnpjClient
from src.ingestion.br_opencnpj.discovery import SitemapCnpjDiscovery, SitemapDiscoveryError

INDEX_URL = "https://cnpja.com/sitemaps/establishments/index.xml"


def _index_xml(entries: list[tuple[str, int]]) -> str:
    items = "".join(
        f"<sitemap><loc>{url}</loc><lastmod>2026-01-01T00:00:00+00:00</lastmod>"
        f"<!--{count}--></sitemap>\n"
        for url, count in entries
    )
    return f'<?xml version="1.0"?><sitemapindex xmlns="x">\n{items}</sitemapindex>'


def _sitemap_xml(cnpjs: list[str]) -> str:
    items = "".join(
        f"<url><loc>https://cnpja.com/office/{c}</loc>"
        f"<lastmod>2026-01-01T00:00:00+00:00</lastmod></url>\n"
        for c in cnpjs
    )
    return f'<?xml version="1.0"?><urlset xmlns="x">\n{items}</urlset>'


class TestSitemapCnpjDiscovery:
    def test_discover_picks_largest_subsitemap(self) -> None:
        small_url = "https://cnpja.com/sitemaps/establishments/1900-W01.xml.gz"
        large_url = "https://cnpja.com/sitemaps/establishments/2026-W09.xml.gz"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == INDEX_URL:
                return httpx.Response(
                    200, text=_index_xml([(small_url, 2), (large_url, 39000)])
                )
            if str(request.url) == small_url:
                return httpx.Response(200, text=_sitemap_xml(["00000000000191"]))
            if str(request.url) == large_url:
                return httpx.Response(
                    200, text=_sitemap_xml(["11111111000111", "22222222000122", "33333333000133"])
                )
            return httpx.Response(404)

        with SitemapCnpjDiscovery(
            INDEX_URL, transport=httpx.MockTransport(handler), retry_wait_seconds=0
        ) as discovery:
            cnpjs = discovery.discover(10)

        assert cnpjs == ["11111111000111", "22222222000122", "33333333000133"]

    def test_discover_limits_to_n(self) -> None:
        url = "https://cnpja.com/sitemaps/establishments/2026-W09.xml.gz"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == INDEX_URL:
                return httpx.Response(200, text=_index_xml([(url, 5)]))
            return httpx.Response(
                200,
                text=_sitemap_xml(
                    ["11111111000111", "22222222000122", "33333333000133", "44444444000144"]
                ),
            )

        with SitemapCnpjDiscovery(
            INDEX_URL, transport=httpx.MockTransport(handler), retry_wait_seconds=0
        ) as discovery:
            cnpjs = discovery.discover(2)

        assert cnpjs == ["11111111000111", "22222222000122"]

    def test_discover_dedupes_repeated_cnpjs(self) -> None:
        url = "https://cnpja.com/sitemaps/establishments/2026-W09.xml.gz"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == INDEX_URL:
                return httpx.Response(200, text=_index_xml([(url, 5)]))
            return httpx.Response(
                200, text=_sitemap_xml(["11111111000111", "11111111000111", "22222222000122"])
            )

        with SitemapCnpjDiscovery(
            INDEX_URL, transport=httpx.MockTransport(handler), retry_wait_seconds=0
        ) as discovery:
            cnpjs = discovery.discover(10)

        assert cnpjs == ["11111111000111", "22222222000122"]

    def test_raises_when_index_has_no_entries(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_index_xml([]))

        with (
            SitemapCnpjDiscovery(
                INDEX_URL, transport=httpx.MockTransport(handler), retry_wait_seconds=0
            ) as discovery,
            pytest.raises(SitemapDiscoveryError),
        ):
            discovery.discover(10)


@dataclass
class FakeOpenCnpjServer:
    """Handler de `httpx.MockTransport` que simula a API do OpenCNPJ."""

    records: dict[str, dict[str, object]]
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        cnpj = request.url.path.rsplit("/", 1)[-1]
        record = self.records.get(cnpj)
        if record is None:
            return httpx.Response(200, json={"success": False, "message": "não encontrado"})
        return httpx.Response(200, json={"success": True, "message": None, "data": record})


class TestOpenCnpjClient:
    def test_fetch_one_returns_data_on_success(self) -> None:
        server = FakeOpenCnpjServer(
            records={"00000000083208": {"razaoSocial": "BANCO DO BRASIL SA"}}
        )
        with OpenCnpjClient(
            transport=httpx.MockTransport(server), rate_limit_seconds=0, retry_wait_seconds=0
        ) as client:
            record = client.fetch_one("00000000083208")

        assert record == {"razaoSocial": "BANCO DO BRASIL SA"}

    def test_fetch_one_returns_none_when_not_found(self) -> None:
        server = FakeOpenCnpjServer(records={})
        with OpenCnpjClient(
            transport=httpx.MockTransport(server), rate_limit_seconds=0, retry_wait_seconds=0
        ) as client:
            record = client.fetch_one("99999999000199")

        assert record is None

    def test_fetch_many_skips_not_found_and_yields_the_rest(self) -> None:
        server = FakeOpenCnpjServer(
            records={
                "11111111000111": {"razaoSocial": "EMPRESA UM"},
                "33333333000133": {"razaoSocial": "EMPRESA TRES"},
            }
        )
        with OpenCnpjClient(
            transport=httpx.MockTransport(server), rate_limit_seconds=0, retry_wait_seconds=0
        ) as client:
            records = list(
                client.fetch_many(["11111111000111", "22222222000122", "33333333000133"])
            )

        assert [r["razaoSocial"] for r in records] == ["EMPRESA UM", "EMPRESA TRES"]

    def test_fetch_many_sleeps_between_requests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = FakeOpenCnpjServer(
            records={"11111111000111": {"a": 1}, "22222222000122": {"a": 2}}
        )
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "src.ingestion.br_opencnpj.client.time.sleep", lambda s: sleep_calls.append(s)
        )

        with OpenCnpjClient(
            transport=httpx.MockTransport(server), rate_limit_seconds=0.7, retry_wait_seconds=0
        ) as client:
            list(client.fetch_many(["11111111000111", "22222222000122"]))

        # 2 CNPJs -> 1 pausa entre eles (não antes do primeiro).
        assert sleep_calls == [0.7]
