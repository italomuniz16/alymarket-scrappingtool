"""Testes de `segmentation/suppression.py`: cada regra isoladamente (exclusão hard,
dedup por id_estab, dedup por domínio de e-mail, lista de supressão) e o portão
completo — incluindo a prova explícita de que um lead sintético e um de difusão
restrita ("diffusion partielle") nunca passam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.segmentation.suppression import (
    SuppressionList,
    add_to_suppression_list,
    apply_suppression_gate,
    apply_suppression_gate_from_path,
    dedupe_by_email_domain,
    dedupe_by_id_estab,
    email_domain,
    is_hard_excluded,
    is_suppressed,
    load_suppression_list,
    remove_from_suppression_list,
)


def _lead(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id_estab": "11111111000191",
        "email": None,
        "is_synthetic": False,
        "flag_difusao_restrita": False,
    }
    return {**base, **overrides}


class TestEmailDomain:
    def test_valid_email(self) -> None:
        assert email_domain("contato@Empresa.COM.BR") == "empresa.com.br"

    def test_none_returns_none(self) -> None:
        assert email_domain(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert email_domain("") is None

    def test_no_at_sign_returns_none(self) -> None:
        assert email_domain("nao-e-um-email") is None


class TestIsHardExcluded:
    def test_synthetic_is_excluded(self) -> None:
        assert is_hard_excluded(_lead(is_synthetic=True)) is True

    def test_flag_difusao_restrita_is_excluded(self) -> None:
        assert is_hard_excluded(_lead(flag_difusao_restrita=True)) is True

    def test_both_true_is_excluded(self) -> None:
        assert is_hard_excluded(_lead(is_synthetic=True, flag_difusao_restrita=True)) is True

    def test_neither_is_not_excluded(self) -> None:
        assert is_hard_excluded(_lead()) is False


class TestDedupeByIdEstab:
    def test_keeps_first_drops_duplicates(self) -> None:
        leads = [_lead(id_estab="A", email="1@x.com"), _lead(id_estab="A", email="2@x.com")]
        result = list(dedupe_by_id_estab(leads))
        assert len(result) == 1
        assert result[0]["email"] == "1@x.com"

    def test_different_ids_both_kept(self) -> None:
        leads = [_lead(id_estab="A"), _lead(id_estab="B")]
        assert len(list(dedupe_by_id_estab(leads))) == 2

    def test_missing_id_estab_always_kept(self) -> None:
        leads = [_lead(id_estab=None), _lead(id_estab=None), _lead(id_estab=None)]
        assert len(list(dedupe_by_id_estab(leads))) == 3


class TestDedupeByEmailDomain:
    def test_same_domain_keeps_first(self) -> None:
        leads = [
            _lead(id_estab="A", email="a@empresa.com"),
            _lead(id_estab="B", email="b@empresa.com"),
        ]
        result = list(dedupe_by_email_domain(leads))
        assert len(result) == 1
        assert result[0]["id_estab"] == "A"

    def test_different_domains_both_kept(self) -> None:
        leads = [
            _lead(id_estab="A", email="a@empresa1.com"),
            _lead(id_estab="B", email="b@empresa2.com"),
        ]
        assert len(list(dedupe_by_email_domain(leads))) == 2

    def test_leads_without_email_always_kept(self) -> None:
        leads = [_lead(id_estab="A", email=None), _lead(id_estab="B", email=None)]
        assert len(list(dedupe_by_email_domain(leads))) == 2

    def test_malformed_email_always_kept(self) -> None:
        leads = [_lead(id_estab="A", email="lixo"), _lead(id_estab="B", email="lixo")]
        assert len(list(dedupe_by_email_domain(leads))) == 2


class TestLoadSuppressionList:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_suppression_list(tmp_path / "nao-existe.csv")
        assert result == SuppressionList()

    def test_loads_id_estab_and_email(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "supressao.csv"
        csv_path.write_text(
            "id_estab,email\n11111111000191,\n,contato@empresa.com\n22222222000122,outro@x.com\n",
            encoding="utf-8",
        )
        result = load_suppression_list(csv_path)
        assert result.ids_estab == {"11111111000191", "22222222000122"}
        assert result.emails == {"contato@empresa.com", "outro@x.com"}

    def test_email_normalized_to_lowercase(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "supressao.csv"
        csv_path.write_text("id_estab,email\n,Contato@Empresa.COM\n", encoding="utf-8")
        result = load_suppression_list(csv_path)
        assert result.emails == {"contato@empresa.com"}

    def test_blank_rows_ignored(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "supressao.csv"
        csv_path.write_text("id_estab,email\n,\n,\n", encoding="utf-8")
        result = load_suppression_list(csv_path)
        assert result == SuppressionList()


class TestAddToSuppressionList:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        assert not path.exists()

        added = add_to_suppression_list(path, id_estab="11111111000191")

        assert added is True
        assert path.is_file()

    def test_add_by_id_estab_only(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="11111111000191")

        result = load_suppression_list(path)
        assert result.ids_estab == {"11111111000191"}
        assert result.emails == frozenset()

    def test_add_by_email_only_normalizes_to_lowercase(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, email="Contato@Empresa.COM")

        result = load_suppression_list(path)
        assert result.emails == {"contato@empresa.com"}

    def test_add_both_id_estab_and_email(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="A", email="a@x.com")

        result = load_suppression_list(path)
        assert result.ids_estab == {"A"}
        assert result.emails == {"a@x.com"}

    def test_appends_to_existing_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="A")
        add_to_suppression_list(path, id_estab="B")

        result = load_suppression_list(path)
        assert result.ids_estab == {"A", "B"}

    def test_duplicate_add_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        first = add_to_suppression_list(path, id_estab="A", email="a@x.com")
        second = add_to_suppression_list(path, id_estab="A", email="a@x.com")

        assert first is True
        assert second is False
        result = load_suppression_list(path)
        assert result.ids_estab == {"A"}  # não duplicou linha

    def test_neither_id_estab_nor_email_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            add_to_suppression_list(tmp_path / "supressao.csv")

    def test_motivo_persisted_but_does_not_affect_loading(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="A", motivo="solicitação do titular")

        raw = path.read_text(encoding="utf-8")
        assert "solicitação do titular" in raw
        # load_suppression_list só expõe os conjuntos agregados, não o motivo.
        assert load_suppression_list(path).ids_estab == {"A"}


class TestRemoveFromSuppressionList:
    def test_removes_by_id_estab(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="A")
        add_to_suppression_list(path, id_estab="B")

        n_removed = remove_from_suppression_list(path, id_estab="A")

        assert n_removed == 1
        assert load_suppression_list(path).ids_estab == {"B"}

    def test_removes_by_email(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, email="a@x.com")
        add_to_suppression_list(path, email="b@x.com")

        n_removed = remove_from_suppression_list(path, email="a@x.com")

        assert n_removed == 1
        assert load_suppression_list(path).emails == {"b@x.com"}

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        n_removed = remove_from_suppression_list(tmp_path / "nao-existe.csv", id_estab="A")
        assert n_removed == 0

    def test_no_match_returns_zero_and_leaves_file_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        add_to_suppression_list(path, id_estab="A")

        n_removed = remove_from_suppression_list(path, id_estab="NAO-EXISTE")

        assert n_removed == 0
        assert load_suppression_list(path).ids_estab == {"A"}

    def test_neither_id_estab_nor_email_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            remove_from_suppression_list(tmp_path / "supressao.csv")


class TestOptOutAdditionReflectedInSuppression:
    """O caso pedido explicitamente: registrar um opt-out precisa se refletir de
    verdade no portão de supressão (`apply_suppression_gate`) -- não só na lista
    carregada isoladamente."""

    def test_lead_excluded_after_optout_by_id_estab(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        leads = [_lead(id_estab="A"), _lead(id_estab="B")]

        # Antes do opt-out, os dois passam.
        before, _ = apply_suppression_gate_from_path(leads, path)
        assert {lead["id_estab"] for lead in before} == {"A", "B"}

        add_to_suppression_list(path, id_estab="A", motivo="solicitação do titular")

        after, report = apply_suppression_gate_from_path(leads, path)
        assert {lead["id_estab"] for lead in after} == {"B"}
        assert report.n_suppressed == 1

    def test_lead_excluded_after_optout_by_email(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        # Domínios diferentes de propósito: senão dedupe_by_email_domain (que roda
        # antes da supressão no portão) já removeria um dos dois por conta própria,
        # confundindo o que este teste quer provar.
        leads = [
            _lead(id_estab="A", email="a@empresa1.com"),
            _lead(id_estab="B", email="b@empresa2.com"),
        ]

        add_to_suppression_list(path, email="a@empresa1.com")

        final, report = apply_suppression_gate_from_path(leads, path)
        assert {lead["id_estab"] for lead in final} == {"B"}
        assert report.n_suppressed == 1

    def test_lead_no_longer_excluded_after_removal(self, tmp_path: Path) -> None:
        path = tmp_path / "supressao.csv"
        leads = [_lead(id_estab="A"), _lead(id_estab="B")]
        add_to_suppression_list(path, id_estab="A")

        remove_from_suppression_list(path, id_estab="A")

        final, report = apply_suppression_gate_from_path(leads, path)
        assert {lead["id_estab"] for lead in final} == {"A", "B"}
        assert report.n_suppressed == 0


class TestIsSuppressed:
    SUPPRESSION = SuppressionList(
        ids_estab=frozenset({"11111111000191"}), emails=frozenset({"opt-out@x.com"})
    )

    def test_suppressed_by_id_estab(self) -> None:
        assert is_suppressed(_lead(id_estab="11111111000191"), self.SUPPRESSION) is True

    def test_suppressed_by_email_case_insensitive(self) -> None:
        lead = _lead(id_estab="99999999000100", email="Opt-Out@X.com")
        assert is_suppressed(lead, self.SUPPRESSION) is True

    def test_not_suppressed(self) -> None:
        lead = _lead(id_estab="99999999000100", email="alguem@outraempresa.com")
        assert is_suppressed(lead, self.SUPPRESSION) is False

    def test_empty_suppression_list_suppresses_nothing(self) -> None:
        lead = _lead(id_estab="11111111000191", email="opt-out@x.com")
        assert is_suppressed(lead, SuppressionList()) is False


class TestApplySuppressionGate:
    def test_synthetic_lead_never_passes(self) -> None:
        leads = [_lead(id_estab="A", is_synthetic=True)]
        final, report = apply_suppression_gate(leads, SuppressionList())
        assert final == []
        assert report.n_hard_excluded == 1
        assert report.n_out == 0

    def test_diffusion_partielle_lead_never_passes(self) -> None:
        leads = [_lead(id_estab="B", flag_difusao_restrita=True)]
        final, report = apply_suppression_gate(leads, SuppressionList())
        assert final == []
        assert report.n_hard_excluded == 1
        assert report.n_out == 0

    def test_synthetic_and_restricted_never_pass_alongside_clean_leads(self) -> None:
        leads = [
            _lead(id_estab="clean-1"),
            _lead(id_estab="synth-1", is_synthetic=True),
            _lead(id_estab="fr-restrito-1", flag_difusao_restrita=True),
            _lead(id_estab="clean-2"),
        ]
        final, report = apply_suppression_gate(leads, SuppressionList())

        ids_finais = {lead["id_estab"] for lead in final}
        assert ids_finais == {"clean-1", "clean-2"}
        assert report.n_hard_excluded == 2
        assert report.n_out == 2

    def test_deduplicates_by_id_estab(self) -> None:
        leads = [_lead(id_estab="A"), _lead(id_estab="A")]
        final, report = apply_suppression_gate(leads, SuppressionList())
        assert len(final) == 1
        assert report.n_deduped_id_estab == 1

    def test_deduplicates_by_email_domain(self) -> None:
        leads = [
            _lead(id_estab="A", email="a@empresa.com"),
            _lead(id_estab="B", email="b@empresa.com"),
        ]
        final, report = apply_suppression_gate(leads, SuppressionList())
        assert len(final) == 1
        assert report.n_deduped_email_domain == 1

    def test_removes_suppressed_lead(self) -> None:
        leads = [_lead(id_estab="A"), _lead(id_estab="B")]
        suppression = SuppressionList(ids_estab=frozenset({"A"}))
        final, report = apply_suppression_gate(leads, suppression)
        assert [lead["id_estab"] for lead in final] == ["B"]
        assert report.n_suppressed == 1

    def test_clean_lead_passes_through_untouched(self) -> None:
        lead = _lead(id_estab="A", email="a@empresa.com")
        final, report = apply_suppression_gate([lead], SuppressionList())
        assert final == [lead]
        assert report == report.__class__(
            n_in=1,
            n_hard_excluded=0,
            n_deduped_id_estab=0,
            n_deduped_email_domain=0,
            n_suppressed=0,
            n_out=1,
        )

    def test_report_counts_are_consistent(self) -> None:
        leads = [
            _lead(id_estab="synth", is_synthetic=True),
            _lead(id_estab="dup", email="a@x.com"),
            _lead(id_estab="dup", email="a@x.com"),  # duplicado por id_estab
            _lead(id_estab="dom1", email="c@mesmodominio.com"),
            _lead(id_estab="dom2", email="d@mesmodominio.com"),  # duplicado por dominio
            _lead(id_estab="suprimido"),
            _lead(id_estab="ok"),
        ]
        suppression = SuppressionList(ids_estab=frozenset({"suprimido"}))
        final, report = apply_suppression_gate(leads, suppression)

        assert report.n_in == 7
        assert report.n_hard_excluded == 1
        assert report.n_deduped_id_estab == 1
        assert report.n_deduped_email_domain == 1
        assert report.n_suppressed == 1
        assert report.n_out == len(final) == 3
        assert {lead["id_estab"] for lead in final} == {"dup", "dom1", "ok"}

    def test_empty_input_returns_empty(self) -> None:
        final, report = apply_suppression_gate([], SuppressionList())
        assert final == []
        assert report.n_in == 0
        assert report.n_out == 0


class TestApplySuppressionGateFromPath:
    def test_loads_and_applies(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "supressao.csv"
        csv_path.write_text("id_estab,email\nA,\n", encoding="utf-8")

        leads = [_lead(id_estab="A"), _lead(id_estab="B")]
        final, report = apply_suppression_gate_from_path(leads, csv_path)

        assert [lead["id_estab"] for lead in final] == ["B"]
        assert report.n_suppressed == 1

    def test_missing_file_suppresses_nothing(self, tmp_path: Path) -> None:
        leads = [_lead(id_estab="A")]
        final, _ = apply_suppression_gate_from_path(leads, tmp_path / "nao-existe.csv")
        assert len(final) == 1


@pytest.mark.parametrize("hard_field", ["is_synthetic", "flag_difusao_restrita"])
def test_hard_exclusion_wins_even_if_lead_would_otherwise_be_kept(hard_field: str) -> None:
    """Um lead que passaria em toda regra (sem duplicata, sem estar na lista de
    supressão) ainda assim nunca sai se for sintético ou de difusão restrita."""
    lead = _lead(id_estab="unico", email="unico@dominio-unico.com", **{hard_field: True})
    final, report = apply_suppression_gate([lead], SuppressionList())
    assert final == []
    assert report.n_hard_excluded == 1
