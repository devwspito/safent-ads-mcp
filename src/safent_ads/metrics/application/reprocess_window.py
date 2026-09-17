"""`ReprocessWindow` (plan.md §5, tasks.md T031, FR-5): reproceso de una
ventana de hasta 28 dias cuando plataforma o CRM reportan tarde.

Idempotente: reprocesar el mismo lote dos veces produce el mismo estado final
y no duplica `Restatement` (`test_reprocess_is_idempotent`) porque solo se
anexa un `Restatement` cuando el valor corregido difiere del almacenado."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.metrics.application.ports import MetricFactRepository, RestatementRepository
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.domain.restatement import Restatement

_SCALAR_FIELDS = (
    "spend_minor",
    "impressions",
    "clicks",
    "reach",
    "conversion_value_minor",
    "video_views_3s",
    "video_views_75pct",
)


@dataclass(frozen=True, kw_only=True, slots=True)
class ReprocessWindowRequest:
    corrected_facts: Sequence[MetricFact]
    reason: str
    recorded_at: datetime


class ReprocessWindow:
    def __init__(self, facts: MetricFactRepository, restatements: RestatementRepository) -> None:
        self._facts = facts
        self._restatements = restatements

    async def execute(self, request: ReprocessWindowRequest) -> Sequence[Restatement]:
        recorded: list[Restatement] = []
        for corrected in request.corrected_facts:
            existing = await self._facts.find_by_natural_key(
                entity_ref=corrected.entity_ref,
                stat_date=corrected.stat_date,
                stat_hour=corrected.stat_hour,
            )
            if existing is not None:
                context = _DiffContext(
                    old=existing, new=corrected, reason=request.reason,
                    recorded_at=request.recorded_at,
                )
                recorded.extend(await self._record_diffs(context))
            await self._facts.upsert_many([corrected])
        return recorded

    async def _record_diffs(self, context: _DiffContext) -> list[Restatement]:
        diffs = _diff_scalar_fields(context) + _diff_conversion_fields(context)
        for restatement in diffs:
            await self._restatements.record(restatement)
        return diffs


@dataclass(frozen=True, kw_only=True, slots=True)
class _DiffContext:
    old: MetricFact
    new: MetricFact
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class _FieldDiff:
    field_name: str
    old_value: int
    new_value: int


def _diff_scalar_fields(context: _DiffContext) -> list[Restatement]:
    diffs = [
        _FieldDiff(field_name=name, old_value=getattr(context.old, name), new_value=value)
        for name in _SCALAR_FIELDS
        if (value := getattr(context.new, name)) != getattr(context.old, name)
    ]
    return [_build(context, diff) for diff in diffs]


def _diff_conversion_fields(context: _DiffContext) -> list[Restatement]:
    diffs = []
    for kind in set(context.old.conversions) | set(context.new.conversions):
        old_value, new_value = context.old.conversions_of(kind), context.new.conversions_of(kind)
        if old_value != new_value:
            diffs.append(
                _FieldDiff(
                    field_name=f"conversions.{kind}", old_value=old_value, new_value=new_value
                )
            )
    return [_build(context, diff) for diff in diffs]


def _build(context: _DiffContext, diff: _FieldDiff) -> Restatement:
    fact = context.new
    return Restatement(
        entity_ref=fact.entity_ref,
        stat_date=fact.stat_date,
        stat_hour=fact.stat_hour,
        field_name=diff.field_name,
        old_value=diff.old_value,
        new_value=diff.new_value,
        reason=context.reason,
        recorded_at=context.recorded_at,
    )
