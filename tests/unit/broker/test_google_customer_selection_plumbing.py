"""Explicit selection survives the real socket/session boundary; no provider calls."""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from safent_ads.accounts.application.begin_oauth_connect import BeginOAuthConnect
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.presentation.payloads import BeginConnectRequest
from safent_ads.accounts.testing.in_memory_repositories import InMemoryOAuthConnectSessionRepository
from safent_ads.broker.application.oauth_connect_flow import BeginResult, OAuthConnectFlow
from safent_ads.broker.application.ports import ConnectSessionRecord
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import (
    EncryptedCredentialStore,
    _connect_session_from_json,
    _to_json,
)
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.request_schemas import OAuthBeginRequest
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode, UuidIdGenerator

NOW = datetime(2026, 9, 13, tzinfo=UTC)
KEY = base64.b64encode(b"m" * 32).decode()


@pytest.mark.parametrize("value", ["1234567890", "123-456-7890", " 123-456-7890 "])
def test_customer_selection_normalizes_at_both_request_boundaries(value):
    assert normalize_google_customer_id(value, provider=PlatformCode.GOOGLE) == "1234567890"
    assert BeginConnectRequest(google_customer_id=value).google_customer_id == "1234567890"
    request = OAuthBeginRequest(
        op="oauth_begin",
        provider=PlatformCode.GOOGLE,
        business_id="b",
        redirect_uri="https://example.test/cb",
        google_customer_id=value,
    )
    assert request.google_customer_id == "1234567890"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "123",
        "12345678901",
        "123x4567890",
        "１２３４５６７８９０",
        "123-45-67890",
        "1234567890/ads",
        1234567890,
        True,
    ],
)
def test_invalid_selection_cannot_cross_input_boundaries(value):
    with pytest.raises(ValidationError):
        BeginConnectRequest(google_customer_id=value)
    with pytest.raises(ValidationError):
        OAuthBeginRequest(
            op="oauth_begin",
            provider="google",
            business_id="b",
            redirect_uri="https://example.test/cb",
            google_customer_id=value,
        )


def test_meta_selection_and_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        OAuthBeginRequest(
            op="oauth_begin",
            provider="meta",
            business_id="b",
            redirect_uri="https://example.test/cb",
            google_customer_id="1234567890",
        )
    with pytest.raises(ValidationError):
        BeginConnectRequest(google_customer_id="1234567890", connected_account_id="ca_untrusted")
    assert BeginConnectRequest().google_customer_id is None
    assert normalize_google_customer_id(None, provider=PlatformCode.META) is None


@pytest.mark.parametrize("selection", [None, "123-456-7890"])
async def test_use_case_real_socket_and_native_session_round_trip(tmp_path, selection):
    encrypted = EncryptedCredentialStore(tmp_path / "credentials", KEY)
    google = Mock()
    google.authorization_url.return_value = "https://accounts.google.com/synthetic-auth"
    flow = OAuthConnectFlow(encrypted, google, Mock(), FixedClock(NOW))
    runtime = BrokerRuntime(
        adapters=PlatformAdapterRegistry({}), oauth_flow=flow, app_credentials=Mock()
    )
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    try:
        sessions = InMemoryOAuthConnectSessionRepository()
        use_case = BeginOAuthConnect(
            OAuthBrokerSocketClient(socket_path), sessions, UuidIdGenerator()
        )
        business, owner = BusinessId.new(), uuid4()
        result = await use_case.execute(
            provider=PlatformCode.GOOGLE,
            business_id=business,
            owner_id=owner,
            redirect_uri="https://example.test/cb",
            google_customer_id=selection,
        )
        api_session = await sessions.get_by_id(result.session_id)
        record = encrypted.pop_connect_session(api_session.state_hash)
        assert record.google_customer_id == ("1234567890" if selection else None)
        assert record.owner_id == str(owner) and record.business_id == str(business)
        assert record.connection_id == str(api_session.connection_id)
        google.authorization_url.assert_called_once()
    finally:
        server.close()
        await server.wait_closed()


async def test_normalized_selection_reaches_managed_service_with_existing_owner_scope(tmp_path):
    expected = BeginResult(
        "https://example.test/link", "synthetic-state", NOW + timedelta(minutes=10), str(uuid4())
    )
    managed = SimpleNamespace(
        configured=lambda _: True, required=True, begin=AsyncMock(return_value=expected)
    )
    flow = OAuthConnectFlow(
        EncryptedCredentialStore(tmp_path / "credentials", KEY),
        Mock(),
        Mock(),
        FixedClock(NOW),
        managed=managed,
    )
    owner, business = str(uuid4()), str(uuid4())
    assert (
        await flow.begin(
            provider=PlatformCode.GOOGLE,
            business_id=business,
            redirect_uri="https://example.test/cb",
            owner_id=owner,
            google_customer_id="123-456-7890",
        )
        is expected
    )
    managed.begin.assert_awaited_once_with(
        provider=PlatformCode.GOOGLE,
        business_id=business,
        redirect_uri="https://example.test/cb",
        owner_id=owner,
        google_customer_id="1234567890",
    )


def test_encrypted_session_retains_selection_and_old_sessions_remain_readable(tmp_path):
    store = EncryptedCredentialStore(tmp_path / "credentials", KEY)
    record = ConnectSessionRecord(
        provider=PlatformCode.GOOGLE,
        business_id=str(uuid4()),
        redirect_uri="https://example.test/cb",
        pkce_verifier=None,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        google_customer_id="123-456-7890",
    )
    store.save_connect_session("synthetic-state-hash", record)
    assert record.google_customer_id == "1234567890"
    assert all(
        b"1234567890" not in path.read_bytes() for path in (tmp_path / "credentials").rglob("*.enc")
    )
    assert store.pop_connect_session("synthetic-state-hash") == record
    legacy = json.loads(_to_json(record))
    legacy.pop("google_customer_id")
    assert _connect_session_from_json(legacy).google_customer_id is None
    legacy["google_customer_id"] = "1234567890"
    legacy["provider"] = "meta"
    with pytest.raises(ValueError, match="google_customer_id_only_google"):
        _connect_session_from_json(legacy)


async def test_meta_id_is_rejected_before_client_or_managed_service_io(tmp_path):
    client = OAuthBrokerSocketClient(tmp_path / "nonexistent.sock")
    with pytest.raises(ValueError, match="google_customer_id_only_google"):
        await client.begin(
            PlatformCode.META,
            BusinessId.new(),
            "https://example.test/cb",
            google_customer_id="1234567890",
        )
    managed = SimpleNamespace(configured=lambda _: True, required=True, begin=AsyncMock())
    flow = OAuthConnectFlow(
        EncryptedCredentialStore(tmp_path / "credentials", KEY),
        Mock(),
        Mock(),
        FixedClock(NOW),
        managed=managed,
    )
    with pytest.raises(ValueError, match="google_customer_id_only_google"):
        await flow.begin(
            provider=PlatformCode.META,
            business_id=str(uuid4()),
            redirect_uri="https://example.test/cb",
            google_customer_id="1234567890",
        )
    managed.begin.assert_not_awaited()
