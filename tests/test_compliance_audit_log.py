"""Testes de `compliance/audit_log.py`: criação de evento, persistência (append via
leitura+reescrita) e leitura do log."""

from __future__ import annotations

import json
from pathlib import Path

from src.compliance.audit_log import new_event, read_audit_log, record_event


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
