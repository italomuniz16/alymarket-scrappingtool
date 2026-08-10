"""Gera dados de DEMONSTRAÇÃO via Faker (locales `pt_BR`/`fr_FR`) — NUNCA scraping,
NUNCA dado real (ver CLAUDE.md, docs/PRD.md §1.4).

Todo registro sai no schema canônico (`CanonicalLead`, ver `src/ingestion/base.py`)
com `is_synthetic=True` e `fonte="DEMO"`. `segmentation/suppression.py` trata os dois
como exclusão hard antes de qualquer exportação — este módulo é o único lugar do
projeto que deveria sequer produzir `is_synthetic=True`.

`write_demo_leads` grava num diretório fisicamente separado (`data/warehouse/
demo_leads/pais={pais}/`, não `data/warehouse/versions/...` — a árvore usada pelos
dados reais em `etl/transform.py`) e recusa (levanta `ValueError`) gravar qualquer
registro que não seja `is_synthetic=True`: "fisicamente isolado dos dados reais" não é
só convenção de nome de pasta, é reforçado em código.

Identificadores (CNPJ/SIREN+SIRET) são gerados com dígito verificador válido pelo
próprio Faker (`fake.cnpj()`/`fake.siret()`), não por scraping de gerador de CPF/CNPJ
de terceiros — daí a escolha de biblioteca no PRD (§1.4).

Uso:
    python -m src.seed.synthetic --gerar 100 --pais BR
    python -m src.seed.synthetic --gerar 50 --pais FR --proporcoes docs/proporcoes_demo.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
from faker import Faker

from src.etl.transform import CANONICAL_PARQUET_SCHEMA
from src.ingestion.base import CanonicalLead

logger = logging.getLogger(__name__)

FONTE_DEMO = "DEMO"
DEFAULT_DEMO_DIR = Path("./data/warehouse/demo_leads")

_LOCALE_BY_PAIS: dict[str, str] = {"BR": "pt_BR", "FR": "fr_FR"}

# CNAE (BR) / NAF (FR): Faker não tem provider pra isso -- pool pequeno e plausível,
# usado quando nenhum arquivo de proporções é passado.
_DEFAULT_ATIVIDADES: dict[str, tuple[str, ...]] = {
    "BR": ("4721102", "8630501", "4781400", "6201501", "5611203", "4712100"),
    "FR": ("56.10A", "62.01Z", "47.11F", "86.21Z", "43.21A"),
}

_DEFAULT_SITUACOES: tuple[str, ...] = ("ATIVA", "ATIVA", "ATIVA", "ATIVA", "BAIXADA", "SUSPENSA")
# "ATIVA" repetido de propósito: a maioria dos leads reais é ativa; pondera sem
# precisar de outra estrutura de pesos separada da lista de porte abaixo.
_DEFAULT_PORTES: tuple[str, ...] = (
    "MICRO EMPRESA",
    "MICRO EMPRESA",
    "EMPRESA DE PEQUENO PORTE",
    "DEMAIS",
)

_NATUREZA_JURIDICA_PADRAO: dict[str, str] = {
    "BR": "SOCIEDADE EMPRESARIA LIMITADA",
    "FR": "SOCIETE A RESPONSABILITE LIMITEE",
}


@dataclass(frozen=True)
class ProportionSpec:
    """Distribuição de valores possíveis para uma dimensão (`atividade`/`regiao`),
    usada para amostrar sinteticamente parecido com uma distribuição real."""

    valores: tuple[str, ...]
    pesos: tuple[float, ...]


def load_proportions(path: Path) -> dict[str, ProportionSpec]:
    """Carrega um arquivo de proporções (CSV, colunas `dimensao,valor,peso`) e agrupa
    por dimensão — ex.: `atividade,4721102,35` / `regiao,SP,40`.

    Linhas com qualquer coluna vazia são ignoradas. Os pesos não precisam somar 100
    (são relativos entre si, dentro de cada dimensão).
    """
    grouped: dict[str, tuple[list[str], list[float]]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            dimensao = (row.get("dimensao") or "").strip()
            valor = (row.get("valor") or "").strip()
            peso_raw = (row.get("peso") or "").strip()
            if not dimensao or not valor or not peso_raw:
                continue
            valores, pesos = grouped.setdefault(dimensao, ([], []))
            valores.append(valor)
            pesos.append(float(peso_raw))

    return {
        dimensao: ProportionSpec(valores=tuple(v), pesos=tuple(p))
        for dimensao, (v, p) in grouped.items()
    }


class SyntheticLeadGenerator:
    """Gera leads de demonstração para um país, via Faker.

    Args:
        pais: `"BR"` ou `"FR"`.
        proporcoes: distribuições opcionais de `atividade`/`regiao` (ver
            `load_proportions`) para a demo espelhar uma distribuição real, em vez do
            pool default pequeno (`atividade`) ou da geografia genérica do Faker
            (`regiao`).
        seed: opcional, fixa a semente do Faker — mesma semente produz a mesma
            sequência de leads (útil pra testes determinísticos).
    """

    def __init__(
        self,
        pais: str,
        *,
        proporcoes: dict[str, ProportionSpec] | None = None,
        seed: int | None = None,
    ) -> None:
        if pais not in _LOCALE_BY_PAIS:
            raise ValueError(f"País não suportado para geração sintética: {pais!r}")
        self.pais = pais
        self._faker = Faker(_LOCALE_BY_PAIS[pais])
        if seed is not None:
            self._faker.seed_instance(seed)
        self._proporcoes = proporcoes or {}

    def _weighted_choice(self, spec: ProportionSpec) -> str:
        elements = OrderedDict(zip(spec.valores, spec.pesos, strict=True))
        escolhido = self._faker.random_elements(elements=elements, length=1, use_weighting=True)
        return str(escolhido[0])

    def _amostrar(self, dimensao: str, default: tuple[str, ...]) -> str:
        spec = self._proporcoes.get(dimensao)
        if spec is not None:
            return self._weighted_choice(spec)
        return str(self._faker.random_element(default))

    def _regiao(self) -> str:
        spec = self._proporcoes.get("regiao")
        if spec is not None:
            return self._weighted_choice(spec)
        if self.pais == "BR":
            return str(self._faker.state_abbr())
        return str(self._faker.department()[0])

    def _ids(self) -> tuple[str, str]:
        """`(id_legal, id_estab)` — CNPJ básico/completo (BR) ou SIREN/SIRET (FR),
        derivados de UM único valor gerado (não duas chamadas independentes ao
        Faker), pra `id_legal` ser sempre o prefixo real de `id_estab`."""
        if self.pais == "BR":
            digits = "".join(c for c in self._faker.cnpj() if c.isdigit())
            return digits[:8], digits
        digits = "".join(c for c in self._faker.siret() if c.isdigit())
        return digits[:9], digits

    def gerar_um(self) -> dict[str, Any]:
        """Gera um único lead sintético, validado contra `CanonicalLead`."""
        id_legal, id_estab = self._ids()
        telefone = "".join(c for c in str(self._faker.phone_number()) if c.isdigit())
        data_inicio_atividade: date = self._faker.date_between(start_date="-15y", end_date="today")
        capital_social = self._faker.pydecimal(
            left_digits=6, right_digits=2, positive=True, min_value=1_000, max_value=500_000
        )

        lead = CanonicalLead(
            pais=self.pais,  # type: ignore[arg-type]
            id_legal=id_legal,
            id_estab=id_estab,
            razao_social=f"{self._faker.company()}",
            nome_fantasia=f"{self._faker.company()}",
            cod_atividade=self._amostrar("atividade", _DEFAULT_ATIVIDADES[self.pais]),
            situacao=self._faker.random_element(_DEFAULT_SITUACOES),
            regiao=self._regiao(),
            municipio=str(self._faker.city()),
            cep=str(self._faker.postcode()),
            telefone=telefone,
            email=str(self._faker.company_email()),
            data_inicio_atividade=data_inicio_atividade,
            porte=self._faker.random_element(_DEFAULT_PORTES),
            capital_social=capital_social,
            natureza_juridica=_NATUREZA_JURIDICA_PADRAO[self.pais],
            score_icp=None,
            fonte=FONTE_DEMO,
            enriquecido_em=None,
            is_synthetic=True,
            flag_difusao_restrita=False,
        )
        return lead.model_dump()

    def gerar_muitos(self, n: int) -> Iterator[dict[str, Any]]:
        """Gera `n` leads sintéticos, um de cada vez (generator, não acumula em memória)."""
        for _ in range(n):
            yield self.gerar_um()


def gerar_leads_sinteticos(
    n: int,
    pais: str,
    *,
    proporcoes_path: Path | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Conveniência: monta um `SyntheticLeadGenerator` e gera `n` leads de uma vez."""
    proporcoes = load_proportions(proporcoes_path) if proporcoes_path is not None else None
    gerador = SyntheticLeadGenerator(pais, proporcoes=proporcoes, seed=seed)
    return list(gerador.gerar_muitos(n))


