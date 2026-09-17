"""Exact one-shot consent HTTP contract, no redirects/retry/identity fallback."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.iam.infrastructure.enterprise_ads_authority import EnterpriseAdsTrust
from safent_ads.iam.infrastructure.enterprise_human_approval import EnterpriseHumanApproval
from tests.unit.proposals.test_managed_binding import binding

NOW = 1_800_000_000
SECRET = "ab" * 32  # synthetic service credential
PROPOSAL = uuid4()
TOKEN = "synthetic-human-assertion"  # noqa: S105


def body():
    b = binding()
    return {
        "active": True,
        "principal": b.as_claims(),
        "approved_by": str(b.user_id),
        "proposal_id": str(PROPOSAL),
        "diff_hash": "a" * 64,
        "intent_id": str(uuid4()),
        "expires_at": NOW + 100,
        "audience": "safent-ads-central",
    }


def client(handler):
    return EnterpriseHumanApproval(
        EnterpriseAdsTrust("https://enterprise.invalid", SECRET, frozenset({binding().org_id})),
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )


async def consume(authority):
    return await authority.consume(
        TOKEN, binding=binding(), proposal_id=PROPOSAL, diff_hash="a" * 64
    )


async def test_exact_consume_no_cache_no_cookie_and_second_denial_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json=body(), headers={"Set-Cookie": "owner=not-authority"})
            if len(calls) == 1
            else httpx.Response(409, text=TOKEN)
        )

    authority = client(handler)
    try:
        proof = await consume(authority)
        assert proof.user_id == binding().user_id
        with pytest.raises(ManagedAdsDenied):
            await consume(authority)
        assert len(calls) == 2
        for req in calls:
            assert req.url.path == "/internal/ads/human-approval/consume"
            assert req.headers["authorization"] == f"Bearer {SECRET}"
            assert not req.headers.get("cookie")
            assert json.loads(req.content) == {
                "assertion": TOKEN,
                "binding": binding().as_claims(),
                "proposal_id": str(PROPOSAL),
                "diff_hash": "a" * 64,
            }
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("active", False),
        ("active", "true"),
        ("audience", "wrong"),
        ("approved_by", str(uuid4())),
        ("proposal_id", str(uuid4())),
        ("diff_hash", "b" * 64),
        ("intent_id", "bad"),
        ("expires_at", NOW),
        ("expires_at", NOW + 121),
        ("expires_at", True),
        ("extra", "sensitive"),
    ],
)
async def test_response_closed_and_exact(field, value):
    payload = body() | {field: value}
    authority = client(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises((ManagedAdsDenied, ManagedAdsUnavailable)):
            await consume(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize(
    "field",
    ["org_id", "user_id", "instance_id", "connection_id", "external_account_id", "revision"],
)
async def test_wrong_principal_never_confirms(field):
    payload = deepcopy(body())
    payload["principal"][field] = (
        2 if field == "revision" else "999" if field == "external_account_id" else str(uuid4())
    )
    authority = client(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises((ManagedAdsDenied, ManagedAdsUnavailable)):
            await consume(authority)
    finally:
        await authority.aclose()


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 409, 500, 503])
async def test_failed_consumption_never_retries_or_echoes_secrets(status):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            status, text=TOKEN + SECRET, headers={"Location": "https://attacker.invalid"}
        )

    authority = client(handler)
    try:
        with pytest.raises((ManagedAdsDenied, ManagedAdsUnavailable)) as exc:
            await consume(authority)
        assert TOKEN not in str(exc.value) and SECRET not in str(exc.value)
        assert len(requests) == 1
    finally:
        await authority.aclose()


async def test_registration_exact_snapshot_and_generated_operation_id():
    snapshot = {
        "proposal_id": str(PROPOSAL),
        "diff_hash": "a" * 64,
        "diff": {"managed_binding": binding().as_claims()},
    }
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "intent_id": str(uuid4()),
                "proposal_id": str(PROPOSAL),
                "expires_at": datetime.fromtimestamp(NOW + 900, UTC).isoformat(),
                "confirmation_required": True,
            },
        )

    authority = client(handler)
    try:
        await authority.register(snapshot)
        await authority.register(snapshot)
        assert requests[0]["snapshot"] == snapshot
        assert requests[0]["registration_id"] != requests[1]["registration_id"]
    finally:
        await authority.aclose()
