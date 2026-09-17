"""`TELEGRAM_BOT_TOKEN`/`TELEGRAM_OWNER_CHAT_IDS` (composition/settings.py):
unica excepcion a "ningun secreto tiene valor por defecto" -- una
instalacion limpia no trae el bot todavia, asi que ausentes/vacios
degradan a "desactivado" en vez de tumbar `ApiSettings()`/`WorkerSettings()`
(lo que ya consultaban como tal: `composition/api.py::_telegram_configured`,
que alimenta `/api/v1/health/deep`).

`TELEGRAM_OWNER_CHAT_IDS`/`ADS_BROKER_ALLOWED_UIDS` solo aceptan JSON (un
int suelto o una lista): la forma separada por comas nunca ha funcionado
-- pydantic-settings JSON-decodifica el valor de entorno ANTES de que el
validador `mode="before"` lo vea, asi que `"1,2"` nunca llega a
`_parse_chat_ids`/`_parse_allowed_uids`, levanta `SettingsError` antes."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic_settings import SettingsError

from safent_ads.composition.api import _telegram_configured
from safent_ads.composition.settings import ApiSettings, BrokerSettings, WorkerSettings

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="

_COMMON_KWARGS: dict[str, object] = {
    "database_url": "postgresql+asyncpg://ads:test@localhost:5432/ads_test",
    "session_secret": "test-session-secret-0123456789abcdef",
    "totp_enc_key": _VALID_32_BYTE_KEY_B64,
    "mcp_token": "test-mcp-token-abc123",
    "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
    "public_base_url": "https://ads.test.ts.net",
    "approval_signing_key": _VALID_32_BYTE_KEY_B64,
    "seat_authority_enabled": True,
    "enterprise_origin": "https://enterprise.test",
    "enterprise_service_secret": "a" * 64,
    "enterprise_org_ids": frozenset({UUID("00000000-0000-0000-0000-000000000001")}),
}

_BROKER_KWARGS: dict[str, object] = {
    "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
    "approval_public_key": "test-public-key",
    "hard_caps_file": "/tmp/safent-ads-test/caps.yaml",
    "credential_master_key": _VALID_32_BYTE_KEY_B64,
    "credential_store_dir": "/tmp/safent-ads-test/credentials",
}


def _clear_telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_IDS", raising=False)


class TestApiSettingsTelegramOptional:
    def test_absent_env_vars_build_a_disabled_settings_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_telegram_env(monkeypatch)

        settings = ApiSettings(_env_file=None, **_COMMON_KWARGS)

        assert settings.telegram_bot_token.get_secret_value() == ""
        assert settings.telegram_owner_chat_ids == []

    def test_disabled_settings_report_not_configured_for_deep_health(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_telegram_env(monkeypatch)

        settings = ApiSettings(_env_file=None, **_COMMON_KWARGS)

        assert _telegram_configured(settings) is False

    def test_populated_settings_report_configured_for_deep_health(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_telegram_env(monkeypatch)

        settings = ApiSettings(
            _env_file=None,
            telegram_bot_token="123456:test-bot-token",
            telegram_owner_chat_ids=[111222333],
            **_COMMON_KWARGS,
        )

        assert _telegram_configured(settings) is True


def test_worker_settings_telegram_absent_env_vars_build_a_disabled_settings_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_telegram_env(monkeypatch)

    settings = WorkerSettings(_env_file=None, **_COMMON_KWARGS)

    assert settings.telegram_bot_token.get_secret_value() == ""
    assert settings.telegram_owner_chat_ids == []


class TestTelegramOwnerChatIdsParsing:
    def test_single_int_env_var_wraps_into_a_one_item_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-bot-token")
        monkeypatch.setenv("TELEGRAM_OWNER_CHAT_IDS", "123456789")

        settings = ApiSettings(_env_file=None, **_COMMON_KWARGS)

        assert settings.telegram_owner_chat_ids == [123456789]

    def test_json_list_env_var_is_accepted_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-bot-token")
        monkeypatch.setenv("TELEGRAM_OWNER_CHAT_IDS", "[111,222]")

        settings = ApiSettings(_env_file=None, **_COMMON_KWARGS)

        assert settings.telegram_owner_chat_ids == [111, 222]

    def test_comma_separated_env_var_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-bot-token")
        monkeypatch.setenv("TELEGRAM_OWNER_CHAT_IDS", "111,222")

        with pytest.raises(SettingsError):
            ApiSettings(_env_file=None, **_COMMON_KWARGS)


class TestBrokerAllowedUidsParsing:
    def test_single_int_env_var_wraps_into_a_one_item_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADS_BROKER_ALLOWED_UIDS", "10001")

        settings = BrokerSettings(_env_file=None, **_BROKER_KWARGS)

        assert settings.allowed_uids == [10001]

    def test_json_list_env_var_is_accepted_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADS_BROKER_ALLOWED_UIDS", "[10001,10002]")

        settings = BrokerSettings(_env_file=None, **_BROKER_KWARGS)

        assert settings.allowed_uids == [10001, 10002]

    def test_comma_separated_env_var_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADS_BROKER_ALLOWED_UIDS", "10001,10002")

        with pytest.raises(SettingsError):
            BrokerSettings(_env_file=None, **_BROKER_KWARGS)


def test_short_session_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Revision F4, condicion 6: el secreto de sesion firma cookie, reto TOTP y
    # (por HKDF) las previsualizaciones; menos de 32 bytes no arranca.
    _clear_telegram_env(monkeypatch)
    with pytest.raises(ValueError, match="32 bytes"):
        ApiSettings(_env_file=None, **{**_COMMON_KWARGS, "session_secret": "corto"})
