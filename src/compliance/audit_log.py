"""Registro de tratamento / log de auditoria (LGPD Art. "registro de operações" /
RGPD "registre des traitements"): quem fez o quê, quando, com quais filtros, e
quantos registros — persistido em Parquet (ver `AUDIT_LOG_PATH` em `.env.example`).

Toda exportação (ver `export/exporters.py`) registra um evento aqui antes de
escrever o arquivo de saída — é o que atende ao CLAUDE.md ("registro de tratamento",
"auditoria — log de quem exportou o quê e quando").
"""

from __future__ import annotations

import getpass
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_LOG_PATH = Path("./data/warehouse/audit_log.parquet")

_SCHEMA: dict[str, Any] = {
    "operacao": pl.Utf8,
    "usuario": pl.Utf8,
    "quando": pl.Datetime(time_unit="us", time_zone="UTC"),
    "filtros": pl.Utf8,
    "n_registros": pl.Int64,
    "destino": pl.Utf8,
}


@dataclass(frozen=True)
class AuditEvent:
    """Um evento de auditoria: quem fez o quê, quando, com quais filtros, e quantos
    registros resultaram.

    `filtros` aceita qualquer dict serializável (valores não nativos de JSON, como
    `Decimal`/`date`, caem para `str()` — ver `record_event`); é gravado como texto
    JSON, não como colunas separadas, porque o formato varia por operação/critério.
    """

    operacao: str
    usuario: str
    quando: datetime
    filtros: dict[str, Any] = field(default_factory=dict)
    n_registros: int = 0
    destino: str | None = None


def new_event(
    operacao: str,
    *,
    usuario: str | None = None,
    filtros: dict[str, Any] | None = None,
    n_registros: int = 0,
    destino: str | Path | None = None,
) -> AuditEvent:
    """Monta um `AuditEvent` com os defaults usuais: `usuario` cai para o usuário do
    SO (`getpass.getuser()`) se não informado; `quando` é sempre "agora" em UTC."""
    return AuditEvent(
        operacao=operacao,
        usuario=usuario or getpass.getuser(),
        quando=datetime.now(UTC),
        filtros=dict(filtros) if filtros else {},
        n_registros=n_registros,
        destino=str(destino) if destino is not None else None,
    )


def record_event(event: AuditEvent, log_path: Path | str = DEFAULT_AUDIT_LOG_PATH) -> None:
    """Anexa `event` ao log de auditoria persistido em `log_path` (Parquet).

    Implementado como leitura + append + reescrita: o volume de eventos (uma
    exportação de cada vez, disparada por humano) não justifica um formato de append
    verdadeiro — simplicidade importa mais que performance aqui.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    row = {
        "operacao": event.operacao,
        "usuario": event.usuario,
        "quando": event.quando,
        "filtros": json.dumps(event.filtros, default=str, sort_keys=True, ensure_ascii=False),
        "n_registros": event.n_registros,
        "destino": event.destino,
    }
    new_row = pl.DataFrame([row], schema=_SCHEMA)

    if log_path.is_file():
        existing = pl.read_parquet(log_path)
        combined = pl.concat([existing, new_row], how="diagonal_relaxed")
    else:
        combined = new_row

    combined.write_parquet(log_path)
    logger.info(
        "Evento de auditoria registrado: operacao=%s usuario=%s n_registros=%d -> %s",
        event.operacao,
        event.usuario,
        event.n_registros,
        log_path,
    )


def read_audit_log(log_path: Path | str = DEFAULT_AUDIT_LOG_PATH) -> pl.DataFrame:
    """Lê o log de auditoria completo. DataFrame vazio (mas com o schema certo) se o
    arquivo ainda não existir — nenhuma operação foi registrada ainda."""
    log_path = Path(log_path)
    if not log_path.is_file():
        return pl.DataFrame(schema=_SCHEMA)
    return pl.read_parquet(log_path)
