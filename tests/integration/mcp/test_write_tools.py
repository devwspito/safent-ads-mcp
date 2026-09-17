"""Los tools de escritura de US2/US3 a traves del `ToolDispatcher` real:
args pydantic -> `ContainerProposalWriteAdapter` -> `Container.
build_execution_use_cases` -> Postgres. Prueba la costura completa, no solo
el caso de uso aislado (ya cubierto en `tests/unit/execution/
test_apply_defensive_action.py`).

`ContainerProposalWriteAdapter` abre su PROPIA sesion por llamada
(`container.session_factory()`), asi que el montaje de este test no puede
usar `rolled_back_session`: una sesion aparte no veria filas sin confirmar
de otra. Confirma de verdad sobre `isolated_database_url` y limpia en un
`finally` -- la fila de `rules` (M05) es GLOBAL y compartida con el resto
del banco de `tests/contracts/execution/conftest.py`, asi que dejarla
calibrada rompería a otros tests de la misma base."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from tests.contracts.execution.conftest import (
    FIRING_RULE_CODE,
    NOW,
    GuardrailLimits,
    calibrate_rule,
    seed_freshness,
    seed_guardrails,
)
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.container import Container
from safent_ads.composition.mcp_write_adapter import ContainerProposalWriteAdapter
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.errors import RuleNotApplicableError, ToolDispatchError
from safent_ads.mcp.application.proposal_write_port import ProposalWriteResult
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.presentation.catalog import build_default_registry
from safent_ads.mcp.presentation.dispatcher import ToolDispatcher
from safent_ads.mcp.presentation.native_write_tools import build_native_write_tool_definitions
from safent_ads.mcp.presentation.optimization_write_tools import (
    build_optimization_write_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

def _caller(business_id: object) -> CallerScope:
    return CallerScope(
        caller_id="test-agent",
        allowed_business_ids=frozenset({str(business_id)}),
        permission=Permission.PROPOSE,
        person_label="Agente de prueba",
    )

_RESET_RULE = text(
    "UPDATE rules SET is_enabled = false, condition = '{}'::jsonb "
    "WHERE code = :code AND scope = 'global'"
)

# `seed_entity` (tests/contracts/sql_fixtures.py) no fija presupuesto --
# suficiente para los bancos de `execution` que no lo necesitan, pero
# `apply_defensive_action` (`lower_budget`) si lo lee.
_SET_BUDGET = text(
    "UPDATE ad_entities SET budget_amount_minor = 10000, budget_currency = 'EUR', "
    "budget_kind = 'daily' WHERE entity_ref = :entity_ref"
)


class _UnusedReadPort:
    """Ningun tool de lectura entra en esta prueba; `build_default_registry`
    igualmente exige los 12 puertos -- este falla fuerte si algo los toca
    sin querer, en vez de devolver datos inventados."""

    def __getattr__(self, _name: str) -> object:
        raise AssertionError("puerto de lectura no usado en este test de escritura")


def _minimal_read_model_ports() -> ReadModelPorts:
    unused = _UnusedReadPort()
    return ReadModelPorts(
        business_directory=unused,  # type: ignore[arg-type]
        portfolio=unused,  # type: ignore[arg-type]
        entity=unused,  # type: ignore[arg-type]
        gaql=unused,  # type: ignore[arg-type]
        signal=unused,  # type: ignore[arg-type]
        rule=unused,  # type: ignore[arg-type]
        proposal=unused,  # type: ignore[arg-type]
        catalog=unused,  # type: ignore[arg-type]
        audit=unused,  # type: ignore[arg-type]
        creative=unused,  # type: ignore[arg-type]
        brand=unused,  # type: ignore[arg-type]
        capability=unused,  # type: ignore[arg-type]
    )


async def test_apply_defensive_action_raises_typed_error_at_the_dispatcher(
    isolated_database_url: str,
) -> None:
    """La condicion nunca dispara (sin senal sembrada): el mismo camino que
    recorreria un agente real, hasta el codigo tipado que ve el borde MCP
    (`RULE_NOT_APPLICABLE`, contracts/mcp-tools.md)."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/mcp-write.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    entity_ref = campaign_ref(f"c-{uuid.uuid4().hex[:10]}")
    business_id = None
    try:
        async with container.session_factory() as session:
            business_id = await seed_entity(session, entity_ref)
            await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref)})
            await seed_freshness(session, entity_ref, lag_minutes=5)
            await calibrate_rule(session, enabled=True)
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                limits=GuardrailLimits(),
                level="business",
            )
            await session.commit()

        registry = build_default_registry(
            _minimal_read_model_ports(),
            container.clock,
            ContainerProposalWriteAdapter(container),
        )
        dispatcher = ToolDispatcher(
            registry=registry, quota=InMemoryQuota(clock=container.clock)
        )

        with pytest.raises(ToolDispatchError) as excinfo:
            await dispatcher.dispatch(
                "apply_defensive_action",
                {
                    "business_id": str(business_id),
                    "entity_ref": str(entity_ref),
                    "rule_id": FIRING_RULE_CODE,
                    "action": "lower_budget",
                    "cause": {
                        "text": "ROAS por debajo del objetivo en 7D",
                        "rule_id": FIRING_RULE_CODE,
                    },
                    "magnitude_pct": 0.30,
                },
                caller_scope=_caller(business_id),
            )

        assert isinstance(excinfo.value, RuleNotApplicableError)
        assert excinfo.value.code == "RULE_NOT_APPLICABLE"
    finally:
        await _cleanup(container, business_id)
        await container.aclose()


