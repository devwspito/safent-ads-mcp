"""`FakePanelReadPort` (T046/T110): dos negocios de muestra para que
`test_idor_sweep` tenga algo real que cruzar."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from safent_ads.panel.application.dto import (
    ActionTaken,
    AnomalyView,
    Badges,
    BrakeBadge,
    ConnectionLevel,
    ConnectionsBadge,
    CreativeBadges,
    EntityChild,
    EntityDetail,
    EntityHistoryEntry,
    EntityMetrics,
    LearningState,
    PacingView,
    PortfolioRow,
    PortfolioView,
    ProposalBadges,
    SignalBadges,
    SignalDetailView,
    SignalRef,
    SignalsPage,
    SignalView,
)
from safent_ads.panel.application.errors import PanelEntityNotFoundError
from safent_ads.shared.read_models.dto import (
    Caps,
    CapsSource,
    Freshness,
    Money,
    Pacing,
    SignalOutcome,
    SignalOutcomeStatus,
    SpendBreakdown,
)

BUSINESS_A = "11111111-1111-1111-1111-111111111111"
BUSINESS_B = "22222222-2222-2222-2222-222222222222"

_ENTITY_A = "google:campaign:1111111111"
_ENTITY_B = "google:campaign:2222222222"
_SIGNAL_A = "sig-a-1"
_SIGNAL_B = "sig-b-1"


def _owner_of_entity(entity_ref: str) -> str:
    return BUSINESS_A if entity_ref == _ENTITY_A else BUSINESS_B


def _owner_of_signal(signal_id: str) -> str:
    return BUSINESS_A if signal_id == _SIGNAL_A else BUSINESS_B


def _signal_view(business_id: str, entity_ref: str, signal_id: str) -> SignalView:
    return SignalView(
        signal_id=signal_id,
        business_id=business_id,
        entity_ref=entity_ref,
        entity_name="Búsqueda Marca",
        platform="google",
        kind="SELL",
        strength=78,
        cause="CPL 41 vs 28",
        money_at_stake=Money(Decimal("310")),
        data_window="7D",
        action_taken=ActionTaken.PROPOSAL,
        proposal_id="prop-1",
        emitted_at=datetime.now(UTC),
        outcome=SignalOutcome(
            status=SignalOutcomeStatus.IN_PROGRESS, days_remaining=6, evaluated_at=None
        ),
    )


class FakePanelReadPort:
    async def get_portfolio(self, business_id: str, *, window: str) -> PortfolioView:
        freshness = Freshness(datetime.now(UTC), 12, False)
        row = PortfolioRow(
            entity_ref=_ENTITY_A if business_id == BUSINESS_A else _ENTITY_B,
            name="Búsqueda Marca",
            platform="google",
            platform_account_id="google:account:acc-1",
            platform_account_uuid="00000000-0000-0000-0000-0000000000a1",
            status="ACTIVE",
            currency="EUR",
            budget=Money(Decimal("2000")),
            spend=Money(Decimal("1200.50")),
            cost_per_lead=Money(Decimal("30")),
            signal=SignalRef("SELL", 78, "CPL 41 vs 28"),
            money_at_stake=Money(Decimal("310")),
            is_controllable=True,
            learning_state=LearningState(is_learning=False, reason=None, since=None),
            spend_14d=[80.0] * 14,
            is_degraded=False,
        )
        return PortfolioView(
            window=window,
            currency="EUR",
            spend=SpendBreakdown(
                window=Money(Decimal("1200.50")),
                today=Money(Decimal("90")),
                mtd=Money(Decimal("3400")),
            ),
            caps=Caps(daily=Money(Decimal("150")), monthly=None, source=CapsSource.GUARDRAIL),
            pacing=Pacing(index_pct=1.1, projection_pct=78.0, days_remaining=9),
            conversions_by_kind={"lead": 40},
            cost_per_lead=Money(Decimal("30")),
            cost_per_business_conversion=Money(Decimal("200")),
            pending_proposals=3,
            deferred_proposals=1,
            freshness=freshness,
            deviation_vs_platform_pct=0.4,
            is_partial=False,
            degraded_accounts=[],
            rows=[row],
        )

    async def get_entity(self, entity_ref: str) -> EntityDetail:
        if entity_ref not in (_ENTITY_A, _ENTITY_B):
            raise PanelEntityNotFoundError(entity_ref)
        return EntityDetail(
            business_id=_owner_of_entity(entity_ref),
            entity_ref=entity_ref,
            name="Búsqueda Marca",
            status="active",
            platform_state_hash="h1",
            is_controllable=True,
            learning_state="learned",
            # `status="active"` + `is_controllable=True`
            # (`entity_capabilities`): pausable, no reanudable, borrable.
            can_pause=True,
            can_resume=False,
            can_delete=True,
        )

    async def get_entity_children(self, entity_ref: str) -> list[EntityChild]:
        if entity_ref not in (_ENTITY_A, _ENTITY_B):
            raise PanelEntityNotFoundError(entity_ref)
        return [EntityChild(f"{entity_ref}:child", "Anuncio 1", "ad", "ACTIVE")]

    async def get_entity_metrics(
        self, entity_ref: str, *, window: str, granularity: str
    ) -> EntityMetrics:
        del window
        if entity_ref not in (_ENTITY_A, _ENTITY_B):
            raise PanelEntityNotFoundError(entity_ref)
        return EntityMetrics(entity_ref, granularity, [])

    async def get_entity_history(self, entity_ref: str) -> list[EntityHistoryEntry]:
        if entity_ref not in (_ENTITY_A, _ENTITY_B):
            raise PanelEntityNotFoundError(entity_ref)
        return []

    async def get_freshness(self, business_id: str) -> list[Freshness]:
        del business_id
        return [Freshness(datetime.now(UTC), 12, False)]

    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: object | None = None,
        platform: str | None = None,
        entity_ref: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> SignalsPage:
        del kind, min_strength, since, platform, entity_ref, limit, cursor
        signal_id = _SIGNAL_A if business_id == BUSINESS_A else _SIGNAL_B
        entity_ref_value = _ENTITY_A if business_id == BUSINESS_A else _ENTITY_B
        items = [_signal_view(business_id, entity_ref_value, signal_id)]
        return SignalsPage(
            items=items, next_cursor=None, confirmed_rate_pct=None, confirmed_rate_sample=3
        )

    async def get_signal(self, signal_id: str) -> SignalDetailView:
        if signal_id not in (_SIGNAL_A, _SIGNAL_B):
            raise PanelEntityNotFoundError(signal_id)
        business_id = _owner_of_signal(signal_id)
        entity_ref = _ENTITY_A if business_id == BUSINESS_A else _ENTITY_B
        signal = _signal_view(business_id, entity_ref, signal_id)
        return SignalDetailView(signal, narrative="CPL por encima del objetivo.")

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalyView]:
        del since
        entity_ref = _ENTITY_A if business_id == BUSINESS_A else _ENTITY_B
        anomaly_id = f"anomaly-{business_id[:8]}"
        return [AnomalyView(anomaly_id, business_id, entity_ref, "ewma", 3.1, "high")]

    async def get_pacing(self, entity_ref: str) -> PacingView:
        if entity_ref not in (_ENTITY_A, _ENTITY_B):
            raise PanelEntityNotFoundError(entity_ref)
        return PacingView(
            entity_ref,
            _owner_of_entity(entity_ref),
            1.1,
            Money(Decimal("100")),
            Money(Decimal("20")),
            Money(Decimal("95")),
        )

    async def get_badges(self, business_id: str, *, signals_since: datetime | None) -> Badges:
        del business_id
        since = signals_since or datetime.now(UTC)
        return Badges(
            proposals=ProposalBadges(pending=3, critical=1, deferred=1),
            creatives=CreativeBadges(pending_approval=2),
            signals=SignalBadges(new_since=5, since=since),
            connections=ConnectionsBadge(level=ConnectionLevel.OK, reason=None),
            brake=BrakeBadge(engaged=False, mode=None),
        )
