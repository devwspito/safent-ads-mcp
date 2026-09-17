"""`EnvironmentCapabilityProbe`: lee presencia, nunca el valor de la clave;
nunca lanza por clave ausente."""

from __future__ import annotations

from safent_ads.mcp.infrastructure.env_capability_probe import EnvironmentCapabilityProbe


async def test_reports_only_the_keys_actually_present(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("FAKE_KEY_A", "secret-value")
    monkeypatch.delenv("FAKE_KEY_B", raising=False)
    probe = EnvironmentCapabilityProbe(known_keys=("FAKE_KEY_A", "FAKE_KEY_B"))

    configured = await probe.configured_byok_keys()

    assert configured == frozenset({"FAKE_KEY_A"})


async def test_reports_empty_set_when_nothing_is_configured(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("FAKE_KEY_C", raising=False)
    probe = EnvironmentCapabilityProbe(known_keys=("FAKE_KEY_C",))

    configured = await probe.configured_byok_keys()

    assert configured == frozenset()
