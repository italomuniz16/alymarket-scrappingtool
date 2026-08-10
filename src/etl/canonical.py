"""Mapeamento BR -> schema canônico (`CanonicalLead`, ver `src/ingestion/base.py`).

A função principal, `map_estabelecimento_to_canonical`, recebe um registro já unido
(estabelecimento + empresa + simples + lookups — produzido pelo JOIN de
`etl/transform.py`) e devolve um dict validado contra `CanonicalLead`.

Constantes desta fonte: todo registro que passa por aqui é `pais="BR"`,
`fonte="BR_RECEITA"`, `is_synthetic=False` (dado real, nunca o seed sintético de
`src/seed/synthetic.py`) e `flag_difusao_restrita=False` (conceito específico do
SIRENE francês — não existe para o Brasil).
"""

from __future__ import annotations

from typing import Any

from src.ingestion.base import CanonicalLead

FONTE_BR_RECEITA = "BR_RECEITA"

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
