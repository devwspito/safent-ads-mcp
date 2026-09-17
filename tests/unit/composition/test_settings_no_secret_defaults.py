"""Ningun campo `SecretStr` de las clases de settings tiene un `default=`
que esconda un secreto (composition/settings.py, security-review-f4.md
B-1): `creative_preview_signing_key` era exactamente ese fallo --
`default=SecretStr("dev-only-...")` sin ningun `.env.example`/`compose.yaml`
fijandolo nunca, asi que cualquier despliegue sin la variable firmaba con
una constante versionada. La clave ahora se deriva en caliente de
`session_secret` (`shared/crypto/hkdf.py`) y el campo ya no existe -- este
test evita que reaparezca un campo asi, aqui o en cualquier otra clase de
settings."""

from __future__ import annotations

from pydantic import SecretStr

from safent_ads.composition.settings import ApiSettings, BrokerSettings, WorkerSettings

# Unica excepcion documentada (`CommonSettings` docstring): Telegram no es un
# secreto que una instalacion limpia genere sola, lo crea el propietario
# despues con @BotFather. Su default es la cadena VACIA -- el resto del
# codigo la trata como "Telegram desactivado", nunca como un secreto
# utilizable, asi que no esconde ningun requisito de arranque.
_DOCUMENTED_EMPTY_DEFAULT_FIELDS = {"telegram_bot_token"}


def test_no_settings_field_has_a_default_that_hides_a_non_empty_secret() -> None:
    offending: list[str] = []
    for settings_cls in (ApiSettings, WorkerSettings, BrokerSettings):
        for name, field in settings_cls.model_fields.items():
            if name in _DOCUMENTED_EMPTY_DEFAULT_FIELDS:
                continue
            default = field.default
            if isinstance(default, SecretStr) and default.get_secret_value():
                offending.append(f"{settings_cls.__name__}.{name}")

    assert offending == []


def test_telegram_bot_token_documented_exception_defaults_to_empty() -> None:
    default = ApiSettings.model_fields["telegram_bot_token"].default

    assert isinstance(default, SecretStr)
    assert default.get_secret_value() == ""


def test_api_settings_has_no_creative_preview_signing_key_field() -> None:
    # Regresion directa de B-1: el campo se elimino, la clave se deriva de
    # `session_secret` (`composition/app.py::_CREATIVE_PREVIEW_SIGNING_KEY_INFO`).
    assert "creative_preview_signing_key" not in ApiSettings.model_fields


def test_google_oidc_client_secret_has_no_default_value() -> None:
    """Spec 002b: el secreto del cliente OAuth de Google entra al catalogo
    de secretos. `None` por defecto (integracion opcional, como
    `CLOUDFLARE_API_TOKEN`), nunca una constante versionada -- y su
    ausencia deja `federated_login_active` en `False`, no un arranque roto."""
    field = ApiSettings.model_fields["google_oidc_client_secret"]

    assert field.default is None
    assert not isinstance(field.default, SecretStr)
