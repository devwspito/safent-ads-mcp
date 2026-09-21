"""Client-independent states and transport PATCH regression."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.presentation.mount import CALLER_SCOPE_STATE_ATTR, _build_wrapper
from safent_ads.workspaces.contracts import WorkspaceBrief, campaign_step
from safent_ads.workspaces.presentation import CampaignArgs, SaveWorkspaceArgs


def test_resources_reject_duplicate_keys_and_unsafe_urls():
    with pytest.raises(ValidationError):
        WorkspaceBrief.model_validate(
            {
                "resources": [
                    {"key": "a", "kind": "video", "title": "a", "url": "javascript:alert(1)"}
                ]
            }
        )
    with pytest.raises(ValidationError):
        WorkspaceBrief.model_validate(
            {"resources": [{"key": "a", "kind": "video", "title": "a"}] * 2}
        )


@pytest.mark.parametrize(
    "model,fields,missing",
    [
        (SaveWorkspaceArgs, {"workspace_key": "launch", "changes": {"notes": None}}, "title"),
        (
            CampaignArgs,
            {
                "workspace_id": "00000000-0000-0000-0000-000000000002",
                "draft_key": "a",
                "changes": {"notes": None},
            },
            "platform",
        ),
    ],
)
async def test_sdk_wrapper_preserves_nested_omission_and_explicit_null(model, fields, missing):
    scope = CallerScope("person:test", None, Permission.PROPOSE, "test")
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(state=SimpleNamespace(**{CALLER_SCOPE_STATE_ATTR: scope}))
        )
    )
    dispatcher = SimpleNamespace(dispatch=AsyncMock(return_value={"ok": True}))
    args = model.model_validate({"business_id": "00000000-0000-0000-0000-000000000001", **fields})
    await _build_wrapper("propose_workspace", model, dispatcher)(args, context)
    passed = dispatcher.dispatch.call_args.args[1]
    assert passed["changes"] == {"notes": None}
    assert missing not in passed["changes"]


@pytest.mark.parametrize(
    "proposal,execution,state",
    [
        (None, None, "ready"),
        ({"state": "pending"}, None, "approval"),
        ({"state": "scheduled"}, None, "executing"),
        ({"state": "executed"}, None, "attention"),
        ({"state": "executed"}, {"outcome": "SUCCEEDED"}, "created"),
        ({"state": "failed"}, {"outcome": "FAILED"}, "attention"),
    ],
)
def test_evidence_controls_state_not_model_claims(proposal, execution, state):
    step = campaign_step({"missing_fields": []}, proposal, execution)
    assert step["state"] == state
    assert step["authorizes_spend"] is False
