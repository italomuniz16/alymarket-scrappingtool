"""Parsing dos CSVs da Receita Federal (encoding latin-1, sem cabeçalho, separador ';').

Aplica os layouts oficiais de coluna documentados em
https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf ("Novo Layout para os
DADOS ABERTOS do CNPJ") para as três entidades cobertas nesta fase: `EMPRECSV`
(empresas), `ESTABELE` (estabelecimentos) e `SIMPLES` (Simples Nacional/MEI). As
tabelas auxiliares (CNAE, município, natureza jurídica, ...) têm um layout uniforme
de 2 colunas (`codigo;descricao`) e são tratadas como lookups (`load_lookup_table`),
não como registros — use `enrich_with_lookups` para resolver descrições sob demanda.

Diferente de `sample_probe.py` (que usa DuckDB para estatísticas agregadas), aqui o
parsing é feito com o módulo `csv` da stdlib sobre um arquivo aberto em modo texto com
`encoding="latin-1"`: é um generator que não carrega o arquivo inteiro em memória, o
que importa para os arquivos reais (~30-40 GB descomprimidos, ver PRD §3.1).

Este módulo cobre só o parsing (`SourceConnector.parse`) — o join entre entidades por
`cnpj_basico` e o mapeamento para `CanonicalLead` ficam em `etl/transform.py` e
`etl/canonical.py` (Fase 1, ainda não implementados).
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# -- Layouts oficiais (ordem exata das colunas, sem cabeçalho no CSV) --------------

EMPRESAS_COLUMNS: tuple[str, ...] = (
    "cnpj_basico",
    "razao_social",
    "natureza_juridica",
    "qualificacao_responsavel",
    "capital_social",
    "porte_empresa",
    "ente_federativo_responsavel",
)

ESTABELECIMENTOS_COLUMNS: tuple[str, ...] = (
    "cnpj_basico",
    "cnpj_ordem",
    "cnpj_dv",
    "identificador_matriz_filial",
    "nome_fantasia",
    "situacao_cadastral",
    "data_situacao_cadastral",
    "motivo_situacao_cadastral",
    "nome_cidade_exterior",
    "pais",
    "data_inicio_atividade",
    "cnae_fiscal_principal",
    "cnae_fiscal_secundaria",
    "tipo_logradouro",
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "cep",
    "uf",
    "municipio",
    "ddd_1",
    "telefone_1",
    "ddd_2",
    "telefone_2",
    "ddd_fax",
    "fax",
    "correio_eletronico",
    "situacao_especial",
    "data_situacao_especial",
)

SIMPLES_COLUMNS: tuple[str, ...] = (
    "cnpj_basico",
    "opcao_pelo_simples",
    "data_opcao_pelo_simples",
    "data_exclusao_do_simples",
    "opcao_pelo_mei",
    "data_opcao_pelo_mei",
    "data_exclusao_do_mei",
)

# Marcador no nome do arquivo (convenção da Receita: os CSVs extraídos dos ZIPs têm
# nomes como "K3241.K03200Y0.D50812.ESTABELE", sem extensão) -> entidade reconhecida.
_ENTITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("EMPRECSV", "EMPRESAS"),
    ("ESTABELE", "ESTABELECIMENTOS"),
    ("SIMPLES", "SIMPLES"),
)


class RowLayoutError(ValueError):
    """Levantado quando uma linha não tem o número de campos esperado pelo layout."""


# -- Detecção de entidade por nome de arquivo ----------------------------------------


def detect_entity(path: Path) -> str | None:
    """Identifica a entidade (`"EMPRESAS"`, `"ESTABELECIMENTOS"`, `"SIMPLES"`) pelo
    nome do arquivo. Retorna `None` para arquivos não reconhecidos (ex.: `SOCIOCSV`,
    tabelas auxiliares) — esses devem ser tratados via `load_lookup_table`, não `parse`.
    """
    name_upper = path.name.upper()
    for marker, entity in _ENTITY_MARKERS:
        if marker in name_upper:
            return entity
    return None


# -- Normalização de valores ---------------------------------------------------------


def _none_if_blank(value: str) -> str | None:
    value = value.strip()
    return value or None


def _parse_date(value: str) -> date | None:
    """Datas no layout vêm como `AAAAMMDD`; vazio ou `00000000`/`0` significam ausente."""
    value = value.strip()
    if not value or value in {"0", "00000000"}:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        logger.warning("Data inválida ignorada (mantida como None): %r", value)
        return None


def _parse_capital_social(value: str) -> Decimal | None:
    """Capital social vem com vírgula como separador decimal (padrão BR), ex. '1500,00'."""
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        logger.warning("Capital social inválido ignorado (mantido como None): %r", value)
        return None


def _split_cnae_secundaria(value: str) -> list[str]:
    """CNAE fiscal secundária: múltiplas ocorrências separadas por vírgula (PRD/layout)."""
    value = value.strip()
    return [codigo.strip() for codigo in value.split(",") if codigo.strip()] if value else []


# -- Leitura bruta ---------------------------------------------------------------


def _read_raw_rows(path: Path) -> Iterator[list[str]]:
    """Itera as linhas de um CSV da Receita: latin-1, ';', sem cabeçalho, aspas duplas."""
    with path.open("r", encoding="latin-1", newline="") as f:
        yield from csv.reader(f, delimiter=";", quotechar='"')


def _row_to_values(row: list[str], columns: tuple[str, ...]) -> dict[str, str]:
    if len(row) != len(columns):
        raise RowLayoutError(
            f"Linha com {len(row)} campo(s), esperado {len(columns)} pelo layout: {row!r}"
        )
    return dict(zip(columns, row, strict=True))


# -- Parsers por entidade ---------------------------------------------------------


def parse_empresas(path: Path) -> Iterator[dict[str, Any]]:
    """Faz o parsing de um arquivo `EMPRECSV` (layout `EMPRESAS_COLUMNS`, 7 campos)."""
    for row in _read_raw_rows(path):
        if not row:
            continue
        try:
            v = _row_to_values(row, EMPRESAS_COLUMNS)
        except RowLayoutError as exc:
            logger.warning("Linha ignorada em %s: %s", path.name, exc)
            continue

        yield {
            "entidade": "EMPRESAS",
            "cnpj_basico": v["cnpj_basico"].strip(),
            "razao_social": _none_if_blank(v["razao_social"]),
            "natureza_juridica": _none_if_blank(v["natureza_juridica"]),
            "qualificacao_responsavel": _none_if_blank(v["qualificacao_responsavel"]),
            "capital_social": _parse_capital_social(v["capital_social"]),
            "porte_empresa": _none_if_blank(v["porte_empresa"]),
            "ente_federativo_responsavel": _none_if_blank(v["ente_federativo_responsavel"]),
        }


def parse_estabelecimentos(path: Path) -> Iterator[dict[str, Any]]:
    """Faz o parsing de um arquivo `ESTABELE` (layout `ESTABELECIMENTOS_COLUMNS`, 30 campos)."""
    for row in _read_raw_rows(path):
        if not row:
            continue
        try:
            v = _row_to_values(row, ESTABELECIMENTOS_COLUMNS)
        except RowLayoutError as exc:
            logger.warning("Linha ignorada em %s: %s", path.name, exc)
            continue

        cnpj_basico = v["cnpj_basico"].strip()
        cnpj_ordem = v["cnpj_ordem"].strip()
        cnpj_dv = v["cnpj_dv"].strip()

        yield {
            "entidade": "ESTABELECIMENTOS",
            "cnpj_basico": cnpj_basico,
            "cnpj_ordem": cnpj_ordem,
            "cnpj_dv": cnpj_dv,
            "cnpj_completo": cnpj_basico + cnpj_ordem + cnpj_dv,
            "identificador_matriz_filial": _none_if_blank(v["identificador_matriz_filial"]),
            "nome_fantasia": _none_if_blank(v["nome_fantasia"]),
            "situacao_cadastral": _none_if_blank(v["situacao_cadastral"]),
            "data_situacao_cadastral": _parse_date(v["data_situacao_cadastral"]),
            "motivo_situacao_cadastral": _none_if_blank(v["motivo_situacao_cadastral"]),
            "nome_cidade_exterior": _none_if_blank(v["nome_cidade_exterior"]),
            "pais": _none_if_blank(v["pais"]),
            "data_inicio_atividade": _parse_date(v["data_inicio_atividade"]),
            "cnae_fiscal_principal": _none_if_blank(v["cnae_fiscal_principal"]),
            "cnae_fiscal_secundaria": _split_cnae_secundaria(v["cnae_fiscal_secundaria"]),
            "tipo_logradouro": _none_if_blank(v["tipo_logradouro"]),
            "logradouro": _none_if_blank(v["logradouro"]),
            "numero": _none_if_blank(v["numero"]),
            "complemento": _none_if_blank(v["complemento"]),
            "bairro": _none_if_blank(v["bairro"]),
            "cep": _none_if_blank(v["cep"]),
            "uf": _none_if_blank(v["uf"]),
            "municipio": _none_if_blank(v["municipio"]),
            "ddd_1": _none_if_blank(v["ddd_1"]),
            "telefone_1": _none_if_blank(v["telefone_1"]),
            "ddd_2": _none_if_blank(v["ddd_2"]),
            "telefone_2": _none_if_blank(v["telefone_2"]),
            "ddd_fax": _none_if_blank(v["ddd_fax"]),
            "fax": _none_if_blank(v["fax"]),
            "correio_eletronico": _none_if_blank(v["correio_eletronico"]),
            "situacao_especial": _none_if_blank(v["situacao_especial"]),
            "data_situacao_especial": _parse_date(v["data_situacao_especial"]),
        }


def parse_simples(path: Path) -> Iterator[dict[str, Any]]:
    """Faz o parsing de um arquivo `SIMPLES` (layout `SIMPLES_COLUMNS`, 7 campos)."""
    for row in _read_raw_rows(path):
        if not row:
            continue
        try:
            v = _row_to_values(row, SIMPLES_COLUMNS)
        except RowLayoutError as exc:
            logger.warning("Linha ignorada em %s: %s", path.name, exc)
            continue

        yield {
            "entidade": "SIMPLES",
            "cnpj_basico": v["cnpj_basico"].strip(),
            "opcao_pelo_simples": _none_if_blank(v["opcao_pelo_simples"]),
            "data_opcao_pelo_simples": _parse_date(v["data_opcao_pelo_simples"]),
            "data_exclusao_do_simples": _parse_date(v["data_exclusao_do_simples"]),
            "opcao_pelo_mei": _none_if_blank(v["opcao_pelo_mei"]),
            "data_opcao_pelo_mei": _parse_date(v["data_opcao_pelo_mei"]),
            "data_exclusao_do_mei": _parse_date(v["data_exclusao_do_mei"]),
        }


_ENTITY_PARSERS: dict[str, Any] = {
    "EMPRESAS": parse_empresas,
    "ESTABELECIMENTOS": parse_estabelecimentos,
    "SIMPLES": parse_simples,
}


def parse(files: list[Path]) -> Iterator[dict[str, Any]]:
    """Implementação de `SourceConnector.parse` para os arquivos br_receita.

    Detecta a entidade de cada arquivo pelo nome (`detect_entity`) e delega ao parser
    correspondente, gerando um dict normalizado por linha (chave `"entidade"` indica
    de qual layout veio, para o join em `etl/transform.py` mais adiante). Arquivos não
    reconhecidos (tabelas auxiliares, `SOCIOCSV`) são ignorados com um log informativo
    — use `load_lookup_table` para essas.
    """
    for path in files:
        entity = detect_entity(path)
        if entity is None:
            logger.info("Arquivo ignorado (entidade não reconhecida): %s", path.name)
            continue
        yield from _ENTITY_PARSERS[entity](path)


# -- Tabelas auxiliares (lookups) ---------------------------------------------------


def load_lookup_table(path: Path) -> dict[str, str]:
    """Carrega uma tabela auxiliar de domínio (CNAE, município, natureza jurídica,
    qualificação, motivo, país) no layout uniforme `codigo;descricao` (latin-1, sem
    cabeçalho).

    Returns:
        dict `{codigo: descricao}`.
    """
    lookup: dict[str, str] = {}
    for row in _read_raw_rows(path):
        if len(row) < 2:
            logger.warning("Linha de lookup ignorada em %s: %r", path.name, row)
            continue
        codigo, descricao = row[0].strip(), row[1].strip()
        lookup[codigo] = descricao
    return lookup


def enrich_with_lookups(
    record: dict[str, Any],
    *,
    cnae: Mapping[str, str] | None = None,
    municipio: Mapping[str, str] | None = None,
    natureza_juridica: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Retorna uma cópia de `record` (de `parse_empresas`/`parse_estabelecimentos`) com
    descrições resolvidas a partir das tabelas auxiliares fornecidas.

    Não modifica `record` in-place. Cada chave `*_descricao` só é adicionada quando o
    lookup correspondente foi passado E o registro tem o código correspondente (um
    registro de `EMPRESAS` não tem `cnae_fiscal_principal`, por exemplo — nesse caso
    a chave simplesmente não é adicionada).
    """
    enriched = dict(record)
    if cnae is not None and record.get("cnae_fiscal_principal"):
        enriched["cnae_fiscal_principal_descricao"] = cnae.get(record["cnae_fiscal_principal"])
    if municipio is not None and record.get("municipio"):
        enriched["municipio_descricao"] = municipio.get(record["municipio"])
    if natureza_juridica is not None and record.get("natureza_juridica"):
        enriched["natureza_juridica_descricao"] = natureza_juridica.get(record["natureza_juridica"])
    return enriched
