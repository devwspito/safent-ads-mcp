"""Private broker transport, not a callable MCP tool or a general HTTP proxy.

The native clients retain request construction and ownership/acknowledgement
checks. Their adapters retain the one WriteAuthorizationPipeline. The transport
never selects Composio connections from model arguments and never exports tokens.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

import httpx
import structlog

from safent_ads.broker.application.ports import ComposioAccountBinding, CredentialStorePort
from safent_ads.broker.infrastructure.egress_guard import build_guarded_async_client
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import PlatformCode

logger = structlog.get_logger(__name__)

_BASE_URL = "https://backend.composio.dev"
_PROXY_PATH = "/api/v3.1/tools/execute/proxy"
_GOOGLE_ENDPOINT = re.compile(
    r"/v25/customers/([0-9]+)(?:/(googleAds:searchStream|googleAds:mutate|"
    r"campaignBudgets:mutate|campaigns:mutate|adGroups:mutate|adGroupAds:mutate|"
    r"adGroupCriteria:mutate|assets:mutate)|:generateKeywordIdeas)"
)
_META_ENDPOINT = re.compile(r"/v26\.0/(act_[0-9]+|[0-9]+)(?:/(campaigns|adsets|ads|insights))?")
# Reference reads (spec 004 R3): GET-only, exact-account edges. None can
# write. Only `promote_pages` was admitted before; the other five reference
# tools died here with `composio_endpoint_not_supported` while their native
# clients worked (2026-09-15, companion 0.2.26).
_META_REFERENCE_EDGES = frozenset(
    {
        "promote_pages",
        "adspixels",
        "customaudiences",
        "saved_audiences",
        "product_catalogs",
        "targetingsearch",
        "delivery_estimate",
    }
)
# `LiveMetaGraphClient.create_image` (deliverable 2, BL-6): `POST
# /act_<id>/adimages` is the only creative-upload edge the adapter calls
# (`advideos` has no caller). POST-only, exact-account: never a GET listing
# (this transport never lists existing images) and never another account.
_META_UPLOAD_EDGES = frozenset({"adimages"})
# `LiveMetaAdLibraryClient.search_ads_archive` (meta_ad_library.py, R7): the
# Ad Library is public transparency data, not scoped to any connected ad
# account -- no account segment in the path (unlike every other Meta edge
# above). GET-only, exact node, and only the query keys the client actually
# sends (`live_meta_ad_library_client.py::search_ads_archive`).
_META_ADS_ARCHIVE_ENDPOINT = "/v26.0/ads_archive"
_META_ADS_ARCHIVE_QUERY_KEYS = frozenset(
    {
        "ad_reached_countries",
        "ad_active_status",
        "fields",
        "limit",
        "search_terms",
        "search_page_ids",
    }
)
_TOOLKITS = {PlatformCode.GOOGLE: "googleads", PlatformCode.META: "metaads"}
_NATIVE_ORIGINS = {
    PlatformCode.GOOGLE: "https://googleads.googleapis.com",
    PlatformCode.META: "https://graph.facebook.com",
}
_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_MAX_UPSTREAM_CODE_PARTS = 4  # status/type/code + un errorCode: suficiente para diagnosticar
_HTTP_OK = 200
_HTTP_REDIRECT = 300


class ComposioTransportError(InfrastructureError):
    """Only static error codes are exposed; response bodies/URLs stay private.

    `upstream_status`/`upstream_error_code` (fix/ad-library-identity-reason)
    carry the SAME sanitized status/`type/code` triplet `_log_upstream_failure`
    already logs -- never the raw body, message or headers. Callers that
    need to distinguish one specific, documented upstream failure (today:
    `ComposioMetaAdLibraryClient`, Meta error 10 on `ads_archive`) read
    these two attributes; every other caller ignores them and keeps
    catching this exception exactly as before.
    """

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        upstream_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status
        self.upstream_error_code = upstream_error_code


class ComposioAdsTransport:
    def __init__(
        self,
        *,
        api_key: str = "",
        credential_store: CredentialStorePort,
        api_key_source: Callable[[], str] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if api_key_source is None and (
            not api_key or any(character in api_key for character in "\r\n")
        ):
            raise ValueError("composio_api_key_missing")
        self._api_key = api_key
        self._api_key_source = api_key_source
        self._store = credential_store
        self._http_transport = http_transport

    def request(
        self,
        platform: PlatformCode,
        native_account_id: str,
        *,
        endpoint: str,
        method: str,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> Any:
        """Internal native SDK facade entry. Called in the adapter worker thread."""
        _check_endpoint(platform, native_account_id, endpoint, method, query=query)
        return asyncio.run(
            self._request(
                platform,
                native_account_id,
                endpoint=endpoint,
                method=method,
                body=body,
                query=query,
            )
        )

    async def _binding(
        self, platform: PlatformCode, native_account_id: str
    ) -> ComposioAccountBinding:
        credential = await self._store.get_credential(platform, native_account_id)
        if (
            credential is None
            or credential.platform != platform
            or credential.external_account_id != native_account_id
            or credential.composio is None
        ):
            raise CredentialNotConnectedError("composio_connection_not_verified")
        return credential.composio

    async def _request(
        self,
        platform: PlatformCode,
        account: str,
        *,
        endpoint: str,
        method: str,
        body: Mapping[str, Any] | None,
        query: Mapping[str, str] | None,
    ) -> Any:
        api_key = self._current_api_key()
        binding = await self._binding(platform, account)
        client = (
            httpx.AsyncClient(
                transport=self._http_transport,
                timeout=30.0,
                trust_env=False,
                follow_redirects=False,
            )
            if self._http_transport is not None
            else build_guarded_async_client(timeout=30.0, trust_env=False)
        )
        async with client:
            metadata = await self._http_json(
                client,
                "GET",
                f"/api/v3.1/connected_accounts/{binding.connected_account_id}",
                api_key=api_key,
            )
            _verify_remote_binding(metadata, binding, platform)
            # A revoke/reconnect during the metadata request must affect this request.
            if (
                await self._binding(platform, account) != binding
                or self._current_api_key() != api_key
            ):
                raise CredentialNotConnectedError("composio_connection_changed")
            parameters = [
                {"name": name, "value": value, "type": "query"}
                for name, value in (query or {}).items()
            ]
            if platform == PlatformCode.GOOGLE and binding.login_customer_id:
                parameters.append(
                    {
                        "name": "login-customer-id",
                        "value": binding.login_customer_id,
                        "type": "header",
                    }
                )
            payload: dict[str, Any] = {
                "connected_account_id": binding.connected_account_id,
                "endpoint": f"{_NATIVE_ORIGINS[platform]}{endpoint}",
                "method": method,
                "parameters": parameters,
            }
            if body is not None:
                payload["body"] = dict(body)
            response = await self._http_json(client, "POST", _PROXY_PATH, payload, api_key=api_key)
        if (
            not isinstance(response, dict)
            or type(response.get("status")) is not int
            or not _HTTP_OK <= response["status"] < _HTTP_REDIRECT
            or "data" not in response
        ):
            _fail_upstream(platform, response)
        data = response["data"]
        if isinstance(data, dict) and "error" in data:
            _fail_upstream(platform, response)
        if platform == PlatformCode.GOOGLE:
            return _restore_google_customer_references(data, binding.login_customer_id or account)
        return data

    def _current_api_key(self) -> str:
        key = self._api_key_source() if self._api_key_source is not None else self._api_key
        if not key or any(character in key for character in "\r\n"):
            raise CredentialNotConnectedError("composio_configuration_unavailable")
        return key

    async def _http_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> Any:
        try:
            # No automatic retries, including proxy POST used for upstream reads.
            async with client.stream(
                method,
                f"{_BASE_URL}{path}",
                headers={"x-api-key": api_key if api_key is not None else self._current_api_key()},
                json=payload,
            ) as response:
                if not _HTTP_OK <= response.status_code < _HTTP_REDIRECT:
                    raise ComposioTransportError("composio_request_failed")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_RESPONSE_BYTES:
                        raise ComposioTransportError("composio_response_too_large")
                return json.loads(chunks)
        except ComposioTransportError:
            raise
        except Exception:
            # No provider exception text/response headers/URL in cause or message.
            raise ComposioTransportError("composio_transport_unavailable") from None


def _restore_google_customer_references(value: Any, customer_id: str) -> Any:
    """Restore only a known non-secret customer ID on its authenticated endpoint.

    Managed Composio masks the connection Customer ID (generic_id) in Google
    resource names. Do not disable credential masking, derive IDs from secrets,
    or replace markers in arbitrary text. All entity IDs remain provider-owned.
    """
    if isinstance(value, list):
        return [_restore_google_customer_references(item, customer_id) for item in value]
    if not isinstance(value, dict):
        return value
    resource_fields = {"resourceName", "campaignBudget", "campaign", "adGroup", "asset"}
    result = {}
    for key, item in value.items():
        restored = item
        if key in resource_fields and isinstance(item, str):
            prefix = "customers/***REDACTED***"
            if item == prefix or item.startswith(prefix + "/"):
                restored = f"customers/{customer_id}" + item[len(prefix) :]
        result[key] = _restore_google_customer_references(restored, customer_id)
    if (
        value.get("resourceName") == "customers/***REDACTED***"
        and value.get("id") == "***REDACTED***"
    ):
        result["id"] = customer_id
    return result


def _upstream_error_code(data: Any) -> str | None:
    """Static provider error CODE only (never message/body/URL): Google
    `error.status` + first `errorCode` key/value; Meta `error.code`/`type`."""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    parts: list[str] = []
    # `error_subcode` (Meta) es un entero estable, no un mensaje: distingue
    # "confirma tu identidad" (10/2332002) de otras denegaciones con codigo 10.
    for key in ("status", "type", "code", "error_subcode"):
        value = error.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool):
            parts.append(str(value))
    for detail in error.get("details") or ():
        for item in (detail.get("errors") if isinstance(detail, dict) else None) or ():
            code = item.get("errorCode") if isinstance(item, dict) else None
            if isinstance(code, dict) and code:
                kind, name = next(iter(code.items()))
                parts.append(f"{kind}:{name}")
                break
        if len(parts) >= _MAX_UPSTREAM_CODE_PARTS:
            break
    return "/".join(parts)[:120] or None


_MAX_USER_TITLE_CHARS = 120


def _upstream_user_title(data: Any) -> str | None:
    """Meta's `error_user_title` is a short, static, user-facing title
    ("Authorization and login needed"), never the free-text message nor the
    body: enough to read the real cause of a code-10 denial in the log."""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    title = error.get("error_user_title")
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()[:_MAX_USER_TITLE_CHARS]


def _log_upstream_failure(platform: PlatformCode, response: Any) -> None:
    """2026-09-15: without this a RESOURCE_NAME_MALFORMED (Google) or a
    nonexisting edge (Meta) was indistinguishable from an outage."""
    status = response.get("status") if isinstance(response, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    logger.warning(
        "composio_upstream_failed",
        platform=platform.value,
        upstream_status=status if isinstance(status, int) else None,
        upstream_error=_upstream_error_code(data),
        upstream_user_title=_upstream_user_title(data),
    )


def _fail_upstream(platform: PlatformCode, response: Any) -> NoReturn:
    """Logs, then raises with the SAME sanitized status/`type/code` triplet
    `_log_upstream_failure` just logged -- attached to the exception too
    (fix/ad-library-identity-reason), still never the raw body, message or
    headers. The ONE caller that reads these two attributes today
    (`ComposioMetaAdLibraryClient`) distinguishes Meta's documented
    "identity/location confirmation required" response (400,
    `OAuthException/10`) from every other upstream failure without this
    transport ever exposing more than it already logged."""
    _log_upstream_failure(platform, response)
    status = response.get("status") if isinstance(response, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    raise ComposioTransportError(
        "composio_upstream_failed",
        upstream_status=status if isinstance(status, int) else None,
        upstream_error_code=_upstream_error_code(data),
    )


def _check_endpoint(
    platform: PlatformCode,
    account: str,
    endpoint: str,
    method: str,
    *,
    query: Mapping[str, str] | None = None,
) -> None:
    if platform == PlatformCode.GOOGLE:
        match = _GOOGLE_ENDPOINT.fullmatch(endpoint)
        if match is not None and match[1] == account and method == "POST":
            return
    elif platform == PlatformCode.META:
        # The Ad Library search has no connected ad account of its own
        # (public transparency data): no account check, exact node, and
        # only the query keys the client actually sends.
        if (
            method == "GET"
            and endpoint == _META_ADS_ARCHIVE_ENDPOINT
            and query
            and set(query) <= _META_ADS_ARCHIVE_QUERY_KEYS
        ):
            return
        # Reference reads are GET-only exact-account edges. No writes,
        # cross-account discovery or caller-supplied destination is admitted.
        if (
            method == "GET"
            and re.fullmatch(r"act_[0-9]+", account)
            and endpoint in {f"/v26.0/{account}/{edge}" for edge in _META_REFERENCE_EDGES}
        ):
            return
        # Creative uploads are POST-only exact-account edges: same shape as
        # the reference reads above, mirrored for the opposite verb.
        if (
            method == "POST"
            and re.fullmatch(r"act_[0-9]+", account)
            and endpoint in {f"/v26.0/{account}/{edge}" for edge in _META_UPLOAD_EDGES}
        ):
            return
        match = _META_ENDPOINT.fullmatch(endpoint)
        if (
            re.fullmatch(r"act_[0-9]+", account)
            and match is not None
            and method in {"GET", "POST"}
            and (not match[1].startswith("act_") or match[1] == account)
            and not (
                method == "POST"
                and (match[2] == "insights" or match[1] == account and not match[2])
            )
        ):
            return
    raise ComposioTransportError("composio_endpoint_not_supported")


def _verify_remote_binding(
    metadata: Any, binding: ComposioAccountBinding, platform: PlatformCode
) -> None:
    if (
        not isinstance(metadata, dict)
        or metadata.get("id") != binding.connected_account_id
        or metadata.get("user_id") != binding.user_id
        or metadata.get("status") != "ACTIVE"
        or metadata.get("is_disabled") is True
        or not isinstance(metadata.get("toolkit"), dict)
        or metadata["toolkit"].get("slug") != _TOOLKITS[platform]
        or not isinstance(metadata.get("auth_config"), dict)
        or metadata["auth_config"].get("id") != binding.auth_config_id
        or metadata["auth_config"].get("is_disabled") is True
    ):
        raise CredentialNotConnectedError("composio_connection_not_verified")
