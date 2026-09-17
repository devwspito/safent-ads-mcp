"""`GuardrailPolicy` (data-model.md §GuardrailSet, tabla `guardrails`;
spec.md FR-13: 'guardarraíles duros'). `clamp()` es pura: el chokepoint de
`execution` (otro lane) vuelve a aplicarla justo antes de escribir en
plataforma; aqui solo se define la VO y la funcion, no el punto unico de
escritura (plan.md §6)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.rules.domain.errors import InvalidGuardrailPolicyError

_MAX_STEP_PCT_CEILING = 100.0


@dataclass(frozen=True, kw_only=True, slots=True)
class GuardrailVerdict:
    allowed_value_minor: int
    clamped: bool
    blocked: bool
    breached_limits: tuple[str, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class GuardrailPolicy:
    daily_cap_minor: int
    monthly_cap_minor: int
    floor_minor: int
    ceiling_minor: int
    max_step_pct: float
    max_changes_per_day: int

    def __post_init__(self) -> None:
        self._validate_non_negative()
        if self.floor_minor > self.ceiling_minor:
            raise InvalidGuardrailPolicyError(
                f"floor_minor > ceiling_minor: {self.floor_minor} > {self.ceiling_minor}"
            )
        if not (0 < self.max_step_pct <= _MAX_STEP_PCT_CEILING):
            raise InvalidGuardrailPolicyError(f"max_step_pct fuera de (0,100]: {self.max_step_pct}")

    def _validate_non_negative(self) -> None:
        for name, value in (
            ("daily_cap_minor", self.daily_cap_minor),
            ("monthly_cap_minor", self.monthly_cap_minor),
            ("floor_minor", self.floor_minor),
            ("ceiling_minor", self.ceiling_minor),
            ("max_changes_per_day", self.max_changes_per_day),
        ):
            if value < 0:
                raise InvalidGuardrailPolicyError(f"{name} negativo: {value}")

    def clamp(
        self, *, current_minor: int, proposed_minor: int, changes_today: int, spent_month_minor: int
    ) -> GuardrailVerdict:
        if changes_today >= self.max_changes_per_day:
            return GuardrailVerdict(
                allowed_value_minor=current_minor,
                clamped=True,
                blocked=True,
                breached_limits=("max_changes_per_day",),
            )
        value, breached = self._clamp_step(current_minor, proposed_minor)
        value, breached = self._clamp_bounds(value, breached)
        value, breached = self._clamp_caps(current_minor, value, breached, spent_month_minor)
        return GuardrailVerdict(
            allowed_value_minor=value,
            clamped=value != proposed_minor,
            blocked=False,
            breached_limits=tuple(breached),
        )

    def _clamp_step(self, current_minor: int, proposed_minor: int) -> tuple[int, list[str]]:
        if current_minor <= 0:
            return proposed_minor, []
        max_delta = current_minor * self.max_step_pct / 100
        if abs(proposed_minor - current_minor) <= max_delta:
            return proposed_minor, []
        direction = 1 if proposed_minor > current_minor else -1
        return current_minor + direction * round(max_delta), ["max_step_pct"]

    def _clamp_bounds(self, value: int, breached: list[str]) -> tuple[int, list[str]]:
        if value < self.floor_minor:
            return self.floor_minor, [*breached, "floor"]
        if value > self.ceiling_minor:
            return self.ceiling_minor, [*breached, "ceiling"]
        return value, breached

    def _clamp_caps(
        self, current_minor: int, value: int, breached: list[str], spent_month_minor: int
    ) -> tuple[int, list[str]]:
        if value > self.daily_cap_minor:
            value, breached = self.daily_cap_minor, [*breached, "daily_cap"]
        projected_delta = max(value - current_minor, 0)
        if spent_month_minor + projected_delta > self.monthly_cap_minor:
            allowed_increase = max(self.monthly_cap_minor - spent_month_minor, 0)
            value = current_minor + allowed_increase if value > current_minor else value
            breached = [*breached, "monthly_cap"]
        return value, breached
