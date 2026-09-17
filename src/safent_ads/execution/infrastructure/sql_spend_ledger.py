"""Adaptador SQL de `SpendLedger` sobre `spend_ledger` (0009_executions).

threat-model.md C-17: los topes se calculan "sobre el ledger de cambios
aplicados + gasto reportado por la plataforma, mas contador por entidad y
dia". Sin ese ledger, N cambios pequenos por debajo del salto maximo suman
por encima del tope (bypass por goteo).

Que consulta cada campo del `LedgerSnapshot`:
- `platform_spend_today` / `platform_spend_month_to_date`: apuntes
  `platform_spend` de la CUENTA. El indice unico
  `ix_spend_ledger_daily_spend (entity_ref, ledger_date)` deja una sola
  instantanea por entidad y dia, asi que sumarlas es sumar la ultima de cada
  una, no un historial de lecturas.
- `applied_changes_today`: apuntes `applied_change` del dia, el gasto que
  este sistema ha comprometido y la plataforma todavia no ha reportado. Solo
  entra en el tope diario.
- `applied_increases_month_to_date`: compromisos positivos aplicados durante
  el mes. Se incluyen conservadoramente en el tope mensual: liquidar una
  reserva no debe borrar su proteccion antes de recibir gasto de plataforma.
  Puede sobrecontar gasto ya reportado; no pretende ser una prediccion.
- `reserved_increase`: compromisos ACTIVE, incluso de dias o meses anteriores;
  un resultado remoto desconocido nunca se libera por el paso del tiempo.
- `changes_count_today_for_entity`: cambios de la ENTIDAD hoy
  (`max_changes_per_entity_day`).

Las tres sumas de cuenta caben en una sola pasada por
`ix_spend_ledger_account_date (platform_account_id, ledger_date, kind)
INCLUDE (delta_minor)`: un recorrido de indice sin tocar la tabla, que es
para lo que ese INCLUDE existe (`test_cap_sums_use_the_covering_index`).

El dia es el de la ZONA HORARIA DE LA CUENTA, no UTC: un tope "diario" que
cambia de dia a las 02:00 locales no es el tope que el propietario cree
tener."""

from __future__ import annotations

