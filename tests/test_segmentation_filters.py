"""Testes de `segmentation/filters.py`: cada filtro isoladamente, a composição
(`build_where_clauses`), os dois construtores de SQL, e uma checagem fim-a-fim contra
uma tabela `leads` pequena de verdade no DuckDB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from src.segmentation.filters import (
    BuiltQuery,
    ICPCriteria,
    build_export_query,
    build_leads_sql,
    build_where_clauses,
    filter_aberta_apos,
    filter_cod_atividade,
    filter_com_email,
    filter_com_telefone,
    filter_exclude_difusao_restrita,
    filter_exclude_synthetic,
    filter_pais,
    filter_porte,
    filter_regiao,
    filter_situacao,
)
from src.segmentation.filters import filter_capital_social_range as capital_social_range


class TestFilterExclusions:
    def test_filter_exclude_synthetic(self) -> None:
        clause = filter_exclude_synthetic()
        assert clause.sql == "is_synthetic = false"
        assert clause.params == []

    def test_filter_exclude_difusao_restrita(self) -> None:
        clause = filter_exclude_difusao_restrita()
        assert clause.sql == "flag_difusao_restrita = false"
        assert clause.params == []


class TestFilterPais:
    def test_none_returns_none(self) -> None:
        assert filter_pais(None) is None

    def test_single_value_uppercased(self) -> None:
        clause = filter_pais("br")
        assert clause is not None
        assert clause.sql == "pais = ?"
        assert clause.params == ["BR"]

    def test_list_of_values_builds_in_clause(self) -> None:
        clause = filter_pais(["br", "fr"])
        assert clause is not None
        assert clause.sql == "pais IN (?, ?)"
        assert clause.params == ["BR", "FR"]

    def test_empty_list_returns_none(self) -> None:
        assert filter_pais([]) is None


class TestFilterCodAtividade:
    def test_single_value_not_uppercased(self) -> None:
        clause = filter_cod_atividade("8630501")
        assert clause is not None
        assert clause.sql == "cod_atividade = ?"
        assert clause.params == ["8630501"]

    def test_list_of_values(self) -> None:
        clause = filter_cod_atividade(["8630501", "4721102"])
        assert clause is not None
        assert clause.sql == "cod_atividade IN (?, ?)"
        assert clause.params == ["8630501", "4721102"]

    def test_none_returns_none(self) -> None:
        assert filter_cod_atividade(None) is None


class TestFilterRegiao:
    def test_single_value_uppercased(self) -> None:
        clause = filter_regiao("sp")
        assert clause is not None
        assert clause.sql == "regiao = ?"
        assert clause.params == ["SP"]

    def test_list_of_values(self) -> None:
        clause = filter_regiao(["sp", "rj"])
        assert clause is not None
        assert clause.params == ["SP", "RJ"]


class TestFilterPorte:
    def test_single_value_uppercased(self) -> None:
        clause = filter_porte("micro empresa")
        assert clause is not None
        assert clause.sql == "porte = ?"
        assert clause.params == ["MICRO EMPRESA"]

    def test_none_returns_none(self) -> None:
        assert filter_porte(None) is None


class TestFilterSituacao:
    def test_single_value_uppercased(self) -> None:
        clause = filter_situacao("ativa")
        assert clause is not None
        assert clause.params == ["ATIVA"]

    def test_list_of_values(self) -> None:
        clause = filter_situacao(["ativa", "suspensa"])
        assert clause is not None
        assert clause.sql == "situacao IN (?, ?)"
        assert clause.params == ["ATIVA", "SUSPENSA"]


class TestFilterCapitalSocialRange:
    def test_both_bounds(self) -> None:
        clauses = capital_social_range(Decimal("1000"), Decimal("50000"))
        assert [c.sql for c in clauses] == ["capital_social >= ?", "capital_social <= ?"]
        assert clauses[0].params == [Decimal("1000")]
        assert clauses[1].params == [Decimal("50000")]

    def test_only_min(self) -> None:
        clauses = capital_social_range(Decimal("1000"), None)
        assert [c.sql for c in clauses] == ["capital_social >= ?"]

    def test_only_max(self) -> None:
        clauses = capital_social_range(None, Decimal("50000"))
        assert [c.sql for c in clauses] == ["capital_social <= ?"]

    def test_neither_returns_empty_list(self) -> None:
        assert capital_social_range(None, None) == []


class TestFilterAbertaApos:
    def test_none_returns_none(self) -> None:
        assert filter_aberta_apos(None) is None

    def test_date_builds_clause(self) -> None:
        clause = filter_aberta_apos(date(2020, 1, 1))
        assert clause is not None
        assert clause.sql == "data_inicio_atividade >= ?"
        assert clause.params == [date(2020, 1, 1)]


class TestFilterComEmail:
    def test_true_adds_clause(self) -> None:
        clause = filter_com_email(True)
        assert clause is not None
        assert clause.sql == "email IS NOT NULL"
        assert clause.params == []

    def test_false_returns_none(self) -> None:
        assert filter_com_email(False) is None


class TestFilterComTelefone:
    def test_true_adds_clause(self) -> None:
        clause = filter_com_telefone(True)
        assert clause is not None
        assert clause.sql == "telefone IS NOT NULL"

    def test_false_returns_none(self) -> None:
        assert filter_com_telefone(False) is None


class TestBuildWhereClauses:
    def test_default_criteria_has_only_the_two_hard_exclusions(self) -> None:
        clauses = build_where_clauses(ICPCriteria())
        sqls = [c.sql for c in clauses]
        assert sqls == ["flag_difusao_restrita = false", "is_synthetic = false"]

    def test_demo_mode_keeps_difusao_restrita_but_drops_synthetic(self) -> None:
        clauses = build_where_clauses(ICPCriteria(), demo=True)
        sqls = [c.sql for c in clauses]
        assert "flag_difusao_restrita = false" in sqls
        assert "is_synthetic = false" not in sqls

    def test_all_criteria_produce_all_clauses(self) -> None:
        criteria = ICPCriteria(
            pais="BR",
            cod_atividade="8630501",
            regiao="SP",
            porte="MICRO EMPRESA",
            situacao="ATIVA",
            capital_social_min=Decimal("1000"),
            capital_social_max=Decimal("50000"),
            aberta_apos=date(2020, 1, 1),
            com_email=True,
            com_telefone=True,
        )
        clauses = build_where_clauses(criteria)
        sqls = [c.sql for c in clauses]

        assert "pais = ?" in sqls
        assert "cod_atividade = ?" in sqls
        assert "regiao = ?" in sqls
        assert "porte = ?" in sqls
        assert "situacao = ?" in sqls
        assert "data_inicio_atividade >= ?" in sqls
        assert "email IS NOT NULL" in sqls
        assert "telefone IS NOT NULL" in sqls
        assert "capital_social >= ?" in sqls
        assert "capital_social <= ?" in sqls


class TestBuildLeadsSql:
    def test_default_source_is_leads(self) -> None:
        built = build_leads_sql(ICPCriteria())
        assert "FROM leads WHERE" in built.select_sql
        assert "FROM leads WHERE" in built.count_sql

    def test_custom_source(self) -> None:
        built = build_leads_sql(ICPCriteria(), source="read_parquet('x/*.parquet')")
        assert "FROM read_parquet('x/*.parquet') WHERE" in built.select_sql

    def test_order_by_default(self) -> None:
        built = build_leads_sql(ICPCriteria())
        assert "ORDER BY razao_social" in built.select_sql
        assert "ORDER BY" not in built.count_sql

    def test_order_by_none_omits_clause(self) -> None:
        built = build_leads_sql(ICPCriteria(), order_by=None)
        assert "ORDER BY" not in built.select_sql

    def test_limit_applied_only_to_select(self) -> None:
        built = build_leads_sql(ICPCriteria(), limit=25)
        assert "LIMIT 25" in built.select_sql
        assert "LIMIT" not in built.count_sql

    def test_no_limit_by_default(self) -> None:
        built = build_leads_sql(ICPCriteria())
        assert "LIMIT" not in built.select_sql

    def test_params_follow_clause_order(self) -> None:
        criteria = ICPCriteria(pais="br", regiao="sp", com_email=True)
        built = build_leads_sql(criteria)
        # Ordem: exclusões (sem params) -> pais -> regiao -> ... -> com_email (sem params).
        assert built.params == ["BR", "SP"]

    def test_returns_built_query_instance(self) -> None:
        assert isinstance(build_leads_sql(ICPCriteria()), BuiltQuery)


class TestBuildExportQuery:
    def test_always_excludes_synthetic(self) -> None:
        built = build_export_query(ICPCriteria())
        assert "is_synthetic = false" in built.select_sql
        assert "flag_difusao_restrita = false" in built.select_sql

    def test_has_no_demo_parameter(self) -> None:
        """Garantia em nível de assinatura: não dá pra ligar o modo demo aqui, nem
        por engano -- não é uma checagem em runtime que alguém possa esquecer."""
        with pytest.raises(TypeError):
            build_export_query(ICPCriteria(), demo=True)  # type: ignore[call-arg]

    def test_forwards_source_order_by_and_limit(self) -> None:
        built = build_export_query(
            ICPCriteria(), source="read_parquet('x/*.parquet')", order_by=None, limit=10
        )
        assert "FROM read_parquet('x/*.parquet')" in built.select_sql
        assert "ORDER BY" not in built.select_sql
        assert "LIMIT 10" in built.select_sql


Con = duckdb.DuckDBPyConnection

# Um dict por lead (id_estab -> demais campos), mais legível que uma tupla posicional.
LEADS_ROWS: list[dict[str, object]] = [
    {
        "pais": "BR", "id_estab": "1", "regiao": "SP", "cod_atividade": "8630501",
        "situacao": "ATIVA", "porte": "MICRO EMPRESA", "capital_social": 5000.0,
        "data_inicio_atividade": date(2023, 1, 1), "email": "a@x.com", "telefone": "111",
        "is_synthetic": False, "flag_difusao_restrita": False,
    },
    {
        "pais": "BR", "id_estab": "2", "regiao": "RJ", "cod_atividade": "8630501",
        "situacao": "ATIVA", "porte": "MICRO EMPRESA", "capital_social": 80000.0,
        "data_inicio_atividade": date(2018, 1, 1), "email": None, "telefone": None,
        "is_synthetic": False, "flag_difusao_restrita": False,
    },
    {
        "pais": "BR", "id_estab": "3", "regiao": "SP", "cod_atividade": "4721102",
        "situacao": "BAIXADA", "porte": "DEMAIS", "capital_social": 5000.0,
        "data_inicio_atividade": date(2023, 1, 1), "email": "c@x.com", "telefone": "333",
        "is_synthetic": False, "flag_difusao_restrita": False,
    },
    {
        "pais": "BR", "id_estab": "4", "regiao": "SP", "cod_atividade": "8630501",
        "situacao": "ATIVA", "porte": "MICRO EMPRESA", "capital_social": 5000.0,
        "data_inicio_atividade": date(2023, 1, 1), "email": "d@x.com", "telefone": "444",
        "is_synthetic": True, "flag_difusao_restrita": False,
    },
    {
        "pais": "FR", "id_estab": "5", "regiao": "IDF", "cod_atividade": "8630501",
        "situacao": "ATIVA", "porte": "MICRO EMPRESA", "capital_social": 5000.0,
        "data_inicio_atividade": date(2023, 1, 1), "email": "e@x.com", "telefone": "555",
        "is_synthetic": False, "flag_difusao_restrita": True,
    },
]

_LEADS_COLUMNS = [*LEADS_ROWS[0].keys(), "razao_social"]


@pytest.fixture
def leads_con() -> Con:
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE leads (
            pais VARCHAR, id_estab VARCHAR, regiao VARCHAR, cod_atividade VARCHAR,
            situacao VARCHAR, porte VARCHAR, capital_social DOUBLE,
            data_inicio_atividade DATE, email VARCHAR, telefone VARCHAR,
            is_synthetic BOOLEAN, flag_difusao_restrita BOOLEAN,
            razao_social VARCHAR
        )
        """
    )
    placeholders = ", ".join("?" for _ in _LEADS_COLUMNS)
    for row in LEADS_ROWS:
        values = [*row.values(), f"EMPRESA {row['id_estab']}"]
        con.execute(f"INSERT INTO leads VALUES ({placeholders})", values)
    return con


