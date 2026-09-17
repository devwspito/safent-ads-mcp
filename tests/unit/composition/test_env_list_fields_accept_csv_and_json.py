"""Defecto real en la instancia de produccion (0.2.21):
`CLOUDFLARE_ALLOWED_ZONES=example.com,example.net` tumbaba el arranque
con `SettingsError` -- pydantic-settings JSON-decodifica el valor de
entorno de un `list[str]` ANTES de que el validador `mode="before"` lo
viera, asi que la forma CSV nunca llegaba viva a `_parse_cloudflare_
allowed_zones` (mismo motivo documentado en
`tests/unit/composition/test_telegram_settings.py` para
`TELEGRAM_OWNER_CHAT_IDS`/`ADS_BROKER_ALLOWED_UIDS`, que en cambio nunca
aceptaron CSV). `Annotated[list[str], NoDecode]` + `_parse_comma_or_json_
string_list` corrige esto para `cloudflare_allowed_zones` y
`mcp_extra_allowed_hosts` -- este test cubre ambas formas para los dos
campos, mas el defecto real reproducido literal."""

from __future__ import annotations

import pytest

from tests.unit.composition.factories import build_api_settings


class TestCloudflareAllowedZonesParsing:
    def test_absent_env_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLOUDFLARE_ALLOWED_ZONES", raising=False)

        settings = build_api_settings()

        assert settings.cloudflare_allowed_zones == []

    def test_comma_separated_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El defecto real reportado en la VM: esto reventaba `SettingsError`
        antes de `Annotated[..., NoDecode]`."""
        monkeypatch.setenv("CLOUDFLARE_ALLOWED_ZONES", "example.com,example.net")

        settings = build_api_settings()

        assert settings.cloudflare_allowed_zones == ["example.com", "example.net"]

    def test_json_list_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLOUDFLARE_ALLOWED_ZONES", '["example.com", "example.net"]')

        settings = build_api_settings()

        assert settings.cloudflare_allowed_zones == ["example.com", "example.net"]

    def test_single_zone_without_commas_is_wrapped_in_a_one_item_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDFLARE_ALLOWED_ZONES", "example.com")

        settings = build_api_settings()

        assert settings.cloudflare_allowed_zones == ["example.com"]

    def test_blank_env_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLOUDFLARE_ALLOWED_ZONES", "")

        settings = build_api_settings()

        assert settings.cloudflare_allowed_zones == []


class TestMcpExtraAllowedHostsParsing:
    def test_absent_env_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ADS_MCP_EXTRA_ALLOWED_HOSTS", raising=False)

        settings = build_api_settings()

        assert settings.mcp_extra_allowed_hosts == []

    def test_comma_separated_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "ADS_MCP_EXTRA_ALLOWED_HOSTS", "ads.example.test,ads-otro.example.test"
        )

        settings = build_api_settings()

        assert settings.mcp_extra_allowed_hosts == [
            "ads.example.test",
            "ads-otro.example.test",
        ]

    def test_json_list_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADS_MCP_EXTRA_ALLOWED_HOSTS", '["ads.example.test"]')

        settings = build_api_settings()

        assert settings.mcp_extra_allowed_hosts == ["ads.example.test"]


class TestFederatedAllowedEmailsParsing:
    """`ADS_FEDERATED_ALLOWED_EMAILS` (spec 002b, FR-105): mismo patron
    `Annotated[list[str], NoDecode]` + `_parse_comma_or_json_string_list`
    que los dos campos de arriba, mas normalizacion `strip()` y minusculas.
    Aqui el defecto en produccion no seria un arranque roto sino algo peor:
    una lista que no casa con el correo que llega de Google y deja al dueno
    fuera de su propia instalacion."""

    def test_absent_env_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ADS_FEDERATED_ALLOWED_EMAILS", raising=False)

        settings = build_api_settings()

        assert settings.federated_allowed_emails == []
        assert settings.federated_login_active is False

    def test_comma_separated_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADS_FEDERATED_ALLOWED_EMAILS", "dueno@example.com,socio@example.com")

        settings = build_api_settings()

        assert settings.federated_allowed_emails == [
            "dueno@example.com",
            "socio@example.com",
        ]

    def test_json_list_env_var_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "ADS_FEDERATED_ALLOWED_EMAILS",
            '["dueno@example.com", "socio@example.com"]',
        )

        settings = build_api_settings()

        assert settings.federated_allowed_emails == [
            "dueno@example.com",
            "socio@example.com",
        ]

    def test_single_address_without_commas_is_wrapped_in_a_one_item_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La forma real del despliegue inicial: una sola direccion (S1)."""
        monkeypatch.setenv("ADS_FEDERATED_ALLOWED_EMAILS", "dueno@example.com")

        settings = build_api_settings()

        assert settings.federated_allowed_emails == ["dueno@example.com"]

    def test_blank_env_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADS_FEDERATED_ALLOWED_EMAILS", "")

        settings = build_api_settings()

        assert settings.federated_allowed_emails == []

    def test_addresses_are_stripped_and_lowercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El claim `email` de Google llega en minusculas; una lista escrita
        a mano en `secrets/api.env` no tiene por que."""
        monkeypatch.setenv(
            "ADS_FEDERATED_ALLOWED_EMAILS", '["  Dueno@Example.Com  ", "SOCIO@example.com"]'
        )

        settings = build_api_settings()

        assert settings.federated_allowed_emails == [
            "dueno@example.com",
            "socio@example.com",
        ]

    def test_trailing_separators_do_not_produce_empty_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una entrada vacia en la lista no puede acabar autorizando la
        cadena vacia."""
        monkeypatch.setenv("ADS_FEDERATED_ALLOWED_EMAILS", "dueno@example.com, ,")

        settings = build_api_settings()

        assert settings.federated_allowed_emails == ["dueno@example.com"]
