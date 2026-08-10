"""Testes do contrato `SourceConnector` e do schema canônico `CanonicalLead`.

Usa uma `FakeConnector` local (sem rede/IO real além de `tmp_path`) para provar que:
uma subclasse concreta que implementa os 4 métodos é instanciável e respeita o
contrato ponta a ponta; o ABC recusa instanciar a classe base ou subclasses
incompletas; e `CanonicalLead` valida o schema (defaults de compliance, país
restrito a BR/FR, campos obrigatórios não-vazios).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.ingestion.base import CanonicalLead, SourceConnector


class FakeConnector(SourceConnector):
    """Implementação mínima e determinística do contrato, só para teste."""

    def check_latest(self) -> str:
        return "2026-08"

    def download(self, dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        fake_file = dest / "fake.csv"
        fake_file.write_text("12345678;EMPRESA FAKE LTDA\n", encoding="utf-8")
        return [fake_file]

    def parse(self, files: list[Path]) -> Iterator[dict[str, Any]]:
        for _file in files:
            yield {"raw_id": "12345678", "raw_name": "EMPRESA FAKE LTDA"}

    def to_canonical(self, record: dict[str, Any]) -> dict[str, Any]:
        canonical = CanonicalLead(
            pais="BR",
            id_legal=record["raw_id"],
            id_estab=record["raw_id"] + "0001",
            razao_social=record["raw_name"],
            fonte="fake-source",
        )
        return canonical.model_dump()


def test_source_connector_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        SourceConnector()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    class IncompleteConnector(SourceConnector):
        """Implementa só 3 dos 4 métodos abstratos — falta `to_canonical`."""

        def check_latest(self) -> str:
            return "2026-08"

        def download(self, dest: Path) -> list[Path]:
            return []

        def parse(self, files: list[Path]) -> Iterator[dict[str, Any]]:
            yield from ()

    with pytest.raises(TypeError):
        IncompleteConnector()  # type: ignore[abstract]


def test_fake_connector_respects_contract(tmp_path: Path) -> None:
    connector = FakeConnector()

    latest = connector.check_latest()
    assert isinstance(latest, str)
    assert latest

    files = connector.download(tmp_path)
    assert isinstance(files, list)
    assert files
    assert all(isinstance(f, Path) and f.is_file() for f in files)

    records = list(connector.parse(files))
    assert records
    assert all(isinstance(r, dict) for r in records)

    canonical = connector.to_canonical(records[0])
    assert isinstance(canonical, dict)
    assert set(canonical) == set(CanonicalLead.model_fields)
    assert canonical["is_synthetic"] is False
    assert canonical["flag_difusao_restrita"] is False


def test_canonical_lead_required_defaults() -> None:
    lead = CanonicalLead(
        pais="BR",
        id_legal="12345678",
        id_estab="123456780001",
        razao_social="EMPRESA FAKE LTDA",
        fonte="fake-source",
    )

    assert lead.is_synthetic is False
    assert lead.flag_difusao_restrita is False
    assert lead.nome_fantasia is None
    assert lead.cod_atividade is None
    assert lead.email is None
    assert lead.score_icp is None


def test_canonical_lead_rejects_invalid_pais() -> None:
    with pytest.raises(ValidationError):
        CanonicalLead(
            pais="US",  # type: ignore[arg-type]
            id_legal="12345678",
            id_estab="123456780001",
            razao_social="EMPRESA FAKE LTDA",
            fonte="fake-source",
        )


def test_canonical_lead_rejects_empty_required_string() -> None:
    with pytest.raises(ValidationError):
        CanonicalLead(
            pais="BR",
            id_legal="",
            id_estab="123456780001",
            razao_social="EMPRESA FAKE LTDA",
            fonte="fake-source",
        )
