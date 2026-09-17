from __future__ import annotations

import httpx
import pytest

from safent_ads.broker.platforms.oauth_http import (
    GoogleCloudProjectAccessError,
    HttpxOAuthHttpClient,
    OAuthHttpError,
    _parse_json,
)

_BODY = {
    "error": {
        "message": "private-provider-content",
        "details": [
            {
                "errors": [
                    {
                        "errorCode": {
                            "authorizationError": "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
                        }
                    }
                ]
            }
        ],
    }
}


async def _response(status: int, host: str, body: object) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("GET", f"https://{host}/v25/customers"), json=body
    )


async def test_structured_cloud_access_error_is_actionable_without_exposing_the_body() -> None:
    with pytest.raises(GoogleCloudProjectAccessError) as caught:
        await HttpxOAuthHttpClient()._send(_response(403, "googleads.googleapis.com", _BODY))
    assert "private-provider-content" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "host", "body"),
    [
        (401, "googleads.googleapis.com", _BODY),
        (403, "graph.facebook.com", _BODY),
        (
            403,
            "googleads.googleapis.com",
            {"error": {"message": "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"}},
        ),
        (403, "googleads.googleapis.com", ["unexpected"]),
        (403, "googleads.googleapis.com", {"error": {"details": [None, {"errors": [None]}]}}),
    ],
)
async def test_unrelated_or_malformed_errors_are_not_misclassified(
    status: int, host: str, body: object
) -> None:
    with pytest.raises(OAuthHttpError) as caught:
        await HttpxOAuthHttpClient()._send(_response(status, host, body))
    assert type(caught.value) is OAuthHttpError


def test_success_payload_must_be_a_json_object() -> None:
    with pytest.raises(OAuthHttpError):
        _parse_json(httpx.Response(200, json=[]))
