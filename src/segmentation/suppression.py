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

## Persistência da lista (opt-out) — Fase 2

`load_suppression_list` é só leitura. `add_to_suppression_list`/
`remove_from_suppression_list` são o lado de escrita: registram/removem um opt-out
no CSV persistido (`SUPPRESSION_LIST_PATH`), idempotentes (registrar o mesmo opt-out
duas vezes não duplica linha). `cli.py` expõe isso via o subcomando `optout` — é o
"endpoint" deste projeto pra atender a um pedido de opt-out (não há API HTTP; a
interface operacional do projeto inteiro é a CLI, ver `cli.py query`).
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SUPPRESSION_LIST_PATH = Path("./data/warehouse/suppression_list.csv")

# Colunas gravadas por add_to_suppression_list/remove_from_suppression_list.
# `motivo`/`registrado_em` são extras informativos (auditoria) -- load_suppression_list
# ignora colunas desconhecidas (csv.DictReader por nome), então um CSV legado só com
# `id_estab,email` continua funcionando normalmente.
SUPPRESSION_CSV_FIELDNAMES: tuple[str, ...] = ("id_estab", "email", "motivo", "registrado_em")


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


# -- Persistência (adicionar/remover opt-out) ----------------------------------------


def _read_suppression_rows(path: Path) -> list[dict[str, str]]:
    """Linhas brutas do CSV persistido (todas as colunas, não só os conjuntos
    agregados que `load_suppression_list` devolve) — usado internamente por
    add/remove pra reescrever o arquivo preservando `motivo`/`registrado_em`."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return [
            {
                "id_estab": (row.get("id_estab") or "").strip(),
                "email": (row.get("email") or "").strip().lower(),
                "motivo": (row.get("motivo") or "").strip(),
                "registrado_em": (row.get("registrado_em") or "").strip(),
            }
            for row in csv.DictReader(f)
        ]


def _write_suppression_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUPPRESSION_CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def add_to_suppression_list(
    path: Path,
    *,
    id_estab: str | None = None,
    email: str | None = None,
    motivo: str | None = None,
) -> bool:
    """Registra um opt-out na lista persistida (append), criando o arquivo se ainda
    não existir. Informe `id_estab` e/ou `email` (pelo menos um dos dois).

    Idempotente: registrar exatamente a mesma combinação `id_estab`+`email` de novo
    não duplica linha (mas note que `id_estab` e `email` suprimem
    INDEPENDENTEMENTE — ver `is_suppressed` — então registrar só `id_estab="X"` numa
    chamada e só `email="a@b.com"` noutra são dois opt-outs distintos, cada um
    reforçando sua própria regra; não é preciso registrar os dois juntos).

    Args:
        path: onde a lista está persistida (ver `SUPPRESSION_LIST_PATH`).
        id_estab: identificador do estabelecimento a suprimir.
        email: e-mail a suprimir (normalizado para minúsculo).
        motivo: texto livre opcional, só para auditoria/rastreabilidade — não afeta
            o funcionamento da supressão em si.

    Returns:
        `True` se um registro novo foi adicionado; `False` se a combinação exata já
        estava na lista (nada mudou).

    Raises:
        ValueError: se nem `id_estab` nem `email` forem informados.
    """
    id_estab = (id_estab or "").strip()
    email = (email or "").strip().lower()
    if not id_estab and not email:
        raise ValueError("Informe id_estab e/ou email para registrar o opt-out.")

    rows = _read_suppression_rows(path)
    if any(r["id_estab"] == id_estab and r["email"] == email for r in rows):
        logger.info(
            "Opt-out já registrado (id_estab=%r, email=%r); nada a fazer.",
            id_estab or None,
            email or None,
        )
        return False

    rows.append(
        {
            "id_estab": id_estab,
            "email": email,
            "motivo": (motivo or "").strip(),
            "registrado_em": datetime.now(UTC).isoformat(),
        }
    )
    _write_suppression_rows(path, rows)
    logger.info(
        "Opt-out registrado em %s: id_estab=%r email=%r", path, id_estab or None, email or None
    )
    return True


def remove_from_suppression_list(
    path: Path, *, id_estab: str | None = None, email: str | None = None
) -> int:
    """Remove da lista persistida toda linha que bata com `id_estab` e/ou `email`
    informados (cada critério dado remove por si só — não precisa dos dois juntos).

    Um arquivo ausente é tratado como "nada a remover" (retorna `0`), não como erro
    — mesma convenção de `load_suppression_list` para lista vazia.

    Args:
        path: onde a lista está persistida.
        id_estab: remove toda linha com este `id_estab`.
        email: remove toda linha com este e-mail (comparação case-insensitive).

    Returns:
        Quantas linhas foram removidas.

    Raises:
        ValueError: se nem `id_estab` nem `email` forem informados.
    """
    id_estab = (id_estab or "").strip()
    email = (email or "").strip().lower()
    if not id_estab and not email:
        raise ValueError("Informe id_estab e/ou email para remover o opt-out.")
    if not path.is_file():
        return 0

    rows = _read_suppression_rows(path)
    remaining = [
        r
        for r in rows
        if not ((id_estab and r["id_estab"] == id_estab) or (email and r["email"] == email))
    ]
    n_removed = len(rows) - len(remaining)
    if n_removed:
        _write_suppression_rows(path, remaining)
        logger.info(
            "%d opt-out(s) removido(s) de %s: id_estab=%r email=%r",
            n_removed,
            path,
            id_estab or None,
            email or None,
        )
    return n_removed


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
