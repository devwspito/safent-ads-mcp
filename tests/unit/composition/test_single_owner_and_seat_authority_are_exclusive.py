"""`ApiSettings` exige exactamente uno de `ADS_SEAT_AUTHORITY_ENABLED`/
`ADS_SINGLE_OWNER_MODE` (aclaracion del dueno, 004 tasks.md A6/A10):
produccion/alojado exige Enterprise; el Safent local exige el modo de
propietario unico. Ninguno de los dos, o los dos, no arranca."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.composition.factories import build_api_settings


def test_neither_mode_enabled_fails_loud() -> None:
    with pytest.raises(ValueError, match="exactamente uno"):
        build_api_settings(seat_authority_enabled=False, single_owner_mode=False)


def test_both_modes_enabled_fails_loud() -> None:
    with pytest.raises(ValueError, match="exactamente uno"):
        build_api_settings(seat_authority_enabled=True, single_owner_mode=True)


def test_single_owner_mode_alone_is_valid() -> None:
    settings = build_api_settings(seat_authority_enabled=False, single_owner_mode=True)

    assert settings.single_owner_mode is True
    assert settings.seat_authority_enabled is False


def test_seat_authority_alone_is_valid() -> None:
    settings = build_api_settings()

    assert settings.seat_authority_enabled is True
    assert settings.single_owner_mode is False


def test_managed_central_is_exempt_from_the_exclusivity_rule() -> None:
    settings = build_api_settings(
        managed_central=True, seat_authority_enabled=False, single_owner_mode=False
    )

    assert settings.managed_central is True


def _tls_pair(tmp_path: Path) -> dict[str, Path]:
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert.write_text("cert")
    key.write_text("key")
    return {"tls_certfile": cert, "tls_keyfile": key}


def test_companion_mode_without_flags_assumes_single_owner(tmp_path: Path) -> None:
    """Regresion 18-sep-2026: la app lanza el companion con ADS_COMPANION_MODE=true
    y nunca declaro ADS_SINGLE_OWNER_MODE; ads-api moria al arrancar."""
    settings = build_api_settings(
        companion_mode=True,
        seat_authority_enabled=False,
        single_owner_mode=False,
        **_tls_pair(tmp_path),
    )

    assert settings.single_owner_mode is True
    assert settings.seat_authority_enabled is False


def test_companion_mode_with_seat_authority_keeps_seat_mode(tmp_path: Path) -> None:
    settings = build_api_settings(
        companion_mode=True,
        seat_authority_enabled=True,
        single_owner_mode=False,
        **_tls_pair(tmp_path),
    )

    assert settings.seat_authority_enabled is True
    assert settings.single_owner_mode is False
