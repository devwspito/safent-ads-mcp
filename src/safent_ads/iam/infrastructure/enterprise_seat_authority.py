"""Adaptador HTTP fail-closed hacia `POST /internal/ads/introspect-seat`
(contracts/enterprise-api.md §4, contracts/mcp.md §2). Copia disciplinada de
`EnterpriseAdsAuthority` (`enterprise_ads_authority.py:123-294`): mismo
cliente httpx acotado (`trust_env=False`, sin redirecciones, `verify=True`,
timeout 2 s, deadline total 3 s, 16 KiB, `Accept-Encoding: identity`),
mismo modelo de respuesta cerrado y estricto, mismo fail-closed.

El origen, el secreto de servicio y las organizaciones permitidas vienen de
configuracion de despliegue (`EnterpriseSeatTrust`), nunca de la credencial
que presenta la persona."""

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

from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.seat_authority import (
    SeatAdmission,
    SeatAuthorityDeniedError,
    SeatAuthorityUnavailableError,
)

_MAX_BYTES = 16_384
_MAX_CREDENTIAL_BYTES = 8192
_DEADLINE = 3.0
_REQUEST_TIMEOUT = 2.0
_DENIED_STATUSES = {401, 403, 404, 409}
# contracts/mcp.md §2: "`expires_at` debe estar en el futuro y a menos de 300 s".
_MAX_ADMISSION_TTL = 300
_CREDENTIAL_PATTERN = re.compile(r"^sfa_[0-9a-f]{64}$")


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
_PersonLabel = Annotated[str, Field(min_length=1, max_length=200)]


class _Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    org_id: _Id
    user_id: _Id
    person_label: _PersonLabel
    seat_id: _Id
    business_id: _Id
    permission: Literal["view", "propose", "approve"]
    seat_revision: Annotated[int, Field(ge=1, le=2_147_483_647)]


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    active: bool
    principal: _Principal
    expires_at: int


@dataclass(frozen=True, slots=True)
class EnterpriseSeatTrust:
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
            raise ValueError("invalid Enterprise seat trust configuration")


class EnterpriseSeatAuthority:
    """Implementa `SeatAuthorityPort` estructuralmente (sin heredar del
    `Protocol`), igual que `EnterpriseAdsAuthority`."""

    def __init__(
        self,
        trust: EnterpriseSeatTrust,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._trust = trust
        self._clock = clock
        self._url = trust.origin.rstrip("/") + "/internal/ads/introspect-seat"
        # Sin proxy de entorno, sin redirecciones, sin reintentos: la
        # verificacion TLS se queda encendida (contracts/mcp.md §2).
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            verify=True,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve(self, credential: str) -> SeatAdmission:
        if (
            not isinstance(credential, str)
            or len(credential) > _MAX_CREDENTIAL_BYTES
            or _CREDENTIAL_PATTERN.fullmatch(credential) is None
        ):
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        raw = await self._request({"credential": credential})
        return self._admission(raw)

    async def _request(self, body: dict[str, object]) -> bytes:
        try:
            async with asyncio.timeout(_DEADLINE):
                async with self._client.stream(
                    "POST",
                    self._url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._trust.service_secret}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cookie": "",
                    },
                ) as response:
                    if response.status_code in _DENIED_STATUSES:
                        raise SeatAuthorityDeniedError("ads_seat_invalid")
                    if (
                        response.status_code != HTTPStatus.OK
                        or response.headers.get("content-type", "").split(";", 1)[0].lower()
                        != "application/json"
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise SeatAuthorityUnavailableError("seat_authority_invalid_response")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > _MAX_BYTES:
                            raise SeatAuthorityUnavailableError("seat_authority_invalid_response")
                        data.extend(chunk)
                    return bytes(data)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise SeatAuthorityUnavailableError("seat_authority_unavailable") from None

    def _admission(self, raw: bytes) -> SeatAdmission:
        try:
            response = _Response.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            raise SeatAuthorityUnavailableError("seat_authority_invalid_response") from None
        principal = response.principal
        now = self._clock()
        if (
            not response.active
            or UUID(principal.org_id) not in self._trust.allowed_org_ids
            or not now < response.expires_at <= now + _MAX_ADMISSION_TTL
        ):
            raise SeatAuthorityDeniedError("ads_seat_invalid")
        return SeatAdmission(
            org_id=principal.org_id,
            user_id=principal.user_id,
            person_label=principal.person_label,
            seat_id=principal.seat_id,
            business_id=principal.business_id,
            permission=Permission(principal.permission),
            expires_at=response.expires_at,
        )
