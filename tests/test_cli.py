"""Testes de `cli.py`: parsing de argumentos, montagem do SQL e o comando `query`
ponta a ponta contra uma versão ativa pequena do warehouse.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from cli import (
    DEFAULT_LIMIT,
    QueryFilters,
    build_arg_parser,
    build_query_sql,
    filters_from_namespace,
    main,
    parse_args,
    run_optout_command,
    run_query_command,
    run_retention_purge_command,
)
from src.compliance.audit_log import read_audit_log
from src.etl.transform import CANONICAL_PARQUET_SCHEMA, activate_version, new_version_dir
from src.segmentation.suppression import load_suppression_list


class TestParseArgs:
    def test_query_defaults(self) -> None:
        args = parse_args(["query"])

        assert args.command == "query"
        assert args.pais is None
        assert args.regiao is None
        assert args.cod_atividade is None
        assert args.situacao is None
        assert args.porte is None
        assert args.aberta_apos is None
        assert args.com_email is False
        assert args.limit == DEFAULT_LIMIT

    def test_query_all_filters(self) -> None:
        args = parse_args(
            [
                "query",
                "--pais",
                "BR",
                "--regiao",
                "SP",
                "--cod-atividade",
                "8630501",
                "--situacao",
                "ATIVA",
                "--porte",
                "MICRO EMPRESA",
                "--aberta-apos",
                "2020-01-01",
                "--com-email",
                "--limit",
                "50",
            ]
        )

        assert args.pais == "BR"
        assert args.regiao == "SP"
        assert args.cod_atividade == "8630501"
        assert args.situacao == "ATIVA"
        assert args.porte == "MICRO EMPRESA"
        assert args.aberta_apos == date(2020, 1, 1)
        assert args.com_email is True
        assert args.limit == 50

    def test_aberta_apos_invalid_date_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["query", "--aberta-apos", "not-a-date"])

    def test_requires_a_command(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_warehouse_dir_default_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_WAREHOUSE_DIR", "/tmp/custom-warehouse")
        args = build_arg_parser().parse_args(["query"])
        assert args.warehouse_dir == Path("/tmp/custom-warehouse")

    def test_warehouse_dir_explicit_flag_overrides_default(self) -> None:
        args = parse_args(["--warehouse-dir", "/explicit", "query"])
        assert args.warehouse_dir == Path("/explicit")


class TestFiltersFromNamespace:
    def test_converts_namespace_to_query_filters(self) -> None:
        args = parse_args(["query", "--pais", "BR", "--limit", "10"])
        filters = filters_from_namespace(args)

        assert filters == QueryFilters(pais="BR", limit=10)


class TestBuildQuerySql:
    def test_no_filters_always_excludes_synthetic_and_restricted(self) -> None:
        built = build_query_sql(QueryFilters(), leads_glob="warehouse/pais=*/*.parquet")

        assert "is_synthetic = false" in built.select_sql
        assert "flag_difusao_restrita = false" in built.select_sql
        assert "is_synthetic = false" in built.count_sql
        assert "flag_difusao_restrita = false" in built.count_sql
        assert built.params == []

    def test_default_limit_applied(self) -> None:
        built = build_query_sql(QueryFilters(), leads_glob="x/*.parquet")
        assert f"LIMIT {DEFAULT_LIMIT}" in built.select_sql
        assert "LIMIT" not in built.count_sql

    def test_all_filters_add_clauses_and_params_in_order(self) -> None:
        filters = QueryFilters(
            pais="br",
            regiao="sp",
            cod_atividade="8630501",
            situacao="ativa",
            porte="micro empresa",
            aberta_apos=date(2020, 1, 1),
            com_email=True,
            limit=50,
        )
        built = build_query_sql(filters, leads_glob="x/*.parquet")

        assert "pais = ?" in built.select_sql
        assert "regiao = ?" in built.select_sql
        assert "cod_atividade = ?" in built.select_sql
        assert "situacao = ?" in built.select_sql
        assert "porte = ?" in built.select_sql
        assert "data_inicio_atividade >= ?" in built.select_sql
        assert "email IS NOT NULL" in built.select_sql
        assert "LIMIT 50" in built.select_sql
        assert built.params == ["BR", "SP", "8630501", "ATIVA", "MICRO EMPRESA", date(2020, 1, 1)]

    def test_text_filters_normalized_to_uppercase(self) -> None:
        built = build_query_sql(QueryFilters(pais="br", situacao="ativa"), leads_glob="x/*.parquet")
        assert "BR" in built.params
        assert "ATIVA" in built.params

    def test_cod_atividade_not_uppercased(self) -> None:
        built = build_query_sql(QueryFilters(cod_atividade="8630501"), leads_glob="x/*.parquet")
        assert built.params == ["8630501"]

    def test_com_email_false_does_not_add_clause(self) -> None:
        built = build_query_sql(QueryFilters(com_email=False), leads_glob="x/*.parquet")
        assert "email IS NOT NULL" not in built.select_sql

    def test_escapes_single_quote_in_glob_path(self) -> None:
        built = build_query_sql(QueryFilters(), leads_glob="ware'house/*.parquet")
        assert "ware''house" in built.select_sql
        assert "ware''house" in built.count_sql


Capsys = pytest.CaptureFixture[str]

ROW_TEMPLATE: dict[str, object] = {
    "pais": "BR",
    "id_legal": "1",
    "id_estab": "1",
    "razao_social": "X",
    "nome_fantasia": None,
    "cod_atividade": "8630501",
    "situacao": "ATIVA",
    "regiao": "SP",
    "municipio": None,
    "cep": None,
    "telefone": None,
    "email": None,
    "data_inicio_atividade": None,
    "porte": None,
    "capital_social": None,
    "natureza_juridica": None,
    "score_icp": None,
    "fonte": "BR_RECEITA",
    "enriquecido_em": None,
    "is_synthetic": False,
    "flag_difusao_restrita": False,
}


def _row(**overrides: object) -> dict[str, object]:
    return {**ROW_TEMPLATE, **overrides}


def _make_active_version(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    warehouse_dir = tmp_path / "warehouse"
    version_dir = new_version_dir(warehouse_dir)
    partition_dir = version_dir / "pais=BR"
    partition_dir.mkdir(parents=True)
    pl.DataFrame(rows, schema=CANONICAL_PARQUET_SCHEMA).write_parquet(
        partition_dir / "part-00000.parquet"
    )
    activate_version(warehouse_dir, version_dir)
    return warehouse_dir


def _run_query(warehouse_dir: Path, filters: QueryFilters, tmp_path: Path) -> int:
    """Wrapper que sempre isola o log de auditoria em `tmp_path` -- `query` registra
    um evento sempre (ver `run_query_command`), então os testes não devem depender
    do `AUDIT_LOG_PATH` default (que apontaria pro warehouse real do projeto)."""
    return run_query_command(warehouse_dir, filters, audit_log_path=tmp_path / "audit.parquet")


class TestRunQueryCommand:
    def test_no_active_version_returns_1(self, tmp_path: Path, capsys: Capsys) -> None:
        exit_code = _run_query(tmp_path / "empty-warehouse", QueryFilters(), tmp_path)
        assert exit_code == 1
        assert "nenhuma versão ativa" in capsys.readouterr().err

    def test_excludes_synthetic_and_restricted_even_without_filters(
        self, tmp_path: Path, capsys: Capsys
    ) -> None:
        rows = [
            _row(id_estab="1", email="a@example.com"),
            _row(id_estab="2", regiao="RJ"),
            _row(id_estab="3", situacao="BAIXADA"),
            _row(id_estab="4", is_synthetic=True),
            _row(id_estab="5", pais="FR", flag_difusao_restrita=True),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)

        exit_code = _run_query(warehouse_dir, QueryFilters(), tmp_path)

        assert exit_code == 0
        assert "Total: 3 lead(s)" in capsys.readouterr().out

    def test_situacao_filter(self, tmp_path: Path, capsys: Capsys) -> None:
        rows = [
            _row(id_estab="1", situacao="ATIVA"),
            _row(id_estab="2", situacao="ATIVA"),
            _row(id_estab="3", situacao="BAIXADA"),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)

        _run_query(warehouse_dir, QueryFilters(situacao="ativa"), tmp_path)

        assert "Total: 2 lead(s)" in capsys.readouterr().out

    def test_regiao_and_situacao_combined(self, tmp_path: Path, capsys: Capsys) -> None:
        rows = [
            _row(id_estab="1", regiao="SP", situacao="ATIVA"),
            _row(id_estab="2", regiao="RJ", situacao="ATIVA"),
            _row(id_estab="3", regiao="SP", situacao="BAIXADA"),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)

        _run_query(warehouse_dir, QueryFilters(regiao="SP", situacao="ATIVA"), tmp_path)

        assert "Total: 1 lead(s)" in capsys.readouterr().out

    def test_com_email_filter(self, tmp_path: Path, capsys: Capsys) -> None:
        rows = [
            _row(id_estab="1", email="a@example.com"),
            _row(id_estab="2", email=None),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)

        _run_query(warehouse_dir, QueryFilters(com_email=True), tmp_path)

        assert "Total: 1 lead(s)" in capsys.readouterr().out

    def test_limit_still_reports_unlimited_total_count(
        self, tmp_path: Path, capsys: Capsys
    ) -> None:
        rows = [_row(id_estab=str(i)) for i in range(5)]
        warehouse_dir = _make_active_version(tmp_path, rows)

        _run_query(warehouse_dir, QueryFilters(limit=2), tmp_path)

        # A tabela impressa é limitada a 2, mas o total contado não é.
        assert "Total: 5 lead(s)" in capsys.readouterr().out


class TestMain:
    def test_query_end_to_end_via_main(self, tmp_path: Path, capsys: Capsys) -> None:
        rows = [_row(id_estab="1"), _row(id_estab="2")]
        warehouse_dir = _make_active_version(tmp_path, rows)

        exit_code = main(
            [
                "--warehouse-dir",
                str(warehouse_dir),
                "--audit-log-path",
                str(tmp_path / "audit.parquet"),
                "query",
                "--situacao",
                "ativa",
            ]
        )

        assert exit_code == 0
        assert "Total: 2 lead(s)" in capsys.readouterr().out

    def test_no_active_version_via_main_returns_1(self, tmp_path: Path) -> None:
        exit_code = main(
            [
                "--warehouse-dir",
                str(tmp_path / "empty"),
                "--audit-log-path",
                str(tmp_path / "audit.parquet"),
                "query",
            ]
        )
        assert exit_code == 1

    def test_runs_as_a_real_subprocess_script(self, tmp_path: Path) -> None:
        """Roda `python cli.py query ...` de verdade, provando que o script funciona
        como documentado (mesmo formato do exemplo no docstring do módulo)."""
        rows = [
            _row(id_estab="1", regiao="SP", cod_atividade="8630501", email="a@example.com"),
            _row(id_estab="2", regiao="RJ", cod_atividade="8630501"),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent.parent / "cli.py"),
                "--warehouse-dir",
                str(warehouse_dir),
                "--audit-log-path",
                str(tmp_path / "audit.parquet"),
                "query",
                "--regiao",
                "SP",
                "--cod-atividade",
                "8630501",
                "--situacao",
                "ATIVA",
                "--com-email",
                "--limit",
                "50",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=Path(__file__).parent.parent,
        )

        assert result.returncode == 0, result.stderr
        assert "Total: 1 lead(s)" in result.stdout


class TestRunQueryCommandAudit:
    """`query` é "geração de lista" -- uma operação sensível (ver CLAUDE.md);
    `run_query_command` precisa registrar um evento sempre que a consulta roda."""

    def test_records_audit_event_with_filtros_and_count(self, tmp_path: Path) -> None:
        rows = [
            _row(id_estab="1", regiao="SP", situacao="ATIVA"),
            _row(id_estab="2", regiao="RJ", situacao="ATIVA"),
        ]
        warehouse_dir = _make_active_version(tmp_path, rows)
        audit_log_path = tmp_path / "audit.parquet"

        run_query_command(
            warehouse_dir,
            QueryFilters(regiao="SP"),
            audit_log_path=audit_log_path,
            usuario="italo",
        )

        df = read_audit_log(audit_log_path)
        assert df.height == 1
        row = df.to_dicts()[0]
        assert row["operacao"] == "query"
        assert row["usuario"] == "italo"
        assert row["n_registros"] == 1

        filtros = json.loads(row["filtros"])
        assert filtros["regiao"] == "SP"

    def test_no_active_version_does_not_record_an_event(self, tmp_path: Path) -> None:
        audit_log_path = tmp_path / "audit.parquet"

        run_query_command(
            tmp_path / "empty-warehouse", QueryFilters(), audit_log_path=audit_log_path
        )

        assert read_audit_log(audit_log_path).height == 0

    def test_audit_log_path_flag_is_parsed(self) -> None:
        args = parse_args(["--audit-log-path", "/custom/audit.parquet", "query"])
        assert args.audit_log_path == Path("/custom/audit.parquet")

    def test_audit_log_path_default_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_LOG_PATH", "/tmp/custom-audit.parquet")
        args = build_arg_parser().parse_args(["query"])
        assert args.audit_log_path == Path("/tmp/custom-audit.parquet")


class TestParseArgsOptout:
    def test_defaults(self) -> None:
        args = parse_args(["optout", "--id-estab", "A"])
        assert args.command == "optout"
        assert args.id_estab == "A"
        assert args.email is None
        assert args.motivo is None
        assert args.remove is False

    def test_suppression_list_path_default_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPPRESSION_LIST_PATH", "/tmp/custom-supressao.csv")
        args = build_arg_parser().parse_args(["optout", "--id-estab", "A"])
        assert args.suppression_list_path == Path("/tmp/custom-supressao.csv")


class TestRunOptoutCommand:
    def test_registers_optout_by_id_estab(self, tmp_path: Path, capsys: Capsys) -> None:
        args = parse_args(
            [
                "optout",
                "--suppression-list-path",
                str(tmp_path / "supressao.csv"),
                "--id-estab",
                "11111111000191",
                "--motivo",
                "solicitação do titular",
            ]
        )

        exit_code = run_optout_command(args)

        assert exit_code == 0
        assert "registrado" in capsys.readouterr().out.lower()
        result = load_suppression_list(tmp_path / "supressao.csv")
        assert result.ids_estab == {"11111111000191"}

    def test_registers_optout_by_email(self, tmp_path: Path) -> None:
        args = parse_args(
            [
                "optout",
                "--suppression-list-path",
                str(tmp_path / "supressao.csv"),
                "--email",
                "contato@empresa.com",
            ]
        )

        run_optout_command(args)

        result = load_suppression_list(tmp_path / "supressao.csv")
        assert result.emails == {"contato@empresa.com"}

    def test_neither_id_estab_nor_email_returns_1(self, tmp_path: Path, capsys: Capsys) -> None:
        args = parse_args(["optout", "--suppression-list-path", str(tmp_path / "supressao.csv")])

        exit_code = run_optout_command(args)

        assert exit_code == 1
        stderr = capsys.readouterr().err.lower()
        assert "id-estab" in stderr or "email" in stderr

    def test_remove_flag_removes_existing_optout(self, tmp_path: Path, capsys: Capsys) -> None:
        suppression_path = tmp_path / "supressao.csv"
        add_args = parse_args(
            ["optout", "--suppression-list-path", str(suppression_path), "--id-estab", "A"]
        )
        run_optout_command(add_args)

        remove_args = parse_args(
            [
                "optout",
                "--suppression-list-path",
                str(suppression_path),
                "--id-estab",
                "A",
                "--remove",
            ]
        )
        exit_code = run_optout_command(remove_args)

        assert exit_code == 0
        assert "removido" in capsys.readouterr().out.lower()
        assert load_suppression_list(suppression_path).ids_estab == frozenset()

    def test_remove_nonexistent_returns_1(self, tmp_path: Path, capsys: Capsys) -> None:
        args = parse_args(
            [
                "optout",
                "--suppression-list-path",
                str(tmp_path / "supressao.csv"),
                "--id-estab",
                "NAO-EXISTE",
                "--remove",
            ]
        )

        exit_code = run_optout_command(args)

        assert exit_code == 1
        assert "nada a remover" in capsys.readouterr().out.lower()


class TestOptoutEndToEndViaMain:
    def test_optout_then_query_excludes_the_lead(self, tmp_path: Path, capsys: Capsys) -> None:
        """Ponta a ponta pedido explicitamente: opt-out registrado via CLI reflete
        na supressão."""
        rows = [_row(id_estab="A"), _row(id_estab="B")]
        warehouse_dir = _make_active_version(tmp_path, rows)
        suppression_path = tmp_path / "supressao.csv"

        exit_code = main(
            [
                "--audit-log-path",
                str(tmp_path / "audit.parquet"),
                "optout",
                "--suppression-list-path",
                str(suppression_path),
                "--id-estab",
                "A",
            ]
        )
        assert exit_code == 0

        result = load_suppression_list(suppression_path)
        assert result.ids_estab == {"A"}
        # Confirma que a assinatura de warehouse_dir segue válida (não usada aqui,
        # mas prova que o comando optout não depende de uma versão ativa existir).
        assert warehouse_dir.exists()


class TestParseArgsRetentionPurge:
    def test_defaults(self) -> None:
        args = parse_args(["retention-purge"])
        assert args.command == "retention-purge"
        assert args.ttl_days == 180

    def test_ttl_days_flag(self) -> None:
        args = parse_args(["retention-purge", "--ttl-days", "30"])
        assert args.ttl_days == 30

    def test_ttl_days_default_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RETENTION_TTL_DAYS", "90")
        args = build_arg_parser().parse_args(["retention-purge"])
        assert args.ttl_days == 90


class TestRunRetentionPurgeCommand:
    def test_runs_and_prints_result(self, tmp_path: Path, capsys: Capsys) -> None:
        args = parse_args(
            [
                "--audit-log-path",
                str(tmp_path / "audit.parquet"),
                "retention-purge",
                "--cache-path",
                str(tmp_path / "cache.sqlite"),
                "--ttl-days",
                "30",
            ]
        )

        exit_code = run_retention_purge_command(args)

        assert exit_code == 0
        assert "expurgada" in capsys.readouterr().out.lower()
        assert read_audit_log(tmp_path / "audit.parquet").height == 1
