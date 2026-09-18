import base64
import json

import pytest

from safent_ads.mcp.domain.google_tag_manager_change import (
    GoogleTagManagerChangeError,
    parse_google_tag_manager_change,
)


def _body(value: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def test_create_tag_decodes_and_validates_body() -> None:
    change = parse_google_tag_manager_change(
        {
            "action": "create_tag",
            "parent_path": "accounts/1/containers/2/workspaces/3",
            "body_encoded": _body(
                {
                    "name": "GA4 - generate_lead",
                    "type": "gaawe",
                    "parameter": [{"key": "eventName", "value": "generate_lead"}],
                }
            ),
        }
    )

    assert change["body"]["name"] == "GA4 - generate_lead"


def test_publish_version_is_separate_and_accepts_fingerprint() -> None:
    change = parse_google_tag_manager_change(
        {
            "action": "publish_version",
            "resource_path": "accounts/1/containers/2/versions/7",
            "fingerprint": "abc123",
        }
    )

    assert change["action"] == "publish_version"
    assert change["body"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "publish_version", "resource_path": "https://evil.test/x"},
        {
            "action": "create_tag",
            "parent_path": "accounts/1/containers/2/workspaces/3",
            "body_encoded": "not-base64",
        },
        {
            "action": "create_tag",
            "parent_path": "accounts/1/containers/2/workspaces/3",
            "body_encoded": _body({"access_token": "secret"}),
        },
        {
            "action": "update_tag",
            "resource_path": "accounts/1/containers/2/workspaces/3/tags/4",
            "body_encoded": _body({"path": "accounts/other"}),
        },
    ],
)
def test_rejects_untrusted_or_overposted_changes(payload: dict[str, object]) -> None:
    with pytest.raises(GoogleTagManagerChangeError):
        parse_google_tag_manager_change(payload)
