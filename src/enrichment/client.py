"""Cliente HTTP resiliente para enriquecimento sob demanda (BrasilAPI, CNPJá, API
Sirene, ...): rate limit configurável, retries com backoff exponencial (tenacity),
timeout, cache local persistido com TTL (evita rechamar o mesmo CNPJ/SIREN entre
execuções), User-Agent identificado.

Este módulo é o cliente HTTP genérico; a lógica específica de cada provedor
(BrasilAPI, API Recherche d'Entreprises) fica em `enrichment/providers.py`, que
constrói sobre `EnrichmentClient`.

## Nunca sobre a base inteira

`enrich_leads` exige uma lista explícita e finita de identificadores (ex.: o
resultado de uma exportação já filtrada) — não existe, de propósito, nenhuma função
aqui que aceite "todos os leads" ou consulte o warehouse diretamente. `max_batch_size`
(default 1000) é reforçado em código, não só documentação: passar mais identificadores
do que isso levanta erro antes de fazer qualquer requisição.

## Auditoria

`enrich_leads` é o único ponto de entrada usado por todo `enrichment/providers.py`
(BR e FR) — por isso é aqui, e só aqui, que cada tentativa de enriquecimento registra
um evento em `compliance/audit_log.py` (operação sensível, ver CLAUDE.md), sempre,
sem parâmetro pra pular essa etapa: mesma filosofia de `export/exporters.py`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.compliance.audit_log import DEFAULT_AUDIT_LOG_PATH, new_event, record_event

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "alymarket-bot/0.1 (enriquecimento sob demanda; contato configuravel via HTTP_USER_AGENT)"
)
DEFAULT_CACHE_PATH = Path("./data/warehouse/enrichment_cache.sqlite")
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 dias — mesmo default do .env.example
DEFAULT_MAX_BATCH_SIZE = 1000


class EnrichmentError(RuntimeError):
    """Levantado para uso inválido do cliente/lote de enriquecimento."""


class RateLimiter:
    """Garante um intervalo mínimo entre chamadas consecutivas de `wait()`.

    `min_interval_seconds <= 0` desativa o limite (`wait()` nunca dorme).
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._last_call: float | None = None

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return

        now = time.monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)

        self._last_call = time.monotonic()


class EnrichmentCache:
    """Cache local persistido (SQLite) com TTL por entrada — evita rechamar o mesmo
    CNPJ/SIREN dentro da janela de validade, inclusive entre execuções diferentes do
    processo (não é só um dict em memória).

    Guarda `created_at` além de `expires_at`: são conceitos diferentes.
    `expires_at` (`ttl_seconds`, passado em cada `set()`) é sobre FRESCOR — quando um
    valor cacheado deixa de ser confiável pra reuso (dias, ver
    `ENRICHMENT_CACHE_TTL_SECONDS` no `.env.example`). `created_at` é sobre
    RETENÇÃO — desde quando esse dado enriquecido está guardado, independente de
    ainda estar "fresco" ou não; é o que `compliance/retention.py` usa pra decidir o
    que expurgar (janela bem mais longa, meses — `RETENTION_TTL_DAYS`). Uma entrada
    pode reexpirar (`expires_at`) e ser reescrita várias vezes sem nunca ser
    expurgada por retenção, e vice-versa.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "created_at REAL NOT NULL, expires_at REAL NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Valor em cache para `key`, ou `None` se ausente ou expirado (entradas
        expiradas são removidas na leitura, não ficam acumulando)."""
        current = now if now is not None else time.time()
        row = self._conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None

        value_json, expires_at = row
        if expires_at < current:
            self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()
            return None

        result: dict[str, Any] = json.loads(value_json)
        return result

    def set(
        self, key: str, value: dict[str, Any], ttl_seconds: int, *, now: float | None = None
    ) -> None:
        current = now if now is not None else time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (key, json.dumps(value), current, current + ttl_seconds),
        )
        self._conn.commit()

    def purge_created_before(self, cutoff: float) -> int:
        """Expurga (DELETE) toda entrada com `created_at < cutoff` — o "job de
        limpeza" de retenção (ver `compliance/retention.py`), independente de a
        entrada ainda estar "fresca" (`expires_at`) ou não: retenção é sobre por
        quanto tempo o dado pode ficar guardado, não sobre se ele ainda é útil.

        Returns:
            Número de entradas removidas.
        """
        cursor = self._conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EnrichmentCache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EnrichmentClient:
    """Cliente HTTP GET resiliente com cache, rate limit e retry.

    Uso típico::

        with EnrichmentClient() as client:
            dados = client.get_json("https://brasilapi.com.br/api/cnpj/v1/12345678000199")

    Para testes, injete `transport=httpx.MockTransport(handler)` — nenhuma chamada de
    rede real é feita nesse caso.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 1.0,
        max_attempts: int = 5,
        retry_wait_seconds: float = 2.0,
        cache_path: Path | str = DEFAULT_CACHE_PATH,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        extra_headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            user_agent: identificação enviada em todo request.
            timeout_seconds: timeout HTTP por request.
            min_interval_seconds: intervalo mínimo entre requisições de rede
                (rate limit); `0` desativa (usado nos testes).
            max_attempts: tentativas por requisição antes de desistir.
            retry_wait_seconds: base do backoff exponencial entre tentativas; `0`
                desativa a espera (usado nos testes).
            cache_path: onde persistir o cache (SQLite).
            cache_ttl_seconds: por quanto tempo uma resposta em cache é válida.
            extra_headers: headers adicionais enviados em todo request (ex.:
                `{"Authorization": "Bearer ..."}` pra uma API autenticada como a
                Sirene do INSEE — ver `ingestion/fr_sirene/api_client.py`). `None`
                (default) não adiciona nenhum.
            transport: transporte httpx alternativo (ex.: `httpx.MockTransport` em
                testes). `None` usa a rede real.
        """
        headers = {"User-Agent": user_agent}
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            transport=transport,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._rate_limiter = RateLimiter(min_interval_seconds)
        self._cache = EnrichmentCache(cache_path)
        self._cache_ttl_seconds = cache_ttl_seconds
        retryable_errors = (httpx.TransportError, httpx.HTTPStatusError)
        self._retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=retry_wait_seconds, min=retry_wait_seconds, max=30),
            retry=retry_if_exception_type(retryable_errors),
            reraise=True,
        )

    def close(self) -> None:
        self._client.close()
        self._cache.close()

    def __enter__(self) -> EnrichmentClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_json(self, url: str, *, cache_key: str | None = None) -> dict[str, Any]:
        """`GET url`, com cache, rate limit e retry. Retorna o corpo JSON decodificado.

        Args:
            url: URL completa a consultar.
            cache_key: chave do cache; default: a própria `url`. Passe o
                identificador (CNPJ/SIREN) quando ele já for único por si só, para o
                cache funcionar mesmo que a URL mude de formato depois.
        """
        key = cache_key or url

        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("Cache hit: %s", key)
            return cached

        logger.debug("Cache miss: %s", key)
        self._rate_limiter.wait()
        response = self._retrying(self._get_once, url)
        data: dict[str, Any] = response.json()

        self._cache.set(key, data, self._cache_ttl_seconds)
        return data

    def _get_once(self, url: str) -> httpx.Response:
        response = self._client.get(url)
        response.raise_for_status()
        return response


