"""Reglas puras de capacidad (tool-surface.md §0: "la capacidad se declara,
nunca se finge"). Sin I/O: `get_capabilities`/`get_project_context`
(application) llaman los puertos y pasan los DTOs ya resueltos aqui."""

from __future__ import annotations

from safent_ads.mcp.application.dto import (
    AccountStatus,
    AutonomyLevel,
    AutonomyStatus,
    GenerationBackendCapability,
    KillSwitchStatus,
    PlatformAccountSummary,
    PlatformCode,
    PlatformWriteCapability,
    RuleSummary,
)

_PLATFORM_WRITE_REASON = (
    "toda escritura pasa por propose_* + aprobacion humana, o por "
    "apply_defensive_action autorizado por el motor de reglas: el agente "
    "nunca escribe en una plataforma de forma directa "
    "(contracts/mcp-tools.md regla 3)"
)

# tool-surface.md §6 "Lo que debe aportar el dueno": claves BYOK de P1.
_GENERATION_BACKENDS: tuple[tuple[str, str, str], ...] = (
    ("image", "FAL_KEY", "fal"),
    ("video", "FAL_KEY", "fal"),
    ("voice", "ELEVENLABS_API_KEY", "elevenlabs"),
    ("web_search", "BRAVE_API_KEY", "brave"),
)


def platform_write_capabilities() -> list[PlatformWriteCapability]:
    """Hecho de arquitectura, no una consulta (INV-2): ninguna plataforma
    tiene escritura directa disponible para el agente, nunca."""
    return [
        PlatformWriteCapability(
            platform=platform, writes_available=False, reason=_PLATFORM_WRITE_REASON
        )
        for platform in PlatformCode
    ]


def generation_backend_capabilities(
    configured_keys: frozenset[str],
) -> list[GenerationBackendCapability]:
    return [
        GenerationBackendCapability(
            capability=capability,
            configured=env_key in configured_keys,
            backend=backend if env_key in configured_keys else None,
        )
        for capability, env_key, backend in _GENERATION_BACKENDS
    ]


def missing_byok_keys(configured_keys: frozenset[str]) -> list[str]:
    known = {env_key for _, env_key, _ in _GENERATION_BACKENDS}
    return sorted(known - configured_keys)


def resolve_autonomy_status(
    kill_switch: KillSwitchStatus, enabled_rules: list[RuleSummary]
) -> AutonomyStatus:
    if kill_switch.engaged:
        reason = kill_switch.reason or f"freno de emergencia activo (modo {kill_switch.mode})"
        return AutonomyStatus(enabled=False, reason=reason)
    has_auto_rule = any(rule.autonomy_level == AutonomyLevel.AUTO for rule in enabled_rules)
    if not has_auto_rule:
        return AutonomyStatus(
            enabled=False, reason="no hay reglas con autonomy_level=AUTO habilitadas"
        )
    return AutonomyStatus(enabled=True, reason=None)


def missing_platform_accounts(accounts: list[PlatformAccountSummary]) -> list[str]:
    """`ACTIVE` es la unica condicion que garantiza que la cuenta puede
    leerse/proponerse sin degradacion (data-model.md `PlatformAccount`:
    `THROTTLED`/`SUSPENDED`/`READ_ONLY` son estados de problema)."""
    connected = {account.platform for account in accounts if account.status == AccountStatus.ACTIVE}
    return [
        f"{platform.value}: sin cuenta de plataforma activa conectada"
        for platform in PlatformCode
        if platform not in connected
    ]
