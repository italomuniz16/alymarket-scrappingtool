"""Modelos Pydantic da API HTTP (`src/api/app.py`) -- shapes de request/response.

`ICPCriteriaIn` espelha `segmentation.filters.ICPCriteria` (a dataclass real, usada
em todo o resto do pipeline) e sabe se converter pra ela (`to_criteria`) -- nenhuma
regra de filtro é reimplementada aqui, só a forma de chegar da rede até a dataclass.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from src.segmentation.filters import ICPCriteria


class ICPCriteriaIn(BaseModel):
    """Espelha `segmentation.filters.ICPCriteria` -- ver lá pro significado de cada
    campo (aceitam um valor único ou lista; `None`/lista vazia = sem filtro)."""

    pais: str | list[str] | None = None
    cod_atividade: str | list[str] | None = None
    regiao: str | list[str] | None = None
    porte: str | list[str] | None = None
    situacao: str | list[str] | None = None
    capital_social_min: Decimal | None = None
    capital_social_max: Decimal | None = None
    aberta_apos: date | None = None
    com_email: bool = False
    com_telefone: bool = False

    def to_criteria(self) -> ICPCriteria:
        return ICPCriteria(**self.model_dump())


class PreviewRequest(BaseModel):
    criteria: ICPCriteriaIn = Field(default_factory=ICPCriteriaIn)
    demo: bool = False
    limit: int = 100


class PreviewResponse(BaseModel):
    rows: list[dict[str, Any]]
    tam: int
    demo: bool


class ChartsRequest(BaseModel):
    criteria: ICPCriteriaIn = Field(default_factory=ICPCriteriaIn)
    demo: bool = False


class ChartsResponse(BaseModel):
    regiao: dict[str, int]
    atividade: dict[str, int]


class ComplianceRequest(BaseModel):
    criteria: ICPCriteriaIn = Field(default_factory=ICPCriteriaIn)


class ComplianceResponse(BaseModel):
    total_bruto: int
    n_sinteticos: int
    n_difusao_restrita: int
    n_duplicados: int
    n_opt_out: int
    total_exportavel: int


class ExportRequest(BaseModel):
    criteria: ICPCriteriaIn = Field(default_factory=ICPCriteriaIn)
    formato: str = "csv"
    usuario: str | None = None
    demo: bool = False


class ExportOneRequest(ExportRequest):
    id_estab: str


class IngestRequest(BaseModel):
    n: int = 40


class IngestResponse(BaseModel):
    activated: bool
    n_rows_written: int
    n_rows_skipped: int
    n_rows_total: int
    failures: list[str]


class SchedulerStatusRow(BaseModel):
    fonte: str
    ultima_competencia: str | None
    ultima_execucao: str | None
