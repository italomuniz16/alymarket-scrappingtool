"""Mapeamento BR e FR -> schema canônico (`CanonicalLead`, ver `src/ingestion/base.py`).

- `map_estabelecimento_to_canonical` (BR): recebe um registro já unido
  (estabelecimento + empresa + simples + lookups — produzido pelo JOIN de
  `etl/transform.py`) e devolve um dict validado contra `CanonicalLead`.
  `fonte="BR_RECEITA"`, `flag_difusao_restrita=False` sempre (conceito específico do
  SIRENE francês — não existe para o Brasil).
- `map_unite_legale_etablissement_to_canonical` (FR): recebe o par (unidade legal,
  estabelecimento) já unido por SIREN — produzido por
  `etl/transform.materialize_leads_fr` — e devolve um dict validado contra
  `CanonicalLead`. `fonte="FR_SIRENE"`. **CRÍTICO**: `flag_difusao_restrita` reflete
  o `statut_diffusion` de qualquer um dos dois registros — ver docstring da função.
- `map_opencnpj_to_canonical` (BR, fonte alternativa): recebe um registro bruto da
  API pública do OpenCNPJ (`ingestion/br_opencnpj/client.py`) e devolve um dict
  validado contra `CanonicalLead`. `fonte="BR_OPENCNPJ"`. Usada enquanto
  `br_receita/downloader.py` (URL oficial original da Receita Federal) está
  desativado — ver CLAUDE.md/histórico do projeto.

Todas: todo registro que passa por aqui é dado real (`is_synthetic=False` — nunca o
seed sintético de `src/seed/synthetic.py`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from src.ingestion.base import CanonicalLead

FONTE_BR_RECEITA = "BR_RECEITA"
FONTE_FR_SIRENE = "FR_SIRENE"
FONTE_OPENCNPJ = "BR_OPENCNPJ"

# Mesma convenção de valores usada em enrichment/providers.py
# (map_recherche_entreprises_response) para o campo etat_administratif da API --
# duplicado aqui de propósito: o mapeamento canônico pertence à camada ETL, que não
# deve importar de enrichment (camada rio abaixo no pipeline, ver CLAUDE.md).
ETAT_ADMINISTRATIF_LABELS: dict[str, str] = {"A": "ATIVA", "F": "BAIXADA"}

# Vocabulário fixo e pequeno (documentado no layout oficial da Receita) -- diferente
# de CNAE/município/natureza jurídica, que são tabelas grandes tratadas como lookups
# em ingestion/br_receita/parser.py (`load_lookup_table`/`enrich_with_lookups`).
SITUACAO_CADASTRAL_LABELS: dict[str, str] = {
    "01": "NULA",
    "02": "ATIVA",
    "03": "SUSPENSA",
    "04": "INAPTA",
    "08": "BAIXADA",
}

PORTE_EMPRESA_LABELS: dict[str, str] = {
    "00": "NAO INFORMADO",
    "01": "MICRO EMPRESA",
    "03": "EMPRESA DE PEQUENO PORTE",
    "05": "DEMAIS",
}


def _compose_telefone(ddd: str | None, numero: str | None) -> str | None:
    """Junta DDD + número num único campo de telefone; `None` se os dois vierem vazios."""
    ddd = ddd or ""
    numero = numero or ""
    if not ddd and not numero:
        return None
    return f"{ddd}{numero}"


def map_estabelecimento_to_canonical(record: dict[str, Any]) -> dict[str, Any]:
    """Mapeia um registro unido (estabelecimento+empresa+simples+lookups) para o
    schema canônico da tabela `leads`.

    `record` deve ter as chaves produzidas pelo JOIN de `etl/transform.py`:
    `cnpj_basico`, `cnpj_completo`, `nome_fantasia`, `situacao_cadastral`,
    `data_inicio_atividade` (já `date`/`None`), `cnae_fiscal_principal`, `uf`,
    `municipio_codigo`, `municipio_descricao`, `cep`, `ddd_1`, `telefone_1`,
    `correio_eletronico`, `razao_social`, `natureza_juridica_codigo`,
    `natureza_juridica_descricao`, `capital_social` (já `Decimal`/`None`),
    `porte_empresa`, `opcao_pelo_simples`, `opcao_pelo_mei`.

    As duas últimas (Simples/MEI) não têm campo correspondente em `CanonicalLead`
    ainda — ficam disponíveis no `record` de entrada para o scoring da Fase 2, mas
    não são usadas aqui.

    Raises:
        pydantic.ValidationError: se o registro não tiver o mínimo exigido pelo
            schema canônico (ex.: `razao_social` ausente — estabelecimento "órfão",
            sem empresa correspondente no join). O chamador (`etl/transform.py`)
            deve pular e contar essas linhas, não interromper a materialização.
    """
    situacao_codigo = record.get("situacao_cadastral")
    situacao = (
        SITUACAO_CADASTRAL_LABELS.get(situacao_codigo, situacao_codigo) if situacao_codigo else None
    )

    porte_codigo = record.get("porte_empresa")
    porte = PORTE_EMPRESA_LABELS.get(porte_codigo, porte_codigo) if porte_codigo else None

    municipio = record.get("municipio_descricao") or record.get("municipio_codigo")
    natureza_juridica = record.get("natureza_juridica_descricao") or record.get(
        "natureza_juridica_codigo"
    )

    lead = CanonicalLead(
        pais="BR",
        id_legal=record["cnpj_basico"],
        id_estab=record["cnpj_completo"],
        razao_social=record.get("razao_social") or "",
        nome_fantasia=record.get("nome_fantasia"),
        cod_atividade=record.get("cnae_fiscal_principal"),
        situacao=situacao,
        regiao=record.get("uf"),
        municipio=municipio,
        cep=record.get("cep"),
        telefone=_compose_telefone(record.get("ddd_1"), record.get("telefone_1")),
        email=record.get("correio_eletronico"),
        data_inicio_atividade=record.get("data_inicio_atividade"),
        porte=porte,
        capital_social=record.get("capital_social"),
        natureza_juridica=natureza_juridica,
        score_icp=None,
        fonte=FONTE_BR_RECEITA,
        enriquecido_em=None,
        is_synthetic=False,
        flag_difusao_restrita=False,
    )
    return lead.model_dump()


# -- FR (SIRENE) ----------------------------------------------------------------


def _departamento_from_code_postal(code_postal: str | None) -> str | None:
    """Aproxima o département a partir dos 2 primeiros dígitos do code postal — mesma
    heurística de `enrichment/providers.py::_departamento_from_code_postal`
    (duplicada de propósito, ver docstring do módulo).

    Não existe campo "région"/"département" no arquivo de stock SIRENE — confirmado
    contra o dessin de fichier oficial (ver `ingestion/fr_sirene/parser.py`); só é
    derivável do endereço do estabelecimento.
    """
    if not code_postal or len(code_postal) < 2 or not code_postal[:2].isdigit():
        return None
    return code_postal[:2]


def _compose_pessoa_fisica_nome(unite_legale: Mapping[str, Any]) -> str | None:
    """Pra empresário individual (personne physique), `denominationUniteLegale` vem
    vazio — o nome vem em nome/prenome separados (ver
    `ingestion/fr_sirene/parser.parse_unite_legale`)."""
    prenome = unite_legale.get("prenome_usual") or unite_legale.get("prenome_1")
    nome = unite_legale.get("nome")
    partes = [p for p in (prenome, nome) if p]
    return " ".join(partes) or None


def map_unite_legale_etablissement_to_canonical(
    unite_legale: Mapping[str, Any], etablissement: Mapping[str, Any]
) -> dict[str, Any]:
    """Mapeia um par (unidade legal, estabelecimento) — já unidos por SIREN, ver
    `etl/transform.materialize_leads_fr` — pro schema canônico da tabela `leads`.

    `unite_legale`/`etablissement` têm as chaves produzidas por
    `ingestion/fr_sirene/parser.parse_unite_legale`/`parse_etablissement`.

    Mapeamento: SIREN -> `id_legal`, SIRET -> `id_estab`, NAF (activité principale)
    -> `cod_atividade`, état administratif -> `situacao` (códigos traduzidos via
    `ETAT_ADMINISTRATIF_LABELS`, mesmo vocabulário de `enrichment/providers.py`),
    région/commune -> `regiao`/`municipio` (région aproximado do code postal — não
    há campo région no stock file, ver `_departamento_from_code_postal`).

    Prioridade estabelecimento > unidade legal pros campos presentes nos dois
    (`situacao`, `cod_atividade`, `nome_fantasia`, `data_inicio_atividade`): o
    estabelecimento é a unidade operacional mais específica — mesmo padrão que
    `map_estabelecimento_to_canonical` usa pro Brasil (estabelecimento antes de
    empresa).

    **CRÍTICO (compliance — ver CLAUDE.md, filtro hard de difusão restrita)**:
    `flag_difusao_restrita` é `True` se QUALQUER um dos dois registros (unidade
    legal OU estabelecimento) tiver `statut_diffusion` diferente de `"O"` — os
    parsers já computam isso por registro (`ingestion/fr_sirene/parser._flag_restrita`);
    aqui é sempre o OR dos dois, nunca só um dos lados, pra não deixar passar por
    engano um par onde só a unidade legal (ou só o estabelecimento) está em
    "diffusion partielle".

    Raises:
        pydantic.ValidationError: se o par não tiver o mínimo exigido pelo schema
            canônico (ex.: nem denominação nem nome de pessoa física em
            `unite_legale`) — o chamador (`etl/transform.materialize_leads_fr`) pula
            e conta essas linhas, não interrompe a materialização.
    """
    situacao_codigo = etablissement.get("situacao") or unite_legale.get("situacao")
    situacao = (
        ETAT_ADMINISTRATIF_LABELS.get(situacao_codigo, situacao_codigo) if situacao_codigo else None
    )

    razao_social = (
        unite_legale.get("razao_social") or _compose_pessoa_fisica_nome(unite_legale) or ""
    )
    nome_fantasia = (
        etablissement.get("nome_fantasia")
        or unite_legale.get("sigla")
        or unite_legale.get("nome_fantasia")
    )
    cod_atividade = etablissement.get("cod_atividade") or unite_legale.get("cod_atividade")
    data_inicio_atividade = etablissement.get("data_criacao") or unite_legale.get("data_criacao")

    # CRÍTICO: OR dos dois lados, nunca só um -- ver docstring acima.
    flag_difusao_restrita = bool(unite_legale.get("flag_difusao_restrita")) or bool(
        etablissement.get("flag_difusao_restrita")
    )

    lead = CanonicalLead(
        pais="FR",
        id_legal=etablissement.get("siren") or unite_legale.get("siren") or "",
        id_estab=etablissement.get("siret") or "",
        razao_social=razao_social,
        nome_fantasia=nome_fantasia,
        cod_atividade=cod_atividade,
        situacao=situacao,
        regiao=_departamento_from_code_postal(etablissement.get("cep")),
        municipio=etablissement.get("municipio"),
        cep=etablissement.get("cep"),
        telefone=None,
        email=None,
        data_inicio_atividade=data_inicio_atividade,
        porte=unite_legale.get("categoria_empresa"),
        capital_social=None,  # ausente do stock file -- só via enrichment sob demanda
        natureza_juridica=unite_legale.get("natureza_juridica"),
        score_icp=None,
        fonte=FONTE_FR_SIRENE,
        enriquecido_em=None,
        is_synthetic=False,
        flag_difusao_restrita=flag_difusao_restrita,
    )
    return lead.model_dump()


# -- BR (OpenCNPJ — fonte alternativa) -------------------------------------------

_NATUREZA_JURIDICA_CODE_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")


def _only_digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(c for c in value if c.isdigit())
    return digits or None


def _parse_ddmmyyyy(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def map_opencnpj_to_canonical(record: Mapping[str, Any]) -> dict[str, Any]:
    """Mapeia um registro bruto da API pública do OpenCNPJ (`kitana.opencnpj.com`,
    sem autenticação, dados oficiais da Receita Federal — ver
    `ingestion/br_opencnpj/client.py`) pro schema canônico da tabela `leads`.

    Usada como fonte alternativa pra `pais=BR` enquanto `br_receita/downloader.py`
    (URL oficial original da Receita Federal) está desativado — ver CLAUDE.md.
    `cnaes`/`naturezaJuridica`/datas vêm em formatos diferentes do stock oficial
    (`etl/transform.py` JOIN), daí este mapeamento separado em vez de reaproveitar
    `map_estabelecimento_to_canonical`.

    Raises:
        pydantic.ValidationError: se o registro não tiver o mínimo exigido pelo
            schema canônico (ex.: `razaoSocial` ausente). O chamador
            (`etl/transform.materialize_leads_opencnpj`) deve pular e contar essas
            linhas, não interromper a materialização.
    """
    cnpj = _only_digits(record.get("cnpj")) or ""
    cnaes = record.get("cnaes") or []
    primeiro_cnae = cnaes[0] if cnaes else {}
    natureza_juridica = (
        _NATUREZA_JURIDICA_CODE_SUFFIX_RE.sub("", record.get("naturezaJuridica") or "").strip()
        or None
    )

    lead = CanonicalLead(
        pais="BR",
        id_legal=cnpj[:8],
        id_estab=cnpj,
        razao_social=record.get("razaoSocial") or "",
        nome_fantasia=record.get("nomeFantasia") or None,
        cod_atividade=_only_digits(primeiro_cnae.get("cnae")),
        situacao=(record.get("situacaoCadastral") or "").upper() or None,
        regiao=record.get("uf"),
        municipio=record.get("municipio"),
        cep=_only_digits(record.get("cep")),
        telefone=_only_digits(record.get("telefone")),
        email=(record.get("email") or "").lower() or None,
        data_inicio_atividade=_parse_ddmmyyyy(record.get("dataInicioAtividades")),
        porte=None,  # ausente da resposta da API
        capital_social=record.get("capitalSocial"),
        natureza_juridica=natureza_juridica,
        score_icp=None,
        fonte=FONTE_OPENCNPJ,
        enriquecido_em=None,
        is_synthetic=False,
        flag_difusao_restrita=False,
    )
    return lead.model_dump()
