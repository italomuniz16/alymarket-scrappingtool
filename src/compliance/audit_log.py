"""Registro de tratamento / log de auditoria (LGPD Art. "registro de operações" /
RGPD "registre des traitements"): quem fez o quê, quando, com quais filtros, e
quantos registros — persistido em Parquet (ver `AUDIT_LOG_PATH` em `.env.example`).

## Toda operação sensível registra um evento aqui

- **Exportação** (`export/exporters.py`): todo `export_csv`/`export_xlsx` registra
  antes de escrever o arquivo de saída — sem parâmetro pra pular essa etapa.
- **Enriquecimento** (`enrichment/client.py::enrich_leads`, o único ponto de entrada
  usado por `enrichment/providers.py`): registra depois de tentar cada lote,
  quantos identificadores foram de fato encontrados.
- **Geração de lista** (`cli.py::run_query_command`, comando `query`): registra os
  filtros ICP usados e a contagem total de leads que bateram.

Nenhuma dessas três chama isto condicionalmente nem tem flag pra desativar — é o que
atende ao CLAUDE.md ("registro de tratamento", "auditoria — log de quem fez o quê e
quando").

## Consulta e exportação do log

`query_audit_log` filtra o log persistido (por operação/usuário/período);
`export_audit_log` escreve o resultado (filtrado ou não) em CSV, para revisão por um
responsável de compliance fora deste sistema. Não confundir com
`export.exporters.export_csv` — aquilo exporta LEADS (com portão de supressão);
isto exporta o LOG DE AUDITORIA em si (é a trilha, não passa por supressão nenhuma).
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


def query_audit_log(
    log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    *,
    operacao: str | None = None,
    usuario: str | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
) -> pl.DataFrame:
    """Consulta o log de auditoria com filtros opcionais, combinados por AND. Sem
    nenhum filtro, equivale a `read_audit_log` (só ordenado).

    Args:
        log_path: onde o log está persistido.
        operacao: só eventos com esta operação exata (ex.: `"export_csv"`,
            `"enrich_leads"`, `"query"`).
        usuario: só eventos deste usuário/ator (comparação exata).
        desde: só eventos com `quando >= desde` (inclusive). Precisa ser
            timezone-aware (UTC) — mesma convenção de `AuditEvent.quando`
            (sempre `datetime.now(UTC)`); um `datetime` ingênuo levanta erro do
            próprio Polars ao comparar com a coluna, de propósito (evita comparação
            silenciosamente errada entre fuso horários).
        ate: só eventos com `quando <= ate` (inclusive); mesma exigência de `desde`.

    Returns:
        DataFrame filtrado, ordenado por `quando` decrescente (mais recente primeiro).
    """
    df = read_audit_log(log_path)
    if operacao is not None:
        df = df.filter(pl.col("operacao") == operacao)
    if usuario is not None:
        df = df.filter(pl.col("usuario") == usuario)
    if desde is not None:
        df = df.filter(pl.col("quando") >= desde)
    if ate is not None:
        df = df.filter(pl.col("quando") <= ate)
    return df.sort("quando", descending=True)


def export_audit_log(
    dest: Path | str,
    *,
    log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    operacao: str | None = None,
    usuario: str | None = None,
    desde: datetime | None = None,
    ate: datetime | None = None,
) -> Path:
    """Exporta o log de auditoria (ou um subconjunto filtrado — mesmos filtros de
    `query_audit_log`) para CSV em `dest`, para revisão por um responsável de
    compliance fora deste sistema (ex.: auditoria externa, atendimento a pedido de
    titular sob LGPD/RGPD).

    Diferente de `export.exporters.export_csv`/`export_xlsx` (que exportam LEADS,
    sempre pelo portão de supressão): isto exporta o LOG DE AUDITORIA em si — a
    trilha, não um dado sujeito a supressão.

    Returns:
        `dest`, já normalizado para `Path` (conveniência para o chamador).
    """
    df = query_audit_log(log_path, operacao=operacao, usuario=usuario, desde=desde, ate=ate)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(dest)

    logger.info("Log de auditoria exportado (%d evento(s)) para %s", df.height, dest)
    return dest
