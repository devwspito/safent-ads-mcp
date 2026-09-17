"""Regression for hotfix 0.2.20 Bug A: a draft with `creation_plan: null` but
every structured field filled in (verbatim example drafts, Meta rev 10 and
Google rev 6) must still promote to a pending proposal instead of failing
with an opaque `CAMPAIGN_DRAFT_INCOMPLETE`/`campaign_creation_plan_invalid`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.mcp.presentation.campaign_draft_tools import (
    DraftSaveArgs,
    build_draft_tools,
)
from safent_ads.opportunities.application.propose_campaign import (
    ProposeCampaign,
    ProposeCampaignRequest,
)
from safent_ads.opportunities.domain.campaign_draft import (
    DraftFields,
    completed_brief,
    default_creation_plan,
)
from safent_ads.opportunities.testing.in_memory_repositories import (
    InMemoryAccountDailyCapPort,
    InMemoryActiveAccountLookupPort,
    InMemoryCampaignProposalPort,
    InMemoryOfferingExistsPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

# Verbatim `brief` payloads for draft_key=acme-meta-primera-consulta-2026-09
# (revision 10) and draft_key=acme-google-consulta-2026-09 (revision 6),
# straight from the owner's Mac: `creation_plan` is null, the whole native
# configuration lives as confirmed prose in `notes`.
META_DRAFT_BRIEF = {
    "geo": "Radio de 3 km desde C/ López de Hoyos, 295, Madrid.",
    "angle": (
        "Primera consulta veterinaria gratis para nuevos clientes, una por mascota; "
        "no incluye pruebas, vacunas, tratamientos ni medicación."
    ),
    "notes": (
        "Facebook e Instagram. Página de Facebook correcta confirmada: "
        "1293244263875457. Configuración confirmada: Facebook e Instagram, "
        "tráfico/clics en enlace, sin categoría especial; estado PAUSED. "
        "No publicado ni ejecutado."
    ),
    "title": "Acme | Primera consulta gratis | Centro Norte | Meta",
    "platform": "meta",
    "headlines": ["Primera consulta gratis", "Tu primera consulta, gratis"],
    "image_url": None,
    "objective": "Conseguir reservas de primera consulta veterinaria en Acme Center.",
    "account_ref": (
        "meta:account:0609e9cf-e861-4c9b-94cf-4611e527fc69:"
        "4b175ccd-4359-4a86-b0b1-55eb1a922c28:act_100000000000002"
    ),
    "landing_url": "https://example.com/",
    "offering_id": "5dd8aeea-dbcd-4deb-855b-73b0d11c9cd0",
    "daily_budget": {"amount": "10", "currency": "EUR"},
    "descriptions": ["Acme Center · Centro Norte", "Para tu perro o tu gato"],
    "meta_page_id": "1293244263875457",
    "primary_text": (
        "La primera consulta veterinaria de tu perro o gato, gratis en Acme "
        "Center. Reserva tu consulta."
    ),
    "creation_plan": None,
    "duration_days": 30,
    "kill_criterion": (
        "Pausar si la medición de reserva web completada no está operativa; "
        "finalizar al cumplir 30 días. Cualquier cambio de gasto requiere aprobación."
    ),
    "targeting_seed": (
        "Personas con perro o gato dentro de un radio de 3 km desde Acme "
        "Center; Facebook e Instagram."
    ),
    "success_criterion": "Reservas web completadas en https://example.com/.",
}

GOOGLE_DRAFT_BRIEF = {
    "geo": "Radio de 3 km desde C/ López de Hoyos, 295, Madrid.",
    "angle": (
        "Primera consulta veterinaria gratis para nuevos clientes, una por mascota; "
        "no incluye pruebas, vacunas, tratamientos ni medicación."
    ),
    "notes": (
        "Solo Google Search. Configuración confirmada por el propietario: "
        "España, solo red de Búsqueda, CPC manual, sin categoría regulada; "
        "estado PAUSED. No publicado ni ejecutado."
    ),
    "title": "Acme | Consulta veterinaria · Primera consulta gratis | Centro Norte | Search",
    "platform": "google",
    "headlines": [
        "Consulta veterinaria",
        "Veterinario en Centro Norte",
        "Acme Center",
        "En López de Hoyos, 295",
    ],
    "image_url": None,
    "objective": "Conseguir reservas de primera consulta veterinaria en Acme Center.",
    "account_ref": (
        "google:account:0609e9cf-e861-4c9b-94cf-4611e527fc69:"
        "356dd45b-43fd-442d-8005-0990a235957e:1000000001"
    ),
    "landing_url": "https://example.com/",
    "offering_id": "5dd8aeea-dbcd-4deb-855b-73b0d11c9cd0",
    "daily_budget": {"amount": "10", "currency": "EUR"},
    "descriptions": [
        "Reserva tu primera consulta veterinaria gratis en Acme Center, Centro Norte.",
    ],
    "meta_page_id": None,
    "primary_text": None,
    "creation_plan": None,
    "duration_days": 30,
    "kill_criterion": (
        "Pausar si la medición de reserva web completada no está operativa; "
        "finalizar al cumplir 30 días. Cualquier cambio de gasto requiere aprobación."
    ),
    "targeting_seed": (
        "Búsquedas locales de personas con perro o gato dentro de un radio de 3 km "
        "desde Acme Center."
    ),
    "success_criterion": "Reservas web completadas en https://example.com/.",
}

_NOW = datetime(2026, 9, 14, tzinfo=UTC)


async def _propose_from_draft(brief_payload: dict) -> None:
    fields = DraftFields.model_validate(brief_payload)
    brief = completed_brief(fields)
    assert brief.creation_plan is not None

    business_id = BusinessId.parse(str(EntityRef.parse(fields.account_ref).business_id))
    account_ref = EntityRef.parse(fields.account_ref)

    offerings = InMemoryOfferingExistsPort(existing_offering_ids=frozenset({fields.offering_id}))
    accounts = InMemoryActiveAccountLookupPort()
    accounts.seed(
        business_id=business_id, platform=PlatformCode(fields.platform), account_ref=account_ref
    )
    daily_caps = InMemoryAccountDailyCapPort()
    campaign_proposals = InMemoryCampaignProposalPort()
    use_case = ProposeCampaign(
        offerings=offerings,
        accounts=accounts,
        daily_caps=daily_caps,
        campaign_proposals=campaign_proposals,
        clock=FixedClock(_NOW),
    )

    outcome = await use_case.execute(
        ProposeCampaignRequest(business_id=business_id, brief=brief, account_ref=account_ref)
    )

    assert outcome.estado in {"pending", "postponed"}
    assert outcome.proposal_id
    assert len(campaign_proposals.accepted) == 1


async def test_meta_draft_with_null_creation_plan_still_becomes_a_proposal() -> None:
    await _propose_from_draft(META_DRAFT_BRIEF)


async def test_google_draft_with_null_creation_plan_still_becomes_a_proposal() -> None:
    await _propose_from_draft(GOOGLE_DRAFT_BRIEF)


def test_default_plan_omits_geographic_targeting_when_geo_is_not_a_known_country() -> None:
    fields = DraftFields.model_validate(GOOGLE_DRAFT_BRIEF)
    plan = default_creation_plan(fields)
    assert "geographic_targeting" not in plan["native"]


def test_default_plan_resolves_spain_from_geo_text() -> None:
    fields = DraftFields.model_validate({**GOOGLE_DRAFT_BRIEF, "geo": "España, radio 3 km"})
    plan = default_creation_plan(fields)
    assert plan["native"]["geographic_targeting"] == {
        "geo_target_constants": ["geoTargetConstants/2724"],
        "positive_geo_target_type": "PRESENCE",
    }


def test_tool_descriptions_state_creation_plan_is_optional_and_derived() -> None:
    class _Store:
        pass

    tools = {tool.name: tool for tool in build_draft_tools(_Store())}  # type: ignore[arg-type]
    assert "OPCIONAL" in tools["propose_campaign_draft"].description
    assert "OPCIONAL" in tools["propose_campaign_from_draft"].description


def test_explicit_invalid_creation_plan_reports_pydantic_errors_and_an_example() -> None:
    payload = {
        "business_id": "11111111-1111-1111-1111-111111111111",
        "draft_key": "k",
        "changes": {
            "title": "t",
            "creation_plan": {
                "schema_version": 1,
                "platform": "meta",
                "name": "x",
                "status": "PAUSED",
                "daily_budget": {"amount": "20.00", "currency": "EUR"},
                "native": {
                    "objective": "NOT_A_REAL_OBJECTIVE",
                    "buying_type": "AUCTION",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "special_ad_categories": [],
                    "special_ad_category_country": [],
                },
            },
        },
    }
    with pytest.raises(ValidationError, match="ejemplo válido") as error:
        DraftSaveArgs.model_validate(payload)
    assert "CAMPAIGN_DRAFT_PLAN_INVALID" in str(error.value)
