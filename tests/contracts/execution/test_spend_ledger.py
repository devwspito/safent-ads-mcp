"""Contrato de `SpendLedger`: identico para el doble en memoria y para
`SqlSpendLedger`.

Lo que se puede exigir a las dos implementaciones es la forma del
`LedgerSnapshot` y que un cambio aplicado quede anotado con su delta. Cuanto
suma cada tope depende de filas reales y se prueba contra Postgres
(`tests/integration/execution/test_spend_ledger_sql.py`)."""

from __future__ import annotations

from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.proposals.domain.money import Money
from tests.contracts.execution.conftest import LedgerFixture


async def test_a_ledger_without_movements_is_all_zeros(ledger: LedgerFixture) -> None:
    entity_ref = await ledger.given_entity_with_a_running_execution()
    scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))

    snapshot = await ledger.ledger.snapshot(scope, entity_ref)

    assert snapshot.platform_spend_today == Money.zero()
    assert snapshot.platform_spend_month_to_date == Money.zero()
    assert snapshot.applied_changes_today == Money.zero()
    assert snapshot.changes_count_today_for_entity == 0


async def test_an_applied_change_is_recorded_with_its_delta(ledger: LedgerFixture) -> None:
    """Una bajada resta: el delta viaja con signo, que es lo que hace que el
    tope diario sea una suma exacta y no un recuento de movimientos."""
    entity_ref = await ledger.given_entity_with_a_running_execution()
    scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))

    await ledger.ledger.record_applied_change(scope, entity_ref, Money.of("-30"))

    assert await ledger.recorded_deltas(entity_ref) == [Money.of("-30")]
