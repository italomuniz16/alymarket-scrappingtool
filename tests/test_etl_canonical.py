"""Testes do mapeamento canônico BR e FR (`etl/canonical.py`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.etl.canonical import (
    FONTE_BR_RECEITA,
    FONTE_FR_SIRENE,
    map_estabelecimento_to_canonical,
    map_unite_legale_etablissement_to_canonical,
)

FULL_RECORD = {
    "cnpj_basico": "11111111",
    "cnpj_completo": "11111111000191",
    "nome_fantasia": "PADARIA SÃO JOÃO",
    "situacao_cadastral": "02",
    "data_inicio_atividade": date(2020, 1, 1),
    "cnae_fiscal_principal": "4721102",
    "uf": "SP",
    "municipio_codigo": "7107",
    "municipio_descricao": "SAO PAULO",
    "cep": "01310100",
    "ddd_1": "011",
    "telefone_1": "22334455",
    "correio_eletronico": "padaria@example.com",
    "razao_social": "PADARIA SÃO JOÃO LTDA",
    "natureza_juridica_codigo": "2062",
    "natureza_juridica_descricao": "SOCIEDADE EMPRESARIA LIMITADA",
    "capital_social": Decimal("150000.00"),
    "porte_empresa": "03",
    "opcao_pelo_simples": "S",
    "opcao_pelo_mei": "N",
}


def test_maps_full_record() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)

    assert lead["pais"] == "BR"
    assert lead["id_legal"] == "11111111"
    assert lead["id_estab"] == "11111111000191"
    assert lead["razao_social"] == "PADARIA SÃO JOÃO LTDA"
    assert lead["nome_fantasia"] == "PADARIA SÃO JOÃO"
    assert lead["cod_atividade"] == "4721102"
    assert lead["regiao"] == "SP"
    assert lead["cep"] == "01310100"
    assert lead["email"] == "padaria@example.com"
    assert lead["data_inicio_atividade"] == date(2020, 1, 1)
    assert lead["capital_social"] == Decimal("150000.00")


def test_situacao_code_resolved_to_label() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)
    assert lead["situacao"] == "ATIVA"


def test_porte_code_resolved_to_label() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)
    assert lead["porte"] == "EMPRESA DE PEQUENO PORTE"


def test_telefone_composed_from_ddd_and_numero() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)
    assert lead["telefone"] == "01122334455"


def test_telefone_none_when_both_blank() -> None:
    record = {**FULL_RECORD, "ddd_1": None, "telefone_1": None}
    lead = map_estabelecimento_to_canonical(record)
    assert lead["telefone"] is None


def test_municipio_uses_lookup_description_when_available() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)
    assert lead["municipio"] == "SAO PAULO"


def test_municipio_falls_back_to_raw_code_when_lookup_missing() -> None:
    record = {**FULL_RECORD, "municipio_descricao": None}
    lead = map_estabelecimento_to_canonical(record)
    assert lead["municipio"] == "7107"


def test_natureza_juridica_uses_lookup_description_when_available() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)
    assert lead["natureza_juridica"] == "SOCIEDADE EMPRESARIA LIMITADA"


def test_natureza_juridica_falls_back_to_raw_code_when_lookup_missing() -> None:
    record = {**FULL_RECORD, "natureza_juridica_descricao": None}
    lead = map_estabelecimento_to_canonical(record)
    assert lead["natureza_juridica"] == "2062"


def test_unknown_situacao_code_falls_back_to_raw_code_without_crashing() -> None:
    record = {**FULL_RECORD, "situacao_cadastral": "99"}
    lead = map_estabelecimento_to_canonical(record)
    assert lead["situacao"] == "99"


def test_constants_always_set() -> None:
    lead = map_estabelecimento_to_canonical(FULL_RECORD)

    assert lead["fonte"] == FONTE_BR_RECEITA == "BR_RECEITA"
    assert lead["is_synthetic"] is False
    assert lead["flag_difusao_restrita"] is False
    assert lead["score_icp"] is None
    assert lead["enriquecido_em"] is None


def test_missing_razao_social_raises_validation_error() -> None:
    record = {**FULL_RECORD, "razao_social": None}
    with pytest.raises(ValidationError):
        map_estabelecimento_to_canonical(record)


def test_missing_simples_data_does_not_break_mapping() -> None:
    """Estabelecimento sem opção pelo Simples/MEI (LEFT JOIN sem match) ainda mapeia."""
    record = {**FULL_RECORD, "opcao_pelo_simples": None, "opcao_pelo_mei": None}
    lead = map_estabelecimento_to_canonical(record)
    assert lead["id_legal"] == "11111111"


# -- FR (SIRENE) ------------------------------------------------------------------

FULL_UNITE_LEGALE = {
    "entidade": "UNITE_LEGALE",
    "siren": "123456789",
    "statut_diffusion": "O",
    "flag_difusao_restrita": False,
    "situacao": "A",
    "razao_social": "BOULANGERIE DU MARCHÉ",
    "nome_fantasia": None,
    "sigla": None,
    "nome": None,
    "prenome_1": None,
    "prenome_usual": None,
    "natureza_juridica": "5710",
    "cod_atividade": "10.71C",
    "categoria_empresa": "PME",
    "data_criacao": date(2015, 3, 12),
}

FULL_ETABLISSEMENT = {
    "entidade": "ETABLISSEMENT",
    "siren": "123456789",
    "siret": "12345678900015",
    "statut_diffusion": "O",
    "flag_difusao_restrita": False,
    "situacao": "A",
    "nome_fantasia": "BOULANGERIE DU MARCHÉ - LOJA CENTRO",
    "cod_atividade": "10.71C",
    "municipio": "PARIS",
    "cep": "75001",
    "data_criacao": date(2015, 3, 12),
}


def test_maps_full_pair() -> None:
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)

    assert lead["pais"] == "FR"
    assert lead["id_legal"] == "123456789"
    assert lead["id_estab"] == "12345678900015"
    assert lead["razao_social"] == "BOULANGERIE DU MARCHÉ"
    assert lead["cod_atividade"] == "10.71C"
    assert lead["municipio"] == "PARIS"
    assert lead["cep"] == "75001"
    assert lead["natureza_juridica"] == "5710"
    assert lead["data_inicio_atividade"] == date(2015, 3, 12)


def test_etat_administratif_translated_to_label() -> None:
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)
    assert lead["situacao"] == "ATIVA"

    fechada = {**FULL_ETABLISSEMENT, "situacao": "F"}
    lead_fechada = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, fechada)
    assert lead_fechada["situacao"] == "BAIXADA"


def test_regiao_derived_from_cep() -> None:
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)
    assert lead["regiao"] == "75"


def test_regiao_none_when_cep_missing() -> None:
    record = {**FULL_ETABLISSEMENT, "cep": None}
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, record)
    assert lead["regiao"] is None


def test_nome_fantasia_prefers_etablissement_over_unite_legale() -> None:
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)
    assert lead["nome_fantasia"] == "BOULANGERIE DU MARCHÉ - LOJA CENTRO"


def test_nome_fantasia_falls_back_to_unite_legale_sigla() -> None:
    unite_legale = {**FULL_UNITE_LEGALE, "sigla": "BDM"}
    etablissement = {**FULL_ETABLISSEMENT, "nome_fantasia": None}
    lead = map_unite_legale_etablissement_to_canonical(unite_legale, etablissement)
    assert lead["nome_fantasia"] == "BDM"


def test_cod_atividade_prefers_etablissement_over_unite_legale() -> None:
    unite_legale = {**FULL_UNITE_LEGALE, "cod_atividade": "62.01Z"}
    etablissement = {**FULL_ETABLISSEMENT, "cod_atividade": "10.71C"}
    lead = map_unite_legale_etablissement_to_canonical(unite_legale, etablissement)
    assert lead["cod_atividade"] == "10.71C"


def test_razao_social_falls_back_to_person_name_for_individual_entrepreneur() -> None:
    """Empresário individual (personne physique): denominationUniteLegale vazio,
    nome vem em nome/prenome separados."""
    unite_legale = {
        **FULL_UNITE_LEGALE,
        "razao_social": None,
        "nome": "DUPONT",
        "prenome_usual": "MARIE",
    }
    lead = map_unite_legale_etablissement_to_canonical(unite_legale, FULL_ETABLISSEMENT)
    assert lead["razao_social"] == "MARIE DUPONT"


def test_capital_social_always_none() -> None:
    """Não existe capital social no arquivo de stock -- só via enriquecimento sob
    demanda (enrichment/providers.py)."""
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)
    assert lead["capital_social"] is None


def test_constants_always_set_fr() -> None:
    lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)

    assert lead["fonte"] == FONTE_FR_SIRENE == "FR_SIRENE"
    assert lead["is_synthetic"] is False
    assert lead["telefone"] is None
    assert lead["email"] is None
    assert lead["score_icp"] is None
    assert lead["enriquecido_em"] is None


def test_missing_razao_social_and_person_name_raises_validation_error() -> None:
    unite_legale = {**FULL_UNITE_LEGALE, "razao_social": None, "nome": None, "prenome_usual": None}
    with pytest.raises(ValidationError):
        map_unite_legale_etablissement_to_canonical(unite_legale, FULL_ETABLISSEMENT)


class TestFlagDifusaoRestrita:
    """CRÍTICO (ver CLAUDE.md): statut_diffusion diferente de "O", em QUALQUER um dos
    dois lados, tem que virar flag_difusao_restrita=True -- nunca só quando os dois
    concordam."""

    def test_both_open_is_not_restricted(self) -> None:
        lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, FULL_ETABLISSEMENT)
        assert lead["flag_difusao_restrita"] is False

    def test_unite_legale_diffusion_partielle_sets_flag(self) -> None:
        unite_legale = {**FULL_UNITE_LEGALE, "statut_diffusion": "P", "flag_difusao_restrita": True}
        lead = map_unite_legale_etablissement_to_canonical(unite_legale, FULL_ETABLISSEMENT)
        assert lead["flag_difusao_restrita"] is True

    def test_etablissement_diffusion_partielle_sets_flag(self) -> None:
        etablissement = {
            **FULL_ETABLISSEMENT,
            "statut_diffusion": "P",
            "flag_difusao_restrita": True,
        }
        lead = map_unite_legale_etablissement_to_canonical(FULL_UNITE_LEGALE, etablissement)
        assert lead["flag_difusao_restrita"] is True

    def test_missing_statut_diffusion_defaults_to_restricted(self) -> None:
        """Ausência de statut_diffusion é tratada como restrito por padrão
        (conservador) -- mesma convenção de enrichment/providers.py."""
        unite_legale = {
            **FULL_UNITE_LEGALE,
            "statut_diffusion": None,
            "flag_difusao_restrita": True,
        }
        lead = map_unite_legale_etablissement_to_canonical(unite_legale, FULL_ETABLISSEMENT)
        assert lead["flag_difusao_restrita"] is True
