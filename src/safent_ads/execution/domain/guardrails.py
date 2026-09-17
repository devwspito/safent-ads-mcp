"""Guardarrailes duros (FR-13), freno de emergencia (FR-14) y el evaluador
que los aplica antes de cualquier escritura (T059/T060; threat-model.md
C-15..C-18).

Nota de ubicacion (asuncion documentada): `plan.md §5` situa `GuardrailSet`
y `EmergencyBrake` en el contexto `rules`. Esa rama (`rules/`, `signals/`,
`accounts/`) no es propiedad de este lote de trabajo (T057-T070); el
encargo de esta rama los reubica explicitamente en `execution/domain/` para
que el chokepoint (unica via de escritura, plan.md §6) no dependa de un
contexto que otra rama aun no ha publicado. Si `rules/` los define mas
adelante, este modulo se convierte en un re-export."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    validate_child_payload,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.errors import DomainError
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import EntityRef


class GuardrailInvariantError(DomainError):
    """Violacion de un invariante de composicion de guardarrailes."""


class GuardrailRelaxationError(GuardrailInvariantError):
    """FR-13/C-17: un ambito especifico intento ser MENOS restrictivo que el
    general — el guardarraíl compuesto nunca puede ampliar un limite."""


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class ScopeKind(StrEnum):
    BUSINESS = "business"
    PLATFORM_ACCOUNT = "platform_account"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True)
class GuardrailScope:
    """Ambito de aplicacion de un `GuardrailSet` (data-model.md: `GuardrailSet
    1-1 Scope`). `ref` es opaco: `business_id`, referencia de cuenta o
    `EntityRef` en texto, segun `kind`."""

    kind: ScopeKind
    ref: str


# ---------------------------------------------------------------------------
# GuardrailSet
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardrailSet:
    """Limites duros de un ambito (FR-13): tope diario/mensual, suelo/techo,
    salto maximo por cambio y cambios maximos por entidad y dia."""

    scope: GuardrailScope
    daily_cap: Money
    monthly_cap: Money
    floor: Money
    ceiling: Money
    max_step_pct: float
    max_changes_per_entity_day: int

    def __post_init__(self) -> None:
        if not (0 < self.max_step_pct <= 1):
            raise GuardrailInvariantError("max_step_pct debe estar en (0, 1]")
        if self.floor > self.ceiling:
            raise GuardrailInvariantError("floor no puede superar a ceiling")

    def is_at_least_as_restrictive_as(self, general: GuardrailSet) -> bool:
        return (
            self.daily_cap <= general.daily_cap
            and self.monthly_cap <= general.monthly_cap
            and self.floor >= general.floor
            and self.ceiling <= general.ceiling
            and self.max_step_pct <= general.max_step_pct
            and self.max_changes_per_entity_day <= general.max_changes_per_entity_day
        )

    def effective_with(self, general: GuardrailSet) -> GuardrailSet:
        """Composicion restrictiva: un ambito especifico (p. ej. una campana)
        solo puede estrechar los limites de uno general (p. ej. la cuenta).
        Lanza `GuardrailRelaxationError` si algun limite se ampliaria."""
        if not self.is_at_least_as_restrictive_as(general):
            raise GuardrailRelaxationError(
                f"el ambito {self.scope} relaja limites de {general.scope}"
            )
        return self


# ---------------------------------------------------------------------------
# EmergencyBrake
# ---------------------------------------------------------------------------


class BrakeMode(StrEnum):
    AUTONOMOUS = "autonomous"  # bloquea solo lo que decide el motor de reglas
    ALL = "all"  # bloquea toda escritura, incluida la aprobada por humano


class BrakeScopeKind(StrEnum):
    GLOBAL = "global"
    BUSINESS = "business"
    PLATFORM_ACCOUNT = "platform_account"


@dataclass(frozen=True, slots=True)
class BrakeScope:
    kind: BrakeScopeKind
    ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not BrakeScopeKind.GLOBAL and not self.ref:
            raise GuardrailInvariantError(f"{self.kind} requiere ref")


def brake_scope_from(scope: GuardrailScope) -> BrakeScope:
    """Asuncion documentada: el freno se comprueba a nivel de cuenta de
    plataforma (`BrakeScope.PLATFORM_ACCOUNT`), no por entidad — coherente
    con FR-14 ("freno global y por cuenta"). El `ref` de `GuardrailScope`
    (que puede ser una entidad especifica) se usa tal cual porque el
    adaptador real resuelve la cuenta desde la entidad; los dobles en
    memoria solo necesitan una clave estable para el lookup. Punto unico
    de esta conversion: tanto `ExecutionChokepoint` como `AuthorizeRuleAction`
    la reutilizan para no divergir en silencio."""
    return BrakeScope(kind=BrakeScopeKind.PLATFORM_ACCOUNT, ref=scope.ref)


@dataclass(slots=True)
class EmergencyBrake:
    """Interruptor que detiene la actuacion autonoma al instante (FR-14).
    Un freno activo por ambito como maximo (data-model.md invariante)."""

    scope: BrakeScope
    mode: BrakeMode
    engaged: bool = False
    reason: str | None = None
    since: datetime | None = None

    def engage(self, reason: str, at: datetime) -> None:
        self.engaged = True
        self.reason = reason
        self.since = at

    def release(self, at: datetime) -> None:
        self.engaged = False
        self.reason = None
        self.since = at

    def blocks(self, authorization_kind: AuthorizationKind) -> bool:
        """`ALL` bloquea cualquier escritura; `AUTONOMOUS` bloquea lo que
        decide el motor de reglas (`RULE_AUTHORIZATION`) **y** un paso de
        publicación de paquete (`PACKAGE_STEP`) — lo aprobado por el
        propietario sigue adelante salvo que el freno sea `ALL`.

        `003-paquete-de-campana` data-model.md Revision 2 §R2.9 (AL-1): un
        paso de paquete corre sin humano delante (lo firma
        `chokepoint_step_executor`, no un clic), así que para el freno
        cuenta como autónomo aunque su origen último sea una aprobación
        humana."""
        if not self.engaged:
            return False
        if self.mode is BrakeMode.ALL:
            return True
        return authorization_kind in (
            AuthorizationKind.RULE_AUTHORIZATION,
            AuthorizationKind.PACKAGE_STEP,
        )


def most_restrictive_brake(brakes: Iterable[EmergencyBrake | None]) -> EmergencyBrake | None:
    """Combina los frenos de los ambitos que alcanzan a una cuenta (GLOBAL,
    BUSINESS que la posee, PLATFORM_ACCOUNT) en el freno EFECTIVO (bug
    corregido: los chokepoints solo comprobaban el freno de cuenta,
    ignorando uno global o de negocio activo). Solo cuentan los `engaged`;
    entre esos, gana el primero en modo `ALL` segun el orden en que el
    llamador pase los candidatos (GLOBAL, luego BUSINESS, luego
    PLATFORM_ACCOUNT -- de mas ancho a mas estrecho), y si ninguno es `ALL`
    se queda el primero (el de alcance mas ancho). Sin este criterio, un
    freno ancho en modo `AUTONOMOUS` podria devolverse en vez de uno mas
    estrecho en modo `ALL`, dejando pasar una aprobacion humana que
    deberia bloquearse."""
    engaged = [brake for brake in brakes if brake is not None and brake.engaged]
    for brake in engaged:
        if brake.mode is BrakeMode.ALL:
            return brake
    return engaged[0] if engaged else None


@dataclass(frozen=True, kw_only=True)
class EmergencyBrakeEngaged(DomainEvent):
    scope_kind: str
    scope_ref: str | None
    mode: str
    reason: str
    # `"unknown"` en vez de inventar: mismo criterio que
    # `SqlBrakeStatePort.DEFAULT_BRAKE_ACTOR` hasta que el llamador (REST
    # `/kill-switch`, `/freno` de Telegram) pase el propietario autenticado.
    engaged_by: str = "unknown"


@dataclass(frozen=True, kw_only=True)
class EmergencyBrakeReleased(DomainEvent):
    scope_kind: str
    scope_ref: str | None
    released_by: str = "unknown"


# ---------------------------------------------------------------------------
# GuardrailEvaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardrailChange:
    """El cambio que se somete a guardarrailes. `before`/`after` se modelan
    siempre como presupuesto diario equivalente: una pausa es `after=0`, una
    reanudacion es `after=before` (asuncion: unifica la aritmetica de
    guardarrailes para toda accion, defensiva o no, sin una rama por tipo)."""

    scope: GuardrailScope
    entity_ref: EntityRef
    authorization_kind: AuthorizationKind
    before: Money
    after: Money
    is_creation: bool = False


def money_pair_from_diff(diff: ProposedDiff) -> tuple[Money, Money]:
    """Guardarrailes solo razonan sobre presupuesto: un diff no monetario
    (p. ej. `status`) no consume cupo de gasto — antes/despues siempre en
    Money "equivalente". Punto unico de esta conversion: lo reutilizan
    `ExecutionChokepoint` y `AuthorizeRuleAction`."""
    if diff.parameter.startswith("new_campaign:"):
        if diff.before is not None:
            raise CampaignCreationError("campaign_creation_before_must_be_absent")
        budget = creation_budget(diff.after, diff.entity_ref)
        return Money.zero(budget.currency), budget
    if diff.parameter.startswith(("new_ad_set:", "new_ad:")):
        plan = validate_child_payload(diff.after, diff.entity_ref)
        if diff.before is not None or not diff.parameter.startswith(f"new_{plan['kind']}:"):
            raise AdChildCreationError
        # No new budget; this does not replace a CPC bid/creative with 0 EUR.
        # effective_diff preserves the complete approved JSON unchanged.
        return Money.zero("EUR"), Money.zero("EUR")
    before = diff.before if isinstance(diff.before, Money) else Money.zero()
    after = diff.after if isinstance(diff.after, Money) else Money.zero()
    return before, after


def effective_diff(diff: ProposedDiff, verdict: GuardrailVerdict) -> ProposedDiff:
    """El diff que de verdad se firma, se verifica y se escribe (BUG
    corregido, threat-model.md C-15/C-17): si el guardarraíl recorta un
    cambio monetario, `after` pasa a ser `verdict.clamped_after` -- nunca
    `proposal.diff.after` tal cual. El `ProposedDiff` de la propuesta NUNCA
    se muta (INV-1: una autorizacion firma un `diff_hash` concreto, y sigue
    siendo auditable lo que se pidio de verdad, sin recortar, en
    `proposals`). Sin recorte -- sobre un diff no monetario (que
    `money_pair_from_diff` ya deja fuera del guardarraíl), o con
    `verdict.allowed=False` (la rama de `_verdict` que deniega fija
    `clamped_after=change.before` solo para el hash del VEREDICTO, no
    representa un diff real que nadie vaya a firmar ni escribir) -- devuelve
    el mismo diff intacto: mismo `diff_hash`, cero cambio de comportamiento.

    Punto unico de esta conversion: `AuthorizeRuleAction`, `SubmitApproval`,
    `UndoExecution` y `ExecutionChokepoint` la reutilizan para firmar y
    verificar exactamente el mismo `diff_hash` -- si divergiesen, la
    reverificacion del bróker (que recalcula el hash desde el valor que de
    verdad viaja en el `WriteCommand`) denegaria por `diff_hash_mismatch`."""
    if not verdict.allowed or not isinstance(diff.after, Money):
        return diff
    return diff.with_new_value(verdict.clamped_after)


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Lectura del `SpendLedger` en el instante de evaluar (T060; C-17: tope
    sobre "cambios aplicados + gasto reportado por la plataforma")."""

    platform_spend_today: Money
    platform_spend_month_to_date: Money
    applied_changes_today: Money
    changes_count_today_for_entity: int
    reserved_increase: Money | None = None
    applied_increases_month_to_date: Money | None = None


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    allowed: bool
    clamped_after: Money
    reasons: tuple[str, ...]
    verdict_hash: str


