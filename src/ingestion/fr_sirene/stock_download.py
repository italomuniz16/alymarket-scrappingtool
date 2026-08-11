"""Download (retomável, com verificação de integridade) dos arquivos de stock SIRENE
— dataset "Base Sirene des entreprises et de leurs établissements (SIREN, SIRET)" —
via a API pública do data.gouv.fr.

## Por que descobrir a URL via API, em vez de assumir um host fixo

O PRD (docs/PRD.md §3.2) alertava que "alguns arquivos migraram (fev/2026) de
`files.data.gouv.fr` para `object.files.data.gouv.fr`". Pesquisa empírica feita nesta
tarefa — contra a API real do dataset, não documentação — mostrou que esse próprio
alerta já está desatualizado: o host atual (ago/2026) é `static.data.gouv.fr`, um
TERCEIRO valor que nenhuma das duas hipóteses do PRD previu. Além disso, a URL de cada
recurso embute um timestamp de publicação que muda a cada atualização mensal do
dataset (ex.: `.../20260801-072607/stock-stockunitelegale-csv.zip`). Ou seja: qualquer
host/URL fixado no código ficaria obsoleto na próxima publicação — a única forma
robusta é consultar `GET https://www.data.gouv.fr/api/1/datasets/{dataset_id}/` em
tempo de execução e usar a URL que ela devolver (campo `resources[].url`).

## Vantagem sobre o downloader BR: checksum garantido

Diferente da Receita Federal (não publica hash — `br_receita/downloader.py` tenta um
sidecar `.sha256`/`.md5` best-effort, que pode nem existir), cada recurso do
data.gouv.fr publica um `checksum` estruturado (`{"type": "sha1", "value": "..."}`) —
verificado SEMPRE aqui, não só quando disponível.

## Escopo desta tarefa

Só os dois arquivos de stock ATUAIS pedidos: unidade legal (`StockUniteLegale`, chave
SIREN) e estabelecimento (`StockEtablissement`, chave SIRET). Os arquivos correlatos
do mesmo dataset (`StockUniteLegaleHistorique`, `StockEtablissementHistorique`,
`StockEtablissementLiensSuccession`, `StockDoublons`) ficam fora de escopo — por isso
o filtro de título usa fronteira de palavra (`\\bStockEtablissement\\b`, não uma
substring solta): confirmado contra a API real que "StockEtablissementHistorique" e
"StockEtablissementLiensSuccession" têm "StockEtablissement" como PREFIXO do próprio
título, então um `in` simples casaria com os três por engano.

Também confirmado contra a API real: ao contrário da Receita Federal (várias pastas
mensais em `arquivos.receitafederal.gov.br`, das quais se escolhe a mais recente), o
dataset SIRENE só tem UM recurso zip vigente por entidade — não há "mais recente entre
vários", só "o que está publicado agora" (o histórico de publicações anteriores não
fica disponível como recurso à parte).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_DATASET_ID = "5b7ffc618b4c4169d30727e0"
DATASET_API_URL_TEMPLATE = "https://www.data.gouv.fr/api/1/datasets/{dataset_id}/"
DEFAULT_USER_AGENT = (
    "alymarket-bot/0.1 (uso academico/prospeccao B2B; contato configuravel via HTTP_USER_AGENT)"
)

# `\b` evita casar "StockEtablissement" dentro de "StockEtablissementHistorique"/
# "...LiensSuccession" (ver docstring do módulo) — confirmado contra os títulos reais
# do dataset.
_RESOURCE_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("UNITE_LEGALE", re.compile(r"\bStockUniteLegale\b")),
    ("ETABLISSEMENT", re.compile(r"\bStockEtablissement\b")),
)

_DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class DownloadIntegrityError(RuntimeError):
    """Levantado quando um arquivo baixado não bate com o tamanho ou checksum esperado."""


class SireneResourceNotFoundError(RuntimeError):
    """Levantado quando o dataset não tem (mais/ainda) um recurso zip pra uma entidade
    esperada — ex.: INSEE renomeou o título do recurso."""


@dataclass(frozen=True)
class SireneResource:
    """Um recurso do dataset SIRENE resolvido via API (URL/checksum/tamanho atuais —
    nunca fixados no código, ver docstring do módulo)."""

    entity: str
    title: str
    url: str
    filesize: int | None
    checksum_algo: str | None
    checksum_value: str | None
    last_modified: str | None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def _competencia_from_resources(resources: dict[str, SireneResource]) -> str:
    """`AAAA-MM-DD` a partir do maior `last_modified` entre os recursos — usado como
    identificador de "competência" (nome da subpasta de destino), já que o dataset só
    tem o recurso vigente por entidade (ver docstring do módulo)."""
    timestamps = [r.last_modified for r in resources.values() if r.last_modified]
    if not timestamps:
        raise RuntimeError(
            "Nenhum recurso com last_modified encontrado para determinar a competência."
        )
    return max(timestamps)[:10]


class SireneStockDownloader:
    """Baixa os arquivos de stock SIRENE (unidade legal e estabelecimento) descobrindo
    a URL vigente de cada um via a API do data.gouv.fr.

    Uso típico::

        with SireneStockDownloader() as downloader:
            arquivos = downloader.download(Path("data/raw"))

    Para testes, injete `transport=httpx.MockTransport(handler)` — nenhuma chamada de
    rede real é feita nesse caso.
    """

    def __init__(
        self,
        *,
        dataset_id: str = DEFAULT_DATASET_ID,
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
            dataset_id: id do dataset no data.gouv.fr ("Base Sirene des entreprises
                et de leurs établissements (SIREN, SIRET)").
            user_agent: identificação enviada em todo request (boa prática para
                acesso educado a fontes públicas — ver CLAUDE.md).
            timeout_seconds: timeout HTTP por request.
            rate_limit_seconds: pausa entre o download de cada arquivo; `0` desativa
                (usado nos testes).
            chunk_size: tamanho do chunk de leitura/gravação em streaming.
            max_attempts: tentativas por arquivo antes de desistir (tenacity).
            retry_wait_seconds: base do backoff exponencial entre tentativas; `0`
                desativa a espera (usado nos testes).
            transport: transporte httpx alternativo (ex.: `httpx.MockTransport` em
                testes). `None` usa a rede real.
        """
        self.dataset_id = dataset_id
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

    def __enter__(self) -> SireneStockDownloader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Descoberta ---------------------------------------------------------------

    def discover_resources(self) -> dict[str, SireneResource]:
        """Consulta a API do dataset e resolve o recurso zip vigente de cada entidade
        (`"UNITE_LEGALE"`, `"ETABLISSEMENT"`).

        Raises:
            SireneResourceNotFoundError: se alguma entidade esperada não tiver
                exatamente um recurso `format == "zip"` cujo título bata com o padrão
                (ver `_RESOURCE_TITLE_PATTERNS`).
        """
        url = DATASET_API_URL_TEMPLATE.format(dataset_id=self.dataset_id)
        data = self._retrying(self._get_json_once, url)
        resources = data.get("resources") or []

        found: dict[str, SireneResource] = {}
        for entity, pattern in _RESOURCE_TITLE_PATTERNS:
            matches = [
                r
                for r in resources
                if r.get("format") == "zip" and pattern.search(r.get("title") or "")
            ]
            if not matches:
                raise SireneResourceNotFoundError(
                    f"Nenhum recurso zip encontrado para {entity!r} no dataset {self.dataset_id!r}"
                )
            if len(matches) > 1:
                logger.warning(
                    "Mais de um recurso zip bate com %s; usando o primeiro: %s",
                    entity,
                    [m.get("title") for m in matches],
                )
            raw = matches[0]
            checksum = raw.get("checksum") or {}
            found[entity] = SireneResource(
                entity=entity,
                title=raw.get("title") or "",
                url=raw["url"],
                filesize=raw.get("filesize"),
                checksum_algo=checksum.get("type"),
                checksum_value=checksum.get("value"),
                last_modified=raw.get("last_modified"),
            )
        return found

    def check_latest(self) -> str:
        """Retorna a data de publicação vigente (`AAAA-MM-DD`) — usada como
        "competência" (nome da subpasta de destino em `download`)."""
        resources = self.discover_resources()
        latest = _competencia_from_resources(resources)
        logger.info("Competência (publicação) vigente: %s", latest)
        return latest

    # -- Download -------------------------------------------------------------------

    def download(self, dest: Path, *, only: list[str] | None = None) -> list[Path]:
        """Baixa os arquivos de stock vigentes para `dest/{competencia}/`.

        Args:
            dest: diretório raiz (ex.: `data/raw`); os arquivos vão para
                `dest/{competencia}/arquivo.zip`.
            only: se dado, baixa só as entidades cujo identificador (`"UNITE_LEGALE"`/
                `"ETABLISSEMENT"`) contém (case-insensitive) algum destes termos.
                Útil para testes e para não precisar baixar os dois arquivos.

        Returns:
            Caminhos dos arquivos baixados (completos e com integridade verificada).
        """
        resources = self.discover_resources()
        if only:
            terms = [t.upper() for t in only]
            resources = {k: v for k, v in resources.items() if any(t in k for t in terms)}
        if not resources:
            raise RuntimeError(f"Nenhum recurso a baixar (only={only!r})")

        competencia = _competencia_from_resources(resources)
        target_dir = dest / competencia
        target_dir.mkdir(parents=True, exist_ok=True)

        downloaded: list[Path] = []
        for i, resource in enumerate(resources.values()):
            if i > 0 and self.rate_limit_seconds > 0:
                time.sleep(self.rate_limit_seconds)

            dest_file = target_dir / resource.filename
            self._retrying(self._download_one_attempt, resource, dest_file)
            downloaded.append(dest_file)

        return downloaded

    # -- Internals --------------------------------------------------------------

    def _get_json_once(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def _download_one_attempt(self, resource: SireneResource, dest_file: Path) -> None:
        """Uma tentativa de baixar (ou retomar) um arquivo. Chamado via `Retrying`."""
        expected_size = resource.filesize
        resume_from = dest_file.stat().st_size if dest_file.exists() else 0

        if expected_size is not None and resume_from == expected_size:
            if self._verify_integrity(dest_file, resource, raise_on_mismatch=False):
                logger.info("Já completo e íntegro, pulando download: %s", dest_file.name)
                return
            logger.warning(
                "Arquivo completo mas com checksum inválido; refazendo: %s", dest_file.name
            )
            resume_from = 0
            dest_file.unlink(missing_ok=True)
        elif expected_size is not None and resume_from > expected_size:
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

        with self._client.stream("GET", resource.url, headers=headers) as response:
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

        self._verify_integrity(dest_file, resource, raise_on_mismatch=True)

    def _verify_integrity(
        self, dest_file: Path, resource: SireneResource, *, raise_on_mismatch: bool
    ) -> bool:
        """Confere tamanho e (sempre que publicado — o que o data.gouv.fr faz de
        forma confiável, ao contrário da Receita Federal) o checksum do arquivo.

        `raise_on_mismatch=False` é usado no atalho de "já baixado" (pra decidir se
        pula ou refaz sem derrubar a tentativa inteira); `True` no caminho normal
        pós-download (erro de verdade se um download recém-feito não bater).
        """
        actual_size = dest_file.stat().st_size
        if resource.filesize is not None and actual_size != resource.filesize:
            if raise_on_mismatch:
                dest_file.unlink(missing_ok=True)
                raise DownloadIntegrityError(
                    f"Tamanho inconsistente para {dest_file.name}: "
                    f"esperado {resource.filesize}, obtido {actual_size}"
                )
            return False

        if resource.checksum_algo and resource.checksum_value:
            actual_hex = self._hash_of(dest_file, resource.checksum_algo)
            if actual_hex.lower() != resource.checksum_value.lower():
                if raise_on_mismatch:
                    dest_file.unlink(missing_ok=True)
                    raise DownloadIntegrityError(
                        f"Checksum ({resource.checksum_algo}) inconsistente para {dest_file.name}"
                    )
                return False
            logger.info("Checksum (%s) verificado para %s", resource.checksum_algo, dest_file.name)
        else:
            logger.debug(
                "Nenhum checksum publicado para %s; validado só por tamanho", dest_file.name
            )

        return True

    @staticmethod
    def _hash_of(path: Path, algo: str) -> str:
        digest = hashlib.new(algo)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_DEFAULT_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()
