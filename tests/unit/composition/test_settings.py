"""`ApiSettings` companion-mode TLS gate (composition/settings.py):
`ADS_COMPANION_MODE=true` exige `ADS_TLS_CERTFILE`/`ADS_TLS_KEYFILE`
legibles al construirse -- fail closed, antes de que `composition/app.py`
arranque nada (INV-7 del companion, definido en el runtime)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from safent_ads.composition.settings import ApiSettings
from tests.unit.composition.factories import build_api_settings


def test_companion_mode_is_off_by_default() -> None:
    settings = build_api_settings()

    assert settings.companion_mode is False


def test_companion_mode_true_without_any_tls_file_fails_closed() -> None:
    with pytest.raises(ValidationError, match="ADS_TLS_CERTFILE"):
        build_api_settings(companion_mode=True)


def test_companion_mode_true_with_only_certfile_fails_closed(tmp_path: Path) -> None:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")

    with pytest.raises(ValidationError, match="ADS_TLS_KEYFILE"):
        build_api_settings(companion_mode=True, tls_certfile=certfile)


def test_companion_mode_true_with_a_missing_file_path_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.crt"

    with pytest.raises(ValidationError, match="no existe o no es legible"):
        build_api_settings(companion_mode=True, tls_certfile=missing, tls_keyfile=missing)


def test_companion_mode_true_with_an_unreadable_file_fails_closed(tmp_path: Path) -> None:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    certfile.chmod(0o000)
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")

    try:
        with pytest.raises(ValidationError, match="no existe o no es legible"):
            build_api_settings(companion_mode=True, tls_certfile=certfile, tls_keyfile=keyfile)
    finally:
        certfile.chmod(0o600)


def test_companion_mode_true_with_readable_tls_files_succeeds(tmp_path: Path) -> None:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")

    settings = build_api_settings(companion_mode=True, tls_certfile=certfile, tls_keyfile=keyfile)

    assert settings.companion_mode is True
    assert settings.tls_certfile == certfile
    assert settings.tls_keyfile == keyfile


def test_mcp_oauth_enabled_defaults_to_true() -> None:
    """Apagarlo (`ADS_MCP_OAUTH_ENABLED=false`) devuelve el comportamiento
    actual exacto -- solo bearer estatico (spec 002 plan.md "Ajustes")."""
    assert build_api_settings().mcp_oauth_enabled is True


def test_mcp_oauth_enabled_can_be_turned_off() -> None:
    assert build_api_settings(mcp_oauth_enabled=False).mcp_oauth_enabled is False


def test_mcp_static_token_enabled_defaults_to_false() -> None:
    """threat-model.md C-53: via de emergencia mientras los agentes migran
    a OAuth. M6 de la revision de seguridad (16-sep): `False` por defecto
    -- el instalador y ambos agentes ya usan OAuth, asi que encenderla es
    un acto explicito (`ADS_MCP_STATIC_TOKEN_ENABLED=true`), nunca el
    comportamiento de fabrica."""
    assert build_api_settings().mcp_static_token_enabled is False


def test_mcp_static_token_enabled_can_be_turned_on() -> None:
    assert build_api_settings(mcp_static_token_enabled=True).mcp_static_token_enabled is True


# --- El bearer de dueño solo existe si su via esta abierta (fase E) ------


def test_the_static_bearer_is_optional_while_its_path_is_closed() -> None:
    """Seguimiento aceptado en la revision T022 (spec 008): exigir
    `ADS_MCP_TOKEN` con la via estatica apagada obligaba al primer arranque
    a generar un bearer de dueño que se quedaba DORMIDO en disco -- eterno,
    con permiso `aprobar` y sin rotar. Sin via abierta no hace falta."""
    settings = build_api_settings(mcp_token=None)

    assert settings.mcp_static_token_enabled is False
    assert settings.mcp_token is None


def test_opening_the_static_path_without_a_bearer_fails_at_startup() -> None:
    """La otra mitad: encender el interruptor sin bearer dejaria `/mcp`
    anunciando una via que no existe. Falla al construir, no en la primera
    peticion."""
    with pytest.raises(ValidationError, match="ADS_MCP_TOKEN"):
        build_api_settings(mcp_token=None, mcp_static_token_enabled=True)


def test_an_empty_static_bearer_counts_as_absent() -> None:
    """`ADS_MCP_TOKEN=` en `secrets/api.env` no es un bearer: abriria la
    puerta a una comparacion contra la cadena vacia."""
    with pytest.raises(ValidationError, match="ADS_MCP_TOKEN"):
        build_api_settings(mcp_token="", mcp_static_token_enabled=True)


# --- Login federado con Google (spec 002b T016, research.md Decision E) ---


def test_federated_login_is_off_by_default() -> None:
    """Default-deny: una instalacion que no sabe nada de 002b se comporta
    exactamente como el spec 002 (FR-124)."""
    settings = build_api_settings()

    assert settings.federated_login_enabled is False
    assert settings.federated_login_active is False
    assert settings.google_oidc_client_id is None
    assert settings.google_oidc_client_secret is None
    assert settings.federated_allowed_emails == []


def test_enabled_with_an_empty_allow_list_builds_and_stays_closed() -> None:
    """El caso que el spec prohibe tratar como fallo de arranque ("lista
    vacia o mal escrita: el login federado queda cerrado, el servicio
    arranca", FR-105). Y vacia NUNCA significa "todos"."""
    settings = build_api_settings(
        federated_login_enabled=True,
        google_oidc_client_id="123.apps.googleusercontent.com",
        google_oidc_client_secret="un-secreto-de-cliente",
        federated_allowed_emails=[],
    )

    assert settings.federated_login_active is False


def test_enabled_without_client_credentials_builds_and_stays_closed() -> None:
    settings = build_api_settings(
        federated_login_enabled=True, federated_allowed_emails=["dueno@example.com"]
    )

    assert settings.federated_login_active is False


def test_a_blank_client_id_or_secret_does_not_count_as_configured() -> None:
    settings = build_api_settings(
        federated_login_enabled=True,
        google_oidc_client_id="   ",
        google_oidc_client_secret="   ",
        federated_allowed_emails=["dueno@example.com"],
    )

    assert settings.federated_login_active is False


def test_a_complete_configuration_activates_the_federated_login() -> None:
    settings = build_api_settings(
        federated_login_enabled=True,
        google_oidc_client_id="123.apps.googleusercontent.com",
        google_oidc_client_secret="un-secreto-de-cliente",
        federated_allowed_emails=["dueno@example.com"],
    )

    assert settings.federated_login_active is True


def test_a_complete_configuration_with_the_switch_off_stays_closed() -> None:
    settings = build_api_settings(
        federated_login_enabled=False,
        google_oidc_client_id="123.apps.googleusercontent.com",
        google_oidc_client_secret="un-secreto-de-cliente",
        federated_allowed_emails=["dueno@example.com"],
    )

    assert settings.federated_login_active is False


def _companion_settings(tmp_path: Path, **overrides: object) -> ApiSettings:
    certfile = tmp_path / "leaf.crt"
    certfile.write_text("cert")
    keyfile = tmp_path / "leaf.key"
    keyfile.write_text("key")
    return build_api_settings(
        companion_mode=True, tls_certfile=certfile, tls_keyfile=keyfile, **overrides
    )


def test_companion_mode_keeps_the_static_bearer_on_by_default(tmp_path: Path) -> None:
    """0.2.30: el motor de la app Safent solo sabe presentar `ADS_MCP_TOKEN`
    (provision.sh nunca escribio `ADS_MCP_STATIC_TOKEN_ENABLED`); con M6 el
    companion 0.2.29 respondia 401 a su propia app."""
    assert _companion_settings(tmp_path).mcp_static_token_enabled is True


def test_companion_mode_respects_an_explicit_static_bearer_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADS_MCP_STATIC_TOKEN_ENABLED", "false")

    assert _companion_settings(tmp_path).mcp_static_token_enabled is False


def test_the_static_bearer_stays_off_outside_companion_mode() -> None:
    assert build_api_settings(companion_mode=False).mcp_static_token_enabled is False


def test_the_static_bearer_is_still_required_in_companion_mode(tmp_path: Path) -> None:
    """El modo companion enciende el interruptor solo (0.2.30): el orden de
    los validadores importa, y ahi el bearer sigue siendo obligatorio."""
    with pytest.raises(ValidationError, match="ADS_MCP_TOKEN"):
        _companion_settings(tmp_path, mcp_token=None)
