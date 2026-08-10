"""Cálculo de `score_icp` (0-100) por lead, a partir de sinais ponderados e
configuráveis (ver `ScoringConfig`).

## Veto

`situacao != "ATIVA"` é obrigatório/veto (PRD §6.3: "Situação cadastral = ATIVA
(obrigatório)") — zera o score inteiro, ignorando todos os outros sinais. Não é um
sinal ponderado como os demais.

## Sinais (cada um numa função `signal_*` testável isoladamente)

- `signal_contato`: presença de e-mail e/ou telefone (nenhum -> 0; um -> 0.5; os dois -> 1.0).
- `signal_atividade`: `cod_atividade` do lead está em `ScoringConfig.atividades_alvo`.
- `signal_porte_capital`: `porte` está em `ScoringConfig.portes_alvo` e/ou
  `capital_social` está na faixa `[capital_social_min, capital_social_max]` — média
  das checagens configuradas (uma só configurada = só ela conta).
- `signal_recencia`: quanto mais recente a `data_inicio_atividade`, maior a nota,
  decaindo linearmente até 0 em `ScoringConfig.recencia_dias_max` dias.
- `signal_regiao`: `regiao` do lead está em `ScoringConfig.regioes_alvo`.

## Sinal não configurado != sinal reprovado

Um sinal cujo alvo não foi configurado (ex.: `atividades_alvo=None`) fica **não
aplicável** — sai do denominador da média ponderada, não conta como 0. Isso evita que
o score máximo caia artificialmente para quem só se importa com alguns critérios.
`signal_recencia` é o único que não tem essa noção de "sem alvo configurado" via
target (sempre roda contra `recencia_dias_max`), mas ainda respeita
`recencia_dias_max=None` como "desativado" pelo mesmo motivo.

Se **nenhum** sinal for aplicável (todos os alvos `None`/vazios e `recencia_dias_max`
também `None`), o score é `0.0` (denominador zero — nada para avaliar).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

SITUACAO_ATIVA = "ATIVA"
DEFAULT_RECENCIA_DIAS_MAX = 730  # ~2 anos


@dataclass(frozen=True)
class ScoringConfig:
    """Pesos de cada sinal + os alvos que definem "aderência ao ICP".

    Os pesos não precisam somar exatamente 100 — o score final é sempre normalizado
    pela soma dos pesos dos sinais *aplicáveis* (ver docstring do módulo). Os
    defaults somam 100 para o caso comum de todos os sinais aplicáveis.
    """

    peso_contato: float = 20.0
    peso_atividade: float = 25.0
    peso_porte_capital: float = 20.0
    peso_recencia: float = 15.0
    peso_regiao: float = 20.0

    atividades_alvo: frozenset[str] | None = None
    regioes_alvo: frozenset[str] | None = None
    portes_alvo: frozenset[str] | None = None
    capital_social_min: Decimal | None = None
    capital_social_max: Decimal | None = None
    recencia_dias_max: int | None = DEFAULT_RECENCIA_DIAS_MAX


@dataclass(frozen=True)
class SignalScore:
    """Resultado de um sinal: fração atingida (`0.0`-`1.0`) e se o sinal se aplica
    (tem alvo configurado) — sinais não aplicáveis saem da média ponderada."""

    fraction: float
    applicable: bool = True


@dataclass(frozen=True)
class ScoreBreakdown:
    """Resultado completo de `score_lead`: o `score_icp` final, se foi vetado por
    situação cadastral, e o detalhamento de cada sinal (útil para debug/auditoria de
    por que um lead pontuou o que pontuou)."""

    score_icp: float
    vetado: bool
    sinais: dict[str, SignalScore] = field(default_factory=dict)


# -- Veto ---------------------------------------------------------------------------


def is_situacao_ativa(lead: Mapping[str, Any]) -> bool:
    """`True` se `lead["situacao"]` for `"ATIVA"` (case-insensitive); `False` também
    se o campo estiver ausente — ausência de dado não deve ser tratada como ativa."""
    situacao = lead.get("situacao")
    return isinstance(situacao, str) and situacao.upper() == SITUACAO_ATIVA


# -- Sinais (cada um testável isoladamente) ------------------------------------------


def signal_contato(lead: Mapping[str, Any]) -> SignalScore:
    """E-mail e/ou telefone presentes. Sempre aplicável (não depende de config)."""
    tem_email = bool(lead.get("email"))
    tem_telefone = bool(lead.get("telefone"))
    if tem_email and tem_telefone:
        return SignalScore(1.0)
    if tem_email or tem_telefone:
        return SignalScore(0.5)
    return SignalScore(0.0)


def signal_atividade(
    lead: Mapping[str, Any], atividades_alvo: Collection[str] | None
) -> SignalScore:
    """`cod_atividade` do lead está entre os códigos alvo do ICP (comparação exata,
    sem normalização de caixa — CNAE/NAF não têm conceito de maiúscula/minúscula)."""
    if not atividades_alvo:
        return SignalScore(0.0, applicable=False)
    cod = lead.get("cod_atividade")
    if cod is None:
        return SignalScore(0.0)
    return SignalScore(1.0 if str(cod) in set(atividades_alvo) else 0.0)


def signal_porte_capital(
    lead: Mapping[str, Any],
    *,
    portes_alvo: Collection[str] | None,
    capital_min: Decimal | None,
    capital_max: Decimal | None,
) -> SignalScore:
    """Porte na lista alvo e/ou capital social na faixa alvo — média das checagens
    configuradas (só uma configurada -> só ela conta; nenhuma -> não aplicável)."""
    checks: list[float] = []

    if portes_alvo:
        alvo = {p.upper() for p in portes_alvo}
        porte = lead.get("porte")
        checks.append(1.0 if (porte is not None and str(porte).upper() in alvo) else 0.0)

    if capital_min is not None or capital_max is not None:
        capital = lead.get("capital_social")
        if capital is None:
            checks.append(0.0)
        else:
            dentro = True
            if capital_min is not None and capital < capital_min:
                dentro = False
            if capital_max is not None and capital > capital_max:
                dentro = False
            checks.append(1.0 if dentro else 0.0)

    if not checks:
        return SignalScore(0.0, applicable=False)
    return SignalScore(sum(checks) / len(checks))


def signal_recencia(
    lead: Mapping[str, Any], *, recencia_dias_max: int | None, hoje: date
) -> SignalScore:
    """Decaimento linear: `data_inicio_atividade == hoje` -> 1.0; `>= recencia_dias_max`
    dias atrás -> 0.0. Sem `data_inicio_atividade` -> 0.0 (mas ainda aplicável, já que
    `recencia_dias_max` está configurado). `recencia_dias_max=None` desativa o sinal."""
    if recencia_dias_max is None:
        return SignalScore(0.0, applicable=False)

    data_inicio = lead.get("data_inicio_atividade")
    if data_inicio is None:
        return SignalScore(0.0)

    dias = max((hoje - data_inicio).days, 0)
    if dias >= recencia_dias_max:
        return SignalScore(0.0)
    return SignalScore(1.0 - dias / recencia_dias_max)


def signal_regiao(lead: Mapping[str, Any], regioes_alvo: Collection[str] | None) -> SignalScore:
    """`regiao` do lead está entre as regiões alvo (comparação normalizada em maiúsculo)."""
    if not regioes_alvo:
        return SignalScore(0.0, applicable=False)
    alvo = {r.upper() for r in regioes_alvo}
    regiao = lead.get("regiao")
    return SignalScore(1.0 if (regiao is not None and str(regiao).upper() in alvo) else 0.0)


# -- Orquestração -----------------------------------------------------------------


def score_lead(
    lead: Mapping[str, Any], config: ScoringConfig, *, hoje: date | None = None
) -> ScoreBreakdown:
    """Calcula o `score_icp` (0-100) de um lead a partir dos sinais configurados em
    `config`.

    Args:
        lead: dict-like com (ao menos) as chaves usadas pelos sinais: `situacao`,
            `email`, `telefone`, `cod_atividade`, `porte`, `capital_social`,
            `data_inicio_atividade`, `regiao` — os mesmos nomes do schema canônico
            (`CanonicalLead`, ver `src/ingestion/base.py`).
        config: pesos + alvos do ICP (ver `ScoringConfig`).
        hoje: data de referência para `signal_recencia`; default `date.today()`.

    Returns:
        `ScoreBreakdown` com o score final, se foi vetado, e o detalhamento por sinal.
    """
    if not is_situacao_ativa(lead):
        return ScoreBreakdown(score_icp=0.0, vetado=True)

    reference_date = hoje if hoje is not None else date.today()

    sinais: dict[str, SignalScore] = {
        "contato": signal_contato(lead),
        "atividade": signal_atividade(lead, config.atividades_alvo),
        "porte_capital": signal_porte_capital(
            lead,
            portes_alvo=config.portes_alvo,
            capital_min=config.capital_social_min,
            capital_max=config.capital_social_max,
        ),
        "recencia": signal_recencia(
            lead, recencia_dias_max=config.recencia_dias_max, hoje=reference_date
        ),
        "regiao": signal_regiao(lead, config.regioes_alvo),
    }
    pesos: dict[str, float] = {
        "contato": config.peso_contato,
        "atividade": config.peso_atividade,
        "porte_capital": config.peso_porte_capital,
        "recencia": config.peso_recencia,
        "regiao": config.peso_regiao,
    }

    peso_total = sum(pesos[nome] for nome, sinal in sinais.items() if sinal.applicable)
    if peso_total <= 0:
        score = 0.0
    else:
        pontos = sum(
            pesos[nome] * sinal.fraction for nome, sinal in sinais.items() if sinal.applicable
        )
        score = round((pontos / peso_total) * 100, 2)

    return ScoreBreakdown(score_icp=score, vetado=False, sinais=sinais)


def apply_score_icp(
    lead: Mapping[str, Any], config: ScoringConfig, *, hoje: date | None = None
) -> dict[str, Any]:
    """Retorna uma cópia de `lead` com `score_icp` preenchido a partir de `score_lead`.
    Não modifica `lead` in-place."""
    breakdown = score_lead(lead, config, hoje=hoje)
    updated = dict(lead)
    updated["score_icp"] = breakdown.score_icp
    return updated
