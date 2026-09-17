"""T121 (tasks.md): `ApiSettings`/`WorkerSettings`/`BrokerSettings` nunca
deben dejar escapar un valor secreto por `repr()`/`str()` ni por la
proyeccion JSON que un `logger.info(..., **settings.model_dump(mode="json"))`
real usaria (la unica forma en que un `dict` acaba sirializado por
`structlog.processors.JSONRenderer`, threat-model.md C-14) -- no basta con
que `pydantic.SecretStr` enmascare el campo que SI esta tipado como tal:
un campo secreto declarado como `str` a secas nunca se enmascara, con
`repr`/`str`/JSON o sin ellos."""

from __future__ import annotations

from typing import Any

from safent_ads.composition.settings import ApiSettings, BrokerSettings, WorkerSettings
from tests.unit.composition.factories import build_api_settings

_MARKER = "MARKER-super-secret-value-0123456789"


def _worker_settings(**overrides: Any) -> WorkerSettings:
    defaults: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://ads:test@localhost:5432/ads_test",
        "session_secret": "test-session-secret-0123456789abcdef",
        "totp_enc_key": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "mcp_token": "test-mcp-token-abc123",
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "public_base_url": "https://ads.test.ts.net",
        "approval_signing_key": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    }
    defaults.update(overrides)
    return WorkerSettings(_env_file=None, **defaults)


def _broker_settings(**overrides: Any) -> BrokerSettings:
    defaults: dict[str, Any] = {
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "approval_public_key": "test-public-key",
        "allowed_uids": [10001],
        "hard_caps_file": "/tmp/safent-ads-test/caps.yaml",
        "credential_master_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "credential_store_dir": "/tmp/safent-ads-test/credentials",
    }
    defaults.update(overrides)
    return BrokerSettings(_env_file=None, **defaults)


def _assert_marker_absent(rendered: str, *, marker: str = _MARKER) -> None:
    assert marker not in rendered, f"un valor secreto escapo sin enmascarar: {rendered!r}"


class TestApiSettingsNeverLeaksSecrets:
    """`session_secret`, `totp_enc_key`, `mcp_token`, `approval_signing_key`,
    `telegram_bot_token`, `google_oidc_client_secret` (spec 002b: secreto del
    cliente OAuth de Google, FR-118/SC-107) y `database_url` (contiene la
    contrasena de Postgres embebida, `.env.example`:
    `postgresql+asyncpg://ads:<pwd>@...`) son secretos de `ApiSettings`;
    ninguno debe aparecer en claro."""

    def _settings_with_markers(self) -> ApiSettings:
        return build_api_settings(
            database_url=f"postgresql+asyncpg://ads:{_MARKER}@localhost:5432/ads_test",
            session_secret=_MARKER,
            mcp_token=_MARKER,
            telegram_bot_token=_MARKER,
            google_oidc_client_secret=_MARKER,
        )

    def test_repr_never_contains_the_secret(self) -> None:
        _assert_marker_absent(repr(self._settings_with_markers()))

    def test_str_never_contains_the_secret(self) -> None:
        _assert_marker_absent(str(self._settings_with_markers()))

    def test_json_mode_dump_never_contains_the_secret(self) -> None:
        """Forma REALISTA en que estos settings acabarian en un log JSON de
        verdad: `logger.info(..., **settings.model_dump(mode="json"))` --
        el unico modo de `model_dump` que produce tipos nativos que
        `structlog.processors.JSONRenderer`/`json.dumps` puede serializar
        sin reventar."""
        dumped = self._settings_with_markers().model_dump(mode="json")
        _assert_marker_absent(str(dumped))


class TestWorkerSettingsNeverLeaksSecrets:
    """Mismos secretos que `ApiSettings` via `CommonSettings` -- `ads-worker`
    firma `rule_authorization` con `approval_signing_key`, nunca menos
    sensible que `ads-api`."""

    def _settings_with_markers(self) -> WorkerSettings:
        return _worker_settings(
            database_url=f"postgresql+asyncpg://ads:{_MARKER}@localhost:5432/ads_test",
            session_secret=_MARKER,
            mcp_token=_MARKER,
        )

    def test_repr_never_contains_the_secret(self) -> None:
        _assert_marker_absent(repr(self._settings_with_markers()))

    def test_str_never_contains_the_secret(self) -> None:
        _assert_marker_absent(str(self._settings_with_markers()))

    def test_json_mode_dump_never_contains_the_secret(self) -> None:
        dumped = self._settings_with_markers().model_dump(mode="json")
        _assert_marker_absent(str(dumped))


class TestBrokerSettingsNeverLeaksSecrets:
    """Credenciales de VENDOR (Google/Meta) y la clave maestra del almacen
    cifrado de credenciales de cliente: la unica clase que las declara
    (threat-model.md C-24)."""

    def _settings_with_markers(self) -> BrokerSettings:
        return _broker_settings(
            credential_master_key=_MARKER,
            google_ads_client_secret=_MARKER,
            meta_app_secret=_MARKER,
            openai_api_key=_MARKER,
            fal_api_key=_MARKER,
        )

    def test_repr_never_contains_the_secret(self) -> None:
        _assert_marker_absent(repr(self._settings_with_markers()))

    def test_str_never_contains_the_secret(self) -> None:
        _assert_marker_absent(str(self._settings_with_markers()))

    def test_json_mode_dump_never_contains_the_secret(self) -> None:
        dumped = self._settings_with_markers().model_dump(mode="json")
        _assert_marker_absent(str(dumped))
