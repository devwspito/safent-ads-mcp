"""Fresh queued-reference admission never renews tokens or human approval."""

import asyncio
import json
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.shared.managed_ads import ManagedAdsBinding
from tests.unit.iam.test_enterprise_ads_authority import SECRET, client, response_body


def binding():
    return ManagedAdsBinding.from_claims(response_body()["principal"])


def response():
    return {"active": True, "principal": binding().as_claims()}


async def test_exact_reference_request_and_revocation_no_positive_cache_or_token_refresh():
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(200, json=response(), headers={"Set-Cookie": "owner=not-authority"})
            if len(requests) == 1
            else httpx.Response(403, text=SECRET)
        )

    authority = client(handler)
    try:
        assert await authority.admit_binding(binding()) == binding()
        with pytest.raises(ManagedAdsDenied, match="^managed_ads_denied$"):
            await authority.admit_binding(binding())
        assert len(requests) == 2
        for request in requests:
            assert str(request.url) == "https://enterprise.invalid/internal/ads/admit-binding"
            assert json.loads(request.content) == {
                "binding": binding().as_claims(),
                "operation": "execute",
            }
            assert request.headers["authorization"] == f"Bearer {SECRET}"
            assert not request.headers.get("cookie")
            assert request.headers["accept-encoding"] == "identity"
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("grant_id", str(UUID(int=55))),
        ("revision", 2),
        ("org_id", str(UUID(int=55))),
        ("user_id", str(UUID(int=55))),
        ("employee_id", str(UUID(int=55))),
        ("instance_id", str(UUID(int=55))),
        ("business_id", str(UUID(int=55))),
        ("connection_id", str(UUID(int=55))),
        ("platform", "meta"),
        ("external_account_id", "999"),
        ("resource_revision", 4),
        ("capabilities", ["read"]),
    ],
)
async def test_every_signed_identity_dimension_must_match_exactly(field, value):
    body = response()
    body["principal"][field] = value
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(ManagedAdsDenied, match="managed_ads_scope_invalid"):
            await authority.admit_binding(binding())
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {"active": True, "principal": response_body()["principal"], "expires_at": 123},
        {
            "active": True,
            "principal": response_body()["principal"],
            "grant_token": "must-not-be-used",
        },
        {"active": True, "principal": {**response_body()["principal"], "revision": True}},
        {"active": True, "principal": {**response_body()["principal"], "role": "owner"}},
        {"active": 1, "principal": response_body()["principal"]},
    ],
)
async def test_closed_response_rejects_tokens_and_coercions(body):
    authority = client(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="managed_ads_invalid_response"):
            await authority.admit_binding(binding())
    finally:
        await authority.aclose()


async def test_outside_org_or_other_operation_does_not_send():
    calls = []
    authority = client(calls.append)
    try:
        for value, operation in (
            (replace(binding(), org_id=UUID(int=999)), "execute"),
            (binding(), "approve"),
            (None, "execute"),
            (replace(binding(), revision=2**32), "execute"),
        ):
            with pytest.raises(ManagedAdsDenied):
                await authority.admit_binding(value, operation=operation)
        assert calls == []
    finally:
        await authority.aclose()


async def test_inactive_exact_principal_is_not_authority():
    authority = client(lambda _: httpx.Response(200, json={**response(), "active": False}))
    try:
        with pytest.raises(ManagedAdsDenied, match="managed_ads_scope_invalid"):
            await authority.admit_binding(binding())
    finally:
        await authority.aclose()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 409, 429, 500, 503])
async def test_denial_redirect_and_service_failure_are_sanitized_without_retries(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET, headers={"Location": "https://attacker.invalid"})

    authority = client(handler)
    try:
        with pytest.raises((ManagedAdsDenied, ManagedAdsUnavailable)) as error:
            await authority.admit_binding(binding())
        assert SECRET not in str(error.value)
        assert len(calls) == 1
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "raw,headers",
    [
        (b"x" * 16385, {"Content-Type": "application/json"}),
        (b'{"active":false,"active":true}', {"Content-Type": "application/json"}),
        (b"{}", {"Content-Type": "text/html"}),
    ],
)
async def test_transport_limits_and_duplicate_json_keys(raw, headers):
    authority = client(lambda _: httpx.Response(200, content=raw, headers=headers))
    try:
        with pytest.raises(ManagedAdsUnavailable, match="managed_ads_invalid_response"):
            await authority.admit_binding(binding())
    finally:
        await authority.aclose()


async def test_concurrent_references_never_reuse_other_user_result():
    async def handler(request):
        body = json.loads(request.content)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"active": True, "principal": body["binding"]})

    authority = client(handler)
    other = replace(binding(), user_id=UUID(int=90), grant_id=UUID(int=91))
    try:
        results = await asyncio.gather(
            authority.admit_binding(binding()), authority.admit_binding(other)
        )
        assert results == [binding(), other]
    finally:
        await authority.aclose()
