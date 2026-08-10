"""Exportação de listas de leads para CSV/Excel — sempre passando pelo portão de
supressão (`segmentation/suppression.py`) e registrando a operação no `audit_log`
antes de escrever qualquer arquivo de saída (ver CLAUDE.md: "registro de tratamento",
"trilha de conformidade").

## Nunca exporta sem passar pela supressão

`export_csv`/`export_xlsx` chamam `apply_suppression_gate` internamente, sempre —
não existe parâmetro para pular essa etapa nem para escrever direto a partir de
`leads` sem gate. Isso cobre, de novo, as exclusões hard (`is_synthetic`,
`flag_difusao_restrita`), a deduplicação e a lista de opt-out — ver
`segmentation/suppression.py` para os detalhes de cada regra.

## Ordem das operações

Suprimir -> registrar no audit_log -> só então escrever o arquivo. Registrar antes de
escrever significa que a tentativa de exportação fica auditada mesmo se a escrita do
arquivo falhar logo em seguida — não existe um caminho onde um arquivo aparece em
disco sem uma entrada correspondente no log.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from src.compliance.audit_log import DEFAULT_AUDIT_LOG_PATH, new_event, record_event
from src.segmentation.suppression import SuppressionList, SuppressionReport, apply_suppression_gate

logger = logging.getLogger(__name__)

# Schema canônico (CanonicalLead) sem is_synthetic/flag_difusao_restrita: por
# definição, todo lead que sobrevive ao portão de supressão tem os dois em False --
# não agregam informação numa lista de outreach, só ruído.
EXPORT_COLUMNS: tuple[str, ...] = (
    "pais",
    "id_legal",
    "id_estab",
    "razao_social",
    "nome_fantasia",
    "cod_atividade",
    "situacao",
    "regiao",
    "municipio",
    "cep",
    "telefone",
    "email",
    "data_inicio_atividade",
    "porte",
    "capital_social",
    "natureza_juridica",
    "score_icp",
    "fonte",
    "enriquecido_em",
)


class ExportError(RuntimeError):
    """Levantado para um uso inválido da exportação (ex.: nenhuma coluna a exportar)."""


@dataclass(frozen=True)
class ExportResult:
    """Resultado de uma exportação: caminho gerado, contagem final de linhas, e o
    relatório de supressão (quantos leads foram removidos e por quê antes de chegar
    no arquivo)."""

    path: Path
    n_exported: int
    suppression_report: SuppressionReport


def _write_csv(rows: list[dict[str, Any]], dest: Path, columns: Sequence[str]) -> None:
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) for col in columns})


def _write_xlsx(rows: list[dict[str, Any]], dest: Path, columns: Sequence[str]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "leads"
    sheet.append(list(columns))
    for row in rows:
        sheet.append([row.get(col) for col in columns])
    workbook.save(dest)


_Writer = Callable[[list[dict[str, Any]], Path, Sequence[str]], None]


def _export(
    leads: Iterable[Mapping[str, Any]],
    dest: Path | str,
    *,
    suppression: SuppressionList,
    operacao: str,
    writer: _Writer,
    filtros: Mapping[str, Any] | None,
    usuario: str | None,
    audit_log_path: Path | str,
    columns: Sequence[str],
) -> ExportResult:
    if not columns:
        raise ExportError("Nenhuma coluna informada para exportação.")

    final_leads, report = apply_suppression_gate(leads, suppression)

    dest = Path(dest)
    event = new_event(
        operacao,
        usuario=usuario,
        filtros=dict(filtros) if filtros else {},
        n_registros=len(final_leads),
        destino=dest,
    )
    record_event(event, audit_log_path)

    dest.parent.mkdir(parents=True, exist_ok=True)
    writer(final_leads, dest, columns)

    logger.info("%s: %d lead(s) exportado(s) para %s", operacao, len(final_leads), dest)
    return ExportResult(path=dest, n_exported=len(final_leads), suppression_report=report)


def export_csv(
    leads: Iterable[Mapping[str, Any]],
    dest: Path | str,
    *,
    suppression: SuppressionList,
    filtros: Mapping[str, Any] | None = None,
    usuario: str | None = None,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    columns: Sequence[str] = EXPORT_COLUMNS,
) -> ExportResult:
    """Exporta `leads` para CSV em `dest`.

    Args:
        leads: leads candidatos (ex.: resultado de `segmentation/filters.py`) —
            ainda NÃO filtrados pela supressão; isso acontece aqui dentro.
        dest: caminho do arquivo `.csv` de saída.
        suppression: lista de opt-out já carregada (ver
            `segmentation.suppression.load_suppression_list`).
        filtros: critérios ICP usados para gerar `leads`, só para fins de auditoria
            (registrado como estão no audit_log — passe algo serializável, ex.:
            `dataclasses.asdict(criteria)`).
        usuario: quem disparou a exportação; default: usuário do SO.
        audit_log_path: onde persistir o evento de auditoria.
        columns: colunas e ordem no CSV; default: `EXPORT_COLUMNS`.

    Returns:
        `ExportResult` com o caminho gerado, contagem final e relatório de supressão.
    """
    return _export(
        leads,
        dest,
        suppression=suppression,
        operacao="export_csv",
        writer=_write_csv,
        filtros=filtros,
        usuario=usuario,
        audit_log_path=audit_log_path,
        columns=columns,
    )


def export_xlsx(
    leads: Iterable[Mapping[str, Any]],
    dest: Path | str,
    *,
    suppression: SuppressionList,
    filtros: Mapping[str, Any] | None = None,
    usuario: str | None = None,
    audit_log_path: Path | str = DEFAULT_AUDIT_LOG_PATH,
    columns: Sequence[str] = EXPORT_COLUMNS,
) -> ExportResult:
    """Exporta `leads` para Excel (`.xlsx`) em `dest`. Mesmo contrato de `export_csv`
    (portão de supressão + registro em audit_log sempre, antes de escrever)."""
    return _export(
        leads,
        dest,
        suppression=suppression,
        operacao="export_xlsx",
        writer=_write_xlsx,
        filtros=filtros,
        usuario=usuario,
        audit_log_path=audit_log_path,
        columns=columns,
    )
