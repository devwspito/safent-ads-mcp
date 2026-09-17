"""`SqlGuardrailSetRepository` contra Postgres real: la composicion de
ambitos (negocio -> cuenta -> campana) sale del dominio, no de SQL, y un
ambito que relaja los limites del que lo contiene no pasa (FR-13, C-17)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.guardrails import (
    GuardrailRelaxationError,
    GuardrailScope,
    ScopeKind,
)
from safent_ads.execution.infrastructure.errors import IncompleteGuardrailSetError
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import EntityLevel, EntityRef
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_INSERT_CHILD = """
    INSERT INTO ad_entities (business_id, platform_account_id, platform, level, external_id,
                             parent_id, name, status, platform_state_hash)
    SELECT parent.business_id, parent.platform_account_id, parent.platform, :level,
           :external_id, parent.id, 'Conjunto de prueba', 'ACTIVE', parent.platform_state_hash
      FROM ad_entities AS parent WHERE parent.entity_ref = :parent_ref
    RETURNING entity_ref
"""


async def given_campaign(session: AsyncSession) -> GuardrailScope:
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
    await seed_entity(session, entity_ref)
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref))


async def given_ad_set_under(session: AsyncSession, campaign: GuardrailScope) -> GuardrailScope:
    external_id = f"as-{uuid.uuid4().hex[:10]}"
    child_ref = await session.scalar(
        text(_INSERT_CHILD),
        {
            "level": EntityLevel.AD_SET.value,
            "external_id": external_id,
            "parent_ref": campaign.ref,
        },
    )
    return GuardrailScope(kind=ScopeKind.ENTITY, ref=str(EntityRef.parse(str(child_ref))))


async def test_the_most_specific_scope_wins_when_it_is_stricter(
    isolated_session: AsyncSession,
) -> None:
    scope = await given_campaign(isolated_session)
    await seed_guardrails(
        isolated_session, scope=scope, limits=GuardrailLimits(), level="business"
    )
    await seed_guardrails(
        isolated_session,
        scope=scope,
        limits=GuardrailLimits(daily_cap="300", ceiling="250"),
        level="platform_account",
    )
    await seed_guardrails(
        isolated_session,
        scope=scope,
        limits=GuardrailLimits(daily_cap="120", ceiling="200", max_step_pct=0.10),
        level="campaign",
    )

    effective = await SqlGuardrailSetRepository(isolated_session).get_effective(scope)

    assert effective.daily_cap == Money.of("120")
    assert effective.ceiling == Money.of("200")
    assert effective.max_step_pct == pytest.approx(0.10)
    assert effective.scope == scope


async def test_a_scope_that_relaxes_the_general_one_is_rejected(
    isolated_session: AsyncSession,
) -> None:
    """Un guardarraíl de campana mas ancho que el de su cuenta es un dato
    malo: no se recorta en silencio, se denuncia."""
    scope = await given_campaign(isolated_session)
    await seed_guardrails(
        isolated_session,
        scope=scope,
        limits=GuardrailLimits(daily_cap="300"),
        level="platform_account",
    )
    await seed_guardrails(
        isolated_session,
        scope=scope,
        limits=GuardrailLimits(daily_cap="900"),
        level="campaign",
    )

    with pytest.raises(GuardrailRelaxationError):
        await SqlGuardrailSetRepository(isolated_session).get_effective(scope)


async def test_an_ad_set_inherits_the_guardrails_of_its_campaign(
    isolated_session: AsyncSession,
) -> None:
    """Los limites de una campana alcanzan a sus conjuntos y anuncios: el
    ambito se resuelve subiendo por la jerarquia, no exigiendo una fila por
    entidad."""
    campaign = await given_campaign(isolated_session)
    await seed_guardrails(
        isolated_session,
        scope=campaign,
        limits=GuardrailLimits(daily_cap="150"),
        level="campaign",
    )
    ad_set = await given_ad_set_under(isolated_session, campaign)

    effective = await SqlGuardrailSetRepository(isolated_session).get_effective(ad_set)

    assert effective.daily_cap == Money.of("150")
    assert effective.scope == ad_set


async def test_a_guardrail_row_with_holes_is_rejected(isolated_session: AsyncSession) -> None:
    """Componer una politica a medias dejaria pasar cambios que nadie
    acoto: el adaptador prefiere fallar."""
    scope = await given_campaign(isolated_session)
    await seed_guardrails(
        isolated_session, scope=scope, limits=GuardrailLimits(), level="business"
    )
    await isolated_session.execute(
        text("UPDATE guardrails SET budget_ceiling_minor = NULL WHERE scope = 'business'")
    )

    with pytest.raises(IncompleteGuardrailSetError):
        await SqlGuardrailSetRepository(isolated_session).get_effective(scope)
