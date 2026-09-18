"""Small, credential-scoped client for the Google Tag Manager API v2."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, Final, Protocol

import httpx

from safent_ads.broker.application.ports import CredentialStorePort
from safent_ads.broker.infrastructure.egress_guard import build_guarded_async_client
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.mcp.domain.google_tag_manager_change import (
    parse_google_tag_manager_change,
    validate_google_tag_manager_path,
)
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import PlatformCode

_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"  # noqa: S105
_API_ROOT: Final = "https://tagmanager.googleapis.com/tagmanager/v2"
_TIMEOUT_SECONDS: Final = 20.0
_MAX_PAGES: Final = 20
_READ_SCOPE: Final = "https://www.googleapis.com/auth/tagmanager.readonly"
_EDIT_SCOPE: Final = "https://www.googleapis.com/auth/tagmanager.edit.containers"
_VERSION_SCOPE: Final = "https://www.googleapis.com/auth/tagmanager.edit.containerversions"
_PUBLISH_SCOPE: Final = "https://www.googleapis.com/auth/tagmanager.publish"


class GoogleTagManagerError(InfrastructureError):
    """Sanitised GTM API or transport failure; response bodies are never exposed."""


class GoogleTagManagerClient(Protocol):
    async def read(
        self, external_account_id: str, *, resource: str, parent_path: str | None
    ) -> Mapping[str, Any]: ...

    async def apply_change(
        self, external_account_id: str, payload: Mapping[str, object]
    ) -> Mapping[str, Any]: ...


class LiveGoogleTagManagerClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        credential_store: CredentialStorePort,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._credential_store = credential_store

    async def read(
        self, external_account_id: str, *, resource: str, parent_path: str | None
    ) -> Mapping[str, Any]:
        token = await self._access_token(
            external_account_id,
            required_any=frozenset({_READ_SCOPE, _EDIT_SCOPE, _VERSION_SCOPE, _PUBLISH_SCOPE}),
        )
        headers = {"Authorization": f"Bearer {token}"}
        if resource == "accounts":
            return await self._list_all("/accounts", "account", headers)
        if parent_path is None:
            raise GoogleTagManagerError("parent_path_required")
        validate_google_tag_manager_path(parent_path)
        suffix, collection = {
            "containers": ("containers", "container"),
            "workspaces": ("workspaces", "workspace"),
            "tags": ("tags", "tag"),
            "triggers": ("triggers", "trigger"),
            "variables": ("variables", "variable"),
            "version_headers": ("version_headers", "containerVersionHeader"),
        }.get(resource, (None, None))
        if suffix is None or collection is None:
            raise GoogleTagManagerError("resource_not_allowed")
        return await self._list_all(f"/{parent_path}/{suffix}", collection, headers)

    async def apply_change(
        self, external_account_id: str, payload: Mapping[str, object]
    ) -> Mapping[str, Any]:
        change = parse_google_tag_manager_change(payload)
        action = str(change["action"])
        required_scope = (
            _PUBLISH_SCOPE
            if action == "publish_version"
            else _VERSION_SCOPE
            if action == "create_version"
            else _EDIT_SCOPE
        )
        token = await self._access_token(
            external_account_id, required_any=frozenset({required_scope})
        )
        headers = {"Authorization": f"Bearer {token}"}
        parent = change["parent_path"]
        resource = change["resource_path"]
        body = change["body"]
        fingerprint = change["fingerprint"]

        if action == "create_workspace":
            return await self._request("POST", f"/{parent}/workspaces", headers, body)
        for kind in ("tag", "trigger", "variable"):
            if action == f"create_{kind}":
                return await self._request("POST", f"/{parent}/{kind}s", headers, body)
            if action == f"update_{kind}":
                return await self._request("PUT", f"/{resource}", headers, body)
        if action == "create_version":
            return await self._request("POST", f"/{resource}:create_version", headers, body)
        if action == "publish_version":
            params = {"fingerprint": str(fingerprint)} if fingerprint else None
            return await self._request("POST", f"/{resource}:publish", headers, None, params=params)
        raise GoogleTagManagerError("action_not_allowed")

    async def _access_token(self, external_account_id: str, *, required_any: frozenset[str]) -> str:
        credential = await self._credential_store.get_credential(
            PlatformCode.GOOGLE, external_account_id
        )
        if credential is None or credential.refresh_token is None or credential.composio:
            raise CredentialNotConnectedError("google_tag_manager_requires_direct_google_oauth")
        if not required_any.intersection(credential.scopes):
            raise CredentialNotConnectedError("google_tag_manager_scope_missing_reconnect")
        async with build_guarded_async_client(timeout=_TIMEOUT_SECONDS) as client:
            response = await self._send(
                client.post(
                    _TOKEN_ENDPOINT,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": credential.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            )
        try:
            body = response.json()
            access_token = body["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GoogleTagManagerError("invalid_google_token_response") from exc
        if not isinstance(access_token, str) or not access_token:
            raise GoogleTagManagerError("invalid_google_token_response")
        return access_token

    async def _list_all(
        self, path: str, collection: str, headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        rows: list[Any] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"pageToken": page_token} if page_token else None
            result = await self._request("GET", path, headers, None, params=params)
            page_rows = result.get(collection, [])
            if not isinstance(page_rows, list):
                raise GoogleTagManagerError("invalid_google_tag_manager_response")
            rows.extend(page_rows)
            raw_next = result.get("nextPageToken")
            page_token = raw_next if isinstance(raw_next, str) and raw_next else None
            if page_token is None:
                return {collection: rows}
        raise GoogleTagManagerError("google_tag_manager_pagination_limit")

    async def _request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        async with build_guarded_async_client(timeout=_TIMEOUT_SECONDS) as client:
            response = await self._send(
                client.request(
                    method,
                    f"{_API_ROOT}{path}",
                    headers=dict(headers),
                    json=dict(body) if body is not None else None,
                    params=dict(params or {}),
                )
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise GoogleTagManagerError("google_tag_manager_non_json_response") from exc
        if not isinstance(result, dict):
            raise GoogleTagManagerError("google_tag_manager_invalid_response")
        return result

    @staticmethod
    async def _send(pending: Awaitable[httpx.Response]) -> httpx.Response:
        try:
            response = await pending
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GoogleTagManagerError(
                f"google_tag_manager_http_{exc.response.status_code}"
            ) from None
        except httpx.HTTPError:
            raise GoogleTagManagerError("google_tag_manager_transport_failed") from None
        return response
