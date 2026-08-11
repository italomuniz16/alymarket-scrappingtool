"""Cliente HTTP para a API pública e gratuita do OpenCNPJ (`kitana.opencnpj.com`) —
consulta de CNPJ individual, sem autenticação, dados oficiais da Receita Federal
(projeto open source: https://github.com/opencaramelo/opencnpj).

Usado como fonte alternativa para `pais=BR` enquanto `br_receita/downloader.py`
(URL oficial original) está desativado (portal migrou — ver CLAUDE.md/histórico do
projeto). Este módulo só busca e devolve registros BRUTOS (dict no formato da API);
o mapeamento pro schema canônico fica em `etl/canonical.map_opencnpj_to_canonical`
— mesma separação de responsabilidades dos outros conectores (parse bruto na
camada de ingestão, canonical na camada ETL).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://kitana.opencnpj.com/"
DEFAULT_USER_AGENT = (
    "alymarket-bot/0.1 (uso academico/prospeccao B2B; contato configuravel via HTTP_USER_AGENT)"
)
# Documentado como 100 req/min; folga de segurança (≈85 req/min).
DEFAULT_RATE_LIMIT_SECONDS = 0.7


class OpenCnpjClient:
    """Busca dados completos de CNPJs individuais na API pública do OpenCNPJ.

    Uso típico::

        with OpenCnpjClient() as client:
            registros = list(client.fetch_many(["00000000083208", ...]))

    Para testes, injete `transport=httpx.MockTransport(handler)` — nenhuma chamada
    de rede real é feita nesse caso.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20.0,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        max_attempts: int = 5,
        retry_wait_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.rate_limit_seconds = rate_limit_seconds

        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=retry_wait_seconds, min=retry_wait_seconds, max=30),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )

    def close(self) -> None:
        """Fecha o client HTTP subjacente."""
        self._client.close()

    def __enter__(self) -> OpenCnpjClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch_one(self, cnpj: str) -> dict[str, object] | None:
        """Busca um único CNPJ.

        Returns:
            O dict `data` da resposta, ou `None` se a API não encontrou o CNPJ
            (HTTP 404, ou `success: false` no corpo) — não levanta erro; quem chama
            decide se conta/loga o registro pulado (ver `fetch_many`).
        """
        response = self._retrying(self._client.get, f"{self.base_url}cnpj/{cnpj}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            logger.warning("OpenCNPJ recusou cnpj=%r: %s", cnpj, body.get("message"))
            return None
        return body["data"]

    def fetch_many(self, cnpjs: list[str]) -> Iterator[dict[str, object]]:
        """Busca vários CNPJs, um por request (a API não tem endpoint em lote),
        respeitando `rate_limit_seconds` entre chamadas. CNPJs não encontrados são
        pulados silenciosamente (a contagem de pulados fica a cargo de quem chama,
        comparando `len(cnpjs)` com o total gerado aqui)."""
        for i, cnpj in enumerate(cnpjs):
            if i > 0 and self.rate_limit_seconds > 0:
                time.sleep(self.rate_limit_seconds)
            record = self.fetch_one(cnpj)
            if record is not None:
                yield record
