"""Descompactação dos ZIPs de stock da base CNPJ (baixados por `downloader.py`) para
`data/staging/`, com validação de integridade e proteção contra zip-slip.

Este módulo cobre só a extração — o parsing dos CSVs extraídos é `parser.py`.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    """Levantado quando um ZIP está corrompido ou contém um caminho inseguro (zip-slip)."""


def _is_within_directory(directory: Path, target: Path) -> bool:
    """Confere que `target` fica dentro de `directory` após resolver `..`/links."""
    try:
        target.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Descompacta um único ZIP em `dest_dir`, validando integridade e caminhos.

    Validações:
    - `ZipFile.testzip()` confere o CRC de todos os membros antes de extrair
      qualquer coisa — um ZIP truncado/corrompido levanta `ExtractionError` cedo,
      sem deixar arquivos parciais em `dest_dir`.
    - Cada caminho de destino é conferido para não escapar de `dest_dir` (zip-slip:
      um membro malicioso/corrompido com `../../...` no nome).
    - Se o arquivo já existe em `dest_dir` com o mesmo tamanho do membro no ZIP,
      a extração desse membro é pulada (idempotente — reexecutar não regrava tudo).

    Args:
        zip_path: caminho do ZIP baixado (ver `ReceitaCNPJDownloader.download`).
        dest_dir: diretório de destino (ex.: `data/staging/{competencia}/`); criado
            se não existir.

    Returns:
        Caminhos dos arquivos extraídos (ou já presentes e íntegros), na ordem do ZIP.

    Raises:
        ExtractionError: ZIP corrompido ou com caminho inseguro.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    with zipfile.ZipFile(zip_path) as zf:
        bad_file = zf.testzip()
        if bad_file is not None:
            raise ExtractionError(f"ZIP corrompido: {zip_path.name} (membro inválido: {bad_file})")

        for info in zf.infolist():
            if info.is_dir():
                continue

            member_dest = dest_dir / info.filename
            if not _is_within_directory(dest_dir, member_dest):
                raise ExtractionError(
                    f"Caminho inseguro em {zip_path.name}: "
                    f"{info.filename!r} escaparia de {dest_dir}"
                )

            if member_dest.exists() and member_dest.stat().st_size == info.file_size:
                logger.info("Já extraído, pulando: %s", member_dest.name)
                extracted.append(member_dest)
                continue

            logger.info("Extraindo %s -> %s", info.filename, member_dest)
            member_dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(member_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(member_dest)

    return extracted


def extract_all(zip_paths: list[Path], dest_dir: Path) -> list[Path]:
    """Descompacta múltiplos ZIPs (ex.: o retorno de `ReceitaCNPJDownloader.download`)
    para `dest_dir`, retornando todos os arquivos extraídos, na ordem dos ZIPs.
    """
    extracted: list[Path] = []
    for zip_path in zip_paths:
        extracted.extend(extract_zip(zip_path, dest_dir))
    return extracted
