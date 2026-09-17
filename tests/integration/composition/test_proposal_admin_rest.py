"""`PUT /proposals/{id}/owner-context`, `POST /proposals/{id}/postpone`,
`PATCH /proposals/{id}` (contracts/rest-api.md §Propuestas) de extremo a
extremo: `build_proposal_admin_router` real sobre `Container`, cookie de
sesion real (mismo patron que `test_execution_rest.py`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.integration.composition.conftest import AuthenticatedSession
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.proposals.domain.cause import Cause, CauseKey
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Priority, Urgency
from safent_ads.proposals.domain.proposal import Proposal, ProposedDiff, new_proposal_id
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.proposals.presentation.rest import build_proposal_admin_router
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Seeded:
    container: Container
    business_id: uuid.UUID
    entity_ref: EntityRef


@pytest.fixture
async def seeded(isolated_database_url: str) -> AsyncIterator[_Seeded]:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-admin", platform_value="google")
    async with container.session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.commit()
    try:
        yield _Seeded(container=container, business_id=business_id, entity_ref=entity_ref)
    finally:
        await container.aclose()


def _diff(entity_ref: EntityRef) -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=entity_ref,
        parameter="daily_budget",
        before=Money.of("100"),
        after=Money.of("70"),
    )


def _new_proposal(business_id: uuid.UUID, entity_ref: EntityRef) -> Proposal:
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId(business_id),
        diff=_diff(entity_ref),
        classification=Classification.ROUTINE,
        cause=Cause(text="CPL sobre objetivo en 7D", rule_id=None),
        cause_key=CauseKey(entity_ref=entity_ref, rule_id="agent", cause_type="test"),
        evidence=(),
        estimated_impact=Money.of("30"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        expected_state_hash="a" * 64,
    )


async def _save(container: Container, proposal: Proposal) -> None:
    async with container.session_factory() as session:
        await SqlProposalRepository(session).save(proposal)
        await session.commit()


async def _seed_pending(
    container: Container, business_id: uuid.UUID, entity_ref: EntityRef
) -> Proposal:
    proposal = _new_proposal(business_id, entity_ref)
    await _save(container, proposal)
    return proposal


def _app(container: Container) -> FastAPI:
    application = FastAPI()
    application.state.container = container
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(
        build_proposal_admin_router(container.session_factory, SystemClock())
    )
    return application


def _client(container: Container, cookies: dict[str, str]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


class TestOwnerContext:
    async def test_sets_the_text_and_it_is_readable_afterwards(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/proposals/{proposal.proposal_id}/owner-context",
                json={"text": "Esperar a que cierre el evento de calendario."},
            )

        assert response.status_code == 200
        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.owner_context == "Esperar a que cierre el evento de calendario."

    async def test_404_for_an_unknown_proposal(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/proposals/{uuid.uuid4()}/owner-context", json={"text": "nota"}
            )

        assert response.status_code == 404

    async def test_422_when_text_is_missing(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/proposals/{proposal.proposal_id}/owner-context", json={}
            )

        assert response.status_code == 422

    async def test_422_when_text_is_over_the_max_length(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/proposals/{proposal.proposal_id}/owner-context",
                json={"text": "a" * 501},
            )

        assert response.status_code == 422


class TestPostpone:
    async def test_postpones_a_pending_proposal(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/proposals/{proposal.proposal_id}/postpone", json={"until": until}
            )

        assert response.status_code == 200
        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.state.value == "postponed"
        assert reloaded.postponed_reason is not None

    async def test_404_for_an_unknown_proposal(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/proposals/{uuid.uuid4()}/postpone",
                json={"until": (_NOW + timedelta(hours=1)).isoformat()},
            )

        assert response.status_code == 404

    async def test_422_when_until_is_naive(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/proposals/{proposal.proposal_id}/postpone",
                json={"until": "2026-09-10T12:00:00"},
            )

        assert response.status_code == 422

    async def test_409_when_the_proposal_is_not_pending(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        proposal.approve(proposal.diff.diff_hash, _NOW)
        await _save(seeded.container, proposal)

        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/proposals/{proposal.proposal_id}/postpone",
                json={"until": (_NOW + timedelta(hours=1)).isoformat()},
            )

        assert response.status_code == 409


class TestPatchValue:
    async def test_recomputes_the_diff_hash_and_persists_the_new_value(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        original_hash = proposal.diff.diff_hash

        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}", json={"valor_propuesto": 55}
            )

        assert response.status_code == 200
        new_hash = response.json()["diff_hash"]
        assert new_hash != original_hash
        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.diff.diff_hash == new_hash
        assert reloaded.diff.after == Money.of("55.0")

    async def test_404_for_an_unknown_proposal(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{uuid.uuid4()}", json={"valor_propuesto": 55}
            )

        assert response.status_code == 404

    async def test_422_when_valor_propuesto_is_not_a_number(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}", json={"valor_propuesto": "55"}
            )

        assert response.status_code == 422

    @pytest.mark.parametrize("raw_literal", ["NaN", "Infinity", "-1"])
    async def test_422_for_non_finite_or_negative_valor_propuesto(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, raw_literal: str
    ) -> None:
        """security-review-f4.md item 5 (CWE-1287): `json.loads` acepta los
        literales `NaN`/`Infinity`/`-Infinity`, y un negativo tampoco es un
        `valor_propuesto` valido -- `_require_number` debe rechazar los
        cuatro con 422, nunca dejar que lleguen a `Money.of`/`edit_proposed_value`."""
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        body = f'{{"valor_propuesto": {raw_literal}}}'.encode()
        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}",
                content=body,
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.diff.diff_hash == proposal.diff.diff_hash  # sin escritura

    async def test_409_when_the_proposal_is_already_resolved(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        proposal = await _seed_pending(seeded.container, seeded.business_id, seeded.entity_ref)
        proposal.reject(_NOW)
        await _save(seeded.container, proposal)

        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}", json={"valor_propuesto": 55}
            )

        assert response.status_code == 409


_EU_POLITICAL_DECLARATION = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"


def _search_creation_plan(*, name: str = "Campana SEM") -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "google",
        "name": name,
        "status": "PAUSED",
        "daily_budget": {"amount": "20.00", "currency": "EUR"},
        "native": {
            "advertising_channel_type": "SEARCH",
            "bidding_strategy": "MANUAL_CPC",
            "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
            "network_settings": {
                "target_google_search": True,
                "target_search_network": False,
                "target_content_network": False,
                "target_partner_search_network": False,
            },
        },
    }


def _display_creation_plan() -> dict[str, object]:
    plan = _search_creation_plan(name="Campana Display")
    plan["native"] = {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": _EU_POLITICAL_DECLARATION,
    }
    return plan


def _account_ref_for(entity_ref: EntityRef) -> EntityRef:
    """`_edited_value`/`creation_budget` require an ACCOUNT-level
    `entity_ref` for a `new_campaign:` diff -- the same account `seed_entity`
    already created for the CAMPAIGN-level `entity_ref` (`ads_execution_
    targets`, 0037: a `platform_accounts` row surfaces as
    `<platform>:account:<external_account_id>` when `connection_id IS
    NULL`), so `SqlUnitOfWork.lock_account` finds it without seeding a
    second row."""
    return EntityRef.parse(
        f"{entity_ref.platform.value}:{EntityLevel.ACCOUNT.value}:"
        f"{account_external_id(entity_ref)}"
    )


def _new_campaign_proposal(
    business_id: uuid.UUID, account_ref: EntityRef, creation_plan: dict[str, object]
) -> Proposal:
    diff = ProposedDiff.build(
        entity_ref=account_ref,
        parameter="new_campaign:test",
        before=None,
        after={
            "creation_plan": creation_plan,
            "daily_budget_amount": creation_plan["daily_budget"]["amount"],  # type: ignore[index]
            "daily_budget_currency": creation_plan["daily_budget"]["currency"],  # type: ignore[index]
        },
    )
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=BusinessId(business_id),
        diff=diff,
        classification=Classification.ROUTINE,
        cause=Cause(text="Nueva campana de busqueda", rule_id=None),
        cause_key=CauseKey(entity_ref=account_ref, rule_id="agent", cause_type="test"),
        evidence=(),
        estimated_impact=Money.of("20"),
        priority=Priority(urgency=Urgency.RECOMMENDED),
        now=_NOW,
        expires_at=_NOW + timedelta(hours=24),
        expected_state_hash="a" * 64,
    )


class TestPatchValueChannelGate:
    """T035 security re-check (2026-09-15, CWE-284): `ADS_GOOGLE_CHANNELS_
    ENABLED` defaults to SEARCH-only -- before this fix, an owner could
    propose a SEARCH `new_campaign:` and then PATCH `creation_plan` to a
    channel this installation never enabled (`build_proposal_admin_router`
    below never overrides `enabled_google_channels`, same default as
    production)."""

    async def test_422_when_the_patched_plan_uses_a_disabled_channel(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        account_ref = _account_ref_for(seeded.entity_ref)
        proposal = _new_campaign_proposal(seeded.business_id, account_ref, _search_creation_plan())
        await _save(seeded.container, proposal)

        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}",
                json={
                    "diff_hash": proposal.diff.diff_hash,
                    "creation_plan": _display_creation_plan(),
                },
            )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "CHANNEL_TYPE_NOT_ENABLED"
        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.diff.diff_hash == proposal.diff.diff_hash  # sin escritura

    async def test_200_when_the_patched_plan_stays_on_search(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        account_ref = _account_ref_for(seeded.entity_ref)
        proposal = _new_campaign_proposal(seeded.business_id, account_ref, _search_creation_plan())
        await _save(seeded.container, proposal)
        original_hash = proposal.diff.diff_hash
        edited_plan = _search_creation_plan(name="Campana SEM (editada)")

        async with _client(seeded.container, authenticated_session.cookies) as client:
            response = await client.patch(
                f"/api/v1/proposals/{proposal.proposal_id}",
                json={"diff_hash": proposal.diff.diff_hash, "creation_plan": edited_plan},
            )

        assert response.status_code == 200, response.text
        assert response.json()["diff_hash"] != original_hash
        async with seeded.container.session_factory() as session:
            reloaded = await SqlProposalRepository(session).get(proposal.proposal_id)
        assert reloaded is not None
        assert reloaded.diff.diff_hash == response.json()["diff_hash"]


class TestIdorSweep:
    async def test_owner_context_404_for_a_proposal_of_another_business(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession
    ) -> None:
        other_entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-other", platform_value="google")
        async with seeded.container.session_factory() as session:
            other_business_id = await seed_entity(session, other_entity_ref)
            await session.commit()
        other_proposal = await _seed_pending(seeded.container, other_business_id, other_entity_ref)

        app = _app(seeded.container)

        async def _restricted() -> AuthenticatedCaller:
            return AuthenticatedCaller(allowed_business_ids=frozenset({str(seeded.business_id)}))

        app.dependency_overrides[get_authenticated_caller] = _restricted
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=authenticated_session.cookies
        ) as client:
            response = await client.put(
                f"/api/v1/proposals/{other_proposal.proposal_id}/owner-context",
                json={"text": "no deberia poder"},
            )

        assert response.status_code == 404
