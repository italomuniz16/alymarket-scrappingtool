"""Testes do parser br_receita, com fixtures pequenos (3 linhas) de cada entidade:
EMPRESAS, ESTABELECIMENTOS, SIMPLES, e das tabelas auxiliares (CNAE, município,
natureza jurídica). Os fixtures são gravados em bytes ISO-8859-1 reais (ver
`.gitattributes`), cobrindo especificamente os pontos do layout oficial que exigem
normalização: capital social com vírgula decimal, CNAE fiscal secundária multivalorada
(separada por vírgula), campo NÚMERO podendo ser literalmente "S/N", datas ausentes
(`00000000`) e acentuação.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.ingestion.br_receita.parser import (
    EMPRESAS_COLUMNS,
    ESTABELECIMENTOS_COLUMNS,
    SIMPLES_COLUMNS,
    detect_entity,
    enrich_with_lookups,
    load_lookup_table,
    parse,
    parse_empresas,
    parse_estabelecimentos,
    parse_simples,
)

FIXTURES = Path(__file__).parent / "fixtures"
EMPRESAS_CSV = FIXTURES / "br_receita_empresas_sample.csv"
ESTABELECIMENTOS_CSV = FIXTURES / "br_receita_estabelecimentos_sample.csv"
SIMPLES_CSV = FIXTURES / "br_receita_simples_sample.csv"
CNAE_LOOKUP_CSV = FIXTURES / "br_receita_cnae_lookup_sample.csv"
MUNICIPIO_LOOKUP_CSV = FIXTURES / "br_receita_municipio_lookup_sample.csv"
NATUREZA_JURIDICA_LOOKUP_CSV = FIXTURES / "br_receita_natureza_juridica_lookup_sample.csv"


def test_column_layouts_match_official_field_counts() -> None:
    # Layout oficial (gov.br/receitafederal/dados/cnpj-metadados.pdf).
    assert len(EMPRESAS_COLUMNS) == 7
    assert len(ESTABELECIMENTOS_COLUMNS) == 30
    assert len(SIMPLES_COLUMNS) == 7


class TestParseEmpresas:
    def test_row_count_and_accented_text(self) -> None:
        records = list(parse_empresas(EMPRESAS_CSV))
        assert len(records) == 3
        assert records[0]["razao_social"] == "PADARIA SÃO JOÃO LTDA"
        assert records[1]["razao_social"] == "COMÉRCIO DE CONFECÇÕES LTDA"

    def test_capital_social_parsed_as_decimal_with_comma(self) -> None:
        records = list(parse_empresas(EMPRESAS_CSV))
        assert records[0]["capital_social"] == Decimal("150000.00")
        assert records[1]["capital_social"] == Decimal("5000.50")
        assert records[2]["capital_social"] == Decimal("0.00")

    def test_ente_federativo_responsavel_blank_vs_filled(self) -> None:
        records = list(parse_empresas(EMPRESAS_CSV))
        assert records[0]["ente_federativo_responsavel"] is None
        assert records[2]["ente_federativo_responsavel"] == "MUNICIPIO DE TESTE - SP"

    def test_entidade_marker(self) -> None:
        records = list(parse_empresas(EMPRESAS_CSV))
        assert all(r["entidade"] == "EMPRESAS" for r in records)


class TestParseEstabelecimentos:
    def test_row_count(self) -> None:
        assert len(list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))) == 3

    def test_cnpj_completo_is_concatenated(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[0]["cnpj_completo"] == "11111111000191"

    def test_cnae_fiscal_secundaria_splits_multiple_values(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[0]["cnae_fiscal_secundaria"] == ["4722901", "4729699"]
        # Sem ocorrência secundária -> lista vazia, não None.
        assert records[1]["cnae_fiscal_secundaria"] == []

    def test_numero_can_be_literal_sn(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[2]["numero"] == "S/N"

    def test_dates_parsed_and_zero_date_is_none(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[0]["data_inicio_atividade"] == date(2020, 1, 1)
        assert records[0]["data_situacao_cadastral"] == date(2020, 1, 15)
        # "00000000" no último registro (data_situacao_especial) -> None.
        assert records[2]["data_situacao_especial"] is None

    def test_blank_contact_fields_are_none(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[1]["correio_eletronico"] is None
        assert records[1]["ddd_1"] is None

    def test_matriz_filial_identifier(self) -> None:
        records = list(parse_estabelecimentos(ESTABELECIMENTOS_CSV))
        assert records[0]["identificador_matriz_filial"] == "1"
        assert records[2]["identificador_matriz_filial"] == "2"


class TestParseSimples:
    def test_row_count(self) -> None:
        assert len(list(parse_simples(SIMPLES_CSV))) == 3

    def test_opcao_flags_and_dates(self) -> None:
        records = list(parse_simples(SIMPLES_CSV))
        assert records[0]["opcao_pelo_simples"] == "S"
        assert records[0]["data_opcao_pelo_simples"] == date(2018, 1, 1)
        assert records[0]["opcao_pelo_mei"] == "N"

    def test_blank_opcao_is_none_not_empty_string(self) -> None:
        records = list(parse_simples(SIMPLES_CSV))
        assert records[2]["opcao_pelo_simples"] is None
        assert records[2]["opcao_pelo_mei"] is None


class TestMalformedRows:
    def test_row_with_wrong_field_count_is_skipped_not_raised(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        # Segunda linha tem só 3 campos (esperado: 7 para EMPRESAS) -> deve ser
        # pulada com warning, sem derrubar o parsing das linhas boas.
        bad_csv.write_text(
            "11111111;EMPRESA BOA;2062;49;100,00;01;\n"
            "22222222;LINHA QUEBRADA;2062\n"
            "33333333;OUTRA EMPRESA BOA;2062;49;200,00;01;\n",
            encoding="latin-1",
        )
        records = list(parse_empresas(bad_csv))
        assert [r["cnpj_basico"] for r in records] == ["11111111", "33333333"]

    def test_blank_line_is_skipped_in_estabelecimentos(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        good_row = ";".join(["1"] * 30)
        bad_csv.write_text(f"{good_row}\n\n{good_row}\n", encoding="latin-1")

        records = list(parse_estabelecimentos(bad_csv))

        assert len(records) == 2

    def test_row_with_wrong_field_count_is_skipped_in_simples(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text(
            "11111111;S;20180101;;N;;\n"
            "22222222;LINHA;QUEBRADA\n"
            "33333333;N;;;S;20190601;\n",
            encoding="latin-1",
        )

        records = list(parse_simples(bad_csv))

        assert [r["cnpj_basico"] for r in records] == ["11111111", "33333333"]

    def test_invalid_date_is_ignored_as_none(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("11111111;S;NAO-E-UMA-DATA;;N;;\n", encoding="latin-1")

        [record] = list(parse_simples(bad_csv))

        assert record["data_opcao_pelo_simples"] is None

    def test_invalid_capital_social_is_ignored_as_none(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("11111111;EMPRESA;2062;49;NAO-E-NUMERO;01;\n", encoding="latin-1")

        [record] = list(parse_empresas(bad_csv))

        assert record["capital_social"] is None

    def test_blank_capital_social_is_none(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("11111111;EMPRESA;2062;49;;01;\n", encoding="latin-1")

        [record] = list(parse_empresas(bad_csv))

        assert record["capital_social"] is None


class TestDetectEntityAndDispatch:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("K3241.K03200Y0.D50812.EMPRECSV", "EMPRESAS"),
            ("K3241.K03200Y1.D50812.ESTABELE", "ESTABELECIMENTOS"),
            ("K3241.K03200Y2.D50812.SIMPLES.CSV.D50812.SIMPLES", "SIMPLES"),
            ("K3241.K03200Y3.D50812.SOCIOCSV", None),
            ("K3241.K03200Y4.D50812.CNAECSV", None),
        ],
    )
    def test_detect_entity(self, filename: str, expected: str | None) -> None:
        assert detect_entity(Path(filename)) == expected

    def test_parse_dispatches_by_filename_and_skips_unrecognized(
        self, tmp_path: Path
    ) -> None:
        empresas_copy = tmp_path / "K3241.EMPRECSV"
        empresas_copy.write_bytes(EMPRESAS_CSV.read_bytes())
        estab_copy = tmp_path / "K3241.ESTABELE"
        estab_copy.write_bytes(ESTABELECIMENTOS_CSV.read_bytes())
        unrecognized = tmp_path / "K3241.SOCIOCSV"
        unrecognized.write_text("lixo;nao;deve;aparecer\n", encoding="latin-1")

        records = list(parse([empresas_copy, estab_copy, unrecognized]))

        entidades = {r["entidade"] for r in records}
        assert entidades == {"EMPRESAS", "ESTABELECIMENTOS"}
        assert len(records) == 3 + 3  # 3 empresas + 3 estabelecimentos


class TestLookups:
    def test_load_lookup_table(self) -> None:
        cnae = load_lookup_table(CNAE_LOOKUP_CSV)
        assert cnae["4721102"] == "COMERCIO VAREJISTA DE PADARIA"
        assert len(cnae) == 3

    def test_load_lookup_table_skips_rows_with_fewer_than_two_fields(
        self, tmp_path: Path
    ) -> None:
        bad_csv = tmp_path / "bad_lookup.csv"
        bad_csv.write_text(
            "4721102;PADARIA\nCODIGO-SEM-DESCRICAO\n4722901;ACOUGUE\n", encoding="latin-1"
        )

        lookup = load_lookup_table(bad_csv)

        assert lookup == {"4721102": "PADARIA", "4722901": "ACOUGUE"}

    def test_enrich_with_lookups_adds_descriptions(self) -> None:
        cnae = load_lookup_table(CNAE_LOOKUP_CSV)
        municipio = load_lookup_table(MUNICIPIO_LOOKUP_CSV)

        records = parse_estabelecimentos(ESTABELECIMENTOS_CSV)
        [record] = [r for r in records if r["cnpj_basico"] == "11111111"]
        enriched = enrich_with_lookups(record, cnae=cnae, municipio=municipio)

        assert enriched["cnae_fiscal_principal_descricao"] == "COMERCIO VAREJISTA DE PADARIA"
        assert enriched["municipio_descricao"] == "SAO PAULO"
        # Não modifica o original.
        assert "cnae_fiscal_principal_descricao" not in record

    def test_enrich_with_lookups_natureza_juridica_for_empresas(self) -> None:
        natureza = load_lookup_table(NATUREZA_JURIDICA_LOOKUP_CSV)
        [record] = [r for r in parse_empresas(EMPRESAS_CSV) if r["cnpj_basico"] == "33333333"]

        enriched = enrich_with_lookups(record, natureza_juridica=natureza)

        assert enriched["natureza_juridica_descricao"] == "ADMINISTRACAO PUBLICA MUNICIPAL"

    def test_enrich_with_lookups_without_lookup_adds_nothing(self) -> None:
        [record] = [r for r in parse_empresas(EMPRESAS_CSV) if r["cnpj_basico"] == "11111111"]
        enriched = enrich_with_lookups(record)
        assert enriched == record

    def test_enrich_with_lookups_unknown_code_returns_none_description(self) -> None:
        cnae = load_lookup_table(CNAE_LOOKUP_CSV)
        record = {"entidade": "ESTABELECIMENTOS", "cnae_fiscal_principal": "0000000"}

        enriched = enrich_with_lookups(record, cnae=cnae)

        assert "cnae_fiscal_principal_descricao" in enriched
        assert enriched["cnae_fiscal_principal_descricao"] is None
