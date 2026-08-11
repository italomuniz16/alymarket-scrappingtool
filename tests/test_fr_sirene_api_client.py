"""Testes de `ingestion/fr_sirene/api_client.py` com servidor HTTP mockado
(`httpx.MockTransport`) — nenhuma chamada de rede real é feita.

Cobre os dois clientes: `RechercheEntreprisesClient` (busca/descoberta, sem
autenticação) e `SireneApiClient` (dados completos por SIREN/SIRET, com Bearer
token) — incluindo os caminhos de erro específicos de cada um (paginação limitada,
autenticação ausente/recusada, 404).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.enrichment.client import EnrichmentClient
from src.ingestion.fr_sirene.api_client import (
    API_SIRENE_BASE_URL,
    DEFAULT_MIN_INTERVAL_SECONDS,
    RECHERCHE_ENTREPRISES_BASE_URL,
    RechercheEntreprisesClient,
    SireneApiAuthError,
    SireneApiClient,
    SireneApiNotFoundError,
    extract_sirens,
    fetch_client_credentials_token,
    make_recherche_entreprises_client,
)

# Formato baseado numa resposta real de GET .../search?q=Carrefour (já confirmado em
# enrichment/providers.py, reaproveitado aqui).
SEARCH_RESPONSE_PAGE1: dict[str, Any] = {
    "results": [
        {"siren": "111111111", "nom_complet": "EMPRESA UM"},
        {"siren": "222222222", "nom_complet": "EMPRESA DOIS"},
    ],
    "total_results": 3,
    "page": 1,
    "per_page": 2,
    "total_pages": 2,
}
SEARCH_RESPONSE_PAGE2: dict[str, Any] = {
    "results": [{"siren": "333333333", "nom_complet": "EMPRESA TRES"}],
    "total_results": 3,
    "page": 2,
    "per_page": 2,
    "total_pages": 2,
}


def _mock_enrichment_client(handler: object, tmp_path: Path, **kwargs: Any) -> EnrichmentClient:
    kwargs.setdefault("min_interval_seconds", 0)
    kwargs.setdefault("retry_wait_seconds", 0)
    kwargs.setdefault("cache_path", tmp_path / "cache.sqlite")
    return EnrichmentClient(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


# -- Recherche d'Entreprises -----------------------------------------------------


class TestMakeRechercheEntreprisesClient:
    def test_default_rate_limit(self, tmp_path: Path) -> None:
        client = make_recherche_entreprises_client(cache_path=tmp_path / "c.sqlite")
        try:
            assert client._rate_limiter.min_interval_seconds == DEFAULT_MIN_INTERVAL_SECONDS
        finally:
            client.close()


class TestRechercheEntreprisesSearch:
    def test_search_builds_query_and_returns_body(self, tmp_path: Path) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=SEARCH_RESPONSE_PAGE1)

        with (
            _mock_enrichment_client(handler, tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
        ):
            body = re_client.search(code_postal="75001", activite_principale="62.01Z")

        assert body == SEARCH_RESPONSE_PAGE1
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url).startswith(RECHERCHE_ENTREPRISES_BASE_URL)
        params = dict(request.url.params)
        assert params["code_postal"] == "75001"
        assert params["activite_principale"] == "62.01Z"
        assert params["etat_administratif"] == "A"  # default

    def test_etat_administratif_none_omits_filter(self, tmp_path: Path) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=SEARCH_RESPONSE_PAGE1)

        with (
            _mock_enrichment_client(handler, tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
        ):
            re_client.search(q="padaria", etat_administratif=None)

        params = dict(requests[0].url.params)
        assert "etat_administratif" not in params
        assert params["q"] == "padaria"

    def test_per_page_over_documented_max_raises(self, tmp_path: Path) -> None:
        with (
            _mock_enrichment_client(lambda r: httpx.Response(200, json={}), tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
            pytest.raises(ValueError),
        ):
            re_client.search(q="x", per_page=100)

    def test_search_all_results_paginates_until_last_page(self, tmp_path: Path) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            page = dict(request.url.params).get("page")
            body = SEARCH_RESPONSE_PAGE2 if page == "2" else SEARCH_RESPONSE_PAGE1
            return httpx.Response(200, json=body)

        with (
            _mock_enrichment_client(handler, tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
        ):
            sirens = [r["siren"] for r in re_client.search_all_results(q="x")]

        assert sirens == ["111111111", "222222222", "333333333"]
        assert len(requests) == 2

    def test_search_all_results_stops_at_max_pages(self, tmp_path: Path) -> None:
        # total_pages sempre muito maior que o número de chamadas -> sem o limite
        # de max_pages isso paginaria "pra sempre".
        infinite_page: dict[str, Any] = {
            "results": [{"siren": "999999999"}],
            "total_results": 1000,
            "page": 1,
            "per_page": 1,
            "total_pages": 1000,
        }
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=infinite_page)

        with (
            _mock_enrichment_client(handler, tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
        ):
            results = list(re_client.search_all_results(q="x", max_pages=3))

        assert len(results) == 3
        assert len(requests) == 3

    def test_search_all_results_stops_when_page_has_no_results(self, tmp_path: Path) -> None:
        empty_page: dict[str, Any] = {
            "results": [],
            "total_results": 0,
            "page": 1,
            "per_page": 25,
            "total_pages": 5,  # inconsistente de propósito -- não deve confiar só nisso
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=empty_page)

        with (
            _mock_enrichment_client(handler, tmp_path) as client,
            RechercheEntreprisesClient(client) as re_client,
        ):
            results = list(re_client.search_all_results(q="x", max_pages=10))

        assert results == []


class TestExtractSirens:
    def test_extracts_present_sirens_only(self) -> None:
        response = {
            "results": [
                {"siren": "111111111"},
                {"nom_complet": "SEM SIREN"},
                {"siren": "333333333"},
            ]
        }
        assert extract_sirens(response) == ["111111111", "333333333"]

    def test_empty_results_returns_empty_list(self) -> None:
        assert extract_sirens({"results": []}) == []
        assert extract_sirens({}) == []


# -- API Sirene / INSEE -----------------------------------------------------------


UNITE_LEGALE_RESPONSE: dict[str, Any] = {
    "header": {"statut": 200, "message": "OK"},
    "uniteLegale": {
        "siren": "503932568",
        "periodesUniteLegale": [
            {"denominationUniteLegale": "CARREFOUR", "etatAdministratifUniteLegale": "A"}
        ],
    },
}
ETABLISSEMENT_RESPONSE: dict[str, Any] = {
    "header": {"statut": 200, "message": "OK"},
    "etablissement": {"siren": "503932568", "siret": "50393256800010"},
}


class TestSireneApiClientConstruction:
    def test_empty_bearer_token_raises(self) -> None:
        with pytest.raises(SireneApiAuthError):
            SireneApiClient(bearer_token="")


class TestSireneApiClientRequests:
    def test_get_unite_legale_sends_bearer_token_and_correct_url(self, tmp_path: Path) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=UNITE_LEGALE_RESPONSE)

        with SireneApiClient(
            bearer_token="test-token",
            transport=httpx.MockTransport(handler),
            cache_path=tmp_path / "cache.sqlite",
            min_interval_seconds=0,
            retry_wait_seconds=0,
        ) as client:
            body = client.get_unite_legale("503932568")

        assert body == UNITE_LEGALE_RESPONSE
        assert len(requests) == 1
        assert str(requests[0].url) == f"{API_SIRENE_BASE_URL}/siren/503932568"
        assert requests[0].headers.get("authorization") == "Bearer test-token"

    def test_get_etablissement_sends_bearer_token_and_correct_url(self, tmp_path: Path) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=ETABLISSEMENT_RESPONSE)

        with SireneApiClient(
            bearer_token="test-token",
            transport=httpx.MockTransport(handler),
            cache_path=tmp_path / "cache.sqlite",
            min_interval_seconds=0,
            retry_wait_seconds=0,
        ) as client:
            body = client.get_etablissement("50393256800010")

        assert body == ETABLISSEMENT_RESPONSE
        assert str(requests[0].url) == f"{API_SIRENE_BASE_URL}/siret/50393256800010"
        assert requests[0].headers.get("authorization") == "Bearer test-token"

    def test_401_raises_sirene_api_auth_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Unauthorized"})

        with (
            SireneApiClient(
                bearer_token="invalid-token",
                transport=httpx.MockTransport(handler),
                cache_path=tmp_path / "cache.sqlite",
                min_interval_seconds=0,
                retry_wait_seconds=0,
                max_attempts=1,
            ) as client,
            pytest.raises(SireneApiAuthError),
        ):
            client.get_unite_legale("503932568")

    def test_403_raises_sirene_api_auth_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        with (
            SireneApiClient(
                bearer_token="token-sem-permissao",
                transport=httpx.MockTransport(handler),
                cache_path=tmp_path / "cache.sqlite",
                min_interval_seconds=0,
                retry_wait_seconds=0,
                max_attempts=1,
            ) as client,
            pytest.raises(SireneApiAuthError),
        ):
            client.get_unite_legale("503932568")

    def test_404_raises_sirene_api_not_found_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"header": {"statut": 404}})

        with (
            SireneApiClient(
                bearer_token="test-token",
                transport=httpx.MockTransport(handler),
                cache_path=tmp_path / "cache.sqlite",
                min_interval_seconds=0,
                retry_wait_seconds=0,
                max_attempts=1,
            ) as client,
            pytest.raises(SireneApiNotFoundError),
        ):
            client.get_unite_legale("000000000")

    def test_500_reraises_generic_http_status_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        with (
            SireneApiClient(
                bearer_token="test-token",
                transport=httpx.MockTransport(handler),
                cache_path=tmp_path / "cache.sqlite",
                min_interval_seconds=0,
                retry_wait_seconds=0,
                max_attempts=1,
            ) as client,
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.get_unite_legale("503932568")


class TestSireneApiClientFromEnv:
    def test_uses_token_env_var_directly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INSEE_SIRENE_API_TOKEN", "token-do-ambiente")
        monkeypatch.delenv("INSEE_CLIENT_ID", raising=False)
        monkeypatch.delenv("INSEE_CLIENT_SECRET", raising=False)

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=UNITE_LEGALE_RESPONSE)

        with SireneApiClient.from_env(
            transport=httpx.MockTransport(handler),
            cache_path=tmp_path / "cache.sqlite",
            min_interval_seconds=0,
            retry_wait_seconds=0,
        ) as client:
            client.get_unite_legale("503932568")

        assert requests[0].headers.get("authorization") == "Bearer token-do-ambiente"

    def test_falls_back_to_client_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INSEE_SIRENE_API_TOKEN", raising=False)
        monkeypatch.setenv("INSEE_CLIENT_ID", "meu-client-id")
        monkeypatch.setenv("INSEE_CLIENT_SECRET", "meu-client-secret")

        monkeypatch.setattr(
            "src.ingestion.fr_sirene.api_client.fetch_client_credentials_token",
            lambda client_id, client_secret: "token-via-client-credentials",
        )

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=UNITE_LEGALE_RESPONSE)

        with SireneApiClient.from_env(
            transport=httpx.MockTransport(handler),
            cache_path=tmp_path / "cache.sqlite",
            min_interval_seconds=0,
            retry_wait_seconds=0,
        ) as client:
            client.get_unite_legale("503932568")

        assert requests[0].headers.get("authorization") == "Bearer token-via-client-credentials"

    def test_no_credentials_configured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INSEE_SIRENE_API_TOKEN", raising=False)
        monkeypatch.delenv("INSEE_CLIENT_ID", raising=False)
        monkeypatch.delenv("INSEE_CLIENT_SECRET", raising=False)

        with pytest.raises(SireneApiAuthError):
            SireneApiClient.from_env()


class TestFetchClientCredentialsToken:
    def test_posts_client_credentials_and_returns_token(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"access_token": "abc123", "expires_in": 600})

        token = fetch_client_credentials_token(
            "meu-id", "meu-secret", transport=httpx.MockTransport(handler)
        )

        assert token == "abc123"
        assert len(requests) == 1
        assert requests[0].headers.get("authorization", "").startswith("Basic ")

    def test_missing_access_token_in_response_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "bearer"})

        with pytest.raises(SireneApiAuthError):
            fetch_client_credentials_token(
                "meu-id", "meu-secret", transport=httpx.MockTransport(handler)
            )

    def test_error_response_raises_http_status_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        with pytest.raises(httpx.HTTPStatusError):
            fetch_client_credentials_token(
                "id-errado", "secret-errado", transport=httpx.MockTransport(handler)
            )
