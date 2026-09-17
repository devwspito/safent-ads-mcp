"""`google_asset_upload.py` / `LiveGoogleAssetUploadClient.mutate_image_asset`
(threat-model.md #17) died on the hosted (Composio) transport with
`composio_google_service_not_supported` because `AssetService` was missing
from `_GOOGLE_SERVICES`: no image asset could be uploaded for a Composio-
connected Google account. Mirrors
`test_composio_keyword_ideas_facade.py`."""

from __future__ import annotations

import pytest

from safent_ads.broker.platforms.composio_sdk_clients import _GoogleSdkFacade
from safent_ads.broker.platforms.composio_transport import ComposioTransportError
from safent_ads.shared.ids import PlatformCode

_ACCOUNT = "1000000001"


class _FakeTransport:
    def __init__(self, response: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response

    def request(self, platform, account, *, endpoint, method, body=None, query=None):
        self.calls.append(
            {
                "platform": platform,
                "account": account,
                "endpoint": endpoint,
                "method": method,
                "body": body,
                "query": query,
            }
        )
        return self._response


def _asset_operation(sdk: _GoogleSdkFacade) -> object:
    operation = sdk.get_type("AssetOperation")
    asset = operation.create
    asset.name = "checksum-abc"
    asset.image_asset.data = b"\x89PNG"
    asset.image_asset.mime_type = "IMAGE_PNG"
    asset.image_asset.full_size.width_pixels = 300
    asset.image_asset.full_size.height_pixels = 200
    return operation


def test_mutate_assets_builds_endpoint_and_parses_created_resource_name() -> None:
    transport = _FakeTransport({"results": [{"resourceName": f"customers/{_ACCOUNT}/assets/999"}]})
    sdk = _GoogleSdkFacade(transport, _ACCOUNT)

    response = sdk.get_service("AssetService").mutate_assets(
        customer_id=_ACCOUNT, operations=[_asset_operation(sdk)]
    )

    call = transport.calls[0]
    assert call["platform"] is PlatformCode.GOOGLE
    assert call["account"] == _ACCOUNT
    assert call["endpoint"] == f"/v25/customers/{_ACCOUNT}/assets:mutate"
    assert call["method"] == "POST"
    body = call["body"]
    assert "customerId" not in body
    operation = body["operations"][0]["create"]
    assert operation["imageAsset"]["mimeType"] == "IMAGE_PNG"
    assert operation["imageAsset"]["fullSize"] == {"widthPixels": "300", "heightPixels": "200"}
    assert str(response.results[0].resource_name) == f"customers/{_ACCOUNT}/assets/999"


def test_mutate_assets_rejects_another_customer() -> None:
    transport = _FakeTransport({"results": []})
    sdk = _GoogleSdkFacade(transport, _ACCOUNT)

    with pytest.raises(ComposioTransportError, match="customer_mismatch"):
        sdk.get_service("AssetService").mutate_assets(
            customer_id="999", operations=[_asset_operation(sdk)]
        )
    assert transport.calls == []