class _RecordingProposalWritePort:
    """004 tasks-2.md W6: doble en memoria de `ProposalWritePort` que solo
    registra `proposed_by` -- a diferencia del resto de este fichero, no
    hace falta Postgres ni `ContainerProposalWriteAdapter` para probar que
    el handler de cada herramienta nueva reenvia `proposed_by`: eso es
    responsabilidad de `write_handlers.py`/`native_write_tools.py`, no del
    camino de persistencia (ya cubierto por `tests/unit/proposals/
    test_propose_action.py`)."""

    def __init__(self) -> None:
        self.proposed_by_by_tool: dict[str, str | None] = {}

    async def propose_resume(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_resume", kwargs)

    async def propose_bid_target(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_bid_target", kwargs)

    async def propose_negative_keywords(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_negative_keywords", kwargs)

    async def propose_creative_rotation(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_creative_rotation", kwargs)

    async def propose_delete(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_delete", kwargs)

    async def propose_native_write(self, **kwargs: object) -> ProposalWriteResult:
        return self._record("propose_native_write", kwargs)

    def _record(self, name: str, kwargs: dict[str, object]) -> ProposalWriteResult:
        self.proposed_by_by_tool[name] = kwargs.get("proposed_by")  # type: ignore[assignment]
        return ProposalWriteResult(
            proposal_id=str(uuid.uuid4()),
            estado="pendiente",
            diff_hash="a" * 64,
            expires_at=datetime(2026, 9, 15, tzinfo=UTC),
            classification="important",
        )


_NATIVE_WRITE_WHY = "El equipo de marca pidio ampliar la ventana de conversion a 7 dias."

_NEW_PROPOSAL_ARGS: tuple[tuple[str, dict[str, object]], ...] = (
    ("propose_resume", {"cause": {"text": "Estacionalidad favorable"}}),
    (
        "propose_bid_target",
        {
            "bid_target_amount": "1.25",
            "bid_target_currency": "EUR",
            "cause": {"text": "CPA por debajo del objetivo"},
        },
    ),
    (
        "propose_negative_keywords",
        {"keywords": ["gratis"], "cause": {"text": "Trafico no cualificado"}},
    ),
    ("propose_creative_rotation", {"cause": {"text": "Fatiga de creatividad detectada"}}),
    ("propose_delete", {"cause": {"text": "Campana obsoleta"}}),
    (
        "propose_native_write",
        {
            "platform": "meta",
            "operation": "update_targeting",
            "payload": {"headline": "Nuevo anuncio"},
            "why": _NATIVE_WRITE_WHY,
        },
    ),
)


async def test_todas_las_herramientas_proposal_guardan_proposed_by() -> None:
    """004 tasks-2.md W6: las 6 propuestas nuevas pasan `proposed_by` como
    las 5 de A8 -- recorre el registro y falla si alguna clase `PROPOSAL`
    deja `proposed_by` a `NULL` con un `CallerScope` de persona."""
    port = _RecordingProposalWritePort()
    registry = ToolRegistry(
        [
            *build_optimization_write_tool_definitions(port),  # type: ignore[arg-type]
            *build_native_write_tool_definitions(port),  # type: ignore[arg-type]
        ]
    )
    dispatcher = ToolDispatcher(registry=registry, quota=InMemoryQuota(clock=FixedClock(NOW)))
    business_id = str(uuid.uuid4())
    caller_scope = CallerScope(
        caller_id=f"person:{uuid.uuid4()}",
        allowed_business_ids=frozenset({business_id}),
        permission=Permission.PROPOSE,
        person_label="Agente de prueba",
    )
    entity_ref = f"google:campaign:{business_id}:{uuid.uuid4()}:456"

    for tool_name, extra_args in _NEW_PROPOSAL_ARGS:
        await dispatcher.dispatch(
            tool_name,
            {"business_id": business_id, "entity_ref": entity_ref, **extra_args},
            caller_scope=caller_scope,
        )

    for tool_name, _ in _NEW_PROPOSAL_ARGS:
        assert port.proposed_by_by_tool[tool_name] == caller_scope.caller_id, tool_name


_CLEANUP_STATEMENTS = (
    "DELETE FROM guardrails WHERE business_id = :id "
    "OR platform_account_id IN (SELECT id FROM platform_accounts WHERE business_id = :id)",
    "DELETE FROM data_freshness "
    "WHERE platform_account_id IN (SELECT id FROM platform_accounts WHERE business_id = :id)",
    "DELETE FROM ad_entities WHERE business_id = :id",
    "DELETE FROM platform_accounts WHERE business_id = :id",
    "DELETE FROM businesses WHERE id = :id",
)


async def _cleanup(container: Container, business_id: uuid.UUID | None) -> None:
    async with container.session_factory() as session:
        await session.execute(_RESET_RULE, {"code": FIRING_RULE_CODE})
        if business_id is not None:
            for statement in _CLEANUP_STATEMENTS:
                await session.execute(text(statement), {"id": business_id})
        await session.commit()
