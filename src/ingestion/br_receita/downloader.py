"""Download (retomável, com verificação de integridade) dos ZIPs de Dados Abertos CNPJ
da Receita Federal.

Fonte: https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/ — uma
listagem de diretório (estilo Apache/Nginx "Index of ...") com uma subpasta por
competência mensal (ex.: `2025-05/`), cada uma contendo os ZIPs de stock: `Empresas*`,
`Estabelecimentos*`, `Socios*` (numerados, um por "fatia" do arquivo) e as tabelas de
referência menores em arquivo único (`Simples`, `Cnaes`, `Municipios`, `Naturezas`,
`Paises`, `Qualificacoes`, `Motivos`).

Este módulo cobre só a parte de descoberta + download do contrato `SourceConnector`
(`src/ingestion/base.py`) — `check_latest`/`download`. Não faz parsing dos CSVs
(`parser.py`) nem descompacta os ZIPs (`extractor.py`). A futura classe
`BrReceitaConnector(SourceConnector)` deve compor `ReceitaCNPJDownloader` internamente.

Verificação de integridade: o tamanho final é sempre comparado com o `Content-Length`
reportado pelo servidor. Hash (SHA-256/MD5) é verificado *quando disponível*: não há
confirmação de que a Receita publica um arquivo de hash por ZIP, então a busca por um
sidecar (`{arquivo}.sha256`, `{arquivo}.md5`) é best-effort — se não existir (404), a
verificação cai para tamanho apenas, sem erro.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/"
DEFAULT_USER_AGENT = (
    "alymarket-bot/0.1 (uso academico/prospeccao B2B; contato configuravel via HTTP_USER_AGENT)"
)

_HREF_PATTERN = re.compile(r'href="([^"?#]+)"', re.IGNORECASE)
_COMPETENCIA_PATTERN = re.compile(r"^\d{4}-\d{2}$")

# Sidecars de hash tentados, em ordem, contra a URL do próprio arquivo.
_HASH_SIDECARS: tuple[tuple[str, str], ...] = ((".sha256", "sha256"), (".md5", "md5"))

_DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class DownloadIntegrityError(RuntimeError):
    """Levantado quando um arquivo baixado não bate com o tamanho ou hash esperado."""


class ReceitaCNPJDownloader:
    """Baixa os arquivos de stock da base CNPJ (Dados Abertos, Receita Federal).

    Uso típico::

        with ReceitaCNPJDownloader() as downloader:
            competencia = downloader.check_latest()
            arquivos = downloader.download(Path("data/raw"), competencia=competencia)

    Para testes, injete `transport=httpx.MockTransport(handler)` — nenhuma chamada de
    rede real é feita nesse caso.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 30.0,
        rate_limit_seconds: float = 0.5,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        max_attempts: int = 5,
        retry_wait_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            base_url: raiz da listagem de competências.
            user_agent: identificação enviada em todo request (boa prática para
                scraping/acesso educado a fontes públicas — ver CLAUDE.md).
            timeout_seconds: timeout HTTP por request.
            rate_limit_seconds: pausa entre o download de cada arquivo (rate limit
                gentil); `0` desativa (usado nos testes).
            chunk_size: tamanho do chunk de leitura/gravação em streaming.
            max_attempts: tentativas por arquivo antes de desistir (tenacity).
            retry_wait_seconds: base do backoff exponencial entre tentativas; `0`
                desativa a espera (usado nos testes).
            transport: transporte httpx alternativo (ex.: `httpx.MockTransport` em
                testes). `None` usa a rede real.
        """
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.rate_limit_seconds = rate_limit_seconds
        self.chunk_size = chunk_size

        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        retryable_errors = (httpx.TransportError, httpx.HTTPStatusError, DownloadIntegrityError)
        self._retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=retry_wait_seconds, min=retry_wait_seconds, max=30),
            retry=retry_if_exception_type(retryable_errors),
            reraise=True,
        )

    def close(self) -> None:
        """Fecha o client HTTP subjacente."""
        self._client.close()

    def __enter__(self) -> ReceitaCNPJDownloader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Descoberta ---------------------------------------------------------------

    def check_latest(self) -> str:
        """Descobre a competência mais recente disponível (ex.: `"2025-05"`).

        Formato `YYYY-MM` ordena corretamente por ordem lexicográfica, então a mais
        recente é sempre o máximo das pastas encontradas na listagem.
        """
        html = self._get_text(self.base_url)
        hrefs = (href.rstrip("/") for href in _HREF_PATTERN.findall(html))
        candidates = {href for href in hrefs if _COMPETENCIA_PATTERN.match(href)}
        if not candidates:
            raise RuntimeError(f"Nenhuma competência encontrada em {self.base_url}")

        latest = max(candidates)
        logger.info("Competência mais recente encontrada: %s", latest)
        return latest

    def list_files(self, competencia: str) -> list[str]:
        """Lista os nomes dos arquivos `.zip` disponíveis numa competência."""
        url = urljoin(self.base_url, f"{competencia}/")
        html = self._get_text(url)
        hrefs = _HREF_PATTERN.findall(html)
        return sorted({href for href in hrefs if href.lower().endswith(".zip")})

    # -- Download -------------------------------------------------------------------

    def download(
        self,
        dest: Path,
        *,
        competencia: str | None = None,
        only: list[str] | None = None,
    ) -> list[Path]:
        """Baixa os arquivos de stock de uma competência para `dest/{competencia}/`.

        Args:
            dest: diretório raiz (ex.: `data/raw`); os arquivos vão para
                `dest/{competencia}/arquivo.zip`.
            competencia: força uma competência específica; por padrão usa
                `check_latest()`.
            only: se dado, baixa só os arquivos cujo nome contém (case-insensitive)
                algum destes termos — ex.: `["ESTABELE", "EMPRE"]` baixa só
                `Estabelecimentos*.zip` e `Empresas*.zip`. Útil para testes e para
                não precisar da base inteira.

        Returns:
            Caminhos dos arquivos baixados (completos e com integridade verificada).
        """
        competencia = competencia or self.check_latest()
        target_dir = dest / competencia
        target_dir.mkdir(parents=True, exist_ok=True)

        filenames = self.list_files(competencia)
        if only:
            terms = [t.lower() for t in only]
            filenames = [f for f in filenames if any(t in f.lower() for t in terms)]

        if not filenames:
            raise RuntimeError(
                f"Nenhum arquivo encontrado para competência {competencia!r} (only={only!r})"
            )

        downloaded: list[Path] = []
        competencia_url = urljoin(self.base_url, f"{competencia}/")
        for i, filename in enumerate(filenames):
            if i > 0 and self.rate_limit_seconds > 0:
                time.sleep(self.rate_limit_seconds)

            file_url = urljoin(competencia_url, filename)
            dest_file = target_dir / filename
            self._retrying(self._download_one_attempt, file_url, dest_file)
            downloaded.append(dest_file)

        return downloaded

    # -- Internals --------------------------------------------------------------

    def _get_text(self, url: str) -> str:
        return self._retrying(self._get_text_once, url)

    def _get_text_once(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def _download_one_attempt(self, url: str, dest_file: Path) -> None:
        """Uma tentativa de baixar (ou retomar) um arquivo. Chamado via `Retrying`."""
        head = self._client.head(url)
        head.raise_for_status()
        content_length = head.headers.get("content-length")
        expected_size = int(content_length) if content_length is not None else None

        resume_from = dest_file.stat().st_size if dest_file.exists() else 0

        if expected_size is not None and resume_from == expected_size:
            logger.info("Já completo, pulando download: %s", dest_file.name)
            return
        if expected_size is not None and resume_from > expected_size:
            logger.warning("Arquivo local maior que o remoto, reiniciando: %s", dest_file.name)
            resume_from = 0
            dest_file.unlink(missing_ok=True)

        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        mode = "ab" if resume_from else "wb"

        logger.info(
            "Baixando %s (resume_from=%d bytes%s)",
            dest_file.name,
            resume_from,
            f", esperado={expected_size}" if expected_size is not None else "",
        )

        with self._client.stream("GET", url, headers=headers) as response:
            if resume_from and response.status_code != httpx.codes.PARTIAL_CONTENT:
                # Servidor não suportou (ou ignorou) o Range: recomeça do zero para
                # não duplicar bytes gravando o corpo inteiro em cima do parcial.
                logger.warning("Servidor não suportou Range para %s; recomeçando", dest_file.name)
                resume_from = 0
                mode = "wb"
            response.raise_for_status()
            with open(dest_file, mode) as f:
                for chunk in response.iter_bytes(self.chunk_size):
                    f.write(chunk)

        self._verify_integrity(dest_file, expected_size, url)

    def _verify_integrity(self, dest_file: Path, expected_size: int | None, url: str) -> None:
        actual_size = dest_file.stat().st_size
        if expected_size is not None and actual_size != expected_size:
            dest_file.unlink(missing_ok=True)
            raise DownloadIntegrityError(
                f"Tamanho inconsistente para {dest_file.name}: "
                f"esperado {expected_size}, obtido {actual_size}"
            )

        expected_hash = self._fetch_expected_hash(url)
        if expected_hash is None:
            logger.debug("Nenhum hash publicado para %s; validado só por tamanho", dest_file.name)
            return

        digest_hex, algo = expected_hash
        actual_hex = self._hash_of(dest_file, algo)
        if actual_hex.lower() != digest_hex.lower():
            dest_file.unlink(missing_ok=True)
            raise DownloadIntegrityError(f"Hash ({algo}) inconsistente para {dest_file.name}")

        logger.info("Hash (%s) verificado para %s", algo, dest_file.name)

    def _fetch_expected_hash(self, url: str) -> tuple[str, str] | None:
        """Tenta obter um hash publicado para `url` (ex.: `{url}.sha256`).

        Best-effort: se nenhum sidecar existir (404 em todos), retorna `None` e a
        verificação cai para tamanho apenas — ver docstring do módulo.
        """
        for suffix, algo in _HASH_SIDECARS:
            try:
                response = self._client.get(url + suffix)
            except httpx.TransportError:
                continue
            if response.status_code == httpx.codes.OK and response.text.strip():
                digest_hex = response.text.strip().split()[0]
                return digest_hex, algo
        return None

    @staticmethod
    def _hash_of(path: Path, algo: str) -> str:
        digest = hashlib.new(algo)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_DEFAULT_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()
