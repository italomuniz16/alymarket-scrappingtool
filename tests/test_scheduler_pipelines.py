"""Testes de `scheduler/pipelines.collect_opencnpj_leads` -- ponto único de
orquestração (descoberta + busca + materialização + ativação) usado tanto por
`cli.py ingest` quanto pelo botão "Coletar leads" do dashboard. Descoberta/cliente
são injetados como fakes -- nenhuma chamada de rede real.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from src.etl.transform import get_active_version
from src.scheduler.pipelines import collect_opencnpj_leads

RECORD_1 = {
    "cnpj": "00000000083208",
    "situacaoCadastral": "Ativa",
    "razaoSocial": "BANCO DO BRASIL SA",
    "nomeFantasia": None,
    "dataInicioAtividades": "26/09/1974",
    "naturezaJuridica": "Sociedade de Economia Mista (2038)",
    "capitalSocial": 120000000000,
    "email": None,
    "telefone": None,
    "municipio": "SAO PAULO",
    "uf": "SP",
    "cep": "04004-040",
    "cnaes": [{"cnae": "64.22-1-00", "descricao": "Bancos"}],
}
RECORD_2 = {**RECORD_1, "cnpj": "11111111000111", "razaoSocial": "EMPRESA DOIS LTDA"}


class _FakeDiscovery:
    def __init__(self, cnpjs: list[str]) -> None:
        self._cnpjs = cnpjs
        self.closed = False

    def discover(self, n: int) -> list[str]:
        return self._cnpjs[:n]

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self, records_by_cnpj: dict[str, dict[str, object]]) -> None:
        self._records = records_by_cnpj
        self.closed = False

    def fetch_many(self, cnpjs: list[str]) -> list[dict[str, object]]:
        return [self._records[c] for c in cnpjs if c in self._records]

    def close(self) -> None:
        self.closed = True


def test_collects_and_activates_with_injected_fakes(tmp_path: Path) -> None:
    warehouse_dir = tmp_path / "warehouse"
    discovery = _FakeDiscovery(["00000000083208", "11111111000111"])
    client = _FakeClient({"00000000083208": RECORD_1, "11111111000111": RECORD_2})

    result = collect_opencnpj_leads(warehouse_dir, n=2, discovery=discovery, client=client)

    assert result.activated
    assert result.materialize_result.n_rows_written == 2
    assert get_active_version(warehouse_dir) == result.version_dir.name

    df = pl.read_parquet((result.version_dir / "pais=BR").as_posix() + "/*.parquet")
    assert set(df["fonte"].unique().to_list()) == {"BR_OPENCNPJ"}


def test_n_limits_how_many_are_discovered(tmp_path: Path) -> None:
    warehouse_dir = tmp_path / "warehouse"
    discovery = _FakeDiscovery(["00000000083208", "11111111000111"])
    client = _FakeClient({"00000000083208": RECORD_1, "11111111000111": RECORD_2})

    result = collect_opencnpj_leads(warehouse_dir, n=1, discovery=discovery, client=client)

    assert result.materialize_result.n_rows_written == 1


def test_does_not_close_injected_discovery_or_client(tmp_path: Path) -> None:
    """Instâncias injetadas (não criadas por `collect_opencnpj_leads`) não devem
    ser fechadas -- quem as passou é dono do ciclo de vida delas."""
    warehouse_dir = tmp_path / "warehouse"
    discovery = _FakeDiscovery(["00000000083208"])
    client = _FakeClient({"00000000083208": RECORD_1})

    collect_opencnpj_leads(warehouse_dir, n=1, discovery=discovery, client=client)

    assert discovery.closed is False
    assert client.closed is False
