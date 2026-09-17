"""Incidente 2026-09-15: `list_meta_catalogs` salia como `TOOL_FAILED` opaco;
ahora el broker deniega con `PLATFORM_CAPABILITY_NOT_IMPLEMENTED` y el MCP lo
traduce a un error con codigo estable y mensaje en espanol."""

from __future__ import annotations

from safent_ads.mcp.application.errors import CapabilityNotAvailableError, broker_denial_error


def test_capability_not_implemented_translates_to_a_stable_code() -> None:
    error = broker_denial_error("PLATFORM_CAPABILITY_NOT_IMPLEMENTED")
    assert isinstance(error, CapabilityNotAvailableError)
    assert error.code == "PLATFORM_CAPABILITY_NOT_IMPLEMENTED"
    assert "tipo de cuenta" in str(error)


def test_unknown_codes_are_still_not_translated() -> None:
    assert broker_denial_error("SOMETHING_NEW") is None
