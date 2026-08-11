"""Parsing dos arquivos de stock SIRENE (encoding UTF-8, COM cabeçalho, separador ',').

Aplica o layout oficial de colunas ("dessin de fichier") publicado pelo INSEE junto do
dataset no data.gouv.fr, confirmado empiricamente baixando os arquivos reais de
descrição nesta tarefa (não assumido de memória) — ver
`UNITE_LEGALE_COLUMNS`/`ETABLISSEMENT_COLUMNS` abaixo. Cobre as duas entidades pedidas:
unidade legal (`StockUniteLegale`, chave SIREN) e estabelecimento (`StockEtablissement`,
chave SIRET).

## Diferenças confirmadas em relação a `br_receita/parser.py`

- **Tem cabeçalho** (a Receita Federal não tem): por isso o parsing usa
  `csv.DictReader` (mapeamento por NOME de coluna), não uma tupla de posições fixas
  como `EMPRESAS_COLUMNS`. Isso também torna o parser resiliente a uma eventual
  reordenação de colunas pelo INSEE — só quebra (`RowLayoutError`, de propósito) se uma
  coluna esperada sumir de verdade.
- Pelo menos duas colunas do dessin de fichier oficial (`coordonneeLambertAbscisse
  Etablissement `/`...OrdonneeEtablissement `) têm um espaço em branco à direita no
  próprio nome — confirmado baixando o CSV oficial, não suposição. Os nomes de coluna
  lidos do cabeçalho são normalizados com `.strip()` antes de qualquer lookup.
- **Datas em ISO 8601** (`AAAA-MM-DD`), não `AAAAMMDD` como na Receita.
- **Sem capital social**: diferente do que a API Recherche d'Entreprises retorna
  (`enrichment/providers.py`), o arquivo de stock `StockUniteLegale` NÃO tem coluna de
  capital social entre as 35 colunas oficiais — confirmado contra o dessin de fichier
  real. Esse campo só fica disponível via enriquecimento sob demanda pela API, não
  pelo stock.
- Os dois zips baixados por `stock_download.py` contêm, cada um, exatamente **um** CSV
  (diferente da Receita, que fatia Estabelecimentos/Empresas em vários arquivos
  numerados e por isso tem um `extractor.py` dedicado) — por isso este módulo lê
  diretamente de dentro do `.zip`, sem precisar de um extractor separado. Também aceita
  um `.csv` já descompactado, para uso em testes/fixtures.

Este módulo cobre só o parsing (`SourceConnector.parse`) — o mapeamento para
`CanonicalLead` fica em `etl/canonical.py`/`etl/transform.py` (fora de escopo aqui).
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)

# -- Layout oficial (nomes de coluna confirmados contra o "dessin de fichier" real do
# INSEE — ordem alfabética/posicional não importa aqui, já que o parsing é por nome via
# DictReader, mas mantemos a ordem oficial por clareza/rastreabilidade). -------------

UNITE_LEGALE_COLUMNS: tuple[str, ...] = (
    "siren",
    "statutDiffusionUniteLegale",
    "unitePurgeeUniteLegale",
    "dateCreationUniteLegale",
    "sigleUniteLegale",
    "sexeUniteLegale",
    "prenom1UniteLegale",
    "prenom2UniteLegale",
    "prenom3UniteLegale",
    "prenom4UniteLegale",
    "prenomUsuelUniteLegale",
    "pseudonymeUniteLegale",
    "identifiantAssociationUniteLegale",
    "trancheEffectifsUniteLegale",
    "anneeEffectifsUniteLegale",
    "dateDernierTraitementUniteLegale",
    "nombrePeriodesUniteLegale",
    "categorieEntreprise",
    "anneeCategorieEntreprise",
    "dateDebut",
    "etatAdministratifUniteLegale",
    "nomUniteLegale",
    "nomUsageUniteLegale",
    "denominationUniteLegale",
    "denominationUsuelle1UniteLegale",
    "denominationUsuelle2UniteLegale",
    "denominationUsuelle3UniteLegale",
    "categorieJuridiqueUniteLegale",
    "activitePrincipaleUniteLegale",
    "nomenclatureActivitePrincipaleUniteLegale",
    "nicSiegeUniteLegale",
    "economieSocialeSolidaireUniteLegale",
    "societeMissionUniteLegale",
    "caractereEmployeurUniteLegale",
    "activitePrincipaleNAF25UniteLegale",
)

ETABLISSEMENT_COLUMNS: tuple[str, ...] = (
    "siren",
    "nic",
    "siret",
    "statutDiffusionEtablissement",
    "dateCreationEtablissement",
    "trancheEffectifsEtablissement",
    "anneeEffectifsEtablissement",
    "activitePrincipaleRegistreMetiersEtablissement",
    "dateDernierTraitementEtablissement",
    "etablissementSiege",
    "nombrePeriodesEtablissement",
    "complementAdresseEtablissement",
    "numeroVoieEtablissement",
    "indiceRepetitionEtablissement",
    "dernierNumeroVoieEtablissement",
    "indiceRepetitionDernierNumeroVoieEtablissement",
    "typeVoieEtablissement",
    "libelleVoieEtablissement",
    "codePostalEtablissement",
    "libelleCommuneEtablissement",
    "libelleCommuneEtrangerEtablissement",
    "distributionSpecialeEtablissement",
    "codeCommuneEtablissement",
    "codeCedexEtablissement",
    "libelleCedexEtablissement",
    "codePaysEtrangerEtablissement",
    "libellePaysEtrangerEtablissement",
    "identifiantAdresseEtablissement",
    "coordonneeLambertAbscisseEtablissement",
    "coordonneeLambertOrdonneeEtablissement",
    "complementAdresse2Etablissement",
    "numeroVoie2Etablissement",
    "indiceRepetition2Etablissement",
    "typeVoie2Etablissement",
    "libelleVoie2Etablissement",
    "codePostal2Etablissement",
    "libelleCommune2Etablissement",
    "libelleCommuneEtranger2Etablissement",
    "distributionSpeciale2Etablissement",
    "codeCommune2Etablissement",
    "codeCedex2Etablissement",
    "libelleCedex2Etablissement",
    "codePaysEtranger2Etablissement",
    "libellePaysEtranger2Etablissement",
    "dateDebut",
    "etatAdministratifEtablissement",
    "enseigne1Etablissement",
    "enseigne2Etablissement",
    "enseigne3Etablissement",
    "denominationUsuelleEtablissement",
    "activitePrincipaleEtablissement",
    "nomenclatureActivitePrincipaleEtablissement",
    "caractereEmployeurEtablissement",
    "activitePrincipaleNAF25Etablissement",
)

# Marcador no nome do arquivo (zip baixado ou CSV já descompactado) -> entidade
# reconhecida. Exclusão explícita de "Historique"/"Succession"/"Doublon" pelo mesmo
# motivo do filtro por fronteira de palavra em `stock_download.py`: esses arquivos
# correlatos do mesmo dataset compartilham o prefixo do nome e ficam fora de escopo.
_ENTITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("UNITELEGALE", "UNITE_LEGALE"),
    ("ETABLISSEMENT", "ETABLISSEMENT"),
)
_EXCLUDED_MARKERS: tuple[str, ...] = ("HISTORIQUE", "SUCCESSION", "DOUBLON")


class RowLayoutError(ValueError):
    """Levantado quando o cabeçalho de um CSV SIRENE não contém as colunas esperadas
    pelo layout oficial (ex.: INSEE mudou o dessin de fichier), ou o zip não contém
    exatamente 1 CSV."""


# -- Detecção de entidade por nome de arquivo ----------------------------------------


def detect_entity(path: Path) -> str | None:
    """Identifica a entidade (`"UNITE_LEGALE"`, `"ETABLISSEMENT"`) pelo nome do
    arquivo (zip ou csv). Retorna `None` para arquivos não reconhecidos/fora de
    escopo (`Historique`, `LiensSuccession`, `Doublons`)."""
    name_upper = path.name.upper()
    if any(marker in name_upper for marker in _EXCLUDED_MARKERS):
        return None
    for marker, entity in _ENTITY_MARKERS:
        if marker in name_upper:
            return entity
    return None


# -- Normalização de valores ---------------------------------------------------------


def _none_if_blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _parse_date(value: str | None) -> date | None:
    """Datas no layout SIRENE vêm em ISO 8601 (`AAAA-MM-DD`) — diferente do
    `AAAAMMDD` da Receita Federal (ver `br_receita/parser.py`)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        logger.warning("Data inválida ignorada (mantida como None): %r", value)
        return None


