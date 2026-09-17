"""Reglas puras de `mcp.domain.capabilities` (tool-surface.md §0: "la
capacidad se declara, nunca se finge")."""

from __future__ import annotations

from safent_ads.mcp.application.dto import (
    AccountStatus,
    AutonomyLevel,
    KillSwitchStatus,
    PlatformAccountSummary,
    PlatformCode,
    RuleSummary,
)
from safent_ads.mcp.domain.capabilities import (
    generation_backend_capabilities,
    missing_byok_keys,
    missing_platform_accounts,
    platform_write_capabilities,
    resolve_autonomy_status,
)


def test_platform_writes_are_never_available() -> None:
    """Hecho de arquitectura (INV-2, contracts/mcp-tools.md regla 3):
    ninguna plataforma admite escritura directa del agente, siempre."""
    capabilities = platform_write_capabilities()

    assert {c.platform for c in capabilities} == set(PlatformCode)
    assert all(c.writes_available is False for c in capabilities)
    assert all(c.reason for c in capabilities)


def test_generation_backend_reports_configured_and_missing() -> None:
    capabilities = generation_backend_capabilities(frozenset({"FAL_KEY"}))

    by_capability = {c.capability: c for c in capabilities}
    assert by_capability["image"].configured is True
    assert by_capability["image"].backend == "fal"
    assert by_capability["voice"].configured is False
    assert by_capability["voice"].backend is None


def test_missing_byok_keys_reports_nothing_missing_when_all_configured() -> None:
    all_keys = frozenset({"FAL_KEY", "ELEVENLABS_API_KEY", "BRAVE_API_KEY"})

    assert missing_byok_keys(all_keys) == []


def test_missing_byok_keys_reports_everything_missing_when_nothing_configured() -> None:
    missing = missing_byok_keys(frozenset())

    assert missing == ["BRAVE_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY"]


def test_autonomy_disabled_when_kill_switch_engaged() -> None:
    kill_switch = KillSwitchStatus(True, "business", "ALL", "impago detectado", None)

    status = resolve_autonomy_status(kill_switch, [])

    assert status.enabled is False
    assert status.reason == "impago detectado"


def test_autonomy_disabled_without_auto_rules() -> None:
    kill_switch = KillSwitchStatus(False, "global", "ALL", None, None)
    rules = [RuleSummary("r1", "M05", PlatformCode.GOOGLE, True, AutonomyLevel.NOTIFY)]

    status = resolve_autonomy_status(kill_switch, rules)

    assert status.enabled is False
    assert status.reason is not None


def test_autonomy_enabled_with_active_auto_rule() -> None:
    kill_switch = KillSwitchStatus(False, "global", "ALL", None, None)
    rules = [RuleSummary("r1", "M05", PlatformCode.GOOGLE, True, AutonomyLevel.AUTO)]

    status = resolve_autonomy_status(kill_switch, rules)

    assert (status.enabled, status.reason) == (True, None)


def test_missing_platform_accounts_flags_platforms_without_an_active_account() -> None:
    accounts = [
        PlatformAccountSummary(
            "google:account:1",
            PlatformCode.GOOGLE,
            "EUR",
            "Europe/Madrid",
            AccountStatus.ACTIVE,
            "standard",
        )
    ]

    missing = missing_platform_accounts(accounts)

    assert any("meta" in note for note in missing)
    assert not any("google" in note for note in missing)


def test_missing_platform_accounts_flags_suspended_account_as_missing() -> None:
    accounts = [
        PlatformAccountSummary(
            "meta:account:1",
            PlatformCode.META,
            "EUR",
            "Europe/Madrid",
            AccountStatus.SUSPENDED,
            "meta_limited",
        )
    ]

    missing = missing_platform_accounts(accounts)

    assert any("meta" in note for note in missing)
