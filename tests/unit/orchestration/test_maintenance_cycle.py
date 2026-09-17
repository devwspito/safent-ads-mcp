"""`MaintenanceCycle` (tasks.md T078): tres pasos globales (`business_id`
`None` en el `StepResult`) mas un paso por negocio, dentro de un unico
`CycleReport` -- mismo criterio que `NotificationCycle`. Un fallo en un
paso global no impide que el resto (ni el paso por negocio) se ejecuten."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.orchestration.application.maintenance_cycle import MaintenanceCycle
from safent_ads.orchestration.domain.models import StepOutcome
from safent_ads.orchestration.testing.fakes import FakeBusinessListing
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_NOW = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


class _FakeMaintenanceStep:
    def __init__(self, *, fail_step: str | None = None) -> None:
        self.calls: list[str] = []
        self.reconciled_businesses: list[BusinessId] = []
        self._fail_step = fail_step

    async def expire_stale_proposals(self, now: datetime) -> None:
        del now
        self.calls.append("expire_stale_proposals")
        self._maybe_fail("expire_stale_proposals")

    async def purge_telegram_artifacts(self, now: datetime) -> None:
        del now
        self.calls.append("purge_telegram_artifacts")
        self._maybe_fail("purge_telegram_artifacts")

    async def verify_decision_log_chain(self, now: datetime) -> None:
        del now
        self.calls.append("verify_decision_log_chain")
        self._maybe_fail("verify_decision_log_chain")

    async def reconcile_platform_vs_crm(
        self, business_id: BusinessId, cycle_id: str, now: datetime
    ) -> None:
        del cycle_id, now
        self.calls.append("reconcile_platform_vs_crm")
        self.reconciled_businesses.append(business_id)
        self._maybe_fail("reconcile_platform_vs_crm")

    def _maybe_fail(self, step_name: str) -> None:
        if step_name == self._fail_step:
            raise RuntimeError(f"fallo simulado en {step_name}")


async def test_runs_the_three_global_steps_and_one_per_business_step() -> None:
    business_a = BusinessId.new()
    business_b = BusinessId.new()
    step = _FakeMaintenanceStep()
    cycle = MaintenanceCycle(
        businesses=FakeBusinessListing([business_a, business_b]),
        maintenance_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.cycle_name == "maintenance"
    assert report.all_succeeded
    assert step.calls.count("expire_stale_proposals") == 1
    assert step.calls.count("purge_telegram_artifacts") == 1
    assert step.calls.count("verify_decision_log_chain") == 1
    assert step.calls.count("reconcile_platform_vs_crm") == 2
    assert set(step.reconciled_businesses) == {business_a, business_b}


async def test_global_step_names_carry_no_business_id() -> None:
    step = _FakeMaintenanceStep()
    cycle = MaintenanceCycle(
        businesses=FakeBusinessListing([]),
        maintenance_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    global_steps = {
        "expire_stale_proposals",
        "purge_telegram_artifacts",
        "verify_decision_log_chain",
    }
    for result in report.results:
        if result.step_name in global_steps:
            assert result.business_id is None


async def test_a_failed_global_step_does_not_stop_the_rest() -> None:
    business_id = BusinessId.new()
    step = _FakeMaintenanceStep(fail_step="purge_telegram_artifacts")
    cycle = MaintenanceCycle(
        businesses=FakeBusinessListing([business_id]),
        maintenance_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.all_succeeded is False
    assert len(report.failed_results) == 1
    assert report.failed_results[0].step_name == "purge_telegram_artifacts"
    outcomes = {result.step_name: result.outcome for result in report.results}
    assert outcomes["expire_stale_proposals"] is StepOutcome.SUCCESS
    assert outcomes["verify_decision_log_chain"] is StepOutcome.SUCCESS
    assert outcomes["reconcile_platform_vs_crm"] is StepOutcome.SUCCESS


async def test_a_failed_per_business_step_does_not_block_global_steps() -> None:
    business_id = BusinessId.new()
    step = _FakeMaintenanceStep(fail_step="reconcile_platform_vs_crm")
    cycle = MaintenanceCycle(
        businesses=FakeBusinessListing([business_id]),
        maintenance_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.all_succeeded is False
    failed = report.failed_results[0]
    assert failed.step_name == "reconcile_platform_vs_crm"
    assert failed.business_id == str(business_id)


async def test_cycle_id_is_reused_across_all_steps() -> None:
    business_id = BusinessId.new()
    step = _FakeMaintenanceStep()
    cycle = MaintenanceCycle(
        businesses=FakeBusinessListing([business_id]),
        maintenance_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute(cycle_id="fixed-cycle-id")

    assert report.cycle_id == "fixed-cycle-id"
