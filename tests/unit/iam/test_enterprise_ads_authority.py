"""Actual HTTPX client and closed DTO; transport only is an explicit fake."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from uuid import UUID

import httpx
import pytest

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.iam.application.managed_ads_authority import (
    MANAGED_ADS_CAPABILITIES,
    ManagedAdsDenied,
    ManagedAdsUnavailable,
)
from safent_ads.iam.infrastructure.enterprise_ads_authority import (
    EnterpriseAdsAuthority,
    EnterpriseAdsTrust,
)
from safent_ads.shared.ids import PlatformCode

NOW = 1_800_000_000
ORG = UUID(int=1)
INSTANCE = UUID(int=2)
BUSINESS = UUID(int=3)
CONNECTION = UUID(int=4)
ACCOUNT = AccountRef(PlatformCode.GOOGLE, "1234567890", BUSINESS, CONNECTION)
SECRET = "ab" * 32  # synthetic service credential, never deployed
TOKEN = "synthetic-grant-not-a-provider-token"  # noqa: S105 — opaque test fixture only
TRUST = EnterpriseAdsTrust("https://enterprise.invalid", SECRET, frozenset({ORG}))


def response_body() -> dict:
    return {
        "active": True, "expires_at": NOW + 100,
        "principal": {
            "grant_id": str(UUID(int=5)), "revision": 1, "org_id": str(ORG),
            "user_id": str(UUID(int=6)), "employee_id": str(UUID(int=7)),
            "instance_id": str(INSTANCE), "business_id": str(BUSINESS),
            "connection_id": str(CONNECTION), "platform": "google",
            "external_account_id": "1234567890", "resource_revision": 3,
            "role": "ads", "capabilities": list(MANAGED_ADS_CAPABILITIES),
        },
    }


def client(handler) -> EnterpriseAdsAuthority:
    return EnterpriseAdsAuthority(TRUST, transport=httpx.MockTransport(handler), clock=lambda: NOW)


async def admit(authority, *, token=TOKEN, account=ACCOUNT, operation="read"):
    return await authority.introspect(
        token, expected_instance_id=INSTANCE, account=account, operation=operation,
    )


async def test_exact_request_and_server_principal_no_positive_cache_or_cookies():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=response_body(), headers={"Set-Cookie": "owner=bad"})
        return httpx.Response(403, text=f"private upstream text {TOKEN} {SECRET}")

    authority = client(handler)
    try:
        result = await admit(authority)
        assert result.binding.as_claims() == response_body()["principal"]
        assert result.expires_at == NOW + 100
        assert result.operation == "read"
        with pytest.raises(ManagedAdsDenied, match="^managed_ads_denied$"):
            await admit(authority)
        assert len(requests) == 2
        for request in requests:
            assert str(request.url) == "https://enterprise.invalid/internal/ads/introspect"
            assert request.headers["authorization"] == f"Bearer {SECRET}"
            assert not request.headers.get("cookie")
            assert request.headers["accept-encoding"] == "identity"
            assert json.loads(request.content) == {
                "grant_token": TOKEN, "expected_instance_id": str(INSTANCE),
                "operation": "read", "business_id": str(BUSINESS), "platform": "google",
                "connection_id": str(CONNECTION), "external_account_id": "1234567890",
            }
        assert SECRET not in repr(TRUST)
        assert TOKEN not in repr(result)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("origin", [
    "http://enterprise.invalid", "https://user:password@enterprise.invalid",
    "https://enterprise.invalid/path", "https://enterprise.invalid?query=1",
    "https://enterprise.invalid#fragment", "//enterprise.invalid",
    "https://enterprise.invalid:0", "https://enterprise.invalid:65536",
    "https://enterprise.invalid\n", " https://enterprise.invalid", "https://",
    "https://enterprise.invalid\\attacker.invalid",
])
def test_invalid_trust_origin_rejected_without_network(origin):
    with pytest.raises(ValueError, match="^invalid Enterprise Ads trust configuration$"):
        EnterpriseAdsTrust(origin, SECRET, frozenset({ORG}))


@pytest.mark.parametrize("secret,orgs", [
    ("", frozenset({ORG})), ("ab" * 31, frozenset({ORG})),
    ("AB" * 32, frozenset({ORG})), (SECRET, frozenset()),
    (SECRET, frozenset({"*"})), (SECRET, {ORG}),
])
def test_invalid_service_identity_or_allowlist(secret, orgs):
    with pytest.raises(ValueError):
        EnterpriseAdsTrust("https://enterprise.invalid", secret, orgs)


@pytest.mark.parametrize("field,value", [
    ("org_id", str(UUID(int=20))), ("instance_id", str(UUID(int=21))),
    ("business_id", str(UUID(int=22))), ("connection_id", str(UUID(int=23))),
    ("platform", "meta"), ("external_account_id", "999"),
    ("capabilities", ["read"]), ("capabilities", ["read", "propose", "approve", "execute", "*"]),
])
async def test_scope_mismatch_never_yields_principal(field, value):
    body = response_body()
    body["principal"][field] = value
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(ManagedAdsDenied):
            await admit(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("field,value", [
    ("revision", True), ("revision", 0), ("revision", "1"),
    ("resource_revision", 0), ("grant_id", "bad"), ("role", "owner"),
    ("provider_token", "must-not-be-accepted"),
])
async def test_invalid_closed_principal_rejected(field, value):
    body = response_body()
    body["principal"][field] = value
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="^managed_ads_invalid_response$"):
            await admit(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("field,value,error", [
    ("active", False, ManagedAdsDenied), ("active", 1, ManagedAdsUnavailable),
    ("expires_at", NOW, ManagedAdsDenied), ("expires_at", NOW - 1, ManagedAdsDenied),
    ("expires_at", NOW + 121, ManagedAdsDenied),
    ("expires_at", str(NOW + 100), ManagedAdsUnavailable),
    ("expires_at", True, ManagedAdsUnavailable),
    ("unexpected", True, ManagedAdsUnavailable),
])
async def test_invalid_or_expired_envelope(field, value, error):
    body = response_body()
    body[field] = value
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(error):
            await admit(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 409, 429, 500, 503])
async def test_status_denies_without_following_or_retrying(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text=f"do not expose {SECRET} {TOKEN}",
            headers={"Location": "https://attacker.invalid/"},
        )

    authority = client(handler)
    try:
        with pytest.raises((ManagedAdsDenied, ManagedAdsUnavailable)) as captured:
            await admit(authority)
        assert len(calls) == 1
        assert SECRET not in str(captured.value)
        assert TOKEN not in str(captured.value)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("body,headers", [
    (b"x" * 16_385, {"Content-Type": "application/json"}),
    (b"not json", {"Content-Type": "application/json"}),
    (b"{}", {"Content-Type": "text/html"}),
    (b'{"active":false,"active":true}', {"Content-Type": "application/json"}),
])
async def test_invalid_response_body_is_sanitized(body, headers):
    authority = client(lambda _: httpx.Response(200, content=body, headers=headers))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="^managed_ads_invalid_response$"):
            await admit(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failure_contains_no_request_or_secret(error):
    def handler(request):
        raise error(f"private failure {TOKEN} {SECRET}", request=request)

    authority = client(handler)
    try:
        with pytest.raises(ManagedAdsUnavailable) as captured:
            await admit(authority)
        assert str(captured.value) == "managed_ads_unavailable"
        assert captured.value.__cause__ is None
        assert captured.value.__suppress_context__
    finally:
        await authority.aclose()


@pytest.mark.parametrize("token,account,operation", [
    ("", ACCOUNT, "read"), ("x" * 8193, ACCOUNT, "read"),
    ("bad\nvalue", ACCOUNT, "read"), (TOKEN, ACCOUNT, "delete"),
    (TOKEN, AccountRef(PlatformCode.GOOGLE, "123"), "read"),
    (TOKEN, AccountRef(PlatformCode.GOOGLE, "act_123", BUSINESS, CONNECTION), "read"),
])
async def test_invalid_local_scope_never_sends(token, account, operation):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response_body())

    authority = client(handler)
    try:
        with pytest.raises(ManagedAdsDenied):
            await admit(authority, token=token, account=account, operation=operation)
        assert calls == []
    finally:
        await authority.aclose()


async def test_concurrent_scopes_do_not_share_admissions():
    second = AccountRef(PlatformCode.GOOGLE, "555", BUSINESS, CONNECTION)

    async def handler(request):
        incoming = json.loads(request.content)
        await asyncio.sleep(0)
        body = deepcopy(response_body())
        body["principal"]["external_account_id"] = incoming["external_account_id"]
        return httpx.Response(200, json=body)

    authority = client(handler)
    try:
        first_result, second_result = await asyncio.gather(
            admit(authority), admit(authority, account=second),
        )
        assert first_result.binding.account == ACCOUNT
        assert second_result.binding.account == second
    finally:
        await authority.aclose()


async def test_cancellation_closes_response_and_propagates():
    started, closed = asyncio.Event(), asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    authority = client(lambda _: httpx.Response(
        200, stream=WaitingStream(), headers={"Content-Type": "application/json"},
    ))
    try:
        task = asyncio.create_task(admit(authority))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        await authority.aclose()


async def test_total_deadline_closes_a_slow_stream_without_retry():
    closed = asyncio.Event()
    calls = []

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Mock transports do not enforce HTTPX's per-read socket timeout.
            # Exercise the real, independent overall deadline without patching it.
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, stream=WaitingStream(), headers={"Content-Type": "application/json"},
        )

    authority = client(handler)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ManagedAdsUnavailable, match="^managed_ads_unavailable$"):
                await admit(authority)
        assert closed.is_set()
        assert len(calls) == 1
    finally:
        await authority.aclose()


async def test_compressed_response_rejected_before_reading_any_bytes():
    read, closed = [], []

    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(True)
            yield b"intentionally-not-a-gzip-stream"

        async def aclose(self):
            closed.append(True)

    authority = client(lambda _: httpx.Response(
        200, stream=CompressedStream(),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="^managed_ads_invalid_response$"):
            await admit(authority)
        assert not read
        assert closed == [True]
    finally:
        await authority.aclose()


@pytest.mark.parametrize("field,replacement", [
    ('"active": true', '"active": false, "active": true'),
    ('"revision": 1', '"revision": 2, "revision": 1'),
])
async def test_duplicate_keys_rejected_even_if_last_value_would_be_valid(field, replacement):
    raw = json.dumps(response_body()).replace(field, replacement, 1)
    assert json.loads(raw) == response_body()
    authority = client(lambda _: httpx.Response(
        200, content=raw, headers={"Content-Type": "application/json"},
    ))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="^managed_ads_invalid_response$"):
            await admit(authority)
    finally:
        await authority.aclose()


async def test_meta_maps_provider_reference_to_numeric_claim_with_exact_connection():
    account = AccountRef(PlatformCode.META, "act_1234567890", BUSINESS, CONNECTION)
    body = response_body()
    body["principal"]["platform"] = "meta"

    def handler(request):
        incoming = json.loads(request.content)
        assert incoming["platform"] == "meta"
        assert incoming["external_account_id"] == "1234567890"
        assert incoming["connection_id"] == str(CONNECTION)
        return httpx.Response(200, json=body)

    authority = client(handler)
    try:
        result = await admit(authority, account=account, operation="execute")
        assert result.binding.provider_account == account
        assert result.binding.account.external_account_id == "1234567890"
        assert result.operation == "execute"
    finally:
        await authority.aclose()
