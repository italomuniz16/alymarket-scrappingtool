"""Testes do downloader br_receita com servidor HTTP mockado (`httpx.MockTransport`).

Nenhuma chamada de rede real é feita: um handler em memória simula a listagem de
diretório da Receita Federal (competências + arquivos `.zip`) e o comportamento de
Range/HEAD/hash-sidecar, permitindo testar retomada de download, verificação de
integridade e rate limit sem depender de rede nem de bibliotecas de mock de servidor
adicionais.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from src.ingestion.br_receita.downloader import DownloadIntegrityError, ReceitaCNPJDownloader

TEST_BASE_URL = "https://fake-rfb.test/dados_abertos_cnpj/"
TEST_BASE_PATH = "/dados_abertos_cnpj/"


def _index_html(competencias: list[str]) -> str:
    links = "".join(f'<a href="{c}/">{c}/</a>\n' for c in competencias)
    return f'<html><body><a href="../">../</a>\n{links}</body></html>'


def _folder_html(filenames: list[str]) -> str:
    links = "".join(f'<a href="{f}">{f}</a>\n' for f in filenames)
    return f'<html><body><a href="../">../</a>\n{links}</body></html>'


@dataclass
class FakeRfbServer:
    """Handler de `httpx.MockTransport` que simula a listagem + download da RFB."""

    competencia: str
    files: dict[str, bytes]
    competencias: list[str] | None = None
    hash_sidecars: dict[str, tuple[str, str]] = field(default_factory=dict)
    lie_sizes: dict[str, int] = field(default_factory=dict)
    ignore_range: bool = False
    broken_hash_sidecars: frozenset[str] = frozenset()
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.competencias is None:
            self.competencias = [self.competencia]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == TEST_BASE_PATH:
            assert self.competencias is not None
            return httpx.Response(200, text=_index_html(self.competencias))
        if path == f"{TEST_BASE_PATH}{self.competencia}/":
            return httpx.Response(200, text=_folder_html(sorted(self.files)))

        name = path.rsplit("/", 1)[-1]

        for suffix, algo in ((".sha256", "sha256"), (".md5", "md5")):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                if base in self.broken_hash_sidecars:
                    raise httpx.ConnectError("falha de rede simulada ao buscar sidecar de hash")
                sidecar = self.hash_sidecars.get(base)
                if sidecar is not None and sidecar[1] == algo:
                    return httpx.Response(200, text=sidecar[0])
                return httpx.Response(404)

        if name not in self.files:
            return httpx.Response(404)

        content = self.files[name]
        reported_size = self.lie_sizes.get(name, len(content))

        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(reported_size)})

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


def _make_downloader(server: FakeRfbServer, **kwargs: object) -> ReceitaCNPJDownloader:
    kwargs.setdefault("rate_limit_seconds", 0)
    kwargs.setdefault("retry_wait_seconds", 0)
    return ReceitaCNPJDownloader(
        base_url=TEST_BASE_URL,
        transport=httpx.MockTransport(server),
        **kwargs,  # type: ignore[arg-type]
    )


def test_check_latest_picks_max_competencia() -> None:
    server = FakeRfbServer(
        competencia="2025-05",
        files={},
        competencias=["2024-09", "2025-03", "2025-05"],
    )
    with _make_downloader(server) as downloader:
        assert downloader.check_latest() == "2025-05"


def test_check_latest_raises_when_no_competencia_found() -> None:
    server = FakeRfbServer(competencia="2025-05", files={}, competencias=[])
    with _make_downloader(server) as downloader, pytest.raises(RuntimeError):
        downloader.check_latest()


def test_list_files_returns_zip_names_only() -> None:
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Empresas0.zip": b"a", "Estabelecimentos0.zip": b"b", "Simples.zip": b"c"},
    )
    with _make_downloader(server) as downloader:
        files = downloader.list_files("2025-05")
        assert files == ["Empresas0.zip", "Estabelecimentos0.zip", "Simples.zip"]


def test_download_only_subset_filters_by_name(tmp_path: Path) -> None:
    server = FakeRfbServer(
        competencia="2025-05",
        files={
            "Empresas0.zip": b"EMPRESAS0",
            "Estabelecimentos0.zip": b"ESTAB0",
            "Estabelecimentos1.zip": b"ESTAB1",
            "Socios0.zip": b"SOCIOS0",
            "Simples.zip": b"SIMPLES",
        },
    )
    with _make_downloader(server) as downloader:
        result = downloader.download(tmp_path, competencia="2025-05", only=["ESTABELE", "EMPRE"])

    names = {p.name for p in result}
    assert names == {"Empresas0.zip", "Estabelecimentos0.zip", "Estabelecimentos1.zip"}
    assert all(p.exists() for p in result)


def test_download_writes_correct_bytes_and_verifies_size(tmp_path: Path) -> None:
    content = b"ESTABELECIMENTOS-CONTEUDO-DE-TESTE"
    server = FakeRfbServer(competencia="2025-05", files={"Estabelecimentos0.zip": content})
    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    assert path == tmp_path / "2025-05" / "Estabelecimentos0.zip"
    assert path.read_bytes() == content


def test_download_resumes_partial_file_with_range(tmp_path: Path) -> None:
    content = b"0123456789ABCDEFGHIJ"
    server = FakeRfbServer(competencia="2025-05", files={"Empresas0.zip": content})

    target_dir = tmp_path / "2025-05"
    target_dir.mkdir()
    partial_path = target_dir / "Empresas0.zip"
    partial_path.write_bytes(content[:10])

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    assert path.read_bytes() == content

    get_requests = [
        r for r in server.requests if r.method == "GET" and r.url.path.endswith("Empresas0.zip")
    ]
    assert len(get_requests) == 1
    assert get_requests[0].headers.get("range") == "bytes=10-"


def test_download_falls_back_when_server_ignores_range(tmp_path: Path) -> None:
    content = b"0123456789ABCDEFGHIJ"
    server = FakeRfbServer(
        competencia="2025-05", files={"Empresas0.zip": content}, ignore_range=True
    )

    target_dir = tmp_path / "2025-05"
    target_dir.mkdir()
    (target_dir / "Empresas0.zip").write_bytes(content[:10])

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    # Não deve duplicar bytes (conteúdo parcial + corpo inteiro): o arquivo final
    # precisa bater exatamente com o conteúdo remoto, não ser maior que ele.
    assert path.read_bytes() == content


def test_download_restarts_when_local_file_larger_than_remote(tmp_path: Path) -> None:
    """Arquivo local "parcial" maior que o remoto (ex.: lixo de uma execução anterior
    corrompida) deve ser descartado e baixado do zero, não tratado como resumível."""
    content = b"CONTEUDO-CORRETO-E-COMPLETO"
    server = FakeRfbServer(competencia="2025-05", files={"Empresas0.zip": content})

    target_dir = tmp_path / "2025-05"
    target_dir.mkdir()
    (target_dir / "Empresas0.zip").write_bytes(content + b"-LIXO-EXTRA-DE-SOBRA")

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    assert path.read_bytes() == content


def test_fetch_expected_hash_tolerates_transport_error_on_sidecar(tmp_path: Path) -> None:
    """Uma falha de rede ao buscar o sidecar de hash (não um 404) não deve derrubar o
    download: a verificação cai para tamanho apenas, como se o hash não existisse."""
    content = b"CONTEUDO-SEM-HASH-POR-FALHA-DE-REDE"
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Empresas0.zip": content},
        broken_hash_sidecars=frozenset({"Empresas0.zip"}),
    )

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    assert path.read_bytes() == content


def test_download_skips_already_complete_file(tmp_path: Path) -> None:
    content = b"CONTEUDO-JA-BAIXADO"
    server = FakeRfbServer(competencia="2025-05", files={"Simples.zip": content})

    target_dir = tmp_path / "2025-05"
    target_dir.mkdir()
    (target_dir / "Simples.zip").write_bytes(content)

    with _make_downloader(server) as downloader:
        downloader.download(tmp_path, competencia="2025-05")

    get_requests = [
        r for r in server.requests if r.method == "GET" and r.url.path.endswith("Simples.zip")
    ]
    assert get_requests == []


def test_download_raises_integrity_error_on_size_mismatch(tmp_path: Path) -> None:
    content = b"CONTEUDO-CURTO"
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Empresas0.zip": content},
        lie_sizes={"Empresas0.zip": len(content) + 100},  # HEAD mente sobre o tamanho
    )

    with (
        _make_downloader(server, max_attempts=2) as downloader,
        pytest.raises(DownloadIntegrityError),
    ):
        downloader.download(tmp_path, competencia="2025-05")

    assert not (tmp_path / "2025-05" / "Empresas0.zip").exists()


def test_download_verifies_hash_when_sidecar_available(tmp_path: Path) -> None:
    import hashlib

    content = b"CONTEUDO-COM-HASH-PUBLICADO"
    digest = hashlib.sha256(content).hexdigest()
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Municipios.zip": content},
        hash_sidecars={"Municipios.zip": (digest, "sha256")},
    )

    with _make_downloader(server) as downloader:
        [path] = downloader.download(tmp_path, competencia="2025-05")

    assert path.read_bytes() == content
    sidecar_requests = [r for r in server.requests if r.url.path.endswith(".sha256")]
    assert len(sidecar_requests) == 1


def test_download_raises_on_hash_mismatch(tmp_path: Path) -> None:
    content = b"CONTEUDO-COM-HASH-ERRADO"
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Naturezas.zip": content},
        hash_sidecars={"Naturezas.zip": ("0" * 64, "sha256")},  # hash propositalmente errado
    )

    with (
        _make_downloader(server, max_attempts=2) as downloader,
        pytest.raises(DownloadIntegrityError),
    ):
        downloader.download(tmp_path, competencia="2025-05")


def test_download_raises_when_only_filter_matches_nothing(tmp_path: Path) -> None:
    server = FakeRfbServer(competencia="2025-05", files={"Simples.zip": b"x"})
    with _make_downloader(server) as downloader, pytest.raises(RuntimeError):
        downloader.download(tmp_path, competencia="2025-05", only=["NAO-EXISTE"])


def test_rate_limit_sleeps_between_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeRfbServer(
        competencia="2025-05",
        files={"Empresas0.zip": b"a", "Estabelecimentos0.zip": b"b"},
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "src.ingestion.br_receita.downloader.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    with _make_downloader(server, rate_limit_seconds=1.5) as downloader:
        downloader.download(tmp_path, competencia="2025-05")

    # 2 arquivos -> 1 pausa entre eles (não antes do primeiro, não depois do último).
    assert sleep_calls == [1.5]
