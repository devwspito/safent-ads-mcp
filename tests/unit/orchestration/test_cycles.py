"""`IngestionCycle`/`SignalCycle`/`NotificationCycle` (T047): traza,
reintento y "nunca exito parcial silencioso"."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.notifications.application.publish_digest import PublishDigest
from safent_ads.notifications.application.publish_ticker import PublishTicker
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.testing.fakes import (
    FakeMessenger,
    FakeNotificationOutbox,
    FakePendingDigest,
    FakeSignalsForTicker,
)
from safent_ads.orchestration.application.credential_health_cycle import CredentialHealthCycle
from safent_ads.orchestration.application.execution_cycle import ExecutionCycle
from safent_ads.orchestration.application.ingestion_cycle import IngestionCycle
from safent_ads.orchestration.application.notification_cycle import NotificationCycle
from safent_ads.orchestration.application.opportunity_cycle import OpportunityCycle
from safent_ads.orchestration.application.ports import NotificationTarget
from safent_ads.orchestration.application.retry import run_with_retry
from safent_ads.orchestration.application.signal_cycle import SignalCycle
from safent_ads.orchestration.domain.models import StepOutcome
from safent_ads.orchestration.testing.fakes import (
    FakeBusinessListing,
    FakeNotificationTargets,
    FakeStep,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_run_with_retry_returns_attempts_on_first_success() -> None:
    calls = []

    async def operation() -> None:
        calls.append(1)

    attempts = await run_with_retry(operation, sleep=_no_sleep)

    assert attempts == 1
    assert len(calls) == 1


async def test_run_with_retry_retries_then_succeeds() -> None:
    calls = []

    async def operation() -> None:
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("fallo transitorio")

    attempts = await run_with_retry(operation, sleep=_no_sleep)

    assert attempts == 2


async def test_run_with_retry_raises_after_exhausting_attempts() -> None:
    async def operation() -> None:
        raise RuntimeError("fallo persistente")

    try:
        await run_with_retry(operation, max_attempts=3, sleep=_no_sleep)
    except RuntimeError as exc:
        assert str(exc) == "fallo persistente"
    else:
        raise AssertionError("se esperaba RuntimeError")


async def test_ingestion_cycle_reports_success_per_business() -> None:
    business_a = BusinessId.new()
    business_b = BusinessId.new()
    step = FakeStep()
    cycle = IngestionCycle(
        businesses=FakeBusinessListing([business_a, business_b]),
        ingestion_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = cycle.execute()
    report = await report

    assert report.cycle_name == "ingestion"
    assert report.all_succeeded
    assert len(report.results) == 2
    assert len(step.calls) == 2


async def test_ingestion_cycle_never_hides_a_partial_failure() -> None:
    """plan.md §7: "nunca exito parcial silencioso" — un negocio que falla
    no impide que los demas se procesen, y el reporte lo deja explicito."""
    ok_business = BusinessId.new()
    failing_business = BusinessId.new()
    step = FakeStep(fail_for=frozenset({failing_business}))
    cycle = IngestionCycle(
        businesses=FakeBusinessListing([ok_business, failing_business]),
        ingestion_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.all_succeeded is False
    assert len(report.failed_results) == 1
    assert report.failed_results[0].business_id == str(failing_business)
    outcomes = {result.business_id: result.outcome for result in report.results}
    assert outcomes[str(ok_business)] is StepOutcome.SUCCESS


async def test_signal_cycle_uses_its_own_step_port() -> None:
    business_id = BusinessId.new()
    step = FakeStep()
    cycle = SignalCycle(
        businesses=FakeBusinessListing([business_id]),
        signal_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.cycle_name == "signals"
    assert report.all_succeeded
    assert step.calls[0][0] == business_id


async def test_opportunity_cycle_uses_its_own_step_port() -> None:
    business_id = BusinessId.new()
    step = FakeStep()
    cycle = OpportunityCycle(
        businesses=FakeBusinessListing([business_id]),
        opportunity_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.cycle_name == "opportunities"
    assert report.all_succeeded
    assert step.calls[0][0] == business_id


async def test_credential_health_cycle_uses_its_own_step_port() -> None:
    business_id = BusinessId.new()
    step = FakeStep()
    cycle = CredentialHealthCycle(
        businesses=FakeBusinessListing([business_id]),
        credential_health_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute()

    assert report.cycle_name == "credential_health"
    assert report.all_succeeded
    assert step.calls[0][0] == business_id


async def test_notification_cycle_runs_digest_then_ticker_per_target() -> None:
    business_id = BusinessId.new()
    target = NotificationTarget(business_id, "Negocio Ejemplo", (111,))
    outbox = FakeNotificationOutbox()
    pending_digest = FakePendingDigest()
    messenger = FakeMessenger()
    id_generator = UuidIdGenerator()
    active_hours = ActiveHoursWindow.parse("08:00-21:00", tz_name="Europe/Madrid")

    publish_digest = PublishDigest(
        pending_digest=pending_digest, outbox=outbox, messenger=messenger, id_generator=id_generator
    )
    publish_ticker = PublishTicker(
        signals=FakeSignalsForTicker(),
        outbox=outbox,
        pending_digest=pending_digest,
        messenger=messenger,
        id_generator=id_generator,
        active_hours=active_hours,
    )
    cycle = NotificationCycle(
        targets=FakeNotificationTargets([target]),
        publish_digest=publish_digest,
        publish_ticker=publish_ticker,
        clock=FixedClock(_NOW),
        id_generator=id_generator,
    )

    report = await cycle.execute()

    assert report.cycle_name == "notification"
    assert report.all_succeeded
    step_names = {result.step_name for result in report.results}
    assert step_names == {"publish_digest", "publish_ticker"}


async def test_cycle_id_is_reused_across_all_steps() -> None:
    business_a = BusinessId.new()
    business_b = BusinessId.new()
    step = FakeStep()
    cycle = IngestionCycle(
        businesses=FakeBusinessListing([business_a, business_b]),
        ingestion_step=step,
        clock=FixedClock(_NOW),
        id_generator=UuidIdGenerator(),
    )

    report = await cycle.execute(cycle_id="fixed-cycle-id")

    assert report.cycle_id == "fixed-cycle-id"
    assert {call[1] for call in step.calls} == {"fixed-cycle-id"}


class TestExecutionCycle:
    async def test_drains_the_queue_one_attempt_per_run_once_call(self) -> None:
        outcomes = iter(
            [ExecutionStatus.EXECUTED, ExecutionStatus.BLOCKED_GUARDRAIL, None]
        )

        async def run_once() -> ExecutionStatus | None:
            return next(outcomes)

        cycle = ExecutionCycle(
            run_once=run_once, clock=FixedClock(_NOW), id_generator=UuidIdGenerator()
        )

        report = await cycle.execute()

        assert report.cycle_name == "execution"
        assert len(report.results) == 2
        assert report.all_succeeded

    async def test_an_empty_queue_produces_no_results(self) -> None:
        async def run_once() -> ExecutionStatus | None:
            return None

        cycle = ExecutionCycle(
            run_once=run_once, clock=FixedClock(_NOW), id_generator=UuidIdGenerator()
        )

        report = await cycle.execute()

        assert report.results == ()
        assert report.all_succeeded

    async def test_an_unhandled_exception_is_a_failed_step_but_the_cycle_continues(
        self,
    ) -> None:
        outcomes: list[ExecutionStatus | Exception | None] = [
            RuntimeError("fallo tecnico inesperado"),
            ExecutionStatus.EXECUTED,
            None,
        ]

        async def run_once() -> ExecutionStatus | None:
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        cycle = ExecutionCycle(
            run_once=run_once, clock=FixedClock(_NOW), id_generator=UuidIdGenerator()
        )

        report = await cycle.execute()

        assert len(report.results) == 2
        assert len(report.failed_results) == 1

    async def test_a_max_attempts_cap_stops_an_endless_queue(self) -> None:
        async def run_once() -> ExecutionStatus | None:
            return ExecutionStatus.EXECUTED

        cycle = ExecutionCycle(
            run_once=run_once,
            clock=FixedClock(_NOW),
            id_generator=UuidIdGenerator(),
            max_attempts_per_cycle=3,
        )

        report = await cycle.execute()

        assert len(report.results) == 3