class TestEndToEndAgainstDuckDB:
    def test_no_criteria_excludes_synthetic_and_restricted(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria())
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (3,)  # exclui id 4 (synthetic) e id 5 (difusao restrita)

    def test_situacao_and_regiao_combined(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(situacao="ativa", regiao="sp"))
        rows = leads_con.execute(built.select_sql, built.params).fetchall()
        assert [r[1] for r in rows] == ["1"]

    def test_com_email_filter(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(com_email=True))
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (2,)  # ids 1 e 3 (id 2 nao tem email; 4 e 5 excluidos)

    def test_capital_social_range(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(capital_social_min=Decimal("10000")))
        rows = leads_con.execute(built.select_sql, built.params).fetchall()
        assert [r[1] for r in rows] == ["2"]

    def test_aberta_apos_filter(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(aberta_apos=date(2020, 1, 1)))
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (2,)  # ids 1 e 3 (id 2 abriu em 2018)

    def test_cod_atividade_in_list(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(cod_atividade=["8630501", "4721102"]))
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (3,)

    def test_demo_mode_includes_synthetic_but_not_restricted(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(), demo=True)
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (4,)  # inclui id 4 (synthetic), continua excluindo id 5

    def test_export_query_never_includes_synthetic_or_restricted(
        self, leads_con: Con
    ) -> None:
        built = build_export_query(ICPCriteria())
        count = leads_con.execute(built.count_sql, built.params).fetchone()
        assert count == (3,)

    def test_limit_restricts_row_count_but_not_total(self, leads_con: Con) -> None:
        built = build_leads_sql(ICPCriteria(), limit=1)
        rows = leads_con.execute(built.select_sql, built.params).fetchall()
        total = leads_con.execute(built.count_sql, built.params).fetchone()
        assert len(rows) == 1
        assert total == (3,)
