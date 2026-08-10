"""Testes do extractor br_receita. Os ZIPs de teste são construídos em memória
(`zipfile.ZipFile` em `tmp_path`), sem depender de nenhum binário externo checked-in.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.ingestion.br_receita.extractor import ExtractionError, extract_all, extract_zip


def _make_zip(zip_path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return zip_path


def test_extract_zip_writes_all_members(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "Empresas0.zip",
        {"K3241.EMPRECSV": b"11111111;EMPRESA TESTE;2062;49;100,00;01;\n"},
    )
    dest_dir = tmp_path / "staging"

    extracted = extract_zip(zip_path, dest_dir)

    assert len(extracted) == 1
    assert extracted[0] == dest_dir / "K3241.EMPRECSV"
    assert extracted[0].read_bytes() == b"11111111;EMPRESA TESTE;2062;49;100,00;01;\n"


def test_extract_zip_skips_directory_entries(tmp_path: Path) -> None:
    zip_path = tmp_path / "com_pasta.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("uma_pasta/"), b"")
        zf.writestr("uma_pasta/arquivo.txt", b"conteudo")

    extracted = extract_zip(zip_path, tmp_path / "staging")

    assert [p.name for p in extracted] == ["arquivo.txt"]


def test_extract_zip_multiple_members(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "Estabelecimentos.zip",
        {
            "K3241.ESTABELE.0": b"linha 0\n",
            "K3241.ESTABELE.1": b"linha 1\n",
        },
    )
    dest_dir = tmp_path / "staging"

    extracted = extract_zip(zip_path, dest_dir)

    names = {p.name for p in extracted}
    assert names == {"K3241.ESTABELE.0", "K3241.ESTABELE.1"}


def test_extract_zip_creates_dest_dir(tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "a.zip", {"f.txt": b"conteudo"})
    dest_dir = tmp_path / "nao-existe-ainda" / "staging"

    extract_zip(zip_path, dest_dir)

    assert dest_dir.is_dir()


def test_extract_zip_skips_already_extracted_with_matching_size(tmp_path: Path) -> None:
    content = b"conteudo original"
    zip_path = _make_zip(tmp_path / "a.zip", {"f.txt": content})
    dest_dir = tmp_path / "staging"
    dest_dir.mkdir()
    (dest_dir / "f.txt").write_bytes(content)
    sentinel_mtime = (dest_dir / "f.txt").stat().st_mtime

    extracted = extract_zip(zip_path, dest_dir)

    assert extracted == [dest_dir / "f.txt"]
    # Não deve ter sido regravado (mesmo conteúdo/tamanho -> pulado).
    assert (dest_dir / "f.txt").stat().st_mtime == sentinel_mtime


def test_extract_zip_re_extracts_when_size_differs(tmp_path: Path) -> None:
    content = b"conteudo novo e maior que o anterior"
    zip_path = _make_zip(tmp_path / "a.zip", {"f.txt": content})
    dest_dir = tmp_path / "staging"
    dest_dir.mkdir()
    (dest_dir / "f.txt").write_bytes(b"velho")

    extract_zip(zip_path, dest_dir)

    assert (dest_dir / "f.txt").read_bytes() == content


def test_extract_zip_raises_on_corrupted_zip(tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "a.zip", {"f.txt": b"conteudo"})

    # Corrompe o CRC de um membro, sobrescrevendo alguns bytes no meio do arquivo,
    # sem mexer no cabeçalho/central directory (para o ZipFile ainda conseguir abrir
    # e listar, mas `testzip()` detectar o CRC inválido).
    raw = bytearray(zip_path.read_bytes())
    marker = raw.find(b"conteudo")
    assert marker != -1
    raw[marker : marker + 8] = b"XXXXXXXX"
    zip_path.write_bytes(bytes(raw))

    with pytest.raises(ExtractionError):
        extract_zip(zip_path, tmp_path / "staging")


def test_extract_zip_raises_on_zip_slip(tmp_path: Path) -> None:
    zip_path = tmp_path / "malicioso.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("../../fora_do_staging.txt")
        zf.writestr(info, b"conteudo malicioso")

    with pytest.raises(ExtractionError):
        extract_zip(zip_path, tmp_path / "staging")


def test_extract_all_processes_multiple_zips_in_order(tmp_path: Path) -> None:
    zip1 = _make_zip(tmp_path / "Empresas0.zip", {"K3241.EMPRECSV": b"a"})
    zip2 = _make_zip(tmp_path / "Estabelecimentos0.zip", {"K3241.ESTABELE": b"b"})
    dest_dir = tmp_path / "staging"

    extracted = extract_all([zip1, zip2], dest_dir)

    assert [p.name for p in extracted] == ["K3241.EMPRECSV", "K3241.ESTABELE"]
