"""Adaptador de `RuleConditionPort`: ¿la condicion de esta regla esta
disparando AHORA sobre esta entidad? (T067, contracts/mcp-tools.md
`apply_defensive_action`, comprobaciones 1-4).

El servidor no se fia de que el llamador diga que la regla aplica: lo
recalcula. Y lo recalcula CON LOS EVALUADORES QUE YA EXISTEN, sin reescribir
ni un umbral:

- `rules.domain.rule_evaluator.evaluate_rule` decide si la regla actua sobre
  la senal viva (codigo, tipo de senal y nivel de autonomia). Los umbrales
  del catalogo viven en `signals.domain.signal_engine`, que es quien produjo
  esa senal; duplicarlos aqui seria tener dos verdades.
- La senal es la ultima que el ciclo de senales publico para la entidad.

Cinco razones para responder que NO, todas por denegar por defecto:

1. Datos viejos (`FreshnessPort`): `STALE_DATA`. Una regla no dispara sobre
   metricas que no se han refrescado.
2. Regla apagada o desconocida: el propietario manda sobre `is_enabled`; una
   regla que el no ha encendido no autoriza nada.
3. Regla del catalogo aun sin calibrar: la semilla de `0007` deja
   `condition = {}` y el repositorio de `rules` no puede reconstruirla. El
   `rule_id` llega de un agente (contracts/mcp-tools.md), asi que un codigo
   a medio sembrar tiene que ser un "no dispara", no una excepcion que
   suba hasta la respuesta.
4. Senal anterior a la ultima ingesta: llegaron metricas DESPUES de
   calcularla, asi que ya no se sabe si sigue disparando. Reevaluar de
   verdad significa exigir que la senal cubra los ultimos datos, no dar por
   buena la ultima conclusion.
5. Regla habilitada pero NOTIFY/APPROVAL, no AUTO (gap encontrado cableando
   `apply_defensive_action`, US3): el unico consumidor de este puerto es
   `AuthorizeRuleAction`, que acuña `rule_authorization` -- eso solo es
   valido para autonomia AUTO (FR-11/FR-12). `evaluate_rule` por si solo
   solo distingue "no aplica" (`NOOP`) de "aplica con la autonomia que
   sea"; este puerto añade el filtro que le falta a esa señal para su
   unico uso real."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from safent_ads.rules.application.ports import RuleRepository
from safent_ads.rules.domain.autonomy import AutonomyLevel
from safent_ads.rules.domain.rule_evaluator import (
    RuleOutcome,
    evaluate_creative_rule,
    evaluate_rule,
)
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import EntityRef
from safent_ads.signals.application.ports import CreativeSignalRepository, SignalRepository

__all__ = ["MetricsFreshnessPort", "SqlRuleConditionPort"]


class MetricsFreshnessPort(Protocol):
    """Lo que este adaptador necesita de la frescura de datos: si estan
    viejos y cuando se ingesto por ultima vez. `SqlFreshnessPort` lo cumple."""

    async def is_stale(self, entity_ref: EntityRef) -> bool: ...

    async def last_ingested_at(self, entity_ref: EntityRef) -> datetime | None: ...


class SqlRuleConditionPort:
    """Implementa `execution.application.ports.RuleConditionPort` sobre los
    repositorios de `rules` y `signals`. Depende de sus PUERTOS, no de sus
    adaptadores: aqui no hay una sola sentencia SQL propia."""

    def __init__(
        self,
        *,
        rules: RuleRepository,
        signals: SignalRepository,
        creative_signals: CreativeSignalRepository,
        freshness: MetricsFreshnessPort,
    ) -> None:
        self._rules = rules
        self._signals = signals
        self._creative_signals = creative_signals
        self._freshness = freshness

    async def is_condition_live(self, rule_id: str, entity_ref: EntityRef) -> bool:
        if await self._freshness.is_stale(entity_ref):
            return False
        stored = await self._load(rule_id)
        if stored is None or not stored.is_enabled:
            return False
        # Gap encontrado cableando `apply_defensive_action`
        # (contracts/mcp-tools.md comprobacion 1: "la regla debe... tener
        # autonomy_level = AUTO"). `evaluate_rule` responde AUTO_ACTION
        # sobre la SEÑAL, pero `is_condition_live` solo miraba
        # "no es NOOP" -- una regla NOTIFY o APPROVAL habilitada con
        # condicion viva pasaba igual. El unico llamador de este puerto es
        # `AuthorizeRuleAction` (acuña `rule_authorization`), y esa
        # autorizacion NUNCA es valida fuera de AUTO (FR-11/FR-12).
        if stored.rule.autonomy_level is not AutonomyLevel.AUTO:
            return False
        ingested_at = await self._freshness.last_ingested_at(entity_ref)
        return await self._fires(stored, entity_ref, ingested_at)

    async def _load(self, rule_id: str) -> StoredRule | None:
        try:
            return await self._rules.get_by_code(rule_id)
        except (KeyError, DomainError):
            # Fila del catalogo sin calibrar: sin condicion no hay nada que
            # pueda estar disparando.
            return None

    async def _fires(
        self, stored: StoredRule, entity_ref: EntityRef, ingested_at: datetime | None
    ) -> bool:
        signal = await self._signals.find_latest_for_entity(entity_ref=entity_ref)
        if signal is not None and _covers(signal.emitted_at, ingested_at):
            if evaluate_rule(stored.rule, signal) is not RuleOutcome.NOOP:
                return True
        creative = await self._creative_signals.find_latest_for_entity(entity_ref=entity_ref)
        if creative is None or not _covers(creative.emitted_at, ingested_at):
            return False
        return evaluate_creative_rule(stored.rule, creative) is not RuleOutcome.NOOP


def _covers(emitted_at: datetime, ingested_at: datetime | None) -> bool:
    """La senal cubre los ultimos datos si se emitio despues de la ultima
    ingesta. Sin ingesta registrada no hay nada que reevaluar contra."""
    return ingested_at is None or emitted_at >= ingested_at