def enrich_leads(
    client: EnrichmentClient,
    identificadores: Sequence[str],
    *,
    url_template: str,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    usuario: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Enriquece um SUBCONJUNTO explícito de identificadores (CNPJ/SIREN) — nunca a
    base inteira (ver docstring do módulo).

    Args:
        client: `EnrichmentClient` já configurado.
        identificadores: lista já filtrada/selecionada de identificadores (ex.: o
            resultado de uma exportação). Sequência finita, obrigatória.
        url_template: string com um `{id}` a ser preenchido por cada identificador
            (ex.: `"https://brasilapi.com.br/api/cnpj/v1/{id}"`).
        max_batch_size: máximo de identificadores por chamada — reforçado em código
            (`EnrichmentError`), não só como recomendação.
        audit_log_path: onde registrar o evento de auditoria (ver módulo
            `compliance/audit_log.py`) — registrado sempre, sem parâmetro pra
            desativar (operação sensível, ver CLAUDE.md).
        usuario: quem disparou o enriquecimento; default: usuário do SO.

    Returns:
        `{identificador: resposta_json}`; `None` no lugar da resposta se aquele
        identificador falhou (erro logado, não interrompe os demais).

    Raises:
        EnrichmentError: se `identificadores` exceder `max_batch_size`, ou estiver
            vazio. Levantado ANTES de qualquer requisição/registro de auditoria —
            uma tentativa rejeitada por uso inválido não gera evento.
    """
    if not identificadores:
        raise EnrichmentError("Nenhum identificador informado para enriquecer.")
    if len(identificadores) > max_batch_size:
        raise EnrichmentError(
            f"{len(identificadores)} identificadores excede o limite de "
            f"{max_batch_size} por lote — enriquecimento nunca roda sobre a base inteira."
        )

    resultados: dict[str, dict[str, Any] | None] = {}
    for identificador in identificadores:
        url = url_template.format(id=identificador)
        try:
            resultados[identificador] = client.get_json(url, cache_key=identificador)
        except httpx.HTTPError as exc:
            logger.warning("Falha ao enriquecer %s: %s", identificador, exc)
            resultados[identificador] = None

    n_encontrados = sum(1 for v in resultados.values() if v is not None)
    record_event(
        new_event(
            "enrich_leads",
            usuario=usuario,
            filtros={
                "url_template": url_template,
                "quantidade_solicitada": len(identificadores),
            },
            n_registros=n_encontrados,
        ),
        audit_log_path,
    )

    return resultados
