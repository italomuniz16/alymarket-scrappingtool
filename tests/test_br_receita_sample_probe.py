"""Testes do probe de validação de parsing do conector br_receita.

Usa um fixture pequeno (4 linhas) no layout de 30 colunas do ESTABELE, gravado em
bytes ISO-8859-1 de verdade, provando que a leitura latin-1 -> UTF-8 preserva
acentuação e que aspas duplas como qualificador funcionam de fato (campo com ';'
embutido entre aspas).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ingestion.br_receita.sample_probe import probe_csv

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "br_receita_estabele_sample.csv"


def test_probe_csv_shape() -> None:
    result = probe_csv(FIXTURE_PATH)

    assert result.n_rows == 4
    assert result.n_cols == 30


def test_probe_csv_accented_characters_decoded_correctly() -> None:
    result = probe_csv(FIXTURE_PATH)

    nome_fantasia = [row[4] for row in result.sample_rows]
    logradouro = [row[14] for row in result.sample_rows]

    assert "PADARIA SÃO JOÃO" in nome_fantasia
    assert "PANIFICADORA IRMÃOS AÇÚCAR & CAFÉ" in nome_fantasia
    assert "COMÉRCIO DE CONFECÇÕES LTDA" in nome_fantasia
    assert "RUA DAS ACÁCIAS" in logradouro
    assert "PRAÇA DA LIBERDADE" in logradouro


def test_probe_csv_quoted_field_with_embedded_delimiter() -> None:
    result = probe_csv(FIXTURE_PATH)

    nome_fantasia = [row[4] for row in result.sample_rows]

    # Campo original no CSV: "CONFEITARIA ""AÇÚCAR""; CAFÉ E CIA" (entre aspas, com
    # aspas escapadas e um ';' embutido). Deve virar UM único valor, não quebrar em
    # colunas extras, e as aspas duplicadas devem virar aspas literais.
    assert 'CONFEITARIA "AÇÚCAR"; CAFÉ E CIA' in nome_fantasia


def test_probe_csv_non_null_counts() -> None:
    result = probe_csv(FIXTURE_PATH)

    # column08 = "nome da cidade no exterior": vazio nas 4 linhas.
    assert result.non_null_counts["column08"] == 0
    # column27 = e-mail: presente em 3 das 4 linhas (uma linha baixada, sem contato).
    assert result.non_null_counts["column27"] == 3
    # column00 = CNPJ básico: sempre presente.
    assert result.non_null_counts["column00"] == 4


def test_probe_csv_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        probe_csv(FIXTURE_PATH.parent / "nao_existe.csv")