def _parse_int(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Inteiro inválido ignorado (mantido como None): %r", value)
        return None


def _parse_bool_true_or_blank(value: str | None) -> bool:
    """`unitePurgeeUniteLegale` vem como `"true"` ou vazio (layout oficial: "Texte:
    true ou vide") — não é um booleano JSON nem uma lista de códigos O/N como os
    demais campos binários do arquivo."""
    return (value or "").strip().lower() == "true"


def _flag_restrita(value: str | None) -> bool:
    """`True` (restrito) pra qualquer valor diferente de `"O"` (aberto/diffusible) —
    inclusive ausente/vazio. Mesma convenção conservadora de
    `enrichment/providers.py` (`map_recherche_entreprises_response`): melhor excluir
    demais do que deixar passar um registro que a lei francesa proíbe usar para
    prospecção (ver CLAUDE.md — filtro hard de difusão restrita)."""
    return (value or "").strip().upper() != "O"


# -- Leitura bruta ---------------------------------------------------------------


def _validate_header(
    fieldnames: Sequence[str] | None, expected: tuple[str, ...], source_name: str
) -> None:
    if fieldnames is None:
        raise RowLayoutError(f"Arquivo sem cabeçalho: {source_name}")
    missing = set(expected) - set(fieldnames)
    if missing:
        raise RowLayoutError(
            f"Colunas esperadas ausentes em {source_name} (layout SIRENE pode ter mudado): "
            f"{sorted(missing)}"
        )


def _dict_rows_from_file(
    f: TextIO, expected_columns: tuple[str, ...], source_name: str
) -> Iterator[dict[str, str]]:
    reader = csv.DictReader(f)
    if reader.fieldnames:
        # Normaliza espaços à direita presentes no dessin de fichier oficial (ver
        # docstring do módulo) antes de validar/consultar por nome.
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
    _validate_header(reader.fieldnames, expected_columns, source_name)
    yield from reader


def _iter_source_rows(path: Path, expected_columns: tuple[str, ...]) -> Iterator[dict[str, str]]:
    """Itera as linhas de um arquivo de stock SIRENE: UTF-8, `,`, COM cabeçalho.

    Aceita tanto o `.zip` baixado por `stock_download.py` (contém um único CSV) quanto
    um `.csv` já descompactado (usado nos testes, com fixtures pequenos)."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(csv_names) != 1:
                raise RowLayoutError(
                    f"Esperado exatamente 1 CSV dentro de {path.name}, "
                    f"encontrado {len(csv_names)}: {csv_names!r}"
                )
            with (
                zf.open(csv_names[0]) as raw,
                io.TextIOWrapper(raw, encoding="utf-8", newline="") as f,
            ):
                yield from _dict_rows_from_file(f, expected_columns, f"{path.name}!{csv_names[0]}")
    else:
        with path.open("r", encoding="utf-8", newline="") as f:
            yield from _dict_rows_from_file(f, expected_columns, path.name)


# -- Parsers por entidade ---------------------------------------------------------


def parse_unite_legale(path: Path) -> Iterator[dict[str, Any]]:
    """Faz o parsing de um arquivo `StockUniteLegale` (layout `UNITE_LEGALE_COLUMNS`,
    35 campos, chave SIREN)."""
    for row in _iter_source_rows(path, UNITE_LEGALE_COLUMNS):
        statut_diffusion = row.get("statutDiffusionUniteLegale")
        yield {
            "entidade": "UNITE_LEGALE",
            "siren": (row.get("siren") or "").strip(),
            "statut_diffusion": _none_if_blank(statut_diffusion),
            "flag_difusao_restrita": _flag_restrita(statut_diffusion),
            "unite_purgee": _parse_bool_true_or_blank(row.get("unitePurgeeUniteLegale")),
            "data_criacao": _parse_date(row.get("dateCreationUniteLegale")),
            "sigla": _none_if_blank(row.get("sigleUniteLegale")),
            "sexo": _none_if_blank(row.get("sexeUniteLegale")),
            "prenome_1": _none_if_blank(row.get("prenom1UniteLegale")),
            "prenome_2": _none_if_blank(row.get("prenom2UniteLegale")),
            "prenome_3": _none_if_blank(row.get("prenom3UniteLegale")),
            "prenome_4": _none_if_blank(row.get("prenom4UniteLegale")),
            "prenome_usual": _none_if_blank(row.get("prenomUsuelUniteLegale")),
            "pseudonimo": _none_if_blank(row.get("pseudonymeUniteLegale")),
            "identificador_associacao": _none_if_blank(
                row.get("identifiantAssociationUniteLegale")
            ),
            "faixa_efetivos": _none_if_blank(row.get("trancheEffectifsUniteLegale")),
            "ano_efetivos": _parse_int(row.get("anneeEffectifsUniteLegale")),
            "data_ultimo_tratamento": _none_if_blank(row.get("dateDernierTraitementUniteLegale")),
            "numero_periodos": _parse_int(row.get("nombrePeriodesUniteLegale")),
            "categoria_empresa": _none_if_blank(row.get("categorieEntreprise")),
            "ano_categoria_empresa": _parse_int(row.get("anneeCategorieEntreprise")),
            "data_inicio_periodo": _parse_date(row.get("dateDebut")),
            "situacao": _none_if_blank(row.get("etatAdministratifUniteLegale")),
            "nome": _none_if_blank(row.get("nomUniteLegale")),
            "nome_uso": _none_if_blank(row.get("nomUsageUniteLegale")),
            "razao_social": _none_if_blank(row.get("denominationUniteLegale")),
            "nome_fantasia": _none_if_blank(row.get("denominationUsuelle1UniteLegale")),
            "nome_fantasia_2": _none_if_blank(row.get("denominationUsuelle2UniteLegale")),
            "nome_fantasia_3": _none_if_blank(row.get("denominationUsuelle3UniteLegale")),
            "natureza_juridica": _none_if_blank(row.get("categorieJuridiqueUniteLegale")),
            "cod_atividade": _none_if_blank(row.get("activitePrincipaleUniteLegale")),
            "nomenclatura_atividade": _none_if_blank(
                row.get("nomenclatureActivitePrincipaleUniteLegale")
            ),
            "nic_sede": _none_if_blank(row.get("nicSiegeUniteLegale")),
            "economia_social_solidaria": _none_if_blank(
                row.get("economieSocialeSolidaireUniteLegale")
            ),
            "sociedade_missao": _none_if_blank(row.get("societeMissionUniteLegale")),
            "carater_empregador": _none_if_blank(row.get("caractereEmployeurUniteLegale")),
            "cod_atividade_naf25": _none_if_blank(row.get("activitePrincipaleNAF25UniteLegale")),
        }


def parse_etablissement(path: Path) -> Iterator[dict[str, Any]]:
    """Faz o parsing de um arquivo `StockEtablissement` (layout `ETABLISSEMENT_COLUMNS`,
    54 campos, chave SIRET)."""
    for row in _iter_source_rows(path, ETABLISSEMENT_COLUMNS):
        statut_diffusion = row.get("statutDiffusionEtablissement")
        yield {
            "entidade": "ETABLISSEMENT",
            "siren": (row.get("siren") or "").strip(),
            "nic": (row.get("nic") or "").strip(),
            "siret": (row.get("siret") or "").strip(),
            "statut_diffusion": _none_if_blank(statut_diffusion),
            "flag_difusao_restrita": _flag_restrita(statut_diffusion),
            "data_criacao": _parse_date(row.get("dateCreationEtablissement")),
            "faixa_efetivos": _none_if_blank(row.get("trancheEffectifsEtablissement")),
            "ano_efetivos": _parse_int(row.get("anneeEffectifsEtablissement")),
            "cod_atividade_artesao": _none_if_blank(
                row.get("activitePrincipaleRegistreMetiersEtablissement")
            ),
            "data_ultimo_tratamento": _none_if_blank(row.get("dateDernierTraitementEtablissement")),
            "eh_sede": _none_if_blank(row.get("etablissementSiege")),
            "numero_periodos": _parse_int(row.get("nombrePeriodesEtablissement")),
            "complemento_endereco": _none_if_blank(row.get("complementAdresseEtablissement")),
            "numero_via": _none_if_blank(row.get("numeroVoieEtablissement")),
            "indice_repeticao": _none_if_blank(row.get("indiceRepetitionEtablissement")),
            "ultimo_numero_via": _none_if_blank(row.get("dernierNumeroVoieEtablissement")),
            "indice_repeticao_ultimo_numero": _none_if_blank(
                row.get("indiceRepetitionDernierNumeroVoieEtablissement")
            ),
            "tipo_via": _none_if_blank(row.get("typeVoieEtablissement")),
            "logradouro": _none_if_blank(row.get("libelleVoieEtablissement")),
            "cep": _none_if_blank(row.get("codePostalEtablissement")),
            "municipio": _none_if_blank(row.get("libelleCommuneEtablissement")),
            "municipio_exterior": _none_if_blank(row.get("libelleCommuneEtrangerEtablissement")),
            "distribuicao_especial": _none_if_blank(row.get("distributionSpecialeEtablissement")),
            "codigo_municipio": _none_if_blank(row.get("codeCommuneEtablissement")),
            "codigo_cedex": _none_if_blank(row.get("codeCedexEtablissement")),
            "cedex": _none_if_blank(row.get("libelleCedexEtablissement")),
            "codigo_pais_exterior": _none_if_blank(row.get("codePaysEtrangerEtablissement")),
            "pais_exterior": _none_if_blank(row.get("libellePaysEtrangerEtablissement")),
            "identificador_endereco": _none_if_blank(row.get("identifiantAdresseEtablissement")),
            "coordenada_lambert_x": _none_if_blank(
                row.get("coordonneeLambertAbscisseEtablissement")
            ),
            "coordenada_lambert_y": _none_if_blank(
                row.get("coordonneeLambertOrdonneeEtablissement")
            ),
            "data_inicio_periodo": _parse_date(row.get("dateDebut")),
            "situacao": _none_if_blank(row.get("etatAdministratifEtablissement")),
            "nome_fantasia": _none_if_blank(row.get("enseigne1Etablissement")),
            "nome_fantasia_2": _none_if_blank(row.get("enseigne2Etablissement")),
            "nome_fantasia_3": _none_if_blank(row.get("enseigne3Etablissement")),
            "denominacao_usual": _none_if_blank(row.get("denominationUsuelleEtablissement")),
            "cod_atividade": _none_if_blank(row.get("activitePrincipaleEtablissement")),
            "nomenclatura_atividade": _none_if_blank(
                row.get("nomenclatureActivitePrincipaleEtablissement")
            ),
            "carater_empregador": _none_if_blank(row.get("caractereEmployeurEtablissement")),
            "cod_atividade_naf25": _none_if_blank(row.get("activitePrincipaleNAF25Etablissement")),
        }


_ENTITY_PARSERS: dict[str, Any] = {
    "UNITE_LEGALE": parse_unite_legale,
    "ETABLISSEMENT": parse_etablissement,
}


def parse(files: list[Path]) -> Iterator[dict[str, Any]]:
    """Implementação de `SourceConnector.parse` para os arquivos fr_sirene.

    Detecta a entidade de cada arquivo pelo nome (`detect_entity`) e delega ao parser
    correspondente. Arquivos não reconhecidos (`Historique`, `LiensSuccession`,
    `Doublons`) são ignorados com um log informativo.
    """
    for path in files:
        entity = detect_entity(path)
        if entity is None:
            logger.info("Arquivo ignorado (entidade não reconhecida/fora de escopo): %s", path.name)
            continue
        yield from _ENTITY_PARSERS[entity](path)
