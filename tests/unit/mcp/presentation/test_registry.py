"""`ToolRegistry` (T044): lista blanca INV-2."""

from __future__ import annotations

import pytest

from safent_ads.mcp.domain.errors import ForbiddenToolNameError, ToolClassMismatchError
from safent_ads.mcp.presentation.args import ListBusinessesArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry


async def _no_op_handler(_args, _caller_scope):  # noqa: ANN001, ANN202 - doble minimo del test
    return []


def test_no_approval_verb_in_catalog(registry: ToolRegistry) -> None:
    """contracts/mcp-tools.md regla 1 / threat-model.md C-2: ninguna
    herramienta del catalogo real (lecturas de US1-4 mas las escrituras de
    US2/US3 -- `propose_*`/`apply_defensive_action`/`withdraw_proposal`,
    ninguna de las cuales es un verbo de decision) puede empezar por un
    verbo de decision."""
    forbidden_prefixes = ("approve_", "execute_", "apply_proposal")
    assert len(registry) > 0
    for definition in registry:
        assert not definition.name.startswith(forbidden_prefixes), definition.name


def test_catalog_tools_use_generic_names(registry: ToolRegistry) -> None:
    """vocabulary.md §3/T177: el catalogo generico expone
    `list_offerings`/`list_calendar_events`/`get_calendar_event` (la
    guarda de vocabulario vertical, `tests/unit/test_no_vendor_specific_
    names.py`, ya prohibe que los nombres anteriores reaparezcan en
    `src/`)."""
    names = {definition.name for definition in registry}
    assert {"list_offerings", "list_calendar_events", "get_calendar_event"} <= names


def test_registry_rejects_forbidden_verb_at_construction() -> None:
    bad_definition = ToolDefinition(
        name="approve_proposal",
        description="x",
        args_model=ListBusinessesArgs,
        tool_class=ToolClass.READ,
        handler=_no_op_handler,
        business_id_of=None,
    )
    with pytest.raises(ForbiddenToolNameError):
        ToolRegistry([bad_definition])


def test_registry_accepts_proposal_verbs() -> None:
    """`propose_*`/`apply_*` (salvo `apply_proposal*`, ver regla arriba) son
    la clase `PROPOSAL` desde US2/US3 -- ya no quedan fuera del registro."""
    good_definition = ToolDefinition(
        name="propose_budget_change",
        description="x",
        args_model=ListBusinessesArgs,
        tool_class=ToolClass.PROPOSAL,
        handler=_no_op_handler,
        business_id_of=None,
    )
    registry = ToolRegistry([good_definition])
    assert registry.get("propose_budget_change") is not None


def test_registry_rejects_verb_outside_read_and_proposal_conventions() -> None:
    bad_definition = ToolDefinition(
        name="frobnicate_campaign",
        description="x",
        args_model=ListBusinessesArgs,
        tool_class=ToolClass.PROPOSAL,
        handler=_no_op_handler,
        business_id_of=None,
    )
    with pytest.raises(ForbiddenToolNameError):
        ToolRegistry([bad_definition])


def test_registry_get_returns_none_for_unknown_tool(registry: ToolRegistry) -> None:
    assert registry.get("does_not_exist") is None


def test_registry_rejects_a_proposal_verb_declared_as_read() -> None:
    """Nit de la revision de seguridad (16-sep): `propose_*` mal
    clasificado como `ToolClass.READ` dejaria a `ToolDispatcher` sin
    exigir `ads:propose` para invocarlo -- un caller con solo `ads:read`
    podria ejecutar lo que, por su nombre, escribe."""
    mismatched_definition = ToolDefinition(
        name="propose_budget_change",
        description="x",
        args_model=ListBusinessesArgs,
        tool_class=ToolClass.READ,
        handler=_no_op_handler,
        business_id_of=None,
    )
    with pytest.raises(ToolClassMismatchError):
        ToolRegistry([mismatched_definition])