def write_demo_leads(
    leads: Iterable[dict[str, Any]],
    demo_dir: Path | str = DEFAULT_DEMO_DIR,
    *,
    pais: str,
) -> Path:
    """Grava `leads` em `demo_dir/pais={pais}/demo.parquet` — uma árvore de diretórios
    fisicamente separada da usada pelos dados reais (`etl/transform.py` escreve em
    `warehouse_dir/versions/...`, nunca aqui), pra nunca ser lida por engano como se
    fosse dado real.

    Raises:
        ValueError: se `leads` tiver qualquer registro com `is_synthetic` diferente
            de `True` — a garantia de isolamento é reforçada em código, não só pelo
            nome do diretório.
    """
    leads_list = list(leads)
    if any(not lead.get("is_synthetic") for lead in leads_list):
        raise ValueError(
            "write_demo_leads só aceita registros com is_synthetic=True "
            "(recebeu ao menos um registro sem essa flag)."
        )

    partition_dir = Path(demo_dir) / f"pais={pais}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    path = partition_dir / "demo.parquet"

    rows = []
    for lead in leads_list:
        row = dict(lead)
        if row.get("capital_social") is not None:
            row["capital_social"] = float(row["capital_social"])
        rows.append(row)

    pl.DataFrame(rows, schema=CANONICAL_PARQUET_SCHEMA).write_parquet(path)
    logger.info("%d lead(s) sintético(s) gravado(s) em %s", len(rows), path)
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.seed.synthetic",
        description=(
            "Gera dados de DEMONSTRAÇÃO (via Faker) para popular o dashboard antes "
            "de haver dado real carregado. Nunca faz scraping; nunca gera dado real."
        ),
    )
    parser.add_argument(
        "--gerar", type=int, required=True, dest="n", help="Quantidade de leads a gerar."
    )
    parser.add_argument(
        "--pais", choices=sorted(_LOCALE_BY_PAIS), required=True, help="País do locale."
    )
    parser.add_argument(
        "--proporcoes",
        type=Path,
        default=None,
        help=(
            "CSV opcional (dimensao,valor,peso) para espelhar uma distribuição "
            "real de atividade/região."
        ),
    )
    parser.add_argument(
        "--demo-dir",
        type=Path,
        default=DEFAULT_DEMO_DIR,
        help=f"Diretório isolado de saída (default: {DEFAULT_DEMO_DIR}).",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Semente opcional, para reprodutibilidade."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = build_arg_parser().parse_args(argv)
    leads = gerar_leads_sinteticos(
        args.n, args.pais, proporcoes_path=args.proporcoes, seed=args.seed
    )
    path = write_demo_leads(leads, args.demo_dir, pais=args.pais)

    print(f"{len(leads)} lead(s) sintético(s) (is_synthetic=true, fonte=DEMO) gerado(s) em {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
