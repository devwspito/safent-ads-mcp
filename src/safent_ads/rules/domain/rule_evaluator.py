"""`rule_evaluator` (tasks.md T038): mapea `Signal`/`CreativeSignal` + `Rule`
-> `RuleOutcome`. El recorte de guardarrailes (`GuardrailPolicy.clamp`) lo
aplica otro lane despues, en el punto unico de escritura (plan.md §6); aqui
solo se decide QUE hacer con la senal segun la autonomia de la regla, nunca
CUANTO."""

from __future__ import annotations

from enum import StrEnum

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.rule import Rule
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind


class RuleOutcome(StrEnum):
    NOOP = "noop"
    NOTIFY = "notify"
    PROPOSE = "propose"
    AUTO_ACTION = "auto_action"


_AUTONOMY_TO_OUTCOME: dict[AutonomyLevel, RuleOutcome] = {
    AutonomyLevel.NOTIFY: RuleOutcome.NOTIFY,
    AutonomyLevel.APPROVAL: RuleOutcome.PROPOSE,
    AutonomyLevel.AUTO: RuleOutcome.AUTO_ACTION,
}

_ACTION_TO_SIGNAL_KIND: dict[ActionKind, SignalKind] = {
    ActionKind.BUY: SignalKind.BUY,
    ActionKind.SELL: SignalKind.SELL,
    ActionKind.EXIT: SignalKind.EXIT,
    ActionKind.ADD_NEGATIVE_KEYWORD: SignalKind.EXIT,
    ActionKind.HOLD_ALL: SignalKind.HOLD,
    ActionKind.NOTIFY_ONLY: SignalKind.HOLD,
}

_ACTION_TO_CREATIVE_KIND: dict[ActionKind, CreativeSignalKind] = {
    ActionKind.CREATIVE_KILL: CreativeSignalKind.LOSER,
    ActionKind.CREATIVE_SCALE: CreativeSignalKind.WINNER,
}


def evaluate_rule(rule: Rule, signal: Signal) -> RuleOutcome:
    """`rule.code` debe coincidir con `signal.rule_code`: una regla nunca
    actua sobre la senal de otra regla, aunque el `kind` coincida por
    casualidad."""
    if signal.rule_code != rule.code:
        return RuleOutcome.NOOP
    expected_kind = _ACTION_TO_SIGNAL_KIND.get(rule.action_kind)
    if expected_kind is None or signal.kind is not expected_kind:
        return RuleOutcome.NOOP
    return _AUTONOMY_TO_OUTCOME[rule.autonomy_level]


def evaluate_creative_rule(rule: Rule, signal: CreativeSignal) -> RuleOutcome:
    if signal.rule_code != rule.code:
        return RuleOutcome.NOOP
    expected_kind = _ACTION_TO_CREATIVE_KIND.get(rule.action_kind)
    if expected_kind is None or signal.kind is not expected_kind:
        return RuleOutcome.NOOP
    return _AUTONOMY_TO_OUTCOME[rule.autonomy_level]
