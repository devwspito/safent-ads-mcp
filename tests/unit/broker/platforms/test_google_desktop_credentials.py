"""Desktop public credentials through the real store, SDK and ADC refresh code."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials
from pydantic import ValidationError

from safent_ads.accounts.presentation.platform_apps_payloads import SetGoogleAppCredentialsRequest
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.errors import AppCredentialsIncompleteError
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode


def test_desktop_roundtrip_is_explicit_and_secretless(tmp_path: Path) -> None:
    store = EncryptedCredentialStore(tmp_path, base64.b64encode(b"0" * 32).decode())
    service = AppCredentialsService(store, FixedClock(datetime(2026, 9, 13, tzinfo=UTC)))
    parsed = SetGoogleAppCredentialsRequest(
        client_id="desktop.apps.googleusercontent.com", client_type="desktop"
    )
    status = service.set_google(**parsed.model_dump())
    assert status.client_type == "desktop"
    assert status.configured
    loaded = store.get_google_app_credentials()
    assert loaded is not None
    assert loaded.client_type == "desktop"
    assert loaded.client_secret == ""
    assert "client_secret" not in status.__dataclass_fields__


@pytest.mark.parametrize("kind, secret", [("web", ""), ("desktop", "forbidden")])
def test_web_secret_required_desktop_secret_rejected(
    tmp_path: Path, kind: str, secret: str
) -> None:
    payload = {
        "client_id": "example.apps.googleusercontent.com",
        "client_type": kind,
        "client_secret": secret,
    }
    with pytest.raises(ValidationError):
        SetGoogleAppCredentialsRequest.model_validate(payload)
    service = AppCredentialsService(
        EncryptedCredentialStore(tmp_path, base64.b64encode(b"0" * 32).decode()),
        FixedClock(datetime(2026, 9, 13, tzinfo=UTC)),
    )
    with pytest.raises(AppCredentialsIncompleteError):
        service.set_google(**payload, login_customer_id=None)


def test_legacy_encrypted_record_remains_web(tmp_path: Path) -> None:
    store = EncryptedCredentialStore(tmp_path, base64.b64encode(b"0" * 32).decode())
    store._write(
        store._app_credentials_path(PlatformCode.GOOGLE),
        json.dumps(
            {
                "client_id": "legacy.apps.googleusercontent.com",
                "client_secret": "legacy-fixture",
                "login_customer_id": None,
                "updated_at": "2026-09-13T00:00:00+00:00",
            }
        ).encode(),
    )
    assert store.get_google_app_credentials().client_type == "web"


def test_native_adc_refresh_accepts_empty_public_client_secret() -> None:
    # google-auth requires the key in ADC, but no confidential value for Desktop.
    credential = Credentials.from_authorized_user_info(
        {
            "client_id": "desktop.apps.googleusercontent.com",
            "client_secret": "",
            "refresh_token": "refresh-fixture",
        }
    )
    calls = []

    class Response:
        status = 200
        data = b'{"access_token":"access-fixture","expires_in":3600,"token_type":"Bearer"}'

    def request(url, method, body=None, headers=None, **kwargs):
        assert url == "https://oauth2.googleapis.com/token"
        calls.append(parse_qs(body.decode(), keep_blank_values=True))
        return Response()

    credential.refresh(request)
    assert credential.token == "access-fixture"  # noqa: S105 - synthetic token, no network
    assert calls[0]["client_secret"] == [""]
    assert calls[0]["grant_type"] == ["refresh_token"]


def test_google_ads_sdk_uses_empty_public_secret_without_network(monkeypatch) -> None:
    calls = []

    def refresh(self, request):
        calls.append(self.client_secret)
        self.token = "access-fixture"  # noqa: S105 - synthetic token, no network

    monkeypatch.setattr(Credentials, "refresh", refresh)
    client = GoogleAdsClient.load_from_dict(
        {
            "client_id": "desktop.apps.googleusercontent.com",
            "client_secret": "",
            "refresh_token": "refresh-fixture",
            "use_proto_plus": True,
        },
        version="v25",
    )
    assert calls == [""]
    assert client.credentials.client_secret == ""
