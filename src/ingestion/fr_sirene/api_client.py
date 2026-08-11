"""Clientes para as APIs oficiais francesas de empresas: Recherche d'Entreprises
(busca/descoberta, sem autenticação) e API Sirene do INSEE (dados completos por
SIREN/SIRET, com autenticação).

## Duas fontes, dois papéis diferentes

- **Recherche d'Entreprises** (`recherche-entreprises.api.gouv.fr/search`): API
  pública do gouv.fr, SEM autenticação — confirmado empiricamente nesta tarefa
  (`GET .../search?q=...` responde 200 sem nenhum header de auth). Serve pra
  BUSCA/DESCOBERTA: filtros por texto livre, código postal, atividade principal
  (NAF/APE), forma jurídica, departamento/região, com paginação — o caso de uso é
  "encontrar leads que batem com um ICP", diferente de `enrichment/providers.py`
  (que só faz refresh de um SIREN já conhecido, um de cada vez).
- **API Sirene** (`api.insee.fr/api-sirene/3.11`): API oficial do INSEE, EXIGE
  autenticação — confirmado empiricamente (`GET /siren/{siren}` sem header responde
  `401 Unauthorized`). Serve pra buscar o registro OFICIAL completo de um SIREN/SIRET
  já conhecido (fonte primária, mais completa que a Recherche d'Entreprises).

## Sobre a autenticação da API Sirene

O INSEE historicamente documentava OAuth2 `client_credentials` (`POST
https://api.insee.fr/token`, Basic Auth `client_id:client_secret`) — é o modelo que
o `.env.example` deste projeto já reservava (`INSEE_CLIENT_ID`/`INSEE_CLIENT_SECRET`).
Testado empiricamente nesta tarefa: esse endpoint de token não responde mais
normalmente (conexão resetada), sinal de que o INSEE já migrou esse fluxo pro novo
portal (`portail-api.insee.fr`), cujos detalhes exatos exigem uma conta/aplicação
registrada pra confirmar — fora do que dá pra verificar sem credenciais. Por isso
este módulo separa as duas responsabilidades:

1. `SireneApiClient` sempre autentica com um Bearer token já obtido
   (`Authorization: Bearer <token>` — esse padrão está confirmado e não muda,
   independente de como o token foi obtido).
2. `fetch_client_credentials_token` é um helper best-effort pro fluxo OAuth2
   clássico (client_id/client_secret -> token) — não é a única forma suportada de
   obter o token; confirme contra a documentação vigente do INSEE antes de depender
   disso em produção.

Os dois clientes reaproveitam `enrichment/client.py` (`EnrichmentClient`): rate
limit, retry com backoff exponencial, cache local, User-Agent identificado — não
duplicam essa lógica.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from src.enrichment.client import EnrichmentClient

logger = logging.getLogger(__name__)

RECHERCHE_ENTREPRISES_BASE_URL = "https://recherche-entreprises.api.gouv.fr/search"
API_SIRENE_BASE_URL = "https://api.insee.fr/api-sirene/3.11"
DEFAULT_INSEE_TOKEN_URL = "https://api.insee.fr/token"

# ~1000 req/min/IP documentado pra Recherche d'Entreprises (docs/PRD.md §3.2, mesmo
# limite usado em enrichment/providers.py); usado também como default conservador
# pra API Sirene, já que o INSEE não publica um número fixo (varia por
# assinatura/aplicação no portal) -- 10 req/s de default dá margem confortável.
DEFAULT_MIN_INTERVAL_SECONDS = 0.1

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 25  # limite documentado da Recherche d'Entreprises


class SireneApiAuthError(RuntimeError):
    """Levantado quando falta credencial pra API Sirene, ou o INSEE recusa a
    requisição por autenticação inválida/ausente (HTTP 401/403)."""


class SireneApiNotFoundError(RuntimeError):
    """Levantado quando um SIREN/SIRET não existe na API Sirene (HTTP 404)."""


# -- Recherche d'Entreprises (busca/descoberta, sem autenticação) --------------------


def make_recherche_entreprises_client(**overrides: Any) -> EnrichmentClient:
    """`EnrichmentClient` pré-configurado com um rate limit dentro do limite
    documentado (~1000 req/min/IP) da API Recherche d'Entreprises. Passe
    `transport=httpx.MockTransport(...)` em testes."""
    kwargs: dict[str, Any] = {"min_interval_seconds": DEFAULT_MIN_INTERVAL_SECONDS}
    kwargs.update(overrides)
    return EnrichmentClient(**kwargs)


class RechercheEntreprisesClient:
    """Busca/descoberta de empresas francesas via a API pública Recherche
    d'Entreprises — sem autenticação. Uso típico: encontrar leads que batem com um
    ICP (região, atividade, forma jurídica, ...) — diferente de
    `enrichment.providers.enrich_fr_leads`, que só atualiza um SIREN já conhecido.

    Uso típico::

        with RechercheEntreprisesClient() as client:
            body = client.search(code_postal="75001", activite_principale="62.01Z")
    """

    def __init__(self, client: EnrichmentClient | None = None) -> None:
        self._client = client or make_recherche_entreprises_client()
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RechercheEntreprisesClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search(
        self,
        *,
        q: str | None = None,
        code_postal: str | None = None,
        code_commune: str | None = None,
        departement: str | None = None,
        region: str | None = None,
        activite_principale: str | None = None,
        categorie_entreprise: str | None = None,
        nature_juridique: str | None = None,
        etat_administratif: str | None = "A",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> dict[str, Any]:
        """Busca empresas pelos filtros dados (todos opcionais, exceto paginação).

        `etat_administratif="A"` (só ativas) é o default — passe `None`
        explicitamente pra não filtrar por situação. A API exige pelo menos um
        filtro de conteúdo (`q` ou algum dos outros); se nenhum for dado, o `400`
        que ela retorna sobe como `httpx.HTTPStatusError` — não validado aqui de
        propósito, pra não duplicar regra de negócio que já é da própria API.

        Returns:
            Corpo JSON bruto da API (`results`, `total_results`, `page`,
            `per_page`, `total_pages`). Use `extract_sirens` ou
            `search_all_results` pra consumir os resultados diretamente.
        """
        if per_page > MAX_PER_PAGE:
            raise ValueError(f"per_page máximo documentado é {MAX_PER_PAGE}, recebido {per_page}")

        params: dict[str, str] = {"page": str(page), "per_page": str(per_page)}
        optional = {
            "q": q,
            "code_postal": code_postal,
            "code_commune": code_commune,
            "departement": departement,
            "region": region,
            "activite_principale": activite_principale,
            "categorie_entreprise": categorie_entreprise,
            "nature_juridique": nature_juridique,
            "etat_administratif": etat_administratif,
        }
        for key, value in optional.items():
            if value:
                params[key] = value

        query = urlencode(sorted(params.items()))
        url = f"{RECHERCHE_ENTREPRISES_BASE_URL}?{query}"
        result: dict[str, Any] = self._client.get_json(
            url, cache_key=f"recherche-entreprises:{query}"
        )
        return result

    def search_all_results(
        self, *, max_pages: int = 10, **search_kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        """Percorre várias páginas de `search`, gerando um resultado (dict bruto da
        API) por vez, até `max_pages` ou até a última página — limite explícito em
        código, não corre risco de paginar indefinidamente por engano."""
        page = search_kwargs.pop("page", 1)
        for _ in range(max_pages):
            body = self.search(page=page, **search_kwargs)
            results = body.get("results") or []
            yield from results

            total_pages = body.get("total_pages") or 0
            if not results or page >= total_pages:
                return
            page += 1


def extract_sirens(search_response: Mapping[str, Any]) -> list[str]:
    """Extrai os SIRENs de um resultado de `search`/`search_all_results` — passo
    seguinte típico é usar cada um com `SireneApiClient.get_unite_legale` ou
    `enrichment.providers.enrich_fr_leads`."""
    results = search_response.get("results") or []
    return [r["siren"] for r in results if r.get("siren")]


# -- API Sirene / INSEE (dados oficiais completos, com autenticação) -----------------


def fetch_client_credentials_token(
    client_id: str,
    client_secret: str,
    *,
    token_url: str = DEFAULT_INSEE_TOKEN_URL,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """Obtém um Bearer token via OAuth2 `client_credentials` (fluxo clássico
    documentado pelo INSEE). Best-effort — ver docstring do módulo: testado
    empiricamente nesta tarefa que `DEFAULT_INSEE_TOKEN_URL` não responde mais
    normalmente (indício de migração pro novo portal). Passe `token_url` se o
    endpoint mudou pra aplicação registrada de quem chama.
    """
    with httpx.Client(transport=transport, timeout=timeout_seconds) as client:
        response = client.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise SireneApiAuthError(
                f"Resposta do token endpoint sem 'access_token': {response.text!r}"
            )
        return str(token)


class SireneApiClient:
    """Dados oficiais completos de SIREN/SIRET via a API Sirene do INSEE — EXIGE
    autenticação (Bearer token), diferente da Recherche d'Entreprises.

    Uso típico::

        with SireneApiClient(bearer_token=meu_token) as client:
            unidade = client.get_unite_legale("503932568")

    Ou, resolvendo a credencial do ambiente (`.env` — ver docstring do módulo)::

        with SireneApiClient.from_env() as client:
            ...
    """

    def __init__(
        self,
        *,
        bearer_token: str,
        client: EnrichmentClient | None = None,
        **enrichment_client_kwargs: Any,
    ) -> None:
        """
        Args:
            bearer_token: token já obtido (ver `from_env` pra resolver do ambiente).
            client: `EnrichmentClient` já pronto, injetado inteiro (o chamador é
                responsável pelos headers nesse caso). Omitido (default): este
                cliente monta um `EnrichmentClient` próprio, com o Authorization
                Bearer já configurado.
            **enrichment_client_kwargs: repassado ao `EnrichmentClient` construído
                internamente quando `client` não é dado — ex.: `transport=` (testes),
                `cache_path=`, `retry_wait_seconds=`.
        """
        if not bearer_token:
            raise SireneApiAuthError(
                "bearer_token vazio — a API Sirene do INSEE exige autenticação "
                "(confirmado empiricamente: 401 sem header Authorization). Use "
                "SireneApiClient.from_env() pra resolver a partir de "
                "INSEE_SIRENE_API_TOKEN / INSEE_CLIENT_ID+INSEE_CLIENT_SECRET."
            )
        self._owns_client = client is None
        if client is None:
            kwargs: dict[str, Any] = {"min_interval_seconds": DEFAULT_MIN_INTERVAL_SECONDS}
            kwargs.update(enrichment_client_kwargs)
            headers = dict(kwargs.pop("extra_headers", None) or {})
            headers["Authorization"] = f"Bearer {bearer_token}"
            client = EnrichmentClient(extra_headers=headers, **kwargs)
        self._client = client

    @classmethod
    def from_env(
        cls, *, client: EnrichmentClient | None = None, **enrichment_client_kwargs: Any
    ) -> SireneApiClient:
        """Resolve o token a partir do ambiente: `INSEE_SIRENE_API_TOKEN` direto se
        presente; senão tenta `INSEE_CLIENT_ID`+`INSEE_CLIENT_SECRET` via
        `fetch_client_credentials_token` (ver docstring do módulo sobre esse
        fluxo). Levanta `SireneApiAuthError` se nenhuma credencial estiver
        configurada."""
        token = os.environ.get("INSEE_SIRENE_API_TOKEN")
        if not token:
            client_id = os.environ.get("INSEE_CLIENT_ID")
            client_secret = os.environ.get("INSEE_CLIENT_SECRET")
            if client_id and client_secret:
                token = fetch_client_credentials_token(client_id, client_secret)
        if not token:
            raise SireneApiAuthError(
                "Nenhuma credencial da API Sirene configurada — defina "
                "INSEE_SIRENE_API_TOKEN ou INSEE_CLIENT_ID+INSEE_CLIENT_SECRET no .env."
            )
        return cls(bearer_token=token, client=client, **enrichment_client_kwargs)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SireneApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_unite_legale(self, siren: str) -> dict[str, Any]:
        """Dados oficiais completos da unidade legal (`GET /siren/{siren}`)."""
        return self._get(f"{API_SIRENE_BASE_URL}/siren/{siren}", cache_key=f"sirene:siren:{siren}")

    def get_etablissement(self, siret: str) -> dict[str, Any]:
        """Dados oficiais completos do estabelecimento (`GET /siret/{siret}`)."""
        return self._get(f"{API_SIRENE_BASE_URL}/siret/{siret}", cache_key=f"sirene:siret:{siret}")

    def _get(self, url: str, *, cache_key: str) -> dict[str, Any]:
        try:
            result: dict[str, Any] = self._client.get_json(url, cache_key=cache_key)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise SireneApiAuthError(
                    f"Autenticação recusada pela API Sirene (HTTP {status})"
                ) from exc
            if status == 404:
                raise SireneApiNotFoundError(f"Não encontrado na API Sirene: {url}") from exc
            raise
        return result
