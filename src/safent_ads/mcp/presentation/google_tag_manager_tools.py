"""MCP tools for GTM inventory and approval-gated workspace promotion."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.google_tag_manager_port import GoogleTagManagerReadPort
from safent_ads.mcp.application.proposal_write_port import ProposalWritePort, ProposalWriteResult
from safent_ads.mcp.domain.google_tag_manager_change import (
    GTM_CHANGE_OPERATION,
    GoogleTagManagerChangeError,
    parse_google_tag_manager_change,
    validate_google_tag_manager_path,
)
from safent_ads.mcp.domain.native_write_payload import (
    NativeWritePayloadError,
    validate_native_write_payload,
)
from safent_ads.mcp.presentation.args import BusinessId, EntityRefStr, OpaqueId, ToolArgs
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]

_PERSON_CALLER_PREFIX = "person:"
_GtmResource = Literal[
    "accounts",
    "containers",
    "workspaces",
    "tags",
    "triggers",
    "variables",
    "version_headers",
]
_GtmAction = Literal[
    "create_workspace",
    "create_tag",
    "update_tag",
    "create_trigger",
    "update_trigger",
    "create_variable",
    "update_variable",
    "create_version",
    "publish_version",
]


@dataclass(frozen=True, slots=True)
class GoogleTagManagerToolServices:
    read: GoogleTagManagerReadPort
    proposals: ProposalWritePort | None = None


class GetGoogleTagManagerArgs(ToolArgs):
    business_id: BusinessId
    account_ref: OpaqueId
    resource: _GtmResource
    parent_path: Annotated[str | None, Field(default=None, max_length=300)]

    @model_validator(mode="after")
    def _validate_parent(self) -> GetGoogleTagManagerArgs:
        if self.resource == "accounts":
            if self.parent_path is not None:
                raise ValueError("accounts no admite parent_path")
            return self
        if self.parent_path is None:
            raise ValueError(f"{self.resource} requiere parent_path")
        try:
            validate_google_tag_manager_path(self.parent_path)
        except GoogleTagManagerChangeError as exc:
            raise ValueError(str(exc)) from exc
        segments = self.parent_path.split("/")
        expected_segments = 2 if self.resource == "containers" else 4
        if self.resource in {"tags", "triggers", "variables"}:
            expected_segments = 6
        if len(segments) != expected_segments:
            raise ValueError(f"parent_path no corresponde al recurso {self.resource}")
        return self


class ProposeGoogleTagManagerChangeArgs(ToolArgs):
    business_id: BusinessId
    # A live Google Ads entity is the signed drift/audit anchor. GTM itself
    # is addressed only by the validated paths below.
    entity_ref: EntityRefStr
    action: _GtmAction
    parent_path: Annotated[str | None, Field(default=None, max_length=300)]
    resource_path: Annotated[str | None, Field(default=None, max_length=300)]
    body_encoded: Annotated[str | None, Field(default=None, max_length=7000)]
    fingerprint: Annotated[str | None, Field(default=None, max_length=256)]
    why: Annotated[str, Field(min_length=40, max_length=300)]

    @model_validator(mode="after")
    def _validate_change(self) -> ProposeGoogleTagManagerChangeArgs:
        try:
            payload = self._payload()
            validate_native_write_payload(payload)
            parse_google_tag_manager_change(payload)
        except (GoogleTagManagerChangeError, NativeWritePayloadError) as exc:
            raise ValueError(str(exc)) from exc
        return self

    def _payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "action": self.action,
                "parent_path": self.parent_path,
                "resource_path": self.resource_path,
                "body_encoded": self.body_encoded,
                "fingerprint": self.fingerprint,
            }.items()
            if value is not None
        }


def build_google_tag_manager_tool_definitions(
    services: GoogleTagManagerToolServices,
) -> list[ToolDefinition[Any]]:
    definitions: list[ToolDefinition[Any]] = [
        ToolDefinition(
            name="get_google_tag_manager",
            description=(
                "Lee Google Tag Manager con la identidad OAuth de una cuenta Google ya "
                "conectada. `accounts` no usa parent_path; `containers` usa accounts/{id}; "
                "`workspaces` y `version_headers` usan accounts/{id}/containers/{id}; "
                "tags/triggers/variables usan la ruta completa del workspace. No modifica GTM."
            ),
            args_model=GetGoogleTagManagerArgs,
            tool_class=ToolClass.READ,
            handler=_read(services.read),
            business_id_of=lambda args: str(args.business_id),
        )
    ]
    if services.proposals is not None:
        definitions.append(
            ToolDefinition(
                name="propose_google_tag_manager_change",
                description=(
                    "Propone crear/actualizar una entidad de un workspace, crear una version o "
                    "publicar una version GTM. Nunca escribe al llamar: queda pendiente de "
                    "aprobacion humana. El cuerpo de Google viaja como JSON codificado en "
                    "base64 dentro de `body_encoded`; `publish_version` es una accion "
                    "separada y admite fingerprint para impedir publicar una version "
                    "que haya cambiado."
                ),
                args_model=ProposeGoogleTagManagerChangeArgs,
                tool_class=ToolClass.PROPOSAL,
                handler=_propose(services.proposals),
                business_id_of=lambda args: str(args.business_id),
            )
        )
    return definitions


def _read(port: GoogleTagManagerReadPort) -> Handler[GetGoogleTagManagerArgs, object]:
    async def handle(args: GetGoogleTagManagerArgs, _scope: CallerScope) -> object:
        return await port.read(
            str(args.business_id),
            args.account_ref,
            resource=args.resource,
            parent_path=args.parent_path,
        )

    return handle


def _propose(
    port: ProposalWritePort,
) -> Handler[ProposeGoogleTagManagerChangeArgs, ProposalWriteResult]:
    async def handle(
        args: ProposeGoogleTagManagerChangeArgs, scope: CallerScope
    ) -> ProposalWriteResult:
        proposed_by = scope.caller_id if scope.caller_id.startswith(_PERSON_CALLER_PREFIX) else None
        return await port.propose_native_write(
            business_id=str(args.business_id),
            entity_ref=args.entity_ref,
            platform="google",
            operation=GTM_CHANGE_OPERATION,
            payload=args._payload(),
            why=args.why,
            proposed_by=proposed_by,
        )

    return handle
