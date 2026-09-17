"""El agente NO fija sus propios topes (spec 008 T034, invariante i10).

La superficie de topes es del panel y solo del panel: sesion por cookie,
CSRF y confirmacion de accion. Ninguna herramienta MCP la expone, y esta
guarda lo comprueba sobre el catalogo VIVO -- no sobre una lista escrita a
mano que alguien tendria que acordarse de actualizar."""

from __future__ import annotations

from safent_ads.mcp.presentation.registry import ToolRegistry

_FORBIDDEN_FRAGMENTS = ("hard_cap", "hard-cap", "spend_envelope", "panel_managed")


def test_no_mcp_tool_name_mentions_hard_caps(registry: ToolRegistry) -> None:
    offenders = {
        definition.name
        for definition in registry
        for fragment in _FORBIDDEN_FRAGMENTS
        if fragment in definition.name.lower()
    }

    assert not offenders


_CAP_FIELDS = frozenset({"daily_cap_minor", "monthly_cap_minor", "ceiling_minor", "floor_minor"})


def test_no_mcp_tool_takes_a_cap_amount_as_an_argument(registry: ToolRegistry) -> None:
    """Un tope no entra por el catalogo ni siquiera como parametro: si
    apareciera, el agente podria moverlo sin pasar por el panel. Se mira el
    modelo de argumentos REAL de cada herramienta, no su nombre."""
    offenders = {
        definition.name
        for definition in registry
        if _CAP_FIELDS & set(definition.args_model.model_fields)
    }

    assert not offenders


def test_the_guard_would_catch_a_tool_that_took_a_cap(registry: ToolRegistry) -> None:
    """Caso positivo sembrado: sin el, los dos tests de arriba pasarian
    aunque el registro estuviera vacio o el campo hubiera cambiado de
    nombre."""
    assert any(definition.args_model.model_fields for definition in registry)
    assert _CAP_FIELDS & {"daily_cap_minor"}
