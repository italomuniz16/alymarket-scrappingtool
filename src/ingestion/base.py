"""Contrato comum (interface) que todo conector de ingestão (BR, FR, ...) implementa.

O pipeline de ingestão trata qualquer país de forma uniforme através de
`SourceConnector`: descobre a competência mais recente, baixa os arquivos de stock,
faz o parsing bruto e mapeia cada registro para o schema canônico da tabela `leads`
(`CanonicalLead`, definido também aqui — é o contrato entre a camada de ingestão e o
resto do pipeline: ETL, segmentação, enriquecimento, export).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PaisCodigo = Literal["BR", "FR"]


class CanonicalLead(BaseModel):
    """Schema canônico da tabela `leads` (ver CLAUDE.md e docs/PRD.md §4.4).

    Todo conector de país mapeia seus dados brutos para este modelo em
    `SourceConnector.to_canonical`. Campos comuns a BR/FR são obrigatórios; campos
    que só existem/fazem sentido numa das fontes (ex.: `flag_difusao_restrita`,
    específico do SIRENE francês) ficam opcionais com um default seguro.

    `is_synthetic` e `flag_difusao_restrita` têm default `False` de propósito: todo
    registro real de ingestão é `is_synthetic=False` por padrão (só o seed de demo em
    `src/seed/synthetic.py` deve setar `True`); `flag_difusao_restrita=False` é o
    default correto para o Brasil (que não tem esse conceito) e para a maioria dos
    registros franceses — o conector FR deve setar `True` explicitamente quando a
    fonte indicar "diffusion partielle", para o filtro hard de exportação excluir o
    registro de qualquer lista de prospecção.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # Identificação
    pais: PaisCodigo
    id_legal: str = Field(min_length=1, description="CNPJ básico (BR) ou SIREN (FR)")
    id_estab: str = Field(min_length=1, description="CNPJ completo (BR) ou SIRET (FR)")
    razao_social: str = Field(min_length=1)
    nome_fantasia: str | None = None

    # Classificação
    cod_atividade: str | None = Field(default=None, description="CNAE (BR) ou NAF/APE (FR)")
    situacao: str | None = None
    natureza_juridica: str | None = None
    porte: str | None = None
    capital_social: Decimal | None = None

    # Localização
    regiao: str | None = Field(default=None, description="UF (BR) ou région/département (FR)")
    municipio: str | None = None
    cep: str | None = None

    # Contato
    telefone: str | None = None
    email: str | None = None

    # Ciclo de vida / proveniência
    data_inicio_atividade: date | None = None
    score_icp: float | None = None
    fonte: str = Field(min_length=1, description="Origem do dado (ex.: nome do conector)")
    enriquecido_em: datetime | None = None

    # Compliance (ver CLAUDE.md — LGPD/RGPD)
    is_synthetic: bool = False
    flag_difusao_restrita: bool = False


class SourceConnector(ABC):
    """Contrato comum que todo conector de ingestão por país implementa.

    Subclasses concretas (ex.: `src/ingestion/br_receita`, `src/ingestion/fr_sirene`)
    implementam os quatro métodos abaixo; nada no resto do pipeline deve depender de
    detalhes específicos de um país além desta interface.
    """

    @abstractmethod
    def check_latest(self) -> str:
        """Retorna o identificador da competência/atualização mais recente na fonte.

        Ex.: `"2026-08"` para a competência mensal da Receita Federal, ou a data de
        publicação do último arquivo de stock do SIRENE no data.gouv.fr.
        """
        raise NotImplementedError

    @abstractmethod
    def download(self, dest: Path) -> list[Path]:
        """Baixa os arquivos de stock da fonte para `dest`, de forma retomável.

        Implementações devem suportar retomar downloads interrompidos e verificar a
        integridade dos arquivos baixados (ver PRD §6.1).

        Returns:
            Caminhos dos arquivos baixados, dentro de `dest`.
        """
        raise NotImplementedError

    @abstractmethod
    def parse(self, files: list[Path]) -> Iterator[dict[str, Any]]:
        """Faz o parsing bruto dos arquivos baixados, um registro normalizado por vez.

        As chaves e tipos de cada dict ainda são específicos da fonte (não mapeados
        para o schema canônico) — mas encoding e tipagem básica já devem estar
        normalizados aqui (ver `br_receita/sample_probe.py` para a validação de
        encoding/formato que baliza o parser real).
        """
        raise NotImplementedError

    @abstractmethod
    def to_canonical(self, record: dict[str, Any]) -> dict[str, Any]:
        """Mapeia um registro bruto (de `parse`) para o schema canônico da `leads`.

        Implementações devem validar o resultado antes de retornar, ex.:
        `return CanonicalLead.model_validate(mapped).model_dump()`, para garantir que
        o dict resultante respeita o contrato completo de `CanonicalLead`.
        """
        raise NotImplementedError
