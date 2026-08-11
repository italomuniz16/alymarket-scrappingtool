"""Testes de `enrichment/providers.py`: mapeamento de cada provider isoladamente
(com fixtures baseadas em respostas reais confirmadas via chamada às APIs), e o fluxo
completo de enriquecimento contra um servidor mockado por provider.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from src.enrichment.client import EnrichmentClient
from src.enrichment.providers import (
    BRASILAPI_URL_TEMPLATE,
    FONTE_BRASILAPI,
    FONTE_RECHERCHE_ENTREPRISES,
    RECHERCHE_ENTREPRISES_URL_TEMPLATE,
    apply_enrichment_update,
    enrich_br_leads,
    enrich_fr_leads,
    make_brasilapi_client,
    make_recherche_entreprises_client,
    map_brasilapi_response,
    map_recherche_entreprises_response,
)

# Fixture baseada na resposta real de GET /api/cnpj/v1/33000167000101 (confirmada
# via chamada direta à BrasilAPI antes de escrever o mapeamento).
BRASILAPI_RESPONSE: dict[str, object] = {
    "cnpj": "33000167000101",
    "razao_social": "PETROLEO BRASILEIRO S A PETROBRAS",
    "nome_fantasia": "PETROBRAS - EDISE",
    "natureza_juridica": "Sociedade de Economia Mista",
    "capital_social": 205431960000,
    "porte": "DEMAIS",
    "codigo_porte": 5,
    "uf": "RJ",
    "cep": "20031170",
    "logradouro": "REPUBLICA DO CHILE",
    "numero": "65",
    "bairro": "CENTRO",
    "municipio": "RIO DE JANEIRO",
    "codigo_municipio": 6001,
    "cnae_fiscal": 600001,
    "cnae_fiscal_descricao": "Extração de petróleo e gás natural",
    "data_inicio_atividade": "1966-09-28",
    "situacao_cadastral": 2,
    "descricao_situacao_cadastral": "ATIVA",
    "data_situacao_cadastral": "2005-11-03",
    "ddd_telefone_1": "2121660000",
}

# Fixture baseada na resposta real de GET /search?q=Carrefour (confirmada via
# chamada direta à API Recherche d'Entreprises antes de escrever o mapeamento).
RECHERCHE_ENTREPRISES_RESPONSE: dict[str, object] = {
    "results": [
        {
            "siren": "503932568",
            "nom_complet": "CARREFOUR",
            "nom_raison_sociale": "CARREFOUR",
            "sigle": None,
            "activite_principale": "68.32A",
            "date_creation": "2007-01-01",
            "etat_administratif": "A",
            "nature_juridique": "9110",
            "statut_diffusion": "O",
            "siege": {
                "adresse": "1 AVENUE DES ECOLES 06110 LE CANNET",
                "code_postal": "06110",
                "siret": "50393256800010",
                "libelle_commune": "LE CANNET",
            },
        }
    ],
    "total_results": 1,
    "page": 1,
    "per_page": 10,
    "total_pages": 1,
}

RECHERCHE_ENTREPRISES_EMPTY_RESPONSE: dict[str, object] = {
    "results": [],
    "total_results": 0,
    "page": 1,
    "per_page": 10,
    "total_pages": 0,
}


class TestMapBrasilapiResponse:
    def test_maps_known_fields(self) -> None:
        result = map_brasilapi_response(BRASILAPI_RESPONSE)

        assert result["razao_social"] == "PETROLEO BRASILEIRO S A PETROBRAS"
        assert result["nome_fantasia"] == "PETROBRAS - EDISE"
        assert result["cod_atividade"] == "600001"
        assert result["regiao"] == "RJ"
        assert result["municipio"] == "RIO DE JANEIRO"
        assert result["cep"] == "20031170"
        assert result["telefone"] == "2121660000"
        assert result["data_inicio_atividade"] == date(1966, 9, 28)
        assert result["capital_social"] == Decimal("205431960000")

    def test_situacao_and_porte_and_natureza_normalized_to_uppercase(self) -> None:
        result = map_brasilapi_response(BRASILAPI_RESPONSE)
        assert result["situacao"] == "ATIVA"
        assert result["porte"] == "DEMAIS"
        assert result["natureza_juridica"] == "SOCIEDADE DE ECONOMIA MISTA"  # era title case

    def test_fonte_and_enriquecido_em(self) -> None:
        before = datetime.now(UTC)
        result = map_brasilapi_response(BRASILAPI_RESPONSE)
        assert result["fonte"] == FONTE_BRASILAPI == "BRASILAPI"
        assert result["enriquecido_em"] >= before

    def test_missing_optional_fields_become_none(self) -> None:
        minimal = {"razao_social": "X"}
        result = map_brasilapi_response(minimal)
        assert result["nome_fantasia"] is None
        assert result["telefone"] is None
        assert result["capital_social"] is None
        assert result["data_inicio_atividade"] is None
        assert result["situacao"] is None

    def test_invalid_capital_social_is_ignored(self) -> None:
        data = {**BRASILAPI_RESPONSE, "capital_social": "nao-e-numero"}
        result = map_brasilapi_response(data)
        assert result["capital_social"] is None

    def test_invalid_data_inicio_atividade_is_ignored(self) -> None:
        data = {**BRASILAPI_RESPONSE, "data_inicio_atividade": "nao-e-uma-data"}
        result = map_brasilapi_response(data)
        assert result["data_inicio_atividade"] is None


class TestMapRechercheEntreprisesResponse:
    def test_maps_known_fields(self) -> None:
        result = map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_RESPONSE)
        assert result is not None
        assert result["razao_social"] == "CARREFOUR"
        assert result["cod_atividade"] == "68.32A"
        assert result["municipio"] == "LE CANNET"
        assert result["cep"] == "06110"
        assert result["data_inicio_atividade"] == date(2007, 1, 1)
        assert result["natureza_juridica"] == "9110"

    def test_regiao_derived_from_code_postal(self) -> None:
        result = map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_RESPONSE)
        assert result is not None
        assert result["regiao"] == "06"

    def test_regiao_none_when_code_postal_missing(self) -> None:
        raw_item = dict(RECHERCHE_ENTREPRISES_RESPONSE["results"][0])  # type: ignore[index]
        raw_item["siege"] = {**raw_item["siege"], "code_postal": None}  # type: ignore[dict-item]
        result = map_recherche_entreprises_response({"results": [raw_item]})
        assert result is not None
        assert result["regiao"] is None

    def test_etat_administratif_a_maps_to_ativa(self) -> None:
        result = map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_RESPONSE)
        assert result is not None
        assert result["situacao"] == "ATIVA"

    def test_etat_administratif_f_maps_to_baixada(self) -> None:
        data = {
            "results": [{**RECHERCHE_ENTREPRISES_RESPONSE["results"][0], "etat_administratif": "F"}]  # type: ignore[index]
        }
        result = map_recherche_entreprises_response(data)
        assert result is not None
        assert result["situacao"] == "BAIXADA"

    def test_empty_results_returns_none(self) -> None:
        assert map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_EMPTY_RESPONSE) is None

    def test_fonte(self) -> None:
        result = map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_RESPONSE)
        assert result is not None
        assert result["fonte"] == FONTE_RECHERCHE_ENTREPRISES == "RECHERCHE_ENTREPRISES"

    def test_statut_diffusion_o_is_not_restricted(self) -> None:
        result = map_recherche_entreprises_response(RECHERCHE_ENTREPRISES_RESPONSE)
        assert result is not None
        assert result["flag_difusao_restrita"] is False

    def test_statut_diffusion_p_is_restricted(self) -> None:
        data = {
            "results": [{**RECHERCHE_ENTREPRISES_RESPONSE["results"][0], "statut_diffusion": "P"}]  # type: ignore[index]
        }
        result = map_recherche_entreprises_response(data)
        assert result is not None
        assert result["flag_difusao_restrita"] is True

    def test_statut_diffusion_missing_is_restricted_by_default(self) -> None:
        raw_item = dict(RECHERCHE_ENTREPRISES_RESPONSE["results"][0])  # type: ignore[index]
        raw_item.pop("statut_diffusion", None)
        data = {"results": [raw_item]}
        result = map_recherche_entreprises_response(data)
        assert result is not None
        assert result["flag_difusao_restrita"] is True

    def test_sigle_used_as_nome_fantasia_when_present(self) -> None:
        data = {"results": [{**RECHERCHE_ENTREPRISES_RESPONSE["results"][0], "sigle": "CRF"}]}  # type: ignore[index]
        result = map_recherche_entreprises_response(data)
        assert result is not None
        assert result["nome_fantasia"] == "CRF"


class TestApplyEnrichmentUpdate:
    def _lead(self) -> dict[str, object]:
        return {
            "pais": "BR",
            "id_legal": "33000167",
            "id_estab": "33000167000101",
            "razao_social": "NOME ANTIGO",
            "nome_fantasia": None,
            "cod_atividade": None,
            "situacao": None,
            "regiao": None,
            "municipio": None,
            "cep": None,
            "telefone": None,
            "email": None,
            "data_inicio_atividade": None,
            "porte": None,
            "capital_social": None,
            "natureza_juridica": None,
            "score_icp": None,
            "fonte": "BR_RECEITA",
            "enriquecido_em": None,
            "is_synthetic": False,
            "flag_difusao_restrita": False,
        }

    def test_none_update_returns_unchanged_copy(self) -> None:
        lead = self._lead()
        result = apply_enrichment_update(lead, None)
        assert result == lead
        assert result is not lead

    def test_merges_non_none_fields(self) -> None:
        lead = self._lead()
        update = map_brasilapi_response(BRASILAPI_RESPONSE)
        result = apply_enrichment_update(lead, update)

        assert result["razao_social"] == "PETROLEO BRASILEIRO S A PETROBRAS"
        assert result["situacao"] == "ATIVA"
        assert result["enriquecido_em"] is not None

    def test_does_not_mutate_original_lead(self) -> None:
        lead = self._lead()
        original = dict(lead)
        apply_enrichment_update(lead, map_brasilapi_response(BRASILAPI_RESPONSE))
        assert lead == original

    def test_update_fields_that_are_none_do_not_overwrite_existing_value(self) -> None:
        lead = self._lead()
        lead["nome_fantasia"] = "JA TINHA UM VALOR"
        update = {"nome_fantasia": None, "razao_social": "NOVO NOME"}
        result = apply_enrichment_update(lead, update)
        assert result["nome_fantasia"] == "JA TINHA UM VALOR"
        assert result["razao_social"] == "NOVO NOME"

    def test_invalid_merge_raises_validation_error(self) -> None:
        lead = self._lead()
        with pytest.raises(ValidationError):
            apply_enrichment_update(lead, {"razao_social": ""})


class TestMakeClients:
    def test_brasilapi_client_default_rate_limit(self, tmp_path: Path) -> None:
        client = make_brasilapi_client(cache_path=tmp_path / "c.sqlite")
        try:
            assert client._rate_limiter.min_interval_seconds == 1.0
        finally:
            client.close()

    def test_recherche_entreprises_client_default_rate_limit(self, tmp_path: Path) -> None:
        client = make_recherche_entreprises_client(cache_path=tmp_path / "c.sqlite")
        try:
            assert client._rate_limiter.min_interval_seconds == 0.1
        finally:
            client.close()

    def test_overrides_are_applied(self, tmp_path: Path) -> None:
        client = make_brasilapi_client(cache_path=tmp_path / "c.sqlite", min_interval_seconds=5.0)
        try:
            assert client._rate_limiter.min_interval_seconds == 5.0
        finally:
            client.close()


def _mock_client(handler: object, tmp_path: Path, **kwargs: object) -> EnrichmentClient:
    kwargs.setdefault("min_interval_seconds", 0)
    kwargs.setdefault("retry_wait_seconds", 0)
    kwargs.setdefault("cache_path", tmp_path / "cache.sqlite")
    return EnrichmentClient(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


class TestEnrichBrLeads:
    def test_enriches_a_found_cnpj(self, tmp_path: Path) -> None:
        cnpj = "33000167000101"

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == BRASILAPI_URL_TEMPLATE.format(id=cnpj)
            return httpx.Response(200, json=BRASILAPI_RESPONSE)

        with _mock_client(handler, tmp_path) as client:
            result = enrich_br_leads(client, [cnpj], audit_log_path=tmp_path / "audit.parquet")

        assert result[cnpj] is not None
        assert result[cnpj]["razao_social"] == "PETROLEO BRASILEIRO S A PETROBRAS"

    def test_not_found_cnpj_returns_none(self, tmp_path: Path) -> None:
        cnpj = "00000000000000"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"})

        with _mock_client(handler, tmp_path, max_attempts=1) as client:
            result = enrich_br_leads(client, [cnpj], audit_log_path=tmp_path / "audit.parquet")

        assert result[cnpj] is None

    def test_wires_retry_through_the_provider(self, tmp_path: Path) -> None:
        cnpj = "33000167000101"
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                return httpx.Response(500, json={})
            return httpx.Response(200, json=BRASILAPI_RESPONSE)

        with _mock_client(handler, tmp_path, max_attempts=5) as client:
            result = enrich_br_leads(client, [cnpj], audit_log_path=tmp_path / "audit.parquet")

        assert len(calls) == 2
        assert result[cnpj] is not None


class TestEnrichFrLeads:
    def test_enriches_a_found_siren(self, tmp_path: Path) -> None:
        siren = "503932568"

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == RECHERCHE_ENTREPRISES_URL_TEMPLATE.format(id=siren)
            return httpx.Response(200, json=RECHERCHE_ENTREPRISES_RESPONSE)

        with _mock_client(handler, tmp_path) as client:
            result = enrich_fr_leads(client, [siren], audit_log_path=tmp_path / "audit.parquet")

        assert result[siren] is not None
        assert result[siren]["razao_social"] == "CARREFOUR"

    def test_not_found_siren_returns_none_not_an_error(self, tmp_path: Path) -> None:
        siren = "999999999"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=RECHERCHE_ENTREPRISES_EMPTY_RESPONSE)

        with _mock_client(handler, tmp_path) as client:
            result = enrich_fr_leads(client, [siren], audit_log_path=tmp_path / "audit.parquet")

        assert result[siren] is None

    def test_multiple_sirens(self, tmp_path: Path) -> None:
        found = "503932568"
        not_found = "111111111"

        def handler(request: httpx.Request) -> httpx.Response:
            if found in str(request.url):
                return httpx.Response(200, json=RECHERCHE_ENTREPRISES_RESPONSE)
            return httpx.Response(200, json=RECHERCHE_ENTREPRISES_EMPTY_RESPONSE)

        with _mock_client(handler, tmp_path) as client:
            result = enrich_fr_leads(
                client, [found, not_found], audit_log_path=tmp_path / "audit.parquet"
            )

        assert result[found] is not None
        assert result[not_found] is None
