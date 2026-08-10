"""Adaptadores de enriquecimento sob demanda: BrasilAPI (BR) e API Recherche
d'Entreprises (FR) — cada um traduz a resposta bruta do provedor pro schema canônico
(`CanonicalLead`), sempre marcando `enriquecido_em`.

Formatos de resposta confirmados empiricamente contra as APIs reais (não só
documentação) antes de escrever o mapeamento — inclusive uma diferença importante:

- **BrasilAPI** (`GET /api/cnpj/v1/{cnpj}`): CNPJ não encontrado retorna erro HTTP
  (404) — já tratado por `enrichment.client.enrich_leads` (vira `None` no resultado).
- **API Recherche d'Entreprises** (`GET /search?q=siren:{siren}`): SIREN não
  encontrado retorna **HTTP 200 com `results: []`**, não um erro — por isso
  `map_recherche_entreprises_response` precisa checar isso explicitamente.

Cada provider usa `enrichment/client.py` (rate limit, retry, cache, User-Agent) — não
faz requisições HTTP diretamente. `make_brasilapi_client`/
`make_recherche_entreprises_client` já vêm com um rate limit condizente com o que se
sabe de cada fonte (ver docs/PRD.md §3.2 para a Recherche d'Entreprises, ~1000
req/min/IP).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.enrichment.client import EnrichmentClient, enrich_leads
from src.ingestion.base import CanonicalLead

logger = logging.getLogger(__name__)

BRASILAPI_URL_TEMPLATE = "https://brasilapi.com.br/api/cnpj/v1/{id}"
RECHERCHE_ENTREPRISES_URL_TEMPLATE = "https://recherche-entreprises.api.gouv.fr/search?q=siren:{id}"

FONTE_BRASILAPI = "BRASILAPI"
FONTE_RECHERCHE_ENTREPRISES = "RECHERCHE_ENTREPRISES"

# BrasilAPI é gratuita e sem chave, mas não documenta um limite oficial -- 1
# req/segundo é um default conservador.
DEFAULT_BRASILAPI_MIN_INTERVAL_SECONDS = 1.0
# ~1000 req/min/IP documentado (docs/PRD.md §3.2); ~600/min de default dá margem.
DEFAULT_RECHERCHE_ENTREPRISES_MIN_INTERVAL_SECONDS = 0.1

_ETAT_ADMINISTRATIVO_TO_SITUACAO: dict[str, str] = {"A": "ATIVA", "F": "BAIXADA"}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        logger.warning("Data inválida ignorada: %r", value)
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        logger.warning("Capital social inválido ignorado: %r", value)
        return None


def _upper_or_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value.upper() or None


# -- BrasilAPI (BR) -----------------------------------------------------------------


def map_brasilapi_response(data: dict[str, Any]) -> dict[str, Any]:
    """Traduz a resposta da BrasilAPI (`GET /api/cnpj/v1/{cnpj}`) pro subconjunto de
    campos canônicos usados pra refrescar um lead: valida a situação cadastral atual
    e atualiza contato/porte/capital/natureza jurídica.

    Textos vêm normalizados em maiúsculo (`situacao`, `porte`, `natureza_juridica`)
    para bater com a convenção do resto do projeto (dump da Receita já vem em
    caixa alta; a BrasilAPI às vezes devolve `natureza_juridica` em title case).
    """
    cnae = data.get("cnae_fiscal")
    return {
        "razao_social": data.get("razao_social") or None,
        "nome_fantasia": data.get("nome_fantasia") or None,
        "cod_atividade": str(cnae) if cnae is not None else None,
        "situacao": _upper_or_none(data.get("descricao_situacao_cadastral")),
        "regiao": data.get("uf") or None,
        "municipio": data.get("municipio") or None,
        "cep": data.get("cep") or None,
        "telefone": data.get("ddd_telefone_1") or None,
        "data_inicio_atividade": _parse_date(data.get("data_inicio_atividade")),
        "porte": _upper_or_none(data.get("porte")),
        "capital_social": _parse_decimal(data.get("capital_social")),
        "natureza_juridica": _upper_or_none(data.get("natureza_juridica")),
        "fonte": FONTE_BRASILAPI,
        "enriquecido_em": datetime.now(UTC),
    }


def make_brasilapi_client(**overrides: Any) -> EnrichmentClient:
    """`EnrichmentClient` pré-configurado com um rate limit conservador para a
    BrasilAPI. Passe `transport=httpx.MockTransport(...)` em testes."""
    kwargs: dict[str, Any] = {"min_interval_seconds": DEFAULT_BRASILAPI_MIN_INTERVAL_SECONDS}
    kwargs.update(overrides)
    return EnrichmentClient(**kwargs)


def enrich_br_leads(
    client: EnrichmentClient, cnpjs: Sequence[str], *, max_batch_size: int = 1000
) -> dict[str, dict[str, Any] | None]:
    """Enriquece um SUBCONJUNTO explícito de leads BR via BrasilAPI, por CNPJ
    completo (14 dígitos) — nunca a base inteira (ver `enrichment.client.enrich_leads`).

    Returns:
        `{cnpj: atualizacao_canonica}` — `None` se aquele CNPJ não foi encontrado ou
        a requisição falhou (não interrompe os demais).
    """
    raw = enrich_leads(
        client, cnpjs, url_template=BRASILAPI_URL_TEMPLATE, max_batch_size=max_batch_size
    )
    return {
        cnpj: (map_brasilapi_response(data) if data is not None else None)
        for cnpj, data in raw.items()
    }


# -- API Recherche d'Entreprises (FR) ------------------------------------------------


def _departamento_from_code_postal(code_postal: str | None) -> str | None:
    """Aproxima o département a partir dos 2 primeiros dígitos do code postal —
    heurística padrão francesa (não cobre exceções de ultramar/Córsega)."""
    if not code_postal or len(code_postal) < 2 or not code_postal[:2].isdigit():
        return None
    return code_postal[:2]


def map_recherche_entreprises_response(data: dict[str, Any]) -> dict[str, Any] | None:
    """Traduz a resposta da API Recherche d'Entreprises (`GET /search?q=siren:...`)
    pro subconjunto de campos canônicos usados pra refrescar um lead francês.

    `statut_diffusion` decide `flag_difusao_restrita`: só `"O"` (aberto/difundido)
    NÃO é restrito — qualquer outro valor (`"P"`/parcial, ausente, etc.) é tratado
    como restrito por padrão, conservadoramente (ver CLAUDE.md: filtro hard de
    difusão restrita — melhor excluir demais do que deixar passar um registro que a
    lei francesa proíbe usar para prospecção).

    Returns:
        `None` se `results` vier vazio — SIREN não encontrado (resposta HTTP 200,
        não um erro; chamador deve tratar como "sem atualização", não como falha).
    """
    results = data.get("results") or []
    if not results:
        return None

    item = results[0]
    siege = item.get("siege") or {}

    etat = item.get("etat_administratif")
    situacao = _ETAT_ADMINISTRATIVO_TO_SITUACAO.get(etat, _upper_or_none(etat))

    statut_diffusion = item.get("statut_diffusion")
    flag_difusao_restrita = statut_diffusion != "O"

    return {
        "razao_social": item.get("nom_raison_sociale") or item.get("nom_complet") or None,
        "nome_fantasia": item.get("sigle") or None,
        "cod_atividade": item.get("activite_principale") or None,
        "situacao": situacao,
        "regiao": _departamento_from_code_postal(siege.get("code_postal")),
        "municipio": siege.get("libelle_commune") or None,
        "cep": siege.get("code_postal") or None,
        "data_inicio_atividade": _parse_date(item.get("date_creation")),
        "natureza_juridica": item.get("nature_juridique") or None,
        "flag_difusao_restrita": flag_difusao_restrita,
        "fonte": FONTE_RECHERCHE_ENTREPRISES,
        "enriquecido_em": datetime.now(UTC),
    }


def make_recherche_entreprises_client(**overrides: Any) -> EnrichmentClient:
    """`EnrichmentClient` pré-configurado com um rate limit dentro do limite
    documentado da API Recherche d'Entreprises. Passe `transport=httpx.MockTransport(...)`
    em testes."""
    kwargs: dict[str, Any] = {
        "min_interval_seconds": DEFAULT_RECHERCHE_ENTREPRISES_MIN_INTERVAL_SECONDS
    }
    kwargs.update(overrides)
    return EnrichmentClient(**kwargs)


def enrich_fr_leads(
    client: EnrichmentClient, sirens: Sequence[str], *, max_batch_size: int = 1000
) -> dict[str, dict[str, Any] | None]:
    """Enriquece um SUBCONJUNTO explícito de leads FR via API Recherche d'Entreprises,
    por SIREN (9 dígitos) — nunca a base inteira.

    Returns:
        `{siren: atualizacao_canonica}` — `None` se aquele SIREN não foi encontrado
        ou a requisição falhou (não interrompe os demais).
    """
    raw = enrich_leads(
        client,
        sirens,
        url_template=RECHERCHE_ENTREPRISES_URL_TEMPLATE,
        max_batch_size=max_batch_size,
    )
    return {
        siren: (map_recherche_entreprises_response(data) if data is not None else None)
        for siren, data in raw.items()
    }


# -- Aplicação da atualização sobre um lead ------------------------------------------


def apply_enrichment_update(lead: dict[str, Any], update: dict[str, Any] | None) -> dict[str, Any]:
    """Aplica uma atualização de enriquecimento sobre uma CÓPIA de `lead` (não
    modifica o original). Campos ausentes/`None` em `update` não sobrescrevem o valor
    existente do lead — só atualiza o que o provider de fato trouxe.

    `update=None` (identificador não encontrado, ou falha na requisição) retorna uma
    cópia inalterada de `lead`.

    Raises:
        pydantic.ValidationError: se o resultado combinado não bater com o schema
            canônico (`CanonicalLead`) — protege contra um provider devolver algo
            inconsistente sem que ninguém perceba.
    """
    if update is None:
        return dict(lead)

    merged = dict(lead)
    for key, value in update.items():
        if value is not None:
            merged[key] = value

    CanonicalLead.model_validate(merged)
    return merged
