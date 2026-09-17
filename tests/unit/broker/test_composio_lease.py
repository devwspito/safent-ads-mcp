"""Real signatures/encryption, local metadata and fake provider I/O only."""

import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.connection_scope import connection_scope
from safent_ads.broker.application.errors import (
    AppCredentialsNotConfiguredError,
    OAuthProviderDeniedError,
)
from safent_ads.broker.application.managed_oauth_connect import ManagedOAuthConnectService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.composio_lease import ComposioLeaseError, ComposioLeaseStore
from safent_ads.broker.infrastructure.dynamic_platform_adapters import (
    DynamicPlatformAdapterRegistry,
)
from safent_ads.broker.platforms.composio_transport import ComposioAdsTransport
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.presentation.dispatcher import handle_payload
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode
from tests.unit.broker.platforms.test_composio_transport import (
    SCOPE,
    metadata,
    request,
    setup_store,
)

NOW = 1800000000
MASTER = base64.b64encode(b"m" * 32).decode()


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def claims(revision=1, **overrides):
    result = {
        "v": 1,
        "iss": "safent-runtime",
        "aud": "safent-ads-broker",
        "purpose": "composio-config",
        "sub": "install-one",
        "iat": NOW,
        "exp": NOW + 90,
        "jti": str(uuid4()),
        "revision": revision,
        "config": {
            "enabled": True,
            "api_key": "synthetic-private-composio",
            "entity_id": "instance-one",
            "auth_config_ids": {"googleads": "ac_google", "metaads": "ac_meta"},
        },
    }
    result.update(overrides)
    return result


def seal(store, signer, payload, *, aad=b"safent-ads-broker:composio-config:v1"):
    ephemeral = X25519PrivateKey.generate()
    recipient = X25519PublicKey.from_public_bytes(base64.b64decode(store.channel()["public_key"]))
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"safent-composio-lease-v1"
    ).derive(ephemeral.exchange(recipient))
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    token = f"{b64(raw)}.{b64(signer.sign(raw))}".encode()
    nonce = os.urandom(12)
    outer = {
        "v": 1,
        "ephemeral_public_key": b64(public(ephemeral)),
        "nonce": b64(nonce),
        "ciphertext": b64(AESGCM(key).encrypt(nonce, token, aad)),
    }
    return b64(json.dumps(outer).encode())


@pytest.fixture
def lease(tmp_path):
    signer, now = Ed25519PrivateKey.generate(), [float(NOW)]
    store = ComposioLeaseStore(
        master_key_b64=MASTER,
        issuer_public_key=b64(public(signer)),
        metadata_path=tmp_path / "state.sqlite3",
        clock=lambda: now[0],
        monotonic=lambda: now[0],
    )
    return store, signer, now


def test_rotation_expiration_and_restart_keep_only_replay_metadata(lease, tmp_path):
    store, signer, now = lease
    assert not store.current_config().api_key
    first = claims()
    envelope = seal(store, signer, first)
    store.accept(envelope)
    assert store.current_config().api_key == first["config"]["api_key"]
    assert first["config"]["api_key"] not in repr(store.current_config())
    assert all(
        first["config"]["api_key"].encode() not in p.read_bytes() for p in tmp_path.iterdir()
    )
    with pytest.raises(ComposioLeaseError):
        store.accept(envelope)
    second = claims(2)
    second["config"]["api_key"] = "synthetic-rotated-private-key"
    store.accept(seal(store, signer, second))
    assert store.current_config().api_key == second["config"]["api_key"]
    restarted = ComposioLeaseStore(
        master_key_b64=MASTER,
        issuer_public_key=b64(public(signer)),
        metadata_path=tmp_path / "state.sqlite3",
        clock=lambda: now[0],
        monotonic=lambda: now[0],
    )
    assert restarted.channel() == store.channel()
    assert not restarted.current_config().api_key
    with pytest.raises(ComposioLeaseError):
        restarted.accept(envelope)
    now[0] += 90
    assert not store.current_config().api_key


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "other"},
        {"aud": "owner-session"},
        {"purpose": "login"},
        {"v": True},
        {"exp": NOW},
        {"exp": NOW + 91},
        {"iat": NOW + 6},
        {"revision": True},
        {"revision": -1},
        {"revision": 2**63},
        {"sub": ""},
        {"jti": "not-a-uuid"},
        {"config": {"enabled": True}},
    ],
)
def test_invalid_claims_never_replace_valid_config(lease, override):
    store, signer, _ = lease
    store.accept(seal(store, signer, claims()))
    before = store.current_config()
    invalid = claims(2)
    invalid.update(override)
    with pytest.raises(ComposioLeaseError, match="^composio_lease_invalid$"):
        store.accept(seal(store, signer, invalid))
    assert store.current_config() == before


def test_wrong_signing_key_recipient_aad_and_subject_are_denied(lease, tmp_path):
    store, signer, _ = lease
    store.accept(seal(store, signer, claims()))
    bad = [
        seal(store, Ed25519PrivateKey.generate(), claims(2)),
        seal(store, signer, claims(2), aad=b"other"),
        seal(store, signer, claims(2, sub="other-install")),
    ]
    other = ComposioLeaseStore(
        master_key_b64=base64.b64encode(b"z" * 32).decode(),
        issuer_public_key=b64(public(signer)),
        metadata_path=tmp_path / "other.sqlite3",
        clock=lambda: NOW,
    )
    bad.append(seal(other, signer, claims(2)))
    for envelope in bad:
        with pytest.raises(ComposioLeaseError):
            store.accept(envelope)


