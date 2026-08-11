"""Descoberta de CNPJs reais via sitemap público do cnpja.com
(`/sitemaps/establishments/index.xml`).

Não é scraping de página protegida: sitemap.xml é um recurso público de indexação,
sem `Disallow` no `robots.txt` de cnpja.com, publicado justamente para ser lido por
máquina (ver CLAUDE.md — "fonte oficial/API sempre antes de scraping; scraping só
onde permitido, respeitando robots.txt"). Este módulo só descobre QUAIS CNPJs
existem (números, nada mais); os dados completos de cada um vêm da API aberta do
OpenCNPJ (`ingestion/br_opencnpj/client.py`), não daqui.

O índice mistura baldes semanais minúsculos/antigos (às vezes só 1-2 entradas) com
baldes recentes de dezenas de milhares — `discover` escolhe sempre o maior.
"""

from __future__ import annotations

import re

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

DEFAULT_SITEMAP_INDEX_URL = "https://cnpja.com/sitemaps/establishments/index.xml"
DEFAULT_USER_AGENT = (
    "alymarket-bot/0.1 (uso academico/prospeccao B2B; contato configuravel via HTTP_USER_AGENT)"
)

_INDEX_ENTRY_PATTERN = re.compile(
    r"<loc>(https://cnpja\.com/sitemaps/establishments/[^<]+)</loc>\s*"
    r"<lastmod>[^<]+</lastmod>\s*<!--(\d+)-->"
)
_CNPJ_PATTERN = re.compile(r"https://cnpja\.com/office/(\d{14})")


class SitemapDiscoveryError(RuntimeError):
    """Levantado quando o índice de sitemaps não tem nenhuma entrada utilizável."""


class SitemapCnpjDiscovery:
    """Descobre CNPJs reais a partir do índice de sitemaps de estabelecimentos do
    cnpja.com.

    Uso típico::

        with SitemapCnpjDiscovery() as discovery:
            cnpjs = discovery.discover(40)
    """

    def __init__(
        self,
        index_url: str = DEFAULT_SITEMAP_INDEX_URL,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20.0,
        max_attempts: int = 5,
        retry_wait_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.index_url = index_url
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=retry_wait_seconds, min=retry_wait_seconds, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )

    def close(self) -> None:
        """Fecha o client HTTP subjacente."""
        self._client.close()

    def __enter__(self) -> SitemapCnpjDiscovery:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def discover(self, n: int) -> list[str]:
        """Retorna até `n` CNPJs (14 dígitos, únicos, ordem de aparição) extraídos
        do maior sub-sitemap listado no índice.

        Raises:
            SitemapDiscoveryError: se o índice não tiver nenhum sub-sitemap.
        """
        index_xml = self._retrying(self._get_text, self.index_url)
        entries = _INDEX_ENTRY_PATTERN.findall(index_xml)
        if not entries:
            raise SitemapDiscoveryError(f"Nenhum sub-sitemap encontrado em {self.index_url!r}")

        largest_url = max(entries, key=lambda entry: int(entry[1]))[0]
        sitemap_xml = self._retrying(self._get_text, largest_url)

        seen: dict[str, None] = {}
        for cnpj in _CNPJ_PATTERN.findall(sitemap_xml):
            seen.setdefault(cnpj, None)
            if len(seen) >= n:
                break
        return list(seen)

    def _get_text(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text
