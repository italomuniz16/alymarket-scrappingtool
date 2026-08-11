"""Testes do parser fr_sirene, com fixtures pequenos (3 linhas) de cada entidade:
StockUniteLegale e StockEtablissement — cobrindo especificamente os pontos do layout
oficial que exigem atenção: UTF-8 com acentuação real, cabeçalho com nomes de coluna
com espaço à direita (`coordonneeLambertAbscisseEtablissement `/`...Ordonnee...`),
datas ISO 8601, o campo `unitePurgeeUniteLegale` ("true"/vazio, não O/N), e sobretudo
o `STATUT DE DIFFUSION` (`statutDiffusionUniteLegale`/`Etablissement`) mapeado pra
`flag_difusao_restrita` — o filtro hard de compliance do projeto.

Também cobre a leitura direta de dentro do `.zip` baixado por `stock_download.py`
(sem depender de um extractor separado — ver docstring de `parser.py`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.ingestion.fr_sirene.parser import (
    ETABLISSEMENT_COLUMNS,
    UNITE_LEGALE_COLUMNS,
    RowLayoutError,
    detect_entity,
    parse,
    parse_etablissement,
    parse_unite_legale,
)

FIXTURES = Path(__file__).parent / "fixtures"
UNITE_LEGALE_CSV = FIXTURES / "fr_sirene_unitelegale_sample.csv"
ETABLISSEMENT_CSV = FIXTURES / "fr_sirene_etablissement_sample.csv"
UNITE_LEGALE_ZIP = FIXTURES / "fr_sirene_stockunitelegale_sample.zip"
MULTI_CSV_ZIP = FIXTURES / "fr_sirene_multi_csv_sample.zip"


def test_column_layouts_match_official_field_counts() -> None:
    # Layout oficial (dessin de fichier do INSEE, confirmado baixando o CSV real).
    assert len(UNITE_LEGALE_COLUMNS) == 35
    assert len(ETABLISSEMENT_COLUMNS) == 54


class TestDetectEntity:
    def test_recognizes_unite_legale_and_etablissement(self) -> None:
        assert detect_entity(Path("stock-stockunitelegale-csv.zip")) == "UNITE_LEGALE"
        assert detect_entity(Path("StockEtablissement_utf8.csv")) == "ETABLISSEMENT"

    def test_excludes_historique_and_succession_variants(self) -> None:
        assert detect_entity(Path("stock-stockunitelegalehistorique-csv.zip")) is None
        assert detect_entity(Path("stock-stocketablissementhistorique-csv.zip")) is None
        assert detect_entity(Path("stock-stocketablissementlienssuccession-csv.zip")) is None
        assert detect_entity(Path("stock-stockdoublons-csv.zip")) is None

    def test_unrecognized_file_returns_none(self) -> None:
        assert detect_entity(Path("outro-arquivo-qualquer.csv")) is None


class TestParseUniteLegale:
    def test_row_count_and_accented_text(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert len(records) == 3
        assert records[0]["razao_social"] == "BOULANGERIE DU MARCHÉ"
        assert records[2]["razao_social"] == "ANCIENNE SOCIÉTÉ FERMÉE"

    def test_entidade_tag(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert all(r["entidade"] == "UNITE_LEGALE" for r in records)

    def test_siren_stripped(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert [r["siren"] for r in records] == ["123456789", "987654321", "111222333"]

    def test_statut_diffusion_maps_to_flag_restrita(self) -> None:
        """O caso central pedido: `statutDiffusionUniteLegale` -> `flag_difusao_restrita`.
        `"O"` (aberto) -> não restrito; `"P"` (parcial) e vazio -> restrito por padrão
        (mesma convenção conservadora de `enrichment/providers.py`)."""
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))

        assert records[0]["statut_diffusion"] == "O"
        assert records[0]["flag_difusao_restrita"] is False

        assert records[1]["statut_diffusion"] == "P"
        assert records[1]["flag_difusao_restrita"] is True

        assert records[2]["statut_diffusion"] is None  # vazio no fixture
        assert records[2]["flag_difusao_restrita"] is True

    def test_dates_parsed_as_iso8601(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert records[0]["data_criacao"] == date(2015, 3, 12)
        assert records[1]["data_criacao"] == date(2019, 6, 1)

    def test_unite_purgee_true_or_blank_parsed_as_bool(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert records[0]["unite_purgee"] is False  # vazio no fixture
        assert records[1]["unite_purgee"] is True  # "true" no fixture

    def test_blank_fields_become_none(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        # Linha 1 é pessoa jurídica: campos de pessoa física ficam vazios.
        assert records[0]["sexo"] is None
        assert records[0]["prenome_1"] is None
        # Linha 2 é empresário individual: tem prenome/sexo, não tem denominação.
        assert records[1]["sexo"] == "F"
        assert records[1]["prenome_1"] == "MARIE"
        assert records[1]["razao_social"] is None

    def test_natureza_juridica_and_cod_atividade(self) -> None:
        records = list(parse_unite_legale(UNITE_LEGALE_CSV))
        assert records[0]["natureza_juridica"] == "5710"
        assert records[0]["cod_atividade"] == "10.71C"


class TestParseEtablissement:
    def test_row_count(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert len(records) == 3

    def test_entidade_tag(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert all(r["entidade"] == "ETABLISSEMENT" for r in records)

    def test_siren_nic_siret(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert records[0]["siren"] == "123456789"
        assert records[0]["nic"] == "00015"
        assert records[0]["siret"] == "12345678900015"

    def test_statut_diffusion_maps_to_flag_restrita(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))

        assert records[0]["statut_diffusion"] == "O"
        assert records[0]["flag_difusao_restrita"] is False

        assert records[1]["statut_diffusion"] == "P"
        assert records[1]["flag_difusao_restrita"] is True

        assert records[2]["statut_diffusion"] is None
        assert records[2]["flag_difusao_restrita"] is True

    def test_lambert_columns_read_despite_trailing_space_in_header(self) -> None:
        """O dessin de fichier oficial do INSEE tem um espaço à direita no NOME dessas
        duas colunas — confirmado baixando o CSV real (ver docstring de `parser.py`).
        Se a normalização (`.strip()`) do cabeçalho falhar, esses campos ficariam
        sempre `None` (chave não bate) em vez do valor real do fixture."""
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert records[0]["coordenada_lambert_x"] == "652345"
        assert records[0]["coordenada_lambert_y"] == "6862345"

    def test_accented_text(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert records[0]["logradouro"] == "DU MARCHÉ"
        assert records[2]["nome_fantasia"] == "ANCIENNE SOCIÉTÉ FERMÉE"

    def test_endereco_fields(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert records[0]["cep"] == "75001"
        assert records[0]["municipio"] == "PARIS"

    def test_data_criacao_parsed_as_iso8601(self) -> None:
        records = list(parse_etablissement(ETABLISSEMENT_CSV))
        assert records[0]["data_criacao"] == date(2015, 3, 12)


class TestParseFromZip:
    def test_reads_csv_directly_from_zip(self) -> None:
        """`stock_download.py` baixa `.zip` (não descompacta) -- o parser precisa
        conseguir ler o CSV de dentro dele sem um extractor separado."""
        records = list(parse_unite_legale(UNITE_LEGALE_ZIP))
        assert len(records) == 3
        assert records[0]["razao_social"] == "BOULANGERIE DU MARCHÉ"

    def test_zip_with_more_than_one_csv_raises(self) -> None:
        with pytest.raises(RowLayoutError):
            list(parse_unite_legale(MULTI_CSV_ZIP))


class TestParseDispatch:
    def test_parse_routes_by_filename_and_skips_unrecognized(self, tmp_path: Path) -> None:
        unrecognized = tmp_path / "StockUniteLegaleHistorique_utf8.csv"
        unrecognized.write_text("siren\n123\n", encoding="utf-8")

        records = list(parse([UNITE_LEGALE_CSV, ETABLISSEMENT_CSV, unrecognized]))

        entidades = {r["entidade"] for r in records}
        assert entidades == {"UNITE_LEGALE", "ETABLISSEMENT"}
        assert len(records) == 6  # 3 + 3, arquivo não reconhecido ignorado


class TestHeaderValidation:
    def test_missing_expected_column_raises_row_layout_error(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "stockunitelegale_bad.csv"
        bad_csv.write_text("siren,foo\n123456789,bar\n", encoding="utf-8")

        with pytest.raises(RowLayoutError):
            list(parse_unite_legale(bad_csv))
