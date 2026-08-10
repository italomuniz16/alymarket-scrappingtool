"""Testes do mapeamento canônico BR (`etl/canonical.py`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.etl.canonical import FONTE_BR_RECEITA, map_estabelecimento_to_canonical

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
