"""Owner principle (14-sep addendum): "el MCP le da el puente ... la
potencia la ponen Claude Code y Codex". `MCP_INSTRUCTIONS` es la unica guia
de flujo (corta, en castellano); cada herramienta debe describirse a si
misma para un modelo fuerte, nunca depender de prosa adicional."""

from __future__ import annotations

from safent_ads.mcp.presentation.http import MCP_INSTRUCTIONS, build_mcp_instructions
from tests.unit.mcp.presentation.test_catalog_registries_by_permission import _full_registry
from tests.unit.test_no_client_strings import has_client_string

_MAX_INSTRUCTIONS_LINES = 40  # Includes the versioned shared-workspace workflow.
_MIN_DESCRIPTION_LENGTH = 40

# 004 tasks-2.md §0/Q3 (historia 24): la nota es texto que el modelo lee,
# nunca una instruccion de COMO pensar/redactar/razonar/buscar/resumir --
# eso es trabajo del stack nativo del arnes, no de este servidor.
_COACHING_VERBS = ("piensa", "redacta", "analiza", "razona", "busca en internet", "resume")
_PRE_REVIEW_MARKERS = ("search_competitor_ads", "list_top_performing_ads", "cause.text")

# 005 tasks.md T038, contracts/mcp-tools.md §7: bloque 8, objetivo -> mezcla
# de canales de Google. Los cuatro nombres de canal son literales de
# `GoogleAdvertisingChannelType`; PAUSED es el estado de creacion (invariante
# 7, data-model.md) que solo la aprobacion humana del paquete levanta.
_GOOGLE_CHANNEL_NAMES = ("SEARCH", "PERFORMANCE_MAX", "DEMAND_GEN", "DISPLAY")


def test_instructions_are_present_and_within_the_line_limit() -> None:
    assert MCP_INSTRUCTIONS.strip()
    lines = MCP_INSTRUCTIONS.strip("\n").splitlines()
    assert len(lines) <= _MAX_INSTRUCTIONS_LINES


def test_instructions_are_in_spanish_and_name_the_approval_gate() -> None:
    assert "panel" in MCP_INSTRUCTIONS.lower()
    assert "aprob" in MCP_INSTRUCTIONS.lower()


def test_default_instructions_are_generic_never_a_fixed_client_name() -> None:
    """Lane 006-cloudflare-ui (imagen publica generica): sin `brand_name`/
    `panel_url` explicitos, la nota nunca lleva el nombre ni el host de un
    cliente concreto a pie de letra (contracts/ci-guard.md §1)."""
    assert not has_client_string(MCP_INSTRUCTIONS)
    assert "Sistema de anuncios de tu negocio" in MCP_INSTRUCTIONS


def test_build_mcp_instructions_fills_in_the_brand_and_the_panel_host() -> None:
    instructions = build_mcp_instructions(brand_name="Acme", panel_url="https://ads.acme.example")

    assert "Sistema de anuncios de Acme" in instructions
    assert "ads.acme.example" in instructions
    assert not has_client_string(instructions)


def test_las_instrucciones_avisan_de_available_false_en_search_competitor_ads() -> None:
    """Owner requirement (fix/ad-library-over-composio addendum): el arnes
    nunca debe fingir que no hay datos cuando `search_competitor_ads`
    devuelve `available: false` -- debe ensenar `next_steps` y los
    enlaces."""
    assert "available: false" in MCP_INSTRUCTIONS
    assert "next_steps" in MCP_INSTRUCTIONS


def test_las_instrucciones_solo_declaran_flujo_aprobacion_y_revision_previa() -> None:
    lowered = MCP_INSTRUCTIONS.lower()
    coaching = [verb for verb in _COACHING_VERBS if verb in lowered]
    assert coaching == [], f"verbos de coaching en MCP_INSTRUCTIONS: {coaching}"

    missing = [marker for marker in _PRE_REVIEW_MARKERS if marker not in MCP_INSTRUCTIONS]
    assert missing == [], f"falta letra de la revision previa obligatoria: {missing}"


def _block_8_text() -> str:
    start = MCP_INSTRUCTIONS.index("8. Elige el canal")
    end = MCP_INSTRUCTIONS.index("\nNunca inventes", start)
    return MCP_INSTRUCTIONS[start:end]


def test_el_bloque_8_nombra_los_cuatro_canales_y_la_regla_pausada() -> None:
    block_8 = _block_8_text()
    missing = [name for name in _GOOGLE_CHANNEL_NAMES if name not in block_8]
    assert missing == [], f"faltan canales en el bloque 8: {missing}"
    assert "PAUSED" in block_8


def test_el_bloque_8_exige_list_google_conversion_actions() -> None:
    block_8 = _block_8_text()
    assert "list_google_conversion_actions" in block_8
    assert "PERFORMANCE_MAX" in block_8
    assert "DEMAND_GEN" in block_8


def test_las_instrucciones_no_dicen_como_redactar() -> None:
    lowered = _block_8_text().lower()
    coaching = [verb for verb in _COACHING_VERBS if verb in lowered]
    assert coaching == [], f"verbos de coaching en el bloque 8: {coaching}"


def test_no_tool_description_is_shorter_than_the_minimum() -> None:
    registry = _full_registry()
    short = [
        (d.name, len(d.description))
        for d in registry
        if len(d.description) < _MIN_DESCRIPTION_LENGTH
    ]

    assert short == []
