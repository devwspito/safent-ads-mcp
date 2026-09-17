"""Provider refs enter introspection once; numeric signed EE claims never change."""

import json
from dataclasses import replace

import httpx
import pytest

from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied
from safent_ads.shared.ids import PlatformCode
from tests.unit.iam.test_enterprise_ads_authority import ACCOUNT, admit, client, response_body


async def test_meta_native_account_roundtrip_and_fresh_binding_request_are_numeric():
    requests = []
    body = response_body()
    body["principal"]["platform"] = "meta"

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=body if len(requests) == 1 else {"active": True, "principal": body["principal"]},
        )

    authority = client(handler)
    native = replace(ACCOUNT, platform=PlatformCode.META, external_account_id="act_1234567890")
    try:
        admitted = await admit(authority, account=native)
        assert admitted.binding.provider_account == native
        assert admitted.binding.account.external_account_id == "1234567890"
        assert await authority.admit_binding(admitted.binding) == admitted.binding
        assert requests[0]["external_account_id"] == "1234567890"
        assert requests[1]["binding"] == body["principal"]
    finally:
        await authority.aclose()


@pytest.mark.parametrize("raw", ["1234567890", "act_act_1234567890", "act_1234567890/1"])
async def test_wrong_provider_form_is_rejected_without_network_or_fallback(raw):
    called = []
    authority = client(called.append)
    try:
        with pytest.raises(ManagedAdsDenied):
            await admit(
                authority,
                account=replace(ACCOUNT, platform=PlatformCode.META, external_account_id=raw),
            )
        assert called == []
    finally:
        await authority.aclose()