def test_same_jti_cannot_be_reissued_with_new_revision(lease):
    store, signer, _ = lease
    first = claims()
    store.accept(seal(store, signer, first))
    with pytest.raises(ComposioLeaseError):
        store.accept(seal(store, signer, claims(2, jti=first["jti"])))


@pytest.mark.asyncio
async def test_readiness_activates_live_and_disabled_cannot_fall_back(lease):
    store, signer, now = lease
    clock = FixedClock(datetime.fromtimestamp(NOW, UTC))
    credential_store, http = Mock(), Mock()
    managed = ManagedOAuthConnectService(
        store.current_config, http, credential_store, clock, required=True
    )
    app = AppCredentialsService(
        credential_store,
        clock,
        managed_platforms=lambda: frozenset(p for p in PlatformCode if managed.configured(p)),
        managed_required=True,
    )
    flow = OAuthConnectFlow(credential_store, Mock(), Mock(), clock, managed=managed)
    assert not app.status(PlatformCode.GOOGLE).configured
    store.accept(seal(store, signer, claims()))
    assert app.status(PlatformCode.GOOGLE).configured
    disabled = claims(2)
    disabled["config"].update(enabled=False, api_key="", auth_config_ids={})
    store.accept(seal(store, signer, disabled))
    assert not app.status(PlatformCode.GOOGLE).configured
    with pytest.raises(OAuthProviderDeniedError, match="managed_configuration_missing"):
        await flow.begin(
            provider=PlatformCode.GOOGLE,
            business_id=str(uuid4()),
            owner_id=str(uuid4()),
            redirect_uri="https://example.test/callback",
        )
    http.get_json.assert_not_called()
    credential_store.get_google_app_credentials.assert_not_called()
    store.accept(seal(store, signer, claims(3)))
    now[0] += 90
    with pytest.raises(OAuthProviderDeniedError):
        await flow.begin(
            provider=PlatformCode.GOOGLE,
            business_id=str(uuid4()),
            owner_id=str(uuid4()),
            redirect_uri="https://example.test/callback",
        )


def test_config_changes_do_not_reset_cached_adapter_budget(lease):
    store, signer, _ = lease
    credentials = Mock()
    credentials.get_google_app_credentials.return_value = None
    credentials.get_meta_app_credentials.return_value = None
    factory = Mock(side_effect=lambda _: SimpleNamespace(spent=0))
    registry = DynamicPlatformAdapterRegistry(
        store=credentials,
        google_factory=factory,
        meta_factory=factory,
        google_fallback=None,
        meta_fallback=None,
        google_egress_allowed=False,
        meta_egress_allowed=False,
        managed_transport_enabled=lambda: bool(store.current_config().api_key),
    )
    with pytest.raises(AppCredentialsNotConfiguredError):
        registry[PlatformCode.GOOGLE]
    store.accept(seal(store, signer, claims()))
    adapter = registry[PlatformCode.GOOGLE]
    adapter.spent = 9
    rotated = claims(2)
    rotated["config"]["api_key"] = "synthetic-rotation"
    store.accept(seal(store, signer, rotated))
    assert registry[PlatformCode.GOOGLE] is adapter
    disabled = claims(3)
    disabled["config"]["enabled"] = False
    store.accept(seal(store, signer, disabled))
    with pytest.raises(AppCredentialsNotConfiguredError):
        registry[PlatformCode.GOOGLE]
    store.accept(seal(store, signer, claims(4)))
    assert registry[PlatformCode.GOOGLE] is adapter
    assert adapter.spent == 9 and factory.call_count == 1


@pytest.mark.asyncio
async def test_broker_relay_returns_no_configuration(lease):
    store, signer, _ = lease
    runtime = SimpleNamespace(composio_lease=store)
    channel = json.loads(await handle_payload(b'{"op":"composio_channel"}', runtime))
    assert channel == {"ok": True, "result": store.channel()}
    envelope = seal(store, signer, claims())
    response = await handle_payload(
        json.dumps({"op": "composio_lease", "envelope": envelope}).encode(), runtime
    )
    assert json.loads(response) == {"ok": True, "result": {"accepted": True}}
    invalid = json.loads(await handle_payload(b'{"op":"composio_lease","envelope":"bad"}', runtime))
    assert invalid["error_code"] == "COMPOSIO_LEASE_DENIED"


def test_transport_rotates_live_and_disabled_or_revoked_never_dispatch(lease, tmp_path):
    store, signer, _ = lease
    encrypted, reader, ref, record = setup_store(tmp_path / "credentials")
    calls = []

    def respond(outbound):
        calls.append(outbound.headers["x-api-key"])
        return httpx.Response(
            200, json=metadata() if outbound.method == "GET" else {"status": 200, "data": []}
        )

    transport = ComposioAdsTransport(
        credential_store=reader,
        api_key_source=lambda: store.current_config().api_key,
        http_transport=httpx.MockTransport(respond),
    )
    with connection_scope(SCOPE):
        store.accept(seal(store, signer, claims()))
        assert request(transport) == []
        rotated = claims(2)
        rotated["config"]["api_key"] = "synthetic-new-key"
        store.accept(seal(store, signer, rotated))
        assert request(transport) == []
        assert calls == ["synthetic-private-composio"] * 2 + ["synthetic-new-key"] * 2
        disabled = claims(3)
        disabled["config"]["enabled"] = False
        store.accept(seal(store, signer, disabled))
        with pytest.raises(CredentialNotConnectedError):
            request(transport)
        store.accept(seal(store, signer, claims(4)))
        encrypted.save_credential(ref, replace(record, revoked_at=record.obtained_at))
        with pytest.raises(CredentialNotConnectedError):
            request(transport)
        assert len(calls) == 4
