"""Fail-closed HTTP adapter for the real Enterprise Ads introspection contract.

Unwired until the managed REST/MCP/human-session/worker boundaries are complete.
The origin, service identity and allowed organizations come from deployment
configuration, NEVER token claims, browser input or an account's OAuth metadata.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.iam.application.managed_ads_authority import (
    MANAGED_ADS_CAPABILITIES,
    ManagedAdsAdmission,
    ManagedAdsBinding,
    ManagedAdsDenied,
    ManagedAdsOperation,
    ManagedAdsUnavailable,
)
from safent_ads.shared.ids import PlatformCode
from safent_ads.shared.managed_ads import enterprise_account_ref

_MAX_BYTES = 16_384
_MAX_TOKEN_BYTES = 8192
_MAX_TTL = 120
_DEADLINE = 3.0
_DENIED_STATUSES = {401, 403, 404, 409}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _canonical_uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("canonical UUID required")
    return value


_Id = Annotated[str, AfterValidator(_canonical_uuid)]
_Revision = Annotated[int, Field(ge=1, le=2_147_483_647)]


class _Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    grant_id: _Id
    revision: _Revision
    org_id: _Id
    user_id: _Id
    employee_id: _Id
    instance_id: _Id
    business_id: _Id
    platform: Literal["google", "meta"]
    connection_id: _Id
    external_account_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    resource_revision: _Revision
    role: Literal["ads"]
    capabilities: list[str]


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    active: bool
    principal: _Principal
    expires_at: int


class _BindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    active: bool
    principal: _Principal


@dataclass(frozen=True, slots=True)
class EnterpriseAdsTrust:
    origin: str
    service_secret: str = field(repr=False)
    allowed_org_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.origin)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
                and (url.port is None or url.port > 0)
                and re.fullmatch(r"[\x21-\x7e]+", self.origin) is not None
                and "\\" not in self.origin
                and re.fullmatch(r"[a-f0-9]{64}", self.service_secret) is not None
                and isinstance(self.allowed_org_ids, frozenset)
                and bool(self.allowed_org_ids)
                and all(isinstance(org, UUID) for org in self.allowed_org_ids)
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("invalid Enterprise Ads trust configuration")


class EnterpriseAdsAuthority:
    def __init__(
        self,
        trust: EnterpriseAdsTrust,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._trust = trust
        self._clock = clock
        self._url = trust.origin.rstrip("/") + "/internal/ads/introspect"
        self._binding_url = trust.origin.rstrip("/") + "/internal/ads/admit-binding"
        # No environment proxy, redirects, retries, cookies as authority or
        # caller-supplied AsyncClient defaults. TLS verification stays enabled.
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            verify=True,
            timeout=httpx.Timeout(2.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def introspect(
        self,
        grant_token: str,
        *,
        expected_instance_id: UUID,
        operation: ManagedAdsOperation,
        account: AccountRef,
    ) -> ManagedAdsAdmission:
        if (
            not isinstance(grant_token, str)
            or not 1 <= len(grant_token) <= _MAX_TOKEN_BYTES
            or re.fullmatch(r"[\x21-\x7e]+", grant_token) is None
            or not isinstance(expected_instance_id, UUID)
            or operation not in MANAGED_ADS_CAPABILITIES
            or account.business_id is None
            or account.connection_id is None
        ):
            raise ManagedAdsDenied("managed_ads_scope_invalid")
        try:
            claim_account = enterprise_account_ref(account)
        except ValueError:
            raise ManagedAdsDenied("managed_ads_scope_invalid") from None
        body: dict[str, object] = {
            "grant_token": grant_token,
            "expected_instance_id": str(expected_instance_id),
            "operation": operation,
            "business_id": str(account.business_id),
            "platform": account.platform.value,
            "connection_id": str(account.connection_id),
            "external_account_id": claim_account.external_account_id,
        }
        raw = await self._request(self._url, body)
        return self._admission(raw, expected_instance_id, operation, account)

    async def admit_binding(
        self,
        binding: ManagedAdsBinding,
        *,
        operation: Literal["execute"] = "execute",
    ) -> ManagedAdsBinding:
        """Re-admit an already signed queued context; never refresh consent.

        The broker must verify the approval/hash and resolve the real account
        before calling. A successful result is usable only for this admission,
        not a cached authority token and not evidence of a human gesture.
        """
        if (
            not isinstance(binding, ManagedAdsBinding)
            or operation != "execute"
            or binding.org_id not in self._trust.allowed_org_ids
        ):
            raise ManagedAdsDenied("managed_ads_scope_invalid")
        try:
            _Principal.model_validate(binding.as_claims())
        except ValidationError:
            raise ManagedAdsDenied("managed_ads_scope_invalid") from None
        raw = await self._request(
            self._binding_url,
            {"binding": binding.as_claims(), "operation": operation},
        )
        try:
            response = _BindingResponse.model_validate(
                json.loads(raw, object_pairs_hook=_unique_object)
            )
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            raise ManagedAdsUnavailable("managed_ads_invalid_response") from None
        if not response.active or response.principal.model_dump() != binding.as_claims():
            raise ManagedAdsDenied("managed_ads_scope_invalid")
        return binding

    async def _request(self, url: str, body: dict[str, object]) -> bytes:
        """Fixed internal URLs only. Bounded transport shared by both admissions."""
        try:
            async with asyncio.timeout(_DEADLINE):
                async with self._client.stream(
                    "POST",
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._trust.service_secret}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cookie": "",
                    },
                ) as response:
                    if response.status_code in _DENIED_STATUSES:
                        raise ManagedAdsDenied("managed_ads_denied")
                    if (
                        response.status_code != HTTPStatus.OK
                        or response.headers.get("content-type", "").split(";", 1)[0].lower()
                        != "application/json"
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise ManagedAdsUnavailable("managed_ads_invalid_response")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > _MAX_BYTES:
                            raise ManagedAdsUnavailable("managed_ads_invalid_response")
                        data.extend(chunk)
                    return bytes(data)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise ManagedAdsUnavailable("managed_ads_unavailable") from None

    def _admission(
        self,
        raw: bytes,
        instance_id: UUID,
        operation: ManagedAdsOperation,
        account: AccountRef,
    ) -> ManagedAdsAdmission:
        try:
            response = _Response.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            raise ManagedAdsUnavailable("managed_ads_invalid_response") from None
        principal = response.principal
        resolved_account = AccountRef(
            PlatformCode(principal.platform),
            principal.external_account_id,
            UUID(principal.business_id),
            UUID(principal.connection_id),
        )
        now = self._clock()
        if (
            not response.active
            or UUID(principal.org_id) not in self._trust.allowed_org_ids
            or UUID(principal.instance_id) != instance_id
            or resolved_account != enterprise_account_ref(account)
            or tuple(principal.capabilities) != MANAGED_ADS_CAPABILITIES
            or not now < response.expires_at <= now + _MAX_TTL
        ):
            raise ManagedAdsDenied("managed_ads_scope_invalid")
        return ManagedAdsAdmission(
            binding=ManagedAdsBinding(
                UUID(principal.grant_id),
                principal.revision,
                UUID(principal.org_id),
                UUID(principal.user_id),
                UUID(principal.employee_id),
                instance_id,
                resolved_account,
                principal.resource_revision,
            ),
            expires_at=response.expires_at,
            operation=operation,
        )
