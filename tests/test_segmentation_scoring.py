"""Testes de `segmentation/scoring.py`: veto, cada sinal isoladamente, isolamento de
cada peso (score_lead com um peso ligado por vez) e casos combinados.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.segmentation.scoring import (
    DEFAULT_RECENCIA_DIAS_MAX,
    ScoringConfig,
    SignalScore,
    apply_score_icp,
    is_situacao_ativa,
    score_lead,
    signal_atividade,
    signal_contato,
    signal_porte_capital,
    signal_recencia,
    signal_regiao,
)

HOJE = date(2026, 8, 10)


def _lead(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "situacao": "ATIVA",
        "email": None,
        "telefone": None,
        "cod_atividade": None,
        "porte": None,
        "capital_social": None,
        "data_inicio_atividade": None,
        "regiao": None,
    }
    return {**base, **overrides}


class TestIsSituacaoAtiva:
    def test_ativa_uppercase(self) -> None:
        assert is_situacao_ativa(_lead(situacao="ATIVA")) is True

    def test_ativa_case_insensitive(self) -> None:
        assert is_situacao_ativa(_lead(situacao="ativa")) is True

    def test_baixada_is_false(self) -> None:
        assert is_situacao_ativa(_lead(situacao="BAIXADA")) is False

    def test_missing_situacao_is_false(self) -> None:
        assert is_situacao_ativa(_lead(situacao=None)) is False


class TestScoreLedeVeto:
    def test_situacao_nao_ativa_zera_score(self) -> None:
        # Lead que bateria 100 em tudo, não fosse a situação.
        config = ScoringConfig(
            atividades_alvo=frozenset({"8630501"}), regioes_alvo=frozenset({"SP"})
        )
        lead = _lead(
            situacao="BAIXADA",
            email="a@x.com",
            telefone="111",
            cod_atividade="8630501",
            regiao="SP",
            data_inicio_atividade=HOJE,
        )
        result = score_lead(lead, config, hoje=HOJE)
        assert result.vetado is True
        assert result.score_icp == 0.0
        assert result.sinais == {}

    def test_situacao_ausente_veta(self) -> None:
        result = score_lead(_lead(situacao=None), ScoringConfig(), hoje=HOJE)
        assert result.vetado is True
        assert result.score_icp == 0.0


class TestSignalContato:
    def test_nenhum_contato(self) -> None:
        assert signal_contato(_lead()).fraction == 0.0

    def test_so_email(self) -> None:
        assert signal_contato(_lead(email="a@x.com")).fraction == 0.5

    def test_so_telefone(self) -> None:
        assert signal_contato(_lead(telefone="111")).fraction == 0.5

    def test_email_e_telefone(self) -> None:
        assert signal_contato(_lead(email="a@x.com", telefone="111")).fraction == 1.0

    def test_sempre_aplicavel(self) -> None:
        assert signal_contato(_lead()).applicable is True


class TestSignalAtividade:
    def test_sem_alvo_nao_aplicavel(self) -> None:
        result = signal_atividade(_lead(cod_atividade="8630501"), None)
        assert result.applicable is False

    def test_alvo_vazio_nao_aplicavel(self) -> None:
        result = signal_atividade(_lead(cod_atividade="8630501"), frozenset())
        assert result.applicable is False

    def test_codigo_bate(self) -> None:
        result = signal_atividade(_lead(cod_atividade="8630501"), frozenset({"8630501"}))
        assert result.fraction == 1.0

    def test_codigo_nao_bate(self) -> None:
        result = signal_atividade(_lead(cod_atividade="4721102"), frozenset({"8630501"}))
        assert result.fraction == 0.0

    def test_codigo_ausente(self) -> None:
        result = signal_atividade(_lead(cod_atividade=None), frozenset({"8630501"}))
        assert result.applicable is True
        assert result.fraction == 0.0


class TestSignalPorteCapital:
    def test_nada_configurado_nao_aplicavel(self) -> None:
        result = signal_porte_capital(_lead(), portes_alvo=None, capital_min=None, capital_max=None)
        assert result.applicable is False

    def test_so_porte_configurado_bate(self) -> None:
        result = signal_porte_capital(
            _lead(porte="MICRO EMPRESA"),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=None,
            capital_max=None,
        )
        assert result.fraction == 1.0

    def test_so_porte_configurado_case_insensitive(self) -> None:
        result = signal_porte_capital(
            _lead(porte="micro empresa"),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=None,
            capital_max=None,
        )
        assert result.fraction == 1.0

    def test_so_porte_configurado_nao_bate(self) -> None:
        result = signal_porte_capital(
            _lead(porte="DEMAIS"),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=None,
            capital_max=None,
        )
        assert result.fraction == 0.0

    def test_so_capital_dentro_da_faixa(self) -> None:
        result = signal_porte_capital(
            _lead(capital_social=Decimal("5000")),
            portes_alvo=None,
            capital_min=Decimal("1000"),
            capital_max=Decimal("10000"),
        )
        assert result.fraction == 1.0

    def test_so_capital_abaixo_do_minimo(self) -> None:
        result = signal_porte_capital(
            _lead(capital_social=Decimal("500")),
            portes_alvo=None,
            capital_min=Decimal("1000"),
            capital_max=None,
        )
        assert result.fraction == 0.0

    def test_so_capital_acima_do_maximo(self) -> None:
        result = signal_porte_capital(
            _lead(capital_social=Decimal("50000")),
            portes_alvo=None,
            capital_min=None,
            capital_max=Decimal("10000"),
        )
        assert result.fraction == 0.0

    def test_capital_ausente_conta_como_zero(self) -> None:
        result = signal_porte_capital(
            _lead(capital_social=None),
            portes_alvo=None,
            capital_min=Decimal("1000"),
            capital_max=None,
        )
        assert result.applicable is True
        assert result.fraction == 0.0

    def test_ambos_configurados_ambos_batem(self) -> None:
        result = signal_porte_capital(
            _lead(porte="MICRO EMPRESA", capital_social=Decimal("5000")),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=Decimal("1000"),
            capital_max=Decimal("10000"),
        )
        assert result.fraction == 1.0

    def test_ambos_configurados_so_um_bate(self) -> None:
        result = signal_porte_capital(
            _lead(porte="DEMAIS", capital_social=Decimal("5000")),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=Decimal("1000"),
            capital_max=Decimal("10000"),
        )
        assert result.fraction == 0.5

    def test_ambos_configurados_nenhum_bate(self) -> None:
        result = signal_porte_capital(
            _lead(porte="DEMAIS", capital_social=Decimal("50000")),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
            capital_min=Decimal("1000"),
            capital_max=Decimal("10000"),
        )
        assert result.fraction == 0.0


def _recencia(data_inicio: date | None, *, dias_max: int | None = 730) -> SignalScore:
    return signal_recencia(
        _lead(data_inicio_atividade=data_inicio), recencia_dias_max=dias_max, hoje=HOJE
    )


class TestSignalRecencia:
    def test_recencia_desativada_nao_aplicavel(self) -> None:
        assert _recencia(HOJE, dias_max=None).applicable is False

    def test_data_ausente(self) -> None:
        result = _recencia(None)
        assert result.applicable is True
        assert result.fraction == 0.0

    def test_aberta_hoje_nota_maxima(self) -> None:
        assert _recencia(HOJE).fraction == 1.0

    def test_meio_do_prazo_nota_metade(self) -> None:
        data_inicio = date(HOJE.year - 1, HOJE.month, HOJE.day)  # 365 dias atrás
        assert _recencia(data_inicio).fraction == 0.5

    def test_no_limite_do_prazo_nota_zero(self) -> None:
        data_inicio = date(HOJE.year - 2, HOJE.month, HOJE.day)  # 730 dias atrás
        assert _recencia(data_inicio).fraction == 0.0

    def test_mais_antiga_que_o_prazo_nota_zero(self) -> None:
        data_inicio = date(HOJE.year - 5, HOJE.month, HOJE.day)
        assert _recencia(data_inicio).fraction == 0.0

    def test_data_no_futuro_e_tratada_como_hoje(self) -> None:
        data_inicio = HOJE + timedelta(days=5)
        result = signal_recencia(
            _lead(data_inicio_atividade=data_inicio), recencia_dias_max=730, hoje=HOJE
        )
        assert result.fraction == 1.0


class TestSignalRegiao:
    def test_sem_alvo_nao_aplicavel(self) -> None:
        assert signal_regiao(_lead(regiao="SP"), None).applicable is False

    def test_alvo_vazio_nao_aplicavel(self) -> None:
        assert signal_regiao(_lead(regiao="SP"), frozenset()).applicable is False

    def test_regiao_bate(self) -> None:
        assert signal_regiao(_lead(regiao="SP"), frozenset({"SP"})).fraction == 1.0

    def test_regiao_bate_case_insensitive(self) -> None:
        assert signal_regiao(_lead(regiao="sp"), frozenset({"SP"})).fraction == 1.0

    def test_regiao_nao_bate(self) -> None:
        assert signal_regiao(_lead(regiao="RJ"), frozenset({"SP"})).fraction == 0.0

    def test_regiao_ausente(self) -> None:
        result = signal_regiao(_lead(regiao=None), frozenset({"SP"}))
        assert result.applicable is True
        assert result.fraction == 0.0


# -- Isolamento de cada peso em score_lead (config só com um peso ativo) ------------

_SO_CONTATO = ScoringConfig(
    peso_contato=100, peso_atividade=0, peso_porte_capital=0, peso_recencia=0, peso_regiao=0
)
_SO_ATIVIDADE = ScoringConfig(
    peso_contato=0,
    peso_atividade=100,
    peso_porte_capital=0,
    peso_recencia=0,
    peso_regiao=0,
    atividades_alvo=frozenset({"8630501"}),
)
_SO_PORTE_CAPITAL = ScoringConfig(
    peso_contato=0,
    peso_atividade=0,
    peso_porte_capital=100,
    peso_recencia=0,
    peso_regiao=0,
    portes_alvo=frozenset({"MICRO EMPRESA"}),
)
_SO_RECENCIA = ScoringConfig(
    peso_contato=0, peso_atividade=0, peso_porte_capital=0, peso_recencia=100, peso_regiao=0
)
_SO_REGIAO = ScoringConfig(
    peso_contato=0,
    peso_atividade=0,
    peso_porte_capital=0,
    peso_recencia=0,
    peso_regiao=100,
    regioes_alvo=frozenset({"SP"}),
)


class TestPesoContatoIsolado:
    def test_ambos_contatos_nota_maxima(self) -> None:
        lead = _lead(email="a@x.com", telefone="111")
        assert score_lead(lead, _SO_CONTATO, hoje=HOJE).score_icp == 100.0

    def test_um_contato_nota_metade(self) -> None:
        lead = _lead(email="a@x.com")
        assert score_lead(lead, _SO_CONTATO, hoje=HOJE).score_icp == 50.0

    def test_nenhum_contato_nota_zero(self) -> None:
        assert score_lead(_lead(), _SO_CONTATO, hoje=HOJE).score_icp == 0.0


class TestPesoAtividadeIsolado:
    def test_atividade_no_icp_nota_maxima(self) -> None:
        lead = _lead(cod_atividade="8630501")
        assert score_lead(lead, _SO_ATIVIDADE, hoje=HOJE).score_icp == 100.0

    def test_atividade_fora_do_icp_nota_zero(self) -> None:
        lead = _lead(cod_atividade="4721102")
        assert score_lead(lead, _SO_ATIVIDADE, hoje=HOJE).score_icp == 0.0


class TestPesoPorteCapitalIsolado:
    def test_porte_no_icp_nota_maxima(self) -> None:
        lead = _lead(porte="MICRO EMPRESA")
        assert score_lead(lead, _SO_PORTE_CAPITAL, hoje=HOJE).score_icp == 100.0

    def test_porte_fora_do_icp_nota_zero(self) -> None:
        lead = _lead(porte="DEMAIS")
        assert score_lead(lead, _SO_PORTE_CAPITAL, hoje=HOJE).score_icp == 0.0


class TestPesoRecenciaIsolado:
    def test_aberta_hoje_nota_maxima(self) -> None:
        lead = _lead(data_inicio_atividade=HOJE)
        assert score_lead(lead, _SO_RECENCIA, hoje=HOJE).score_icp == 100.0

    def test_aberta_no_meio_do_prazo_nota_metade(self) -> None:
        lead = _lead(data_inicio_atividade=date(HOJE.year - 1, HOJE.month, HOJE.day))
        assert score_lead(lead, _SO_RECENCIA, hoje=HOJE).score_icp == 50.0

    def test_aberta_ha_muito_tempo_nota_zero(self) -> None:
        lead = _lead(data_inicio_atividade=date(HOJE.year - 5, HOJE.month, HOJE.day))
        assert score_lead(lead, _SO_RECENCIA, hoje=HOJE).score_icp == 0.0


class TestPesoRegiaoIsolado:
    def test_regiao_no_icp_nota_maxima(self) -> None:
        lead = _lead(regiao="SP")
        assert score_lead(lead, _SO_REGIAO, hoje=HOJE).score_icp == 100.0

    def test_regiao_fora_do_icp_nota_zero(self) -> None:
        lead = _lead(regiao="RJ")
        assert score_lead(lead, _SO_REGIAO, hoje=HOJE).score_icp == 0.0


class TestScoreLeadCombinado:
    def test_nenhum_sinal_aplicavel_nota_zero(self) -> None:
        config = ScoringConfig(
            peso_contato=0,
            peso_atividade=0,
            peso_porte_capital=0,
            peso_recencia=0,
            peso_regiao=0,
            recencia_dias_max=None,
        )
        result = score_lead(_lead(), config, hoje=HOJE)
        assert result.vetado is False
        assert result.score_icp == 0.0

    def test_lead_perfeito_bate_tudo(self) -> None:
        config = ScoringConfig(
            atividades_alvo=frozenset({"8630501"}),
            regioes_alvo=frozenset({"SP"}),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
        )
        lead = _lead(
            email="a@x.com",
            telefone="111",
            cod_atividade="8630501",
            porte="MICRO EMPRESA",
            regiao="SP",
            data_inicio_atividade=HOJE,
        )
        result = score_lead(lead, config, hoje=HOJE)
        assert result.score_icp == 100.0

    def test_lead_sem_nenhum_sinal_positivo_com_config_completa(self) -> None:
        config = ScoringConfig(
            atividades_alvo=frozenset({"8630501"}),
            regioes_alvo=frozenset({"SP"}),
            portes_alvo=frozenset({"MICRO EMPRESA"}),
        )
        lead = _lead(
            cod_atividade="4721102",
            porte="DEMAIS",
            regiao="RJ",
            data_inicio_atividade=date(HOJE.year - 10, HOJE.month, HOJE.day),
        )
        result = score_lead(lead, config, hoje=HOJE)
        assert result.score_icp == 0.0

    def test_score_arredondado_a_duas_casas(self) -> None:
        # Pesos que não dividem exato: contato ligado sozinho com fração 0.5 e peso
        # 33 (do total configurado só ele é aplicável) -> 100*0.5 = 50.0 exato, mas
        # aqui misturamos com um segundo peso pra forçar dízima.
        config = ScoringConfig(
            peso_contato=10,
            peso_atividade=0,
            peso_porte_capital=0,
            peso_recencia=0,
            peso_regiao=20,
            regioes_alvo=frozenset({"SP"}),
        )
        lead = _lead(email="a@x.com", regiao="RJ")  # contato=0.5 (peso 10), regiao=0 (peso 20)
        result = score_lead(lead, config, hoje=HOJE)
        # pontos = 10*0.5 + 20*0 = 5; peso_total = 30; 5/30*100 = 16.666...
        assert result.score_icp == round(5 / 30 * 100, 2)

    def test_default_config_recencia_dias_max(self) -> None:
        assert ScoringConfig().recencia_dias_max == DEFAULT_RECENCIA_DIAS_MAX


class TestApplyScoreIcp:
    def test_retorna_copia_com_score_icp(self) -> None:
        lead = _lead(email="a@x.com", telefone="111")
        updated = apply_score_icp(lead, _SO_CONTATO, hoje=HOJE)

        assert updated["score_icp"] == 100.0
        assert "score_icp" not in lead

    def test_nao_modifica_original(self) -> None:
        lead = _lead(email="a@x.com")
        original = dict(lead)
        apply_score_icp(lead, _SO_CONTATO, hoje=HOJE)
        assert lead == original

    def test_veto_zera_score_icp_tambem(self) -> None:
        lead = _lead(situacao="BAIXADA", email="a@x.com", telefone="111")
        updated = apply_score_icp(lead, _SO_CONTATO, hoje=HOJE)
        assert updated["score_icp"] == 0.0
