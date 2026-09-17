"""Central composition never exposes the local-owner or unscoped tool surface."""

import base64
import json
from dataclasses import replace

import httpx
import pytest

from safent_ads.composition.app import create_app
from safent_ads.composition.managed_app import create_managed_app
from safent_ads.composition.managed_service import TOOL_MODELS
from safent_ads.iam.application.managed_token_locator import grant_locator
from safent_ads.iam.infrastructure.enterprise_human_approval import EnterpriseHumanApproval
from safent_ads.shared.ids import PlatformCode
from tests.unit.composition.factories import build_api_settings
from tests.unit.iam.test_enterprise_ads_authority import NOW, ORG, SECRET, TRUST, response_body


def grant_token():
    claims = {
        **response_body()["principal"],
        "v": 1,
        "purpose": "ads_account_delegation",
        "aud": "safent-ads-central",
        "iat": NOW,
        "exp": NOW + 100,
        "jti": "synthetic",
    }
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + "ab" * 64


def settings():
    return build_api_settings(
        managed_central=True,
        enterprise_origin=TRUST.origin,
        enterprise_service_secret=SECRET,
        enterprise_org_ids=frozenset({ORG}),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"enterprise_origin": "http://invalid"},
        {"enterprise_service_secret": "short"},
        {"enterprise_org_ids": frozenset()},
    ],
)
def test_central_requires_server_trust(overrides):
    values = settings().model_dump()
    values.update(overrides)
    with pytest.raises(ValueError):
        build_api_settings(**values)


async def test_real_composition_excludes_local_owner_and_public_tools():
    app = create_app(settings())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://ads.test.ts.net"
        ) as client:
            for path in (
                "/auth/login",
                "/auth/exchange",
                "/api/v1/proposals",
                "/api/v1/accounts",
                "/",
                "/docs",
            ):
                result = await client.get(path)
                assert result.status_code == 404
            result = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                headers={"Authorization": "Bearer test-mcp-token-abc123"},
            )
            assert result.status_code == 403
            result = await client.post(
                "/api/v1/managed/human-approval", json={"assertion": "sensitive-bad-proof"}
            )
            assert result.status_code == 403 and "sensitive-bad-proof" not in result.text
            result = await client.post("/api/v1/managed/human-approval", content=b"a" * 32769)
            assert result.status_code == 413 and result.headers["cache-control"] == "no-store"
        assert not any(
            name.startswith(("execute", "approve", "apply", "set")) for name in TOOL_MODELS
        )
    finally:
        await app.state.container.aclose()


async def test_mcp_catalog_requires_fresh_introspection_and_revocation_denies():
    app = create_app(settings())
    container = app.state.container
    await container.managed_human_authority.aclose()
    state = {"revoked": False, "requests": 0}

    def response(request):
        state["requests"] += 1
        return httpx.Response(403 if state["revoked"] else 200, json=response_body())

    authority = EnterpriseHumanApproval(
        TRUST, transport=httpx.MockTransport(response), clock=lambda: NOW
    )
    # Rebuild the actual isolated app using this server-owned transport only.
    container.managed_human_authority = authority
    app = create_managed_app(container)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="https://ads.test.ts.net"
            ) as client:
                headers = {
                    "Authorization": "Bearer " + grant_token(),
                    "Accept": "application/json, text/event-stream",
                }
                result = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                assert result.status_code == 200, result.text
                assert "mcp-session-id" not in result.headers
                catalog = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                assert catalog.status_code == 200, catalog.text
                assert {tool["name"] for tool in catalog.json()["result"]["tools"]} == set(
                    TOOL_MODELS
                )
                state["revoked"] = True
                headers["Mcp-Session-Id"] = result.headers.get("mcp-session-id", "")
                result = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                assert result.status_code == 403
                assert state["requests"] == 3
    finally:
        await authority.aclose()


def test_locator_does_not_translate_meta_claims_into_google_or_another_account():
    binding = grant_locator(grant_token())
    assert binding.account.external_account_id == "1234567890"
    assert binding.provider_account == binding.account
    meta = replace(binding, account=replace(binding.account, platform=PlatformCode.META))
    assert meta.provider_account.external_account_id == "act_1234567890"