from typing import Any, Final, Protocol

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.domain.guardrails import GuardrailScope, LedgerSnapshot
from safent_ads.execution.infrastructure.errors import (
    LedgerContextMissingError,
    LedgerCurrencyMismatchError,
    UnknownEntityRefError,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.infrastructure.value_codec import money_from_minor, money_to_minor
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import EntityRef
from safent_ads.shared.physical_ads_sql import PHYSICAL_ENTITY_REFS_SQL

__all__ = ["CAP_SUMS_SQL", "ClaimedExecutionPort", "SqlSpendLedger"]


class ClaimedExecutionPort(Protocol):
    """Quien sabe que intento se esta ejecutando ahora mismo
    (`SqlExecutionQueue`). El puerto `SpendLedger` no recibe el intento y un
    apunte sin ejecucion rompe "un apunte por ejecucion" — el indice unico
    que impide que un reintento infle el tope."""

    def current(self) -> ExecutionAttempt | None: ...


_CONTEXT: Final = """
    SELECT entity.business_id,
           entity.platform_account_id,
           account.currency,
           (CAST(:now AS timestamptz) AT TIME ZONE account.timezone)::date AS today,
           date_trunc('month',
                      CAST(:now AS timestamptz) AT TIME ZONE account.timezone)::date
               AS month_start
      FROM ads_execution_targets AS entity
      JOIN platform_accounts AS account ON account.id = entity.platform_account_id
     WHERE entity.entity_ref = :entity_ref
"""

# Consulta de los topes (C-17). Publica a proposito: el test de integracion
# le pasa EXPLAIN para comprobar que sigue resolviendose por el indice
# cubridor de 0009 y no por un barrido de la tabla.
CAP_SUMS_SQL: Final = """
    SELECT COALESCE(sum(delta_minor)
                    FILTER (WHERE kind = 'platform_spend' AND ledger_date = :today), 0)
               AS platform_spend_today,
           COALESCE(sum(delta_minor) FILTER (WHERE kind = 'platform_spend'), 0)
               AS platform_spend_month_to_date,
           COALESCE(sum(delta_minor)
                    FILTER (WHERE kind = 'applied_change' AND ledger_date = :today), 0)
               AS applied_changes_today,
           COALESCE(sum(GREATEST(delta_minor, 0)) FILTER (WHERE kind = 'applied_change'), 0)
               AS applied_increases_month_to_date
      FROM spend_ledger
     WHERE platform_account_id IN (
        SELECT sibling.id FROM platform_accounts sibling JOIN platform_accounts chosen
          ON sibling.business_id = chosen.business_id AND sibling.platform = chosen.platform
         AND sibling.external_account_id = chosen.external_account_id
         WHERE chosen.id = :platform_account_id)
       AND ledger_date BETWEEN :month_start AND :today
"""

# `ix_spend_ledger_entity_changes (entity_ref, ledger_date) WHERE kind =
# 'applied_change'`.
_ENTITY_CHANGES_TODAY: Final = f"""
    SELECT count(*) FROM spend_ledger
     WHERE entity_ref IN ({PHYSICAL_ENTITY_REFS_SQL})
       AND ledger_date = :today AND kind = 'applied_change'
"""  # noqa: S608 - fixed SQL fragment; all refs remain bound

# Un apunte por ejecucion: reejecutar el registro actualiza el suyo en vez de
# anadir otro (indice unico parcial `ix_spend_ledger_execution`).
_RECORD_APPLIED_CHANGE: Final = """
    INSERT INTO spend_ledger (business_id, platform_account_id, entity_ref, ledger_date,
                              currency, kind, delta_minor, previous_value_minor,
                              new_value_minor, execution_id, proposal_id)
    SELECT entity.business_id, entity.platform_account_id, entity.entity_ref,
           (CAST(:now AS timestamptz) AT TIME ZONE account.timezone)::date, :currency,
           'applied_change', :delta_minor, :previous_minor, :new_minor, :execution_id,
           :proposal_id
      FROM ads_execution_targets AS entity
      JOIN platform_accounts AS account ON account.id = entity.platform_account_id
     WHERE entity.entity_ref = :entity_ref
    ON CONFLICT (execution_id) WHERE execution_id IS NOT NULL
    DO UPDATE SET delta_minor          = EXCLUDED.delta_minor,
                  previous_value_minor = EXCLUDED.previous_value_minor,
                  new_value_minor      = EXCLUDED.new_value_minor,
                  ledger_date          = EXCLUDED.ledger_date,
                  currency             = EXCLUDED.currency
    RETURNING id
"""


class SqlSpendLedger:
    """Implementa `execution.domain.guardrails.SpendLedger`. Vive en la
    transaccion de la sesion que le pasan: el apunte del cambio aplicado se
    confirma con el desenlace del intento, nunca por su cuenta."""

    def __init__(self, session: AsyncSession, clock: Clock, attempts: ClaimedExecutionPort) -> None:
        self._session = session
        self._clock = clock
        self._attempts = attempts

    async def snapshot(
        self,
        scope: GuardrailScope,  # noqa: ARG002 - forma exacta del puerto: la cuenta sale de la entidad
        entity_ref: EntityRef,
    ) -> LedgerSnapshot:
        context = await self._context(entity_ref)
        sums = await self._session.execute(
            text(CAP_SUMS_SQL),
            {
                "platform_account_id": context["platform_account_id"],
                "today": context["today"],
                "month_start": context["month_start"],
            },
        )
        totals = sums.mappings().one()
        currency = str(context["currency"])
        # Unresolved reservations never expire at midnight or with a worker lease.
        pending = (
            (
                await self._session.execute(
                    text(f"""
            SELECT COALESCE(sum(positive_delta_minor), 0) AS amount,
                   count(*) FILTER (WHERE entity_ref IN ({PHYSICAL_ENTITY_REFS_SQL})) AS changes
              FROM execution_reservations
             WHERE platform_account_id IN (
                SELECT sibling.id FROM platform_accounts sibling JOIN platform_accounts chosen
                  ON sibling.business_id = chosen.business_id AND sibling.platform = chosen.platform
                 AND sibling.external_account_id = chosen.external_account_id
                 WHERE chosen.id = :account) AND state = 'ACTIVE'
        """),  # noqa: S608 - fixed SQL fragment; all refs remain bound
                    {"account": context["platform_account_id"], "entity_ref": str(entity_ref)},
                )
            )
            .mappings()
            .one()
        )
        return LedgerSnapshot(
            platform_spend_today=_money(totals["platform_spend_today"], currency),
            platform_spend_month_to_date=_money(totals["platform_spend_month_to_date"], currency),
            applied_changes_today=_money(totals["applied_changes_today"], currency),
            changes_count_today_for_entity=(
                await self._changes_today(entity_ref, context["today"]) + int(pending["changes"])
            ),
            reserved_increase=_money(pending["amount"], currency),
            applied_increases_month_to_date=_money(
                totals["applied_increases_month_to_date"], currency
            ),
        )

    async def record_applied_change(
        self,
        scope: GuardrailScope,  # noqa: ARG002 - forma exacta del puerto
        entity_ref: EntityRef,
        delta: Money,
    ) -> None:
        attempt = self._attempts.current()
        if attempt is None:
            raise LedgerContextMissingError(
                f"no hay ejecucion reclamada a la que atribuir el cambio de {entity_ref}"
            )
        context = await self._context(entity_ref)
        if delta.currency != str(context["currency"]):
            raise LedgerCurrencyMismatchError(
                f"{delta.currency} no es la divisa de la cuenta ({context['currency']})"
            )
        await self._session.execute(
            text(_RECORD_APPLIED_CHANGE), _change_params(attempt, entity_ref, delta, self._now())
        )

    async def _context(self, entity_ref: EntityRef) -> RowMapping:
        result = await self._session.execute(
            text(_CONTEXT), {"entity_ref": str(entity_ref), "now": self._now()}
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise UnknownEntityRefError(
                f"{entity_ref} no esta en ad_entities: sin cuenta no hay tope que aplicar"
            )
        return row

    async def _changes_today(self, entity_ref: EntityRef, today: object) -> int:
        result = await self._session.execute(
            text(_ENTITY_CHANGES_TODAY), {"entity_ref": str(entity_ref), "today": today}
        )
        return int(result.scalar_one())

    def _now(self) -> object:
        return self._clock.now()


def _change_params(
    attempt: ExecutionAttempt, entity_ref: EntityRef, delta: Money, now: object
) -> dict[str, Any]:
    """`previous_value_minor` sale del valor que la fila de la ejecucion
    guarda como anterior; el nuevo es ese mas el delta, que es exactamente lo
    que exige `spend_ledger_applied_change_check`. Un cambio no monetario
    (pausar, negativa) tiene delta cero: cuenta para el limite de cambios por
    entidad y dia, no para el tope de gasto."""
    previous_minor = money_to_minor(attempt.previous_value)
    delta_minor = money_to_minor(delta)
    return {
        "entity_ref": str(entity_ref),
        "now": now,
        "currency": delta.currency,
        "delta_minor": delta_minor,
        "previous_minor": previous_minor,
        "new_minor": previous_minor + delta_minor,
        "execution_id": str(attempt.execution_id),
        "proposal_id": str(attempt.proposal_id),
    }


def _money(minor_units: object, currency: str) -> Money:
    return money_from_minor(int(str(minor_units)), currency)