class SpendLedger(Protocol):
    """Puerto: lee el gasto ya aplicado/reportado para construir un
    `LedgerSnapshot`, y registra cada cambio que el chokepoint aplica."""

    async def snapshot(self, scope: GuardrailScope, entity_ref: EntityRef) -> LedgerSnapshot: ...

    async def record_applied_change(
        self, scope: GuardrailScope, entity_ref: EntityRef, delta: Money
    ) -> None: ...


class GuardrailEvaluator:
    """Servicio de dominio puro: sin I/O, recibe el `LedgerSnapshot` ya
    leido. `evaluate` nunca lanza — el rechazo es un `GuardrailVerdict` con
    `allowed=False`, para que el chokepoint decida el `Outcome`."""

    def evaluate(
        self, change: GuardrailChange, guardrails: GuardrailSet, ledger: LedgerSnapshot
    ) -> GuardrailVerdict:
        auto_increase = self._auto_action_would_increase_spend(change)
        if auto_increase:
            return self._verdict(change, change.after, False, ("auto_action_would_increase_spend",))

        if change.is_creation:
            # There is no previous campaign budget to scale by. Creation must
            # satisfy absolute limits, not be silently clamped from 20 EUR to 0.
            reasons = self._check_caps(guardrails, ledger, change.after)
            if change.before.amount != 0 or not (
                guardrails.floor <= change.after <= guardrails.ceiling
            ):
                reasons += ("campaign_creation_budget_outside_limits",)
            return self._verdict(change, change.after, not reasons, reasons)

        clamped_after, clamp_reasons = self._clamp(change, guardrails)
        delta = clamped_after - change.before
        # El clamp puede convertir una bajada nominal en una subida real
        # (suelo por encima del valor vivo): el principio FR-11 se aplica al
        # valor EFECTIVO, no solo al propuesto. El broker lo vuelve a comprobar
        # por su cuenta (defensa en profundidad), pero esta capa no puede
        # depender de aquella.
        if self._is_rule_authorization(change) and clamped_after > change.before:
            return self._verdict(
                change, change.before, False, ("auto_action_would_increase_spend_after_clamp",)
            )

        cap_reasons = self._check_caps(guardrails, ledger, delta)
        if cap_reasons:
            return self._verdict(change, change.before, False, cap_reasons)

        return self._verdict(change, clamped_after, True, clamp_reasons)

    def _auto_action_would_increase_spend(self, change: GuardrailChange) -> bool:
        """Ninguna accion `AUTO` (motor de reglas) puede subir gasto —
        principio de autonomia defensiva (FR-11)."""
        return self._is_rule_authorization(change) and change.after > change.before

    @staticmethod
    def _is_rule_authorization(change: GuardrailChange) -> bool:
        return change.authorization_kind is AuthorizationKind.RULE_AUTHORIZATION

    def _clamp(
        self, change: GuardrailChange, guardrails: GuardrailSet
    ) -> tuple[Money, tuple[str, ...]]:
        reasons: list[str] = []
        clamped = change.after
        if clamped < guardrails.floor:
            clamped = guardrails.floor
            reasons.append("clamped_to_floor")
        elif clamped > guardrails.ceiling:
            clamped = guardrails.ceiling
            reasons.append("clamped_to_ceiling")

        max_step = change.before.scaled_by(Decimal(str(guardrails.max_step_pct)))
        if clamped > change.before and clamped - change.before > max_step:
            clamped = change.before + max_step
            reasons.append("clamped_to_max_step")
        elif clamped < change.before and change.before - clamped > max_step:
            clamped = change.before - max_step
            reasons.append("clamped_to_max_step")
        return clamped, tuple(reasons)

    def _check_caps(
        self,
        guardrails: GuardrailSet,
        ledger: LedgerSnapshot,
        delta: Money,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if ledger.changes_count_today_for_entity >= guardrails.max_changes_per_entity_day:
            reasons.append("max_changes_per_entity_day_reached")
        if delta.is_positive():
            reserved = ledger.reserved_increase or Money.zero(delta.currency)
            projected_daily = (
                ledger.platform_spend_today + ledger.applied_changes_today + reserved + delta
            )
            if projected_daily > guardrails.daily_cap:
                reasons.append("daily_cap_exceeded")
            committed_monthly = ledger.applied_increases_month_to_date or Money.zero(delta.currency)
            projected_monthly = (
                ledger.platform_spend_month_to_date + committed_monthly + reserved + delta
            )
            if projected_monthly > guardrails.monthly_cap:
                reasons.append("monthly_cap_exceeded")
        return tuple(reasons)

    def _verdict(
        self,
        change: GuardrailChange,
        after: Money,
        allowed: bool,
        reasons: tuple[str, ...],
    ) -> GuardrailVerdict:
        payload = {
            "scope": change.scope.ref,
            "entity_ref": str(change.entity_ref),
            "before": change.before.to_canonical(),
            "after": after.to_canonical(),
            "allowed": allowed,
            "reasons": list(reasons),
        }
        verdict_hash = _sha256_hex(canonical_json_bytes(payload))
        return GuardrailVerdict(
            allowed=allowed, clamped_after=after, reasons=reasons, verdict_hash=verdict_hash
        )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
