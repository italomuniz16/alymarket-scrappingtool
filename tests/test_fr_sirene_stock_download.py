"""Testes do downloader fr_sirene com servidor HTTP mockado (`httpx.MockTransport`) —
simula tanto a API de metadados do dataset (`GET /api/1/datasets/{id}/`) quanto o
download dos arquivos. Inclui "recursos-armadilha" (StockUniteLegaleHistorique,
StockEtablissementHistorique, StockEtablissementLiensSuccession, uma versão parquet)
para provar que a descoberta por título não confunde essas variantes com os dois
arquivos pedidos — exatamente o cenário real encontrado no dataset (ver docstring de
`stock_download.py`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from src.ingestion.fr_sirene.stock_download import (
    DownloadIntegrityError,
    SireneResourceNotFoundError,
    SireneStockDownloader,
)

TEST_DATASET_ID = "fake-dataset-id"
API_PATH = f"/api/1/datasets/{TEST_DATASET_ID}/"

UL_URL = "https://static.fake-datagouv.test/resources/xxx/stock-stockunitelegale-csv.zip"
ETAB_URL = "https://static.fake-datagouv.test/resources/xxx/stock-stocketablissement-csv.zip"
UL_HIST_URL = (
    "https://static.fake-datagouv.test/resources/xxx/stock-stockunitelegalehistorique-csv.zip"
)
ETAB_HIST_URL = (
    "https://static.fake-datagouv.test/resources/xxx/stock-stocketablissementhistorique-csv.zip"
)
ETAB_SUCC_URL = "https://static.fake-datagouv.test/resources/xxx/stock-stocketablissementlienssuccession-csv.zip"
UL_PARQUET_URL = (
    "https://static.fake-datagouv.test/resources/xxx/stock-stockunitelegale-parquet.parquet"
)

DEFAULT_LAST_MODIFIED = "2026-08-01T07:26:56.675000+00:00"


def _resource(
    *,
    title: str,
    url: str,
    content: bytes,
    fmt: str = "zip",
    checksum_algo: str | None = "sha1",
    filesize_override: int | None = None,
    last_modified: str = DEFAULT_LAST_MODIFIED,
) -> dict[str, object]:
    checksum = None
    if checksum_algo is not None:
        checksum = {"type": checksum_algo, "value": hashlib.new(checksum_algo, content).hexdigest()}
    return {
        "title": title,
        "url": url,
        "format": fmt,
        "filesize": filesize_override if filesize_override is not None else len(content),
        "checksum": checksum,
        "last_modified": last_modified,
    }


@dataclass
class FakeDataGouvServer:
    """Handler de `httpx.MockTransport` que simula a API do dataset + download dos
    arquivos. Um único client HTTP "atende" os dois hosts reais (`www.data.gouv.fr`
    pra API, `static.data.gouv.fr` pros arquivos) — o `MockTransport` intercepta por
    request, não por DNS de verdade, então isso não exige nada especial aqui."""

    resources: list[dict[str, object]]
    files: dict[str, bytes]
    ignore_range: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.url.path == API_PATH:
            return httpx.Response(200, json={"resources": self.resources})

        url = str(request.url)
        if url not in self.files:
            return httpx.Response(404)

        content = self.files[url]
        range_header = request.headers.get("range")
        if range_header and not self.ignore_range:
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            partial = content[start:]
            return httpx.Response(
                206,
                content=partial,
                headers={
                    "content-range": f"bytes {start}-{len(content) - 1}/{len(content)}",
                    "content-length": str(len(partial)),
                },
            )
        return httpx.Response(200, content=content)


def _make_downloader(server: FakeDataGouvServer, **kwargs: object) -> SireneStockDownloader:
    kwargs.setdefault("rate_limit_seconds", 0)
    kwargs.setdefault("retry_wait_seconds", 0)
    return SireneStockDownloader(
        dataset_id=TEST_DATASET_ID,
        transport=httpx.MockTransport(server),
        **kwargs,  # type: ignore[arg-type]
    )


def _standard_resources(ul_content: bytes, etab_content: bytes) -> list[dict[str, object]]:
    """Os 2 recursos válidos + as armadilhas que um filtro ingênuo por substring
    (`"StockEtablissement" in title`) casaria por engano."""
    return [
        _resource(
            title="Sirene : Fichier StockUniteLegale - 01 août 2026", url=UL_URL, content=ul_content
        ),
        _resource(
            title="Sirene : Fichier StockEtablissement - 01 août 2026",
            url=ETAB_URL,
            content=etab_content,
        ),
        _resource(
            title="Sirene : Fichier StockUniteLegaleHistorique - 01 août 2026",
            url=UL_HIST_URL,
            content=b"HISTORIQUE-UL",
        ),
        _resource(
            title="Sirene : Fichier StockEtablissementHistorique - 01 août 2026",
            url=ETAB_HIST_URL,
            content=b"HISTORIQUE-ETAB",
        ),
        _resource(
            title="Sirene : Fichier StockEtablissementLiensSuccession - 01 août 2026",
            url=ETAB_SUCC_URL,
            content=b"SUCCESSION",
        ),
        _resource(
            title="Sirene : Fichier StockUniteLegale - 01 août 2026",
            url=UL_PARQUET_URL,
            content=b"PARQUET-NAO-E-ZIP",
            fmt="parquet",
        ),
    ]


# -- Descoberta -----------------------------------------------------------------------


def test_discover_resources_picks_exact_matches_not_historique_or_succession() -> None:
    ul_content, etab_content = b"UL-CONTEUDO", b"ETAB-CONTEUDO"
    resources = _standard_resources(ul_content, etab_content)
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with _make_downloader(server) as downloader:
        found = downloader.discover_resources()

    assert set(found) == {"UNITE_LEGALE", "ETABLISSEMENT"}
    assert found["UNITE_LEGALE"].url == UL_URL
    assert found["ETABLISSEMENT"].url == ETAB_URL
    assert found["UNITE_LEGALE"].checksum_algo == "sha1"
    assert found["UNITE_LEGALE"].checksum_value == hashlib.sha1(ul_content).hexdigest()
    assert found["UNITE_LEGALE"].filesize == len(ul_content)


def test_discover_resources_raises_when_entity_missing() -> None:
    resources = [
        _resource(
            title="Sirene : Fichier StockUniteLegale - 01 août 2026", url=UL_URL, content=b"x"
        )
    ]
    server = FakeDataGouvServer(resources=resources, files={UL_URL: b"x"})

    with _make_downloader(server) as downloader, pytest.raises(SireneResourceNotFoundError):
        downloader.discover_resources()


def test_check_latest_returns_max_last_modified_date() -> None:
    resources = [
        _resource(
            title="Sirene : Fichier StockUniteLegale - x",
            url=UL_URL,
            content=b"a",
            last_modified="2026-08-01T07:26:56+00:00",
        ),
        _resource(
            title="Sirene : Fichier StockEtablissement - x",
            url=ETAB_URL,
            content=b"b",
            last_modified="2026-08-01T07:34:40+00:00",
        ),
    ]
    server = FakeDataGouvServer(resources=resources, files={UL_URL: b"a", ETAB_URL: b"b"})

    with _make_downloader(server) as downloader:
        assert downloader.check_latest() == "2026-08-01"


# -- Download ---------------------------------------------------------------------


def test_download_writes_both_files_into_competencia_dir(tmp_path: Path) -> None:
    ul_content, etab_content = b"UNITE-LEGALE-CONTEUDO", b"ETABLISSEMENT-CONTEUDO"
    resources = _standard_resources(ul_content, etab_content)
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with _make_downloader(server) as downloader:
        paths = downloader.download(tmp_path)

    assert {p.name for p in paths} == {UL_URL.rsplit("/", 1)[-1], ETAB_URL.rsplit("/", 1)[-1]}
    for path in paths:
        assert path.parent.name == "2026-08-01"
        assert path.exists()


def test_download_only_filters_entities(tmp_path: Path) -> None:
    ul_content, etab_content = b"UL", b"ETAB"
    resources = _standard_resources(ul_content, etab_content)
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["unite_legale"])

    assert path.name == UL_URL.rsplit("/", 1)[-1]


def test_download_raises_when_only_filter_matches_nothing(tmp_path: Path) -> None:
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=b"a"),
        _resource(title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=b"b"),
    ]
    server = FakeDataGouvServer(resources=resources, files={UL_URL: b"a", ETAB_URL: b"b"})

    with _make_downloader(server) as downloader, pytest.raises(RuntimeError):
        downloader.download(tmp_path, only=["NAO-EXISTE"])


def test_download_resumes_partial_file_with_range(tmp_path: Path) -> None:
    ul_content, etab_content = b"0123456789ABCDEFGHIJ", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    target_dir = tmp_path / "2026-08-01"
    target_dir.mkdir()
    (target_dir / UL_URL.rsplit("/", 1)[-1]).write_bytes(ul_content[:10])

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert path.read_bytes() == ul_content
    get_requests = [r for r in server.requests if r.method == "GET" and str(r.url) == UL_URL]
    assert len(get_requests) == 1
    assert get_requests[0].headers.get("range") == "bytes=10-"


def test_download_falls_back_when_server_ignores_range(tmp_path: Path) -> None:
    ul_content, etab_content = b"0123456789ABCDEFGHIJ", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}, ignore_range=True
    )

    target_dir = tmp_path / "2026-08-01"
    target_dir.mkdir()
    (target_dir / UL_URL.rsplit("/", 1)[-1]).write_bytes(ul_content[:10])

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["UNITE_LEGALE"])

    # Não deve duplicar bytes (conteúdo parcial + corpo inteiro).
    assert path.read_bytes() == ul_content


def test_download_restarts_when_local_file_larger_than_remote(tmp_path: Path) -> None:
    ul_content, etab_content = b"CONTEUDO-CORRETO-E-COMPLETO", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    target_dir = tmp_path / "2026-08-01"
    target_dir.mkdir()
    (target_dir / UL_URL.rsplit("/", 1)[-1]).write_bytes(ul_content + b"-LIXO-EXTRA-DE-SOBRA")

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert path.read_bytes() == ul_content


def test_download_skips_already_complete_and_valid_file(tmp_path: Path) -> None:
    ul_content, etab_content = b"CONTEUDO-JA-BAIXADO-E-VALIDO", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    target_dir = tmp_path / "2026-08-01"
    target_dir.mkdir()
    (target_dir / UL_URL.rsplit("/", 1)[-1]).write_bytes(ul_content)

    with _make_downloader(server) as downloader:
        downloader.download(tmp_path, only=["UNITE_LEGALE"])

    get_requests = [r for r in server.requests if r.method == "GET" and str(r.url) == UL_URL]
    assert get_requests == []


def test_download_redownloads_when_local_content_corrupted_despite_correct_size(
    tmp_path: Path,
) -> None:
    """Tamanho local bate com o esperado, mas o conteúdo não (checksum inválido) —
    diferente da Receita Federal (sem hash confiável), aqui a API sempre publica um
    checksum, então esse caso é detectável e deve disparar um re-download completo."""
    ul_content = b"CONTEUDO-CORRETO-32-BYTES-AQUI!"
    corrupted = b"X" * len(ul_content)
    etab_content = b"ETAB-IRRELEVANTE"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    target_dir = tmp_path / "2026-08-01"
    target_dir.mkdir()
    (target_dir / UL_URL.rsplit("/", 1)[-1]).write_bytes(corrupted)

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert path.read_bytes() == ul_content
    get_requests = [r for r in server.requests if r.method == "GET" and str(r.url) == UL_URL]
    assert len(get_requests) == 1
    assert get_requests[0].headers.get("range") is None  # recomeçou do zero, sem Range


def test_download_raises_integrity_error_on_size_mismatch(tmp_path: Path) -> None:
    ul_content, etab_content = b"CONTEUDO-CURTO", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(
            title="Sirene : Fichier StockUniteLegale - x",
            url=UL_URL,
            content=ul_content,
            filesize_override=len(ul_content) + 100,  # API "mente" sobre o tamanho
        ),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with (
        _make_downloader(server, max_attempts=2) as downloader,
        pytest.raises(DownloadIntegrityError),
    ):
        downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert not (tmp_path / "2026-08-01" / UL_URL.rsplit("/", 1)[-1]).exists()


def test_download_raises_integrity_error_on_checksum_mismatch(tmp_path: Path) -> None:
    ul_content, etab_content = b"CONTEUDO-COM-CHECKSUM-ERRADO", b"ETAB-IRRELEVANTE"
    bad_ul = _resource(
        title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content
    )
    bad_ul["checksum"] = {"type": "sha1", "value": "0" * 40}  # propositalmente errado
    resources = [
        bad_ul,
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with (
        _make_downloader(server, max_attempts=2) as downloader,
        pytest.raises(DownloadIntegrityError),
    ):
        downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert not (tmp_path / "2026-08-01" / UL_URL.rsplit("/", 1)[-1]).exists()


def test_download_falls_back_to_size_only_when_no_checksum_published(tmp_path: Path) -> None:
    ul_content, etab_content = b"CONTEUDO-SEM-CHECKSUM-PUBLICADO", b"ETAB-IRRELEVANTE"
    resources = [
        _resource(
            title="Sirene : Fichier StockUniteLegale - x",
            url=UL_URL,
            content=ul_content,
            checksum_algo=None,
        ),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, only=["UNITE_LEGALE"])

    assert path.read_bytes() == ul_content


def test_rate_limit_sleeps_between_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ul_content, etab_content = b"a", b"b"
    resources = [
        _resource(title="Sirene : Fichier StockUniteLegale - x", url=UL_URL, content=ul_content),
        _resource(
            title="Sirene : Fichier StockEtablissement - x", url=ETAB_URL, content=etab_content
        ),
    ]
    server = FakeDataGouvServer(
        resources=resources, files={UL_URL: ul_content, ETAB_URL: etab_content}
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "src.ingestion.fr_sirene.stock_download.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    with _make_downloader(server, rate_limit_seconds=1.5) as downloader:
        downloader.download(tmp_path)

    # 2 arquivos -> 1 pausa entre eles (não antes do primeiro, não depois do último).
    assert sleep_calls == [1.5]
