"""Actual HTTPX client and closed DTO; transport only is an explicit fake
(004 tasks.md A3). Mirrors `tests/unit/iam/test_enterprise_ads_authority.py`
scope: this adapter never touches the real network in tests."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from safent_ads.iam.infrastructure.enterprise_seat_authority import (  # noqa: I001 - grouped by kind
    EnterpriseSeatAuthority,
    EnterpriseSeatTrust,
)
from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.seat_authority import (
    SeatAuthorityDeniedError,
    SeatAuthorityUnavailableError,
)

NOW = 1_800_000_000
ORG = UUID(int=1)
SECRET = "ab" * 32  # synthetic service credential, never deployed
CREDENTIAL = "sfa_" + "c" * 64
TRUST = EnterpriseSeatTrust("https://enterprise.invalid", SECRET, frozenset({ORG}))


def response_body() -> dict:
    return {
        "active": True,
        "expires_at": NOW + 60,
        "principal": {
            "org_id": str(ORG),
            "user_id": str(UUID(int=2)),
            "person_label": "Ana",
            "seat_id": str(UUID(int=3)),
            "business_id": str(UUID(int=4)),
            "permission": "propose",
            "seat_revision": 3,
        },
    }


def client(handler) -> EnterpriseSeatAuthority:
    return EnterpriseSeatAuthority(TRUST, transport=httpx.MockTransport(handler), clock=lambda: NOW)


async def test_exact_request_and_admission_from_principal():
    def handler(request):
        return httpx.Response(200, json=response_body())

    authority = client(handler)
    try:
        admission = await authority.resolve(CREDENTIAL)
        assert admission.org_id == str(ORG)
        assert admission.person_label == "Ana"
        assert admission.permission is Permission.PROPOSE
        assert admission.expires_at == NOW + 60
    finally:
        await authority.aclose()


async def test_request_carries_service_secret_never_the_credential():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response_body())

    authority = client(handler)
    try:
        await authority.resolve(CREDENTIAL)
        assert len(captured) == 1
        request = captured[0]
        assert str(request.url) == "https://enterprise.invalid/internal/ads/introspect-seat"
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert not request.headers.get("cookie")
        assert json.loads(request.content) == {"credential": CREDENTIAL}
    finally:
        await authority.aclose()


@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_denied_statuses_deny_without_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=f"do not expose {SECRET}")

    authority = client(handler)
    try:
        with pytest.raises(SeatAuthorityDeniedError):
            await authority.resolve(CREDENTIAL)
        assert len(calls) == 1
    finally:
        await authority.aclose()


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_unavailable_not_denied(status):
    authority = client(lambda _: httpx.Response(status, text="upstream failure"))
    try:
        with pytest.raises(SeatAuthorityUnavailableError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_timeout_is_unavailable_not_denied():
    def handler(request):
        raise httpx.ReadTimeout("slow upstream", request=request)

    authority = client(handler)
    try:
        with pytest.raises(SeatAuthorityUnavailableError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_invalid_json_is_unavailable():
    authority = client(
        lambda _: httpx.Response(
            200, content=b"not json", headers={"Content-Type": "application/json"}
        )
    )
    try:
        with pytest.raises(SeatAuthorityUnavailableError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_extra_field_in_principal_is_unavailable_closed_model():
    body = response_body()
    body["principal"]["unexpected"] = "surprise"
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(SeatAuthorityUnavailableError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_extra_field_at_envelope_level_is_unavailable():
    body = response_body()
    body["unexpected"] = True
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(SeatAuthorityUnavailableError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_org_id_outside_allowlist_is_denied():
    body = response_body()
    body["principal"]["org_id"] = str(UUID(int=99))
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(SeatAuthorityDeniedError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "expires_at,error",
    [
        (NOW, SeatAuthorityDeniedError),
        (NOW - 1, SeatAuthorityDeniedError),
        (NOW + 301, SeatAuthorityDeniedError),
    ],
)
async def test_admission_window_is_bounded(expires_at, error):
    body = response_body()
    body["expires_at"] = expires_at
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(error):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


async def test_active_false_is_denied():
    body = response_body()
    body["active"] = False
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(SeatAuthorityDeniedError):
            await authority.resolve(CREDENTIAL)
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "credential", ["", "not-the-right-shape", "sfa_" + "x" * 10, "sfa_" + "C" * 64]
)
async def test_malformed_credential_never_hits_the_network(credential):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response_body())

    authority = client(handler)
    try:
        with pytest.raises(SeatAuthorityDeniedError):
            await authority.resolve(credential)
        assert calls == []
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "origin",
    [
        "http://enterprise.invalid",
        "https://user:password@enterprise.invalid",
        "https://enterprise.invalid/path",
        "//enterprise.invalid",
    ],
)
def test_invalid_trust_origin_rejected_without_network(origin):
    with pytest.raises(ValueError, match="^invalid Enterprise seat trust configuration$"):
        EnterpriseSeatTrust(origin, SECRET, frozenset({ORG}))


@pytest.mark.parametrize(
    "secret,orgs",
    [("", frozenset({ORG})), ("ab" * 31, frozenset({ORG})), (SECRET, frozenset())],
)
def test_invalid_service_identity_or_allowlist(secret, orgs):
    with pytest.raises(ValueError):
        EnterpriseSeatTrust("https://enterprise.invalid", secret, orgs)
