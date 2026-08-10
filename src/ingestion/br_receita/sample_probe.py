"""Probe de validação do parsing de um arquivo de amostra da base CNPJ (Receita Federal).

Objetivo: confirmar, com uma amostra pequena, que sabemos ler corretamente o formato
bruto dos arquivos oficiais (EMPRECSV, ESTABELE, SOCIOCSV, SIMPLES, tabelas auxiliares)
antes de construir o conector de ingestão completo:

- encoding ISO-8859-1 (latin-1);
- separador ';';
- sem linha de cabeçalho;
- aspas duplas como qualificador de string.

Este script é agnóstico ao layout de colunas: usa os nomes genéricos que o próprio
DuckDB atribui (`column00`, `column01`, ...). Mapear nomes de campo reais (CNPJ_BASICO,
NOME_FANTASIA, CNAE, ...) é responsabilidade do `parser.py`/`canonical.py`, na Fase 1 de
ingestão, depois que o layout for conferido contra a documentação oficial.

DuckDB não lê CSV em latin-1 nativamente (só aceita UTF-8), então o arquivo é
decodificado em Python e regravado num arquivo temporário UTF-8 antes de ser carregado.

Uso:
    python -m src.ingestion.br_receita.sample_probe <caminho_para_csv> [--sample-size N]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb

CNPJ_SOURCE_ENCODING = "latin-1"
CNPJ_CSV_SEP = ";"
CNPJ_QUOTECHAR = '"'

DEFAULT_SAMPLE_SIZE = 5


@dataclass(frozen=True)
class ProbeResult:
    """Estatísticas de validação de um arquivo CSV de amostra da base CNPJ."""

    csv_path: Path
    n_rows: int
    columns: list[str]
    sample_rows: list[tuple[str | None, ...]]
    non_null_counts: dict[str, int]

    @property
    def n_cols(self) -> int:
        """Número de colunas detectadas no CSV."""
        return len(self.columns)


def probe_csv(csv_path: str | Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> ProbeResult:
    """Lê um CSV de amostra da Receita Federal e calcula estatísticas de validação.

    Args:
        csv_path: caminho para um arquivo CSV de amostra (ex.: um pedaço do ESTABELE),
            no layout oficial: encoding ISO-8859-1, separador ';', sem cabeçalho,
            aspas duplas como qualificador de string.
        sample_size: quantidade de linhas a incluir em `ProbeResult.sample_rows`.

    Returns:
        Estatísticas de validação: nº de linhas, nº de colunas, amostra de linhas e
        contagem de valores não-nulos por coluna.

    Raises:
        FileNotFoundError: se `csv_path` não existir.
        UnicodeDecodeError: se o arquivo não estiver de fato em ISO-8859-1.
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Arquivo de amostra não encontrado: {csv_path}")

    # DuckDB só lê CSV em UTF-8: decodifica como latin-1 e regrava num temporário UTF-8.
    text = csv_path.read_text(encoding=CNPJ_SOURCE_ENCODING)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", encoding="utf-8", newline="", delete=False
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)

    try:
        with duckdb.connect(":memory:") as con:
            relation = con.read_csv(
                str(tmp_path),
                header=False,
                sep=CNPJ_CSV_SEP,
                quotechar=CNPJ_QUOTECHAR,
                all_varchar=True,
            )
            con.register("sample", relation)

            columns = list(relation.columns)

            n_rows_row = con.execute("SELECT count(*) FROM sample").fetchone()
            assert n_rows_row is not None  # COUNT(*) sempre retorna uma linha
            n_rows = n_rows_row[0]

            sample_rows = con.execute(
                f"SELECT * FROM sample LIMIT {int(sample_size)}"
            ).fetchall()

            # Uma única query para todas as colunas em vez de N round-trips.
            count_exprs = ", ".join(f'count("{col}")' for col in columns)
            counts_row = con.execute(f"SELECT {count_exprs} FROM sample").fetchone()
            assert counts_row is not None  # COUNT(...) sempre retorna uma linha
            non_null_counts = dict(zip(columns, counts_row, strict=True))
    finally:
        tmp_path.unlink(missing_ok=True)

    return ProbeResult(
        csv_path=csv_path,
        n_rows=n_rows,
        columns=columns,
        sample_rows=sample_rows,
        non_null_counts=non_null_counts,
    )


def format_report(result: ProbeResult) -> str:
    """Formata um `ProbeResult` como relatório legível para humanos."""
    lines = [
        f"Arquivo: {result.csv_path}",
        f"Linhas: {result.n_rows}",
        f"Colunas: {result.n_cols}",
        "",
        f"Amostra (primeiras {len(result.sample_rows)} linhas):",
        " | ".join(result.columns),
    ]
    for row in result.sample_rows:
        lines.append(" | ".join("" if v is None else str(v) for v in row))

    lines.append("")
    lines.append("Valores não-nulos por coluna:")
    for col in result.columns:
        lines.append(f"  {col}: {result.non_null_counts[col]}/{result.n_rows}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada de linha de comando do probe."""
    # Em alguns terminais Windows, stdout redirecionado usa a codepage do console
    # (não UTF-8) e falha ao imprimir nomes com acentuação (ç, ã, é). O parsing em
    # si já é validado pelos testes; isto só garante que o relatório impresso também
    # exiba os acentos corretamente, independente da codepage do terminal.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description=(
            "Valida o parsing (encoding latin-1, separador ';', sem cabeçalho, "
            "aspas duplas como qualificador) de um arquivo CSV de amostra da base "
            "CNPJ da Receita Federal."
        )
    )
    parser.add_argument("csv_path", help="Caminho para o CSV de amostra")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Quantidade de linhas a exibir na amostra (default: {DEFAULT_SAMPLE_SIZE})",
    )
    args = parser.parse_args(argv)

    try:
        result = probe_csv(args.csv_path, sample_size=args.sample_size)
    except FileNotFoundError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(
            f"Erro: arquivo não parece estar em ISO-8859-1 (latin-1): {exc}",
            file=sys.stderr,
        )
        return 1

    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
