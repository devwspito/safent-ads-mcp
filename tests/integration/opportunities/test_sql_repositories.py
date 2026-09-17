"""Adaptadores SQL de `opportunities` (tasks.md T113) contra Postgres real:
`SqlCalendarEventGapPort`, `SqlOfferingContributionPort` (reusa
`economics.GetUnitEconomics` real, sin dobles), `SqlDailyCandidateBudgetPort`
y `SqlCampaignProposalPort` (`accept`/`defer` sobre `proposals` real)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.application.build_unit_economics_profile import (
    BuildUnitEconomicsProfile,
)
from safent_ads.economics.application.ports import MarginInputs
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money as EconomicsMoney
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.infrastructure.sql_repositories import (
    SqlCalendarEventLookupPort,
    SqlOfferingPricePort,
    SqlUnitEconomicsProfileRepository,
)
from safent_ads.economics.testing.in_memory_repositories import InMemoryMarginInputsPort
from safent_ads.opportunities.application.errors import AmbiguousActiveAccountForPlatformError
from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlAccountDailyCapPort,
    SqlActiveAccountLookupPort,
    SqlCalendarEventGapPort,
    SqlCampaignProposalPort,
    SqlDailyCandidateBudgetPort,
    SqlOfferingContributionPort,
    SqlOfferingExistsPort,
    SqlOpenOpportunityPort,
)
from safent_ads.proposals.domain.money import Money as ProposalsMoney
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.conftest import OwnerFactory

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


async def _seed_business_with_account(session: AsyncSession, *, suffix: str) -> BusinessId:
    business_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
            "VALUES (:id, :slug, 'Negocio T113', 'Europe/Madrid', 'EUR')"
        ),
        {"id": business_id, "slug": f"t113-{suffix}"},
    )
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'google', :alias)"),
        {"id": credential_id, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
            "currency, timezone, api_tier, credential_ref_id, status) "
            "VALUES (:id, :business_id, 'google', :external_account_id, 'EUR', "
            "'Europe/Madrid', 'google_standard', :credential_ref_id, 'ACTIVE')"
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "external_account_id": f"act-{suffix}",
            "credential_ref_id": credential_id,
        },
    )
    await session.flush()
    return BusinessId(business_id)


async def _seed_offering_and_calendar_event(
    session: AsyncSession, *, business_id: BusinessId, suffix: str
) -> tuple[str, str]:
    offering_id = uuid.uuid4()
    calendar_event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title, price_amount, price_currency) "
            "VALUES (:id, :business_id, :code, 'Oferta T113', 1200, 'EUR')"
        ),
        {"id": offering_id, "business_id": business_id.value, "code": f"off-{suffix}"},
    )
    await session.execute(
        text(
            "INSERT INTO calendar_events (id, business_id, offering_id, name, window_start, "
            "window_end, source) "
            "VALUES (:id, :business_id, :offering_id, 'Hito T113', '2026-09-01', "
            "'2026-09-30', 'test')"
        ),
        {"id": calendar_event_id, "business_id": business_id.value, "offering_id": offering_id},
    )
    await session.flush()
    return str(offering_id), str(calendar_event_id)


async def _seed_unit_economics_profile(session: AsyncSession, *, business_id: BusinessId) -> str:
    """Perfil CONFIRMED real via `BuildUnitEconomicsProfile` -- mismo
    patron que `tests/integration/economics/test_fill_the_tables.py`."""
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        session, business_id=business_id, suffix=business_id.value.hex[:10]
    )
    product_id = ProductId.parse(offering_id)
    margin_inputs = InMemoryMarginInputsPort()
    margin_inputs.seed(
        business_id=business_id,
        product_id=product_id,
        margin_inputs=MarginInputs(
            vat_rate=Rate.zero(),
            delivery_cost=EconomicsMoney.of("90"),
            monthly_sales_team_cost=EconomicsMoney.of("140"),
            theta=Theta(Decimal("0.35")),
            margin_horizon_days=90,
        ),
    )
    use_case = BuildUnitEconomicsProfile(
        profiles=SqlUnitEconomicsProfileRepository(session),
        margin_inputs=margin_inputs,
        offering_prices=SqlOfferingPricePort(session),
        calendar_events=SqlCalendarEventLookupPort(session),
        lead_attributions=_EmptyLeadAttributions(),
        clock=FixedClock(_NOW),
    )
    await use_case.execute(business_id=business_id, product_id=product_id)
    return offering_id


class _EmptyLeadAttributions:
    """Sin conversiones reales: el perfil cae a `provisional_from_price_only`
    (profitability-engine.md §1), que igualmente da `target_cost_per_lead`
    positivo -- basta para probar el adaptador de `opportunities`."""

    async def find_for_calendar_events(self, *, business_id: object, calendar_event_ids: object):  # noqa: ANN001, ANN201, ARG002
        return []

    async def count_by_kind_in_window(self, **kwargs: object) -> int:  # noqa: ARG002
        return 0

    async def last_event_at(self, **kwargs: object):  # noqa: ANN201, ARG002
        return None


async def test_calendar_event_gap_port_lists_gaps_within_horizon(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="gap1")
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id, suffix="gap1"
    )

    gaps = await SqlCalendarEventGapPort(db_session).list_gaps(
        business_id=business_id, today=date(2026, 9, 10), horizon_days=30
    )

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.calendar_event_id == calendar_event_id
    assert gap.offering_id == offering_id
    assert gap.account_ref.platform is PlatformCode.GOOGLE
    assert gap.account_ref.level is EntityLevel.ACCOUNT


async def test_calendar_event_gap_port_excludes_events_outside_horizon(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="gap2")
    offering_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title, price_amount, price_currency) "
            "VALUES (:id, :business_id, 'off-gap2', 'Oferta lejana', 1200, 'EUR')"
        ),
        {"id": offering_id, "business_id": business_id.value},
    )
    await db_session.execute(
        text(
            "INSERT INTO calendar_events (id, business_id, offering_id, name, window_start, "
            "window_end, source) "
            "VALUES (gen_random_uuid(), :business_id, :offering_id, 'Hito lejano', "
            "'2027-01-01', '2027-02-01', 'test')"
        ),
        {"business_id": business_id.value, "offering_id": offering_id},
    )

    gaps = await SqlCalendarEventGapPort(db_session).list_gaps(
        business_id=business_id, today=date(2026, 9, 10), horizon_days=30
    )

    assert gaps == ()


async def test_offering_contribution_port_estimates_from_a_real_profile(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="econ1")
    offering_id = await _seed_unit_economics_profile(db_session, business_id=business_id)

    port = SqlOfferingContributionPort(db_session, FixedClock(_NOW))
    delta = await port.estimate_contribution_delta(
        business_id=business_id,
        offering_id=offering_id,
        daily_budget=ProposalsMoney.of("20.00", "EUR"),
        duration_days=7,
    )

    assert delta is not None
    assert delta.amount > 0
    assert delta.currency == "EUR"


async def test_offering_contribution_port_returns_none_without_a_profile(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="econ2")

    port = SqlOfferingContributionPort(db_session, FixedClock(_NOW))
    delta = await port.estimate_contribution_delta(
        business_id=business_id,
        offering_id=str(uuid.uuid4()),
        daily_budget=ProposalsMoney.of("20.00", "EUR"),
        duration_days=7,
    )

    assert delta is None


def _brief(offering_id: str, calendar_event_id: str) -> CampaignBrief:
    return CampaignBrief(
        objective="Cubrir demanda de prueba",
        platform=PlatformCode.GOOGLE,
        offering_id=offering_id,
        daily_budget=ProposalsMoney.of("20.00", "EUR"),
        duration_days=7,
        success_criterion="CPL bajo objetivo 3 dias seguidos",
        kill_criterion="Sin conversiones en 5 dias a 3x el CPL objetivo",
        angle="angulo de prueba",
        targeting_seed="semilla de prueba",
        calendar_event_id=calendar_event_id,
    )


async def test_accept_creates_a_pending_proposal_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="acc1")
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id, suffix="acc1"
    )
    account_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="act-acc1"
    )
    port = SqlCampaignProposalPort(db_session, clock=FixedClock(_NOW))
    brief = _brief(offering_id, calendar_event_id)

    first = await port.accept(
        business_id=business_id,
        account_ref=account_ref,
        candidate_key=f"calendar_event:{calendar_event_id}:{account_ref}",
        brief=brief,
        expected_contribution_delta=ProposalsMoney.of("50.00", "EUR"),
        cause_sentence="Hito abierto sin campana viva",
        now=_NOW,
    )
    second = await port.accept(
        business_id=business_id,
        account_ref=account_ref,
        candidate_key=f"calendar_event:{calendar_event_id}:{account_ref}",
        brief=brief,
        expected_contribution_delta=ProposalsMoney.of("60.00", "EUR"),
        cause_sentence="Hito abierto sin campana viva (actualizado)",
        now=_NOW,
    )

    assert first.proposal_id == second.proposal_id
    assert first.classification == "important"
    row = await db_session.execute(
        text(
            "SELECT state, expected_contribution_delta_amount, estimated_impact_amount "
            "FROM proposals WHERE id = :id"
        ),
        {"id": first.proposal_id},
    )
    state, expected_delta, estimated_impact = row.one()
    assert state == "pending"
    # `update_with_equivalent` no permite refrescar expected_contribution_delta
    # (limitacion conocida, ver docstring de SqlCampaignProposalPort): se
    # queda con el valor de la primera creacion, 50.00.
    assert expected_delta == Decimal("50.00")
    assert estimated_impact == brief.daily_budget.amount * brief.duration_days
    count = await SqlDailyCandidateBudgetPort(db_session).count_accepted_today(
        business_id=business_id, today=_NOW.date()
    )
    assert count == 1


async def test_defer_creates_a_postponed_proposal_once(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="def1")
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id, suffix="def1"
    )
    account_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="act-def1"
    )
    port = SqlCampaignProposalPort(db_session, clock=FixedClock(_NOW))
    brief = _brief(offering_id, calendar_event_id)
    candidate_key = f"calendar_event:{calendar_event_id}:{account_ref}"

    proposal_id = await port.defer(
        business_id=business_id,
        account_ref=account_ref,
        candidate_key=candidate_key,
        brief=brief,
        expected_contribution_delta=None,
        cause_sentence="Sin cupo de atencion hoy",
        now=_NOW,
        postpone_until=_NOW.replace(day=_NOW.day + 1),
    )
    assert proposal_id is not None
    row = await db_session.execute(
        text("SELECT state, postponed_reason FROM proposals WHERE id = :id"), {"id": proposal_id}
    )
    state, reason = row.one()
    assert state == "postponed"
    assert reason == "attention_budget"

    again = await port.defer(
        business_id=business_id,
        account_ref=account_ref,
        candidate_key=candidate_key,
        brief=brief,
        expected_contribution_delta=None,
        cause_sentence="Sin cupo de atencion hoy",
        now=_NOW,
        postpone_until=_NOW.replace(day=_NOW.day + 1),
    )
    assert again is None
    count = await SqlDailyCandidateBudgetPort(db_session).count_accepted_today(
        business_id=business_id, today=_NOW.date()
    )
    assert count == 1  # postpuesta cuenta como candidata ya surgida hoy


async def test_open_opportunity_port_lists_pending_and_postponed_only(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="open1")
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id, suffix="open1"
    )
    account_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="act-open1"
    )
    port = SqlCampaignProposalPort(db_session, clock=FixedClock(_NOW))
    brief = _brief(offering_id, calendar_event_id)
    await port.accept(
        business_id=business_id,
        account_ref=account_ref,
        candidate_key=f"calendar_event:{calendar_event_id}:{account_ref}",
        brief=brief,
        expected_contribution_delta=ProposalsMoney.of("75.00", "EUR"),
        cause_sentence="Hito abierto sin campana viva",
        now=_NOW,
    )

    opportunities = await SqlOpenOpportunityPort(db_session).list_open(business_id=business_id)

    assert len(opportunities) == 1
    view = opportunities[0]
    assert view.state == "pending"
    assert view.account_ref == account_ref
    assert view.brief.offering_id == offering_id
    assert view.brief.calendar_event_id == calendar_event_id
    assert view.expected_contribution_delta == ProposalsMoney.of("75.00", "EUR")


async def test_open_opportunity_port_excludes_other_businesses(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="open2")

    opportunities = await SqlOpenOpportunityPort(db_session).list_open(business_id=business_id)

    assert opportunities == ()


async def test_offering_exists_port_true_for_an_active_offering(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="off1")
    offering_id, _ = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id, suffix="off1"
    )

    assert await SqlOfferingExistsPort(db_session).exists(
        business_id=business_id, offering_id=offering_id
    )


async def test_offering_exists_port_false_for_an_unknown_offering(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="off2")

    assert not await SqlOfferingExistsPort(db_session).exists(
        business_id=business_id, offering_id=str(uuid.uuid4())
    )


async def test_offering_exists_port_false_for_a_malformed_id(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="off3")

    assert not await SqlOfferingExistsPort(db_session).exists(
        business_id=business_id, offering_id="not-a-uuid"
    )


async def test_active_account_lookup_finds_the_active_account(db_session: AsyncSession) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="acct1")

    account_ref = await SqlActiveAccountLookupPort(db_session).find_active_account(
        business_id=business_id, platform=PlatformCode.GOOGLE
    )

    assert account_ref is not None
    assert account_ref.platform is PlatformCode.GOOGLE
    assert account_ref.level is EntityLevel.ACCOUNT
    assert account_ref.external_id == "act-acct1"


async def test_active_account_lookup_returns_none_for_a_platform_without_accounts(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="acct2")

    account_ref = await SqlActiveAccountLookupPort(db_session).find_active_account(
        business_id=business_id, platform=PlatformCode.META
    )

    assert account_ref is None


async def test_active_account_lookup_rejects_ambiguity_instead_of_picking_the_first(
    db_session: AsyncSession,
) -> None:
    """HANDOFF-ADS02-2026-09-11 'Seleccion ambigua': dos cuentas ACTIVE del
    mismo negocio y plataforma (dos conexiones, o dos cuentas remotas bajo
    la misma conexion) no deben resolverse eligiendo la mas antigua en
    silencio -- se rechaza y exige seleccion explicita."""
    business_id = await _seed_business_with_account(db_session, suffix="acct3a")
    credential_id = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'google', :alias)"),
        {"id": credential_id, "alias": "alias-acct3b"},
    )
    await db_session.execute(
        text(
            "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
            "currency, timezone, api_tier, credential_ref_id, status) "
            "VALUES (:id, :business_id, 'google', :external_account_id, 'EUR', "
            "'Europe/Madrid', 'google_standard', :credential_ref_id, 'ACTIVE')"
        ),
        {
            "id": uuid.uuid4(),
            "business_id": business_id.value,
            "external_account_id": "act-acct3b",
            "credential_ref_id": credential_id,
        },
    )
    await db_session.flush()

    with pytest.raises(AmbiguousActiveAccountForPlatformError):
        await SqlActiveAccountLookupPort(db_session).find_active_account(
            business_id=business_id, platform=PlatformCode.GOOGLE
        )


async def test_explicit_connection_with_same_remote_account_and_revocation_has_no_fallback(
    db_session: AsyncSession,
) -> None:
    business = await _seed_business_with_account(db_session, suffix="selected")
    owner = await OwnerFactory(db_session).create()
    refs = []
    for _ in range(2):
        connection = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO platform_connections "
                "(id,business_id,owner_id,platform) VALUES(:id,:business,:owner,'google')"
            ),
            {"id": connection, "business": business.value, "owner": owner},
        )
        await db_session.execute(
            text(
                "INSERT INTO platform_accounts "
                "(id,business_id,platform,external_account_id,currency,timezone,api_tier,"
                "credential_ref_id,status,connection_id) SELECT :id,business_id,platform,"
                "external_account_id,currency,timezone,api_tier,"
                "credential_ref_id,status,:connection "
                "FROM platform_accounts WHERE business_id=:business AND connection_id IS NULL"
            ),
            {"id": uuid.uuid4(), "connection": connection, "business": business.value},
        )
        refs.append(
            EntityRef(
                PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "act-selected", business.value, connection
            )
        )
    port = SqlActiveAccountLookupPort(db_session)
    with pytest.raises(AmbiguousActiveAccountForPlatformError):
        await port.find_active_account(business_id=business, platform=PlatformCode.GOOGLE)
    for ref in refs:
        assert (
            await port.find_active_account(
                business_id=business, platform=PlatformCode.GOOGLE, account_ref=ref
            )
            == ref
        )
    assert (
        await port.find_active_account(
            business_id=BusinessId.new(), platform=PlatformCode.GOOGLE, account_ref=refs[0]
        )
        is None
    )
    assert (
        await port.find_active_account(
            business_id=business, platform=PlatformCode.META, account_ref=refs[0]
        )
        is None
    )
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE connection_id=:id"),
        {"id": refs[0].connection_id},
    )
    assert (
        await port.find_active_account(
            business_id=business, platform=PlatformCode.GOOGLE, account_ref=refs[0]
        )
        is None
    )
    assert (
        await port.find_active_account(
            business_id=business, platform=PlatformCode.GOOGLE, account_ref=refs[1]
        )
        == refs[1]
    )


async def test_account_daily_cap_returns_none_without_a_configured_guardrail(
    db_session: AsyncSession,
) -> None:
    account_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="act-cap1"
    )

    cap = await SqlAccountDailyCapPort(db_session).get_daily_cap(account_ref=account_ref)

    assert cap is None


async def test_account_daily_cap_reads_the_configured_platform_account_guardrail(
    db_session: AsyncSession,
) -> None:
    business_id = await _seed_business_with_account(db_session, suffix="cap2")
    account_row = await db_session.execute(
        text("SELECT id FROM platform_accounts WHERE business_id = :business_id"),
        {"business_id": business_id.value},
    )
    platform_account_id = account_row.scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO guardrails (scope, platform_account_id, currency, daily_cap_minor, "
            "max_step_pct, max_changes_per_entity_per_day) "
            "VALUES ('platform_account', :platform_account_id, 'EUR', 3000, 20, 3)"
        ),
        {"platform_account_id": platform_account_id},
    )

    account_ref = EntityRef(
        platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="act-cap2"
    )
    cap = await SqlAccountDailyCapPort(db_session).get_daily_cap(account_ref=account_ref)

    assert cap == ProposalsMoney.of("30.00", "EUR")
