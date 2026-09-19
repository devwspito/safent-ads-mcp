import asyncio
import base64
import secrets

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import text
from tests.integration.composition.test_offerings_rest import (
    container as container,  # noqa: PLC0414
)
from tests.integration.composition.test_offerings_rest import (
    two_businesses as two_businesses,  # noqa: PLC0414
)
from tests.integration.composition.test_runtime_jobs import store

from safent_ads.composition.api import CsrfMiddleware, _handle_api_error
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.runtime.connections import RuntimeConnections
from safent_ads.runtime.pairing import PairingRequest, RuntimePairings, oaep
from safent_ads.runtime.rest import build_runtime_router
from safent_ads.runtime.store import RuntimeJobError, digest

pytestmark = pytest.mark.integration


def request_and_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    verifier = secrets.token_hex(32)
    return (
        PairingRequest(
            challenge=digest(verifier), public_key=public, runtime="codex", label="Codex test"
        ),
        key,
        verifier,
    )


async def test_pairing_is_atomic_encrypted_idempotent_and_revocable(container, two_businesses):
    business = str(two_businesses.business_a)
    connections = RuntimeConnections(container.session_factory)
    pairings = RuntimePairings(connections)
    request, key, verifier = request_and_key()
    assert await pairings.poll(verifier) == {"ready": False}
    approvals = await asyncio.gather(
        pairings.approve(business, request), pairings.approve(business, request)
    )
    assert approvals[0] == approvals[1]
    assert len((await connections.list(business))["items"]) == 1
    assert await pairings.poll(secrets.token_hex(32)) == {"ready": False}
    delivered = await pairings.poll(verifier)
    token = key.decrypt(base64.b64decode(delivered["sealed_token"]), oaep()).decode()
    assert (await connections.authenticate(token))[0] == business
    assert token not in str(delivered) and token not in str(approvals)
    with pytest.raises(RuntimeJobError):
        await pairings.approve(str(two_businesses.business_b), request)
    with pytest.raises(RuntimeJobError):
        await pairings.approve(business, request.model_copy(update={"runtime": "claude"}))
    await connections.revoke(business, delivered["connection_id"])
    assert await pairings.poll(verifier) == {"ready": False}
    with pytest.raises(RuntimeJobError):
        await connections.authenticate(token)


async def test_expired_delivery_never_reveals_or_rotates_access(container, two_businesses):
    pairings = RuntimePairings(RuntimeConnections(container.session_factory))
    request, _, verifier = request_and_key()
    await pairings.approve(str(two_businesses.business_a), request)
    async with container.session_factory.begin() as session:
        await session.execute(
            text("UPDATE runtime_pairings SET expires_at=now()-interval '1 second'")
        )
    assert await pairings.poll(verifier) == {"ready": False}
    with pytest.raises(RuntimeJobError):
        await pairings.approve(str(two_businesses.business_a), request)


async def test_pairing_requires_owner_csrf_and_poll_has_no_cookie_authority(
    container, two_businesses, authenticated_session
):
    business = str(two_businesses.business_a)
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_middleware(CsrfMiddleware)
    connections = RuntimeConnections(container.session_factory)
    app.include_router(build_runtime_router(store(container), connections))
    request, _, verifier = request_and_key()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/v1/runtime/pair", params={"business_id": business}, json=request.model_dump()
        )
        assert response.status_code in {401, 403}
        assert (await client.post("/runtime/v1/pair/poll", json={"verifier": verifier})).json() == {
            "ready": False
        }
        client.cookies.update(authenticated_session.cookies)
        response = await client.post(
            "/api/v1/runtime/pair", params={"business_id": business}, json=request.model_dump()
        )
        assert response.status_code == 403
        client.cookies.set("ads_csrf", "synthetic")
        response = await client.post(
            "/api/v1/runtime/pair",
            params={"business_id": business},
            json=request.model_dump(),
            headers={"X-CSRF-Token": "synthetic"},
        )
        assert response.status_code == 200, response.text
        assert "token" not in response.text
        # Owner cookies do not authorize the worker transport.
        assert (await client.post("/runtime/v1/ping")).status_code == 401
