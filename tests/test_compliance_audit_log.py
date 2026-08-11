"""Testes de `compliance/audit_log.py`: criação de evento, persistência (append via
leitura+reescrita), leitura, consulta filtrada e exportação do log."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from src.compliance.audit_log import (
    AuditEvent,
    export_audit_log,
    new_event,
    query_audit_log,
    read_audit_log,
    record_event,
)


class TestNewEvent:
    def test_defaults(self) -> None:
        event = new_event("export_csv")
        assert event.operacao == "export_csv"
        assert event.usuario  # cai pro usuário do SO, não deve ficar vazio
        assert event.filtros == {}
        assert event.n_registros == 0
        assert event.destino is None

    def test_usuario_explicito_sobrepoe_default(self) -> None:
        event = new_event("export_csv", usuario="italo")
        assert event.usuario == "italo"

    def test_filtros_e_destino(self) -> None:
        event = new_event(
            "export_xlsx", filtros={"regiao": "SP"}, n_registros=42, destino=Path("x.xlsx")
        )
        assert event.filtros == {"regiao": "SP"}
        assert event.n_registros == 42
        assert event.destino == str(Path("x.xlsx"))


class TestRecordAndReadAuditLog:
    def test_read_missing_log_returns_empty_dataframe_with_schema(self, tmp_path: Path) -> None:
        df = read_audit_log(tmp_path / "nao-existe.parquet")
        assert df.height == 0
        expected_columns = {"operacao", "usuario", "quando", "filtros", "n_registros", "destino"}
        assert set(df.columns) == expected_columns

    def test_record_single_event(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        event = new_event("export_csv", usuario="italo", filtros={"pais": "BR"}, n_registros=10)

        record_event(event, log_path)

        df = read_audit_log(log_path)
        assert df.height == 1
        row = df.to_dicts()[0]
        assert row["operacao"] == "export_csv"
        assert row["usuario"] == "italo"
        assert row["n_registros"] == 10
        assert json.loads(row["filtros"]) == {"pais": "BR"}

    def test_record_appends_not_overwrites(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        record_event(new_event("export_csv", usuario="a", n_registros=1), log_path)
        record_event(new_event("export_xlsx", usuario="b", n_registros=2), log_path)

        df = read_audit_log(log_path)
        assert df.height == 2
        assert set(df["operacao"].to_list()) == {"export_csv", "export_xlsx"}

    def test_filtros_with_non_json_native_values_are_stringified(self, tmp_path: Path) -> None:
        from datetime import date
        from decimal import Decimal

        log_path = tmp_path / "audit.parquet"
        event = new_event(
            "export_csv",
            filtros={"capital_social_min": Decimal("1000.00"), "aberta_apos": date(2020, 1, 1)},
        )
        record_event(event, log_path)

        row = read_audit_log(log_path).to_dicts()[0]
        parsed = json.loads(row["filtros"])
        assert parsed == {"capital_social_min": "1000.00", "aberta_apos": "2020-01-01"}


def _record(
    log_path: Path, *, operacao: str, usuario: str, quando: datetime, n_registros: int = 0
) -> None:
    """Registra um evento com `quando` controlado (`new_event` sempre usa "agora") —
    necessário pros testes de filtro por período/ordenação."""
    record_event(
        AuditEvent(operacao=operacao, usuario=usuario, quando=quando, n_registros=n_registros),
        log_path,
    )


class TestQueryAuditLog:
    def test_no_filters_returns_everything_sorted_desc(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path, operacao="export_csv", usuario="a", quando=datetime(2026, 1, 1, tzinfo=UTC)
        )
        _record(log_path, operacao="query", usuario="b", quando=datetime(2026, 1, 3, tzinfo=UTC))
        _record(
            log_path, operacao="enrich_leads", usuario="c", quando=datetime(2026, 1, 2, tzinfo=UTC)
        )

        df = query_audit_log(log_path)

        assert df["operacao"].to_list() == ["query", "enrich_leads", "export_csv"]

    def test_filters_by_operacao(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path, operacao="export_csv", usuario="a", quando=datetime(2026, 1, 1, tzinfo=UTC)
        )
        _record(log_path, operacao="query", usuario="a", quando=datetime(2026, 1, 2, tzinfo=UTC))

        df = query_audit_log(log_path, operacao="export_csv")

        assert df.height == 1
        assert df["operacao"].to_list() == ["export_csv"]

    def test_filters_by_usuario(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path,
            operacao="export_csv",
            usuario="italo",
            quando=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _record(
            log_path,
            operacao="export_csv",
            usuario="outra_pessoa",
            quando=datetime(2026, 1, 1, tzinfo=UTC),
        )

        df = query_audit_log(log_path, usuario="italo")

        assert df.height == 1
        assert df["usuario"].to_list() == ["italo"]

    def test_filters_by_date_range(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(log_path, operacao="a", usuario="x", quando=datetime(2026, 1, 1, tzinfo=UTC))
        _record(log_path, operacao="b", usuario="x", quando=datetime(2026, 1, 15, tzinfo=UTC))
        _record(log_path, operacao="c", usuario="x", quando=datetime(2026, 1, 31, tzinfo=UTC))

        df = query_audit_log(
            log_path,
            desde=datetime(2026, 1, 10, tzinfo=UTC),
            ate=datetime(2026, 1, 20, tzinfo=UTC),
        )

        assert df["operacao"].to_list() == ["b"]

    def test_combined_filters_are_anded(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path,
            operacao="export_csv",
            usuario="italo",
            quando=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _record(
            log_path,
            operacao="export_csv",
            usuario="outra_pessoa",
            quando=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _record(
            log_path, operacao="query", usuario="italo", quando=datetime(2026, 1, 1, tzinfo=UTC)
        )

        df = query_audit_log(log_path, operacao="export_csv", usuario="italo")

        assert df.height == 1

    def test_missing_log_returns_empty(self, tmp_path: Path) -> None:
        df = query_audit_log(tmp_path / "nao-existe.parquet", operacao="export_csv")
        assert df.height == 0


class TestExportAuditLog:
    def test_writes_csv_with_all_events(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path,
            operacao="export_csv",
            usuario="a",
            quando=datetime(2026, 1, 1, tzinfo=UTC),
            n_registros=10,
        )
        _record(
            log_path,
            operacao="query",
            usuario="b",
            quando=datetime(2026, 1, 2, tzinfo=UTC),
            n_registros=5,
        )

        dest = export_audit_log(tmp_path / "trilha.csv", log_path=log_path)

        assert dest == tmp_path / "trilha.csv"
        with dest.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert {row["operacao"] for row in rows} == {"export_csv", "query"}

    def test_applies_filters_before_exporting(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path, operacao="export_csv", usuario="a", quando=datetime(2026, 1, 1, tzinfo=UTC)
        )
        _record(log_path, operacao="query", usuario="a", quando=datetime(2026, 1, 1, tzinfo=UTC))

        dest = export_audit_log(tmp_path / "trilha.csv", log_path=log_path, operacao="query")

        with dest.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["operacao"] == "query"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.parquet"
        _record(
            log_path, operacao="export_csv", usuario="a", quando=datetime(2026, 1, 1, tzinfo=UTC)
        )

        dest = export_audit_log(tmp_path / "nested" / "dir" / "trilha.csv", log_path=log_path)

        assert dest.is_file()

    def test_empty_log_exports_header_only(self, tmp_path: Path) -> None:
        dest = export_audit_log(tmp_path / "trilha.csv", log_path=tmp_path / "nao-existe.parquet")

        with dest.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows == []
