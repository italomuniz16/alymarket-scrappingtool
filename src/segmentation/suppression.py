"""Deduplicação, supressão (opt-out) e exclusões hard — o último portão antes de
qualquer exportação/outreach (ver CLAUDE.md: "supressão consultada em toda
exportação").

`apply_suppression_gate` é o ponto de entrada: roda as quatro etapas nesta ordem,
sempre:

1. **Exclusão hard** (`is_hard_excluded`): `is_synthetic=true` ou
   `flag_difusao_restrita=true` — sem exceção, sem flag para desativar. Roda
   primeiro de propósito: nenhuma decisão de dedup/supressão depende de qual
   duplicata "ganhou", então essas duas categorias nunca sobrevivem por acidente.
2. **Deduplicação por `id_estab`**: mesma empresa não aparece duas vezes.
3. **Deduplicação por domínio de e-mail**: leads sem e-mail (ou e-mail sem `@`) não
   participam dessa regra — só duplicam quem de fato compartilha o mesmo domínio.
4. **Supressão (opt-out)**: contra a lista persistida (`load_suppression_list`),
   por `id_estab` ou por e-mail.

Cada etapa devolve uma contagem no `SuppressionReport` final, para auditoria (CLAUDE.md
pede registro de tratamento em toda exportação).
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SuppressionList:
    """Lista de opt-out carregada: conjuntos de `id_estab` e e-mails suprimidos."""

    ids_estab: frozenset[str] = frozenset()
    emails: frozenset[str] = frozenset()


def load_suppression_list(path: Path) -> SuppressionList:
    """Carrega a lista de supressão persistida: CSV com colunas `id_estab,email`
    (qualquer uma das duas pode ficar em branco por linha; uma linha pode suprimir só
    por `id_estab`, só por e-mail, ou ambos).

    Um arquivo ausente é tratado como lista vazia (nada suprimido) — é o estado
    inicial normal antes de qualquer opt-out ter sido registrado, não um erro.
    """
    if not path.is_file():
        logger.info("Lista de supressão não encontrada em %s; tratando como vazia.", path)
        return SuppressionList()

    ids_estab: set[str] = set()
    emails: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            id_estab = (row.get("id_estab") or "").strip()
            email = (row.get("email") or "").strip()
            if id_estab:
                ids_estab.add(id_estab)
            if email:
                emails.add(email.lower())

    logger.info(
        "Lista de supressão carregada de %s: %d id_estab, %d e-mail(s).",
        path,
        len(ids_estab),
        len(emails),
    )
    return SuppressionList(ids_estab=frozenset(ids_estab), emails=frozenset(emails))


def is_suppressed(lead: Mapping[str, Any], suppression: SuppressionList) -> bool:
    """`True` se `lead` está na lista de supressão, por `id_estab` ou por e-mail
    (comparação de e-mail case-insensitive)."""
    id_estab = lead.get("id_estab")
    if id_estab is not None and str(id_estab) in suppression.ids_estab:
        return True

    email = lead.get("email")
    return bool(email and str(email).lower() in suppression.emails)


def is_hard_excluded(lead: Mapping[str, Any]) -> bool:
    """`True` se o lead deve ser excluído incondicionalmente: dado sintético
    (`is_synthetic`) ou registro de difusão restrita francês (`flag_difusao_restrita`)
    — nunca podem alcançar exportação/outreach (ver CLAUDE.md)."""
    return bool(lead.get("is_synthetic")) or bool(lead.get("flag_difusao_restrita"))


def email_domain(email: str | None) -> str | None:
    """Domínio de um e-mail (`"a@empresa.com.br"` -> `"empresa.com.br"`), em
    minúsculo. `None` para e-mail ausente, vazio ou sem `@`."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def dedupe_by_id_estab(leads: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    """Mantém só a primeira ocorrência de cada `id_estab` (ordem de entrada). Leads
    sem `id_estab` sempre passam — ausência de identificador não é tratada como
    "todos duplicados entre si"."""
    seen: set[str] = set()
    for lead in leads:
        id_estab = lead.get("id_estab")
        key = str(id_estab) if id_estab is not None else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        yield dict(lead)


def dedupe_by_email_domain(leads: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    """Mantém só a primeira ocorrência de cada domínio de e-mail (ordem de entrada).
    Leads sem e-mail (ou e-mail sem domínio válido) sempre passam."""
    seen: set[str] = set()
    for lead in leads:
        domain = email_domain(lead.get("email"))
        if domain is not None:
            if domain in seen:
                continue
            seen.add(domain)
        yield dict(lead)


@dataclass(frozen=True)
class SuppressionReport:
    """Contagem de quantos leads foram removidos em cada etapa do portão — para
    auditoria (registro de tratamento, ver CLAUDE.md)."""

    n_in: int
    n_hard_excluded: int
    n_deduped_id_estab: int
    n_deduped_email_domain: int
    n_suppressed: int
    n_out: int


def apply_suppression_gate(
    leads: Iterable[Mapping[str, Any]],
    suppression: SuppressionList,
) -> tuple[list[dict[str, Any]], SuppressionReport]:
    """O último portão antes de exportação: exclusão hard -> dedup por `id_estab` ->
    dedup por domínio de e-mail -> supressão (opt-out), nessa ordem (ver docstring do
    módulo).

    Args:
        leads: leads candidatos (ex.: resultado de `segmentation/filters.py`).
        suppression: lista de opt-out já carregada (ver `load_suppression_list`).

    Returns:
        `(leads_finais, relatório)` — `leads_finais` preserva a ordem de entrada.
    """
    leads_list = list(leads)
    n_in = len(leads_list)

    after_hard = [lead for lead in leads_list if not is_hard_excluded(lead)]
    n_hard_excluded = n_in - len(after_hard)

    after_id = list(dedupe_by_id_estab(after_hard))
    n_deduped_id_estab = len(after_hard) - len(after_id)

    after_domain = list(dedupe_by_email_domain(after_id))
    n_deduped_email_domain = len(after_id) - len(after_domain)

    final = [lead for lead in after_domain if not is_suppressed(lead, suppression)]
    n_suppressed = len(after_domain) - len(final)

    report = SuppressionReport(
        n_in=n_in,
        n_hard_excluded=n_hard_excluded,
        n_deduped_id_estab=n_deduped_id_estab,
        n_deduped_email_domain=n_deduped_email_domain,
        n_suppressed=n_suppressed,
        n_out=len(final),
    )
    return final, report


def apply_suppression_gate_from_path(
    leads: Iterable[Mapping[str, Any]],
    suppression_list_path: Path,
) -> tuple[list[dict[str, Any]], SuppressionReport]:
    """Conveniência: carrega a lista de supressão de `suppression_list_path` (ver
    `SUPPRESSION_LIST_PATH` em `.env.example`) e aplica `apply_suppression_gate`."""
    suppression = load_suppression_list(suppression_list_path)
    return apply_suppression_gate(leads, suppression)
