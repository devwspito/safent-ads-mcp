import base64
import json
from datetime import UTC, datetime

from safent_ads.mcp.application.caller_scope import CallerScope, Permission
from safent_ads.mcp.application.proposal_write_port import ProposalWriteResult
from safent_ads.mcp.presentation.google_tag_manager_tools import (
    GoogleTagManagerToolServices,
    build_google_tag_manager_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolRegistry

_BUSINESS = "0d9f6e4a-3f1a-4a2c-9b7e-1a2b3c4d5e6f"
_SCOPE = "11111111-1111-1111-1111-111111111111:22222222-2222-2222-2222-222222222222:"
_ACCOUNT = f"google:account:{_SCOPE}1234567890"
_ENTITY = f"google:campaign:{_SCOPE}customers/1234567890/campaigns/7"


class _Read:
    async def read(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"args": args, "kwargs": kwargs}


class _Proposals:
    def __init__(self) -> None:
        self.call: dict[str, object] = {}

    async def propose_native_write(self, **kwargs: object) -> ProposalWriteResult:
        self.call = kwargs
        return ProposalWriteResult(
            proposal_id="p1",
            estado="pendiente",
            diff_hash="a" * 64,
            expires_at=datetime(2026, 9, 19, tzinfo=UTC),
            classification="important",
        )


def _scope() -> CallerScope:
    return CallerScope(
        caller_id="person:00000000-0000-0000-0000-000000000001",
        allowed_business_ids=frozenset({_BUSINESS}),
        permission=Permission.PROPOSE,
        person_label="Owner",
    )


async def test_read_and_proposal_tools_are_registered_with_correct_classes() -> None:
    proposals = _Proposals()
    definitions = build_google_tag_manager_tool_definitions(
        GoogleTagManagerToolServices(read=_Read(), proposals=proposals)
    )
    registry = ToolRegistry(definitions)

    assert registry.get("get_google_tag_manager").tool_class is ToolClass.READ  # type: ignore[union-attr]
    assert registry.get("propose_google_tag_manager_change").tool_class is ToolClass.PROPOSAL  # type: ignore[union-attr]


async def test_proposal_uses_google_native_write_and_keeps_publish_separate() -> None:
    proposals = _Proposals()
    definitions = {
        item.name: item
        for item in build_google_tag_manager_tool_definitions(
            GoogleTagManagerToolServices(read=_Read(), proposals=proposals)
        )
    }
    args = definitions["propose_google_tag_manager_change"].args_model(
        business_id=_BUSINESS,
        entity_ref=_ENTITY,
        action="publish_version",
        resource_path="accounts/1/containers/2/versions/7",
        fingerprint="fp-7",
        why="Publicar la version revisada para activar la medicion aprobada.",
    )

    await definitions["propose_google_tag_manager_change"].handler(args, _scope())

    assert proposals.call["platform"] == "google"
    assert proposals.call["operation"] == "gtm_change"
    assert proposals.call["payload"] == {
        "action": "publish_version",
        "resource_path": "accounts/1/containers/2/versions/7",
        "fingerprint": "fp-7",
    }


async def test_read_tool_forwards_owned_account_and_parent() -> None:
    read = _Read()
    definition = build_google_tag_manager_tool_definitions(GoogleTagManagerToolServices(read=read))[
        0
    ]
    args = definition.args_model(
        business_id=_BUSINESS,
        account_ref=_ACCOUNT,
        resource="containers",
        parent_path="accounts/1",
    )

    result = await definition.handler(args, _scope())

    assert result["args"] == (_BUSINESS, _ACCOUNT)
    assert result["kwargs"] == {"resource": "containers", "parent_path": "accounts/1"}


def test_body_encoded_allows_a_validated_custom_html_url() -> None:
    body = base64.b64encode(
        json.dumps({"name": "Meta Pixel", "type": "html", "html": "https://example.test"}).encode()
    ).decode()
    definition = build_google_tag_manager_tool_definitions(
        GoogleTagManagerToolServices(read=_Read(), proposals=_Proposals())
    )[1]

    args = definition.args_model(
        business_id=_BUSINESS,
        entity_ref=_ENTITY,
        action="create_tag",
        parent_path="accounts/1/containers/2/workspaces/3",
        body_encoded=body,
        why="Crear la etiqueta aprobada dentro del workspace de medicion.",
    )

    assert args.body_encoded == body
