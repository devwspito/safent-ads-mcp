"""Journey 5: every MCP error from journeys 1/3/4's flows carries a
stable `code`, a plain-Spanish `message`, and never a traceback, a DSN or
provider-SDK vocabulary -- `mount.py`'s wrapper is the single place that
translates a `ToolDispatchError` to `{"error": {"code", "message"}}`
(`presentation/mount.py::_build_wrapper`), so one sweep over a handful of
representative failures covers every tool that raises through it.

`test_malformed_arguments_reach_the_clean_envelope` pins bug 3 (hotfix
0.2.20, now fixed): `mount.py::_gate_call_tool` intercepts the SDK's own
pydantic `ValidationError` (raised before our wrapper ever runs) and
translates it to `{"error": {"code": "INVALID_ARGUMENTS", "fields": [...]}}`
uniformly, the same shape `test_journey_campaign.py::test_invalid_creation_
plan_reports_exact_fields` (bug A, a nested model validator) already
reaches."""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text

from safent_ads.composition.container import Container
from safent_ads.mcp.application.caller_scope import Permission
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.e2e.journeys.conftest import _FixedScopeResolver, mcp_session, person_scope

pytestmark = pytest.mark.integration

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LEAK_MARKERS = (
    "Traceback",
    "pydantic.dev",
    "File \"",
    "sqlalchemy",
    "asyncpg",
    "postgresql://",
    "postgresql+asyncpg",
    " at 0x",
)


def _assert_clean_error(error: dict) -> None:
    assert _CODE_PATTERN.match(error["code"]), error
    message = error["message"]
    assert message and message.strip(), error
    for marker in _LEAK_MARKERS:
        assert marker not in message, (marker, error)


@pytest.fixture
async def seeded(container: Container):
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}-errsweep", platform_value="google")
    async with container.session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.commit()
    return business_id, entity_ref


async def test_unknown_entity_is_a_clean_error(container: Container, seeded) -> None:
    business_id, _entity_ref = seeded
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_pause",
            {
                "args": {
                    "business_id": str(business_id),
                    "entity_ref": str(campaign_ref("does-not-exist-999", platform_value="google")),
                    "cause": {"text": "Entidad que no existe"},
                    "evidence": [],
                    "urgency": "recommended",
                }
            },
        )
    error = reply.structured_content["error"]
    # `ContainerProposalWriteAdapter._require_entity` (mcp_write_adapter.py)
    # treats an unrecognised entity_ref as a request problem, not a lookup
    # miss: VALIDATION_ERROR here, ENTITY_NOT_FOUND on the read ports.
    assert error["code"] == "VALIDATION_ERROR"
    _assert_clean_error(error)


async def test_business_rule_violation_is_a_clean_error(container: Container, seeded) -> None:
    """The entity exists but has no daily budget (`seed_entity` never sets
    one): `propose_budget_change` rejects it as a domain rule, not a
    schema error."""
    business_id, entity_ref = seeded
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_budget_change",
            {
                "args": {
                    "business_id": str(business_id),
                    "entity_ref": str(entity_ref),
                    "new_daily_budget_amount": "90",
                    "new_daily_budget_currency": "EUR",
                    "cause": {"text": "Sin presupuesto diario propio"},
                    "evidence": [],
                    "urgency": "recommended",
                }
            },
        )
    error = reply.structured_content["error"]
    assert error["code"] == "VALIDATION_ERROR"
    _assert_clean_error(error)


async def test_malformed_arguments_reach_the_clean_envelope(
    container: Container, seeded
) -> None:
    """Bug 3 (hotfix 0.2.20, now fixed): a schema-level violation (here,
    `new_daily_budget_amount` failing its regex) used to never reach
    `mount.py`'s `{"error": {"code", "message"}}` envelope -- the MCP SDK
    validates `args_model` against the raw call BEFORE our wrapper runs and
    raised its own generic `ToolError`, leaking implementation detail
    (`pydantic.dev`, `input_value=...`). `mount.py::_gate_call_tool` now
    intercepts that `ToolError` uniformly (same path for every tool) and
    translates it to `INVALID_ARGUMENTS`, consistent with
    `test_journey_campaign.py::test_invalid_creation_plan_reports_exact_
    fields` (bug A, a nested model validator that already reached the
    clean envelope)."""
    business_id, entity_ref = seeded
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=business_id))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_budget_change",
            {
                "args": {
                    "business_id": str(business_id),
                    "entity_ref": str(entity_ref),
                    "new_daily_budget_amount": "not-a-number",
                    "new_daily_budget_currency": "EUR",
                    "cause": {"text": "Importe invalido"},
                    "evidence": [],
                    "urgency": "recommended",
                }
            },
        )
    assert reply.is_error is False
    error = reply.structured_content["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    _assert_clean_error(error)
    assert error["fields"] == [
        {
            "path": "new_daily_budget_amount",
            "message": "String should match pattern '^\\d+(\\.\\d{1,2})?$'",
        }
    ]
    assert "not-a-number" not in str(error)


async def test_cross_business_call_is_a_clean_error(container: Container, seeded) -> None:
    business_id, entity_ref = seeded
    other_business = uuid.uuid4()
    async with container.session_factory() as db_session:
        await db_session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Otro negocio', 'Europe/Madrid', 'EUR')"
            ),
            {"id": other_business, "slug": f"other-{other_business.hex[:10]}"},
        )
        await db_session.commit()
    resolver = _FixedScopeResolver(person_scope(Permission.PROPOSE, business_id=other_business))
    async with mcp_session(container, resolver) as session:
        reply = await session.call_tool(
            "propose_pause",
            {
                "args": {
                    "business_id": str(business_id),  # not in the caller's scope
                    "entity_ref": str(entity_ref),
                    "cause": {"text": "Negocio ajeno"},
                    "evidence": [],
                    "urgency": "recommended",
                }
            },
        )
    error = reply.structured_content["error"]
    assert error["code"] == "BUSINESS_FORBIDDEN"
    _assert_clean_error(error)
