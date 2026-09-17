"""Reuse official MCP servers behind the broker, never inside the agent cage.

Google's pinned, separately installed executable runs with a per-call ADC file.
Meta uses its fixed HTTPS endpoint and an independently authorized user token.
No SDK globals, shared token cache, arbitrary URLs/commands or remote writes.
The existing official API adapters remain available when MCP is not configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.broker.application.ports import AppCredentialsStorePort, CredentialStorePort
from safent_ads.broker.infrastructure.egress_guard import (
    assert_egress_allowed,
    build_guarded_async_client,
    build_guarded_async_client2,
)
from safent_ads.broker.platforms.native_mcp_policy import (
    MAX_RESULT_BYTES,
    NativeMcpDeniedError,
    account_field,
    allowed_tools,
    build_arguments,
    validate_schema,
)
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import PlatformCode

_META_URL = "https://mcp.facebook.com/ads"
_META_PERMISSIONS = "https://graph.facebook.com/v26.0/me/permissions"
_MAX_CATALOG_PAGES = 10
# Small on purpose (T-1/M4, secreview-mac-integration.md): bounds concurrent
# MCP child processes/HTTP sessions per account instead of letting a burst
# fork/connect unbounded (only the generic 60/min quota gated this before).
_DEFAULT_MAX_CONCURRENT_CALLS_PER_ACCOUNT = 2
# Short on purpose: the catalog is also keyed by the live server_info
# version, so a pinned upstream bump invalidates it well before the TTL --
# this bound only covers "how stale can a cache hit be if the version
# didn't change", not schema drift detection.
_DEFAULT_CATALOG_TTL = timedelta(minutes=5)


@dataclass(slots=True)
class _CatalogEntry:
    tools: list[dict[str, Any]]
    source_version: str
    expires_at: datetime


def _public_catalog(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    public = deepcopy(catalog)
    for entry in public:
        field = entry.pop("account_field")
        schema = entry["input_schema"]
        if field is not None:
            schema.get("properties", {}).pop(field, None)
            schema["required"] = [key for key in schema.get("required", []) if key != field]
    return {
        "transport": "official_mcp",
        "tools": public,
        "writes_exposed": False,
        "untrusted_data": True,
    }


class NativeMcpReadGateway:
    def __init__(
        self,
        credentials: CredentialStorePort,
        apps: AppCredentialsStorePort,
        *,
        google_executable: Path | None = None,
        google_project: str | None = None,
        meta_enabled: bool = False,
        max_concurrent_calls_per_account: int = _DEFAULT_MAX_CONCURRENT_CALLS_PER_ACCOUNT,
        catalog_ttl: timedelta = _DEFAULT_CATALOG_TTL,
        clock: Clock | None = None,
    ) -> None:
        self._credentials = credentials
        self._apps = apps
        self._google_executable = google_executable
        self._google_project = google_project
        self._meta_enabled = meta_enabled
        self._max_concurrent_calls_per_account = max_concurrent_calls_per_account
        self._catalog_ttl = catalog_ttl
        self._clock = clock or SystemClock()
        self._account_semaphores: dict[AccountRef, asyncio.Semaphore] = {}
        self._catalog_cache: dict[AccountRef, _CatalogEntry] = {}

    async def list_native_tools(self, account_ref: AccountRef) -> dict[str, Any]:
        return await self._request(account_ref, None, {})

    async def read_native_tool(
        self, account_ref: AccountRef, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool not in allowed_tools(account_ref.platform):
            raise NativeMcpDeniedError("native_mcp_tool_not_allowed")
        return await self._request(account_ref, tool, arguments)

    async def _request(
        self, account: AccountRef, tool: str | None, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            async with (
                asyncio.timeout(40),
                self._account_semaphore(account),
                self._session(account) as session,
            ):
                catalog = await self._catalog(session, account)
                if tool is None:
                    return _public_catalog(catalog)
                return await self._call_tool(session, account, tool, arguments, catalog)
        except NativeMcpDeniedError:
            raise
        except Exception:
            # Provider exceptions can contain authorization headers, query
            # contents and response bodies. Never send them to the MCP caller.
            raise NativeMcpDeniedError("native_mcp_request_failed") from None

    async def _call_tool(
        self,
        session: ClientSession,
        account: AccountRef,
        tool: str,
        arguments: dict[str, Any],
        catalog: list[dict[str, Any]],
    ) -> dict[str, Any]:
        match = next((t for t in catalog if t["name"] == tool), None)
        if match is None:
            raise NativeMcpDeniedError("native_mcp_tool_unavailable")
        params = build_arguments(account, tool, arguments, match["input_schema"])
        result = await session.call_tool(tool, params)
        if result.is_error:
            raise NativeMcpDeniedError("native_mcp_provider_error")
        body = result.model_dump(mode="json", exclude_none=True)
        if len(json.dumps(body).encode()) > MAX_RESULT_BYTES:
            raise NativeMcpDeniedError("native_mcp_result_too_large")
        return {
            "transport": "official_mcp",
            "account_ref": str(account),
            "tool": tool,
            "untrusted_data": True,
            "result": body,
        }

    def _account_semaphore(self, account: AccountRef) -> asyncio.Semaphore:
        semaphore = self._account_semaphores.get(account)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_concurrent_calls_per_account)
            self._account_semaphores[account] = semaphore
        return semaphore

    async def _catalog(self, session: ClientSession, account: AccountRef) -> list[dict[str, Any]]:
        version = await self._source_version(session)
        entry = self._fresh_catalog_entry(account)
        if entry is not None and entry.source_version == version:
            return entry.tools
        tools = await self._fetch_catalog_from_provider(session, account)
        self._catalog_cache[account] = _CatalogEntry(
            tools=tools, source_version=version, expires_at=self._clock.now() + self._catalog_ttl
        )
        return tools

    async def _source_version(self, session: ClientSession) -> str:
        # Free after `_session()`'s own `initialize()` (MCP `initialize` is
        # idempotent -- returns the memoised result, no second round trip):
        # the natural "pinned source" signal for the cache key, reported by
        # the server itself rather than a config value the broker must keep
        # in sync by hand.
        info = (await session.initialize()).server_info
        return f"{info.name}@{info.version}"

    def _fresh_catalog_entry(self, account: AccountRef) -> _CatalogEntry | None:
        entry = self._catalog_cache.get(account)
        if entry is None or self._clock.now() >= entry.expires_at:
            return None
        return entry

    async def _fetch_catalog_from_provider(
        self, session: ClientSession, account: AccountRef
    ) -> list[dict[str, Any]]:
        from mcp.types import PaginatedRequestParams  # noqa: PLC0415

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(_MAX_CATALOG_PAGES):
            page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            if len(page.model_dump_json().encode()) > MAX_RESULT_BYTES:
                raise NativeMcpDeniedError("native_mcp_catalog_too_large")
            for tool in page.tools:
                if tool.name not in allowed_tools(account.platform):
                    continue
                if tool.annotations is None or tool.annotations.read_only_hint is not True:
                    continue
                schema = tool.input_schema
                validate_schema(schema)
                field = account_field(account.platform, tool.name, schema)
                result.append({"name": tool.name, "input_schema": schema, "account_field": field})
                if len(json.dumps(result).encode()) > MAX_RESULT_BYTES:
                    raise NativeMcpDeniedError("native_mcp_catalog_too_large")
            cursor = page.next_cursor
            if not cursor:
                return result
            if cursor in seen:
                break
            seen.add(cursor)
        raise NativeMcpDeniedError("native_mcp_catalog_pagination_failed")

    @asynccontextmanager
    async def _session(self, account: AccountRef) -> AsyncIterator[ClientSession]:
        if account.platform == PlatformCode.GOOGLE:
            async with self._google_session(account) as session:
                yield session
        else:
            async with self._meta_session(account) as session:
                yield session

    @asynccontextmanager
    async def _google_session(self, account: AccountRef) -> AsyncIterator[ClientSession]:
        executable = self._google_executable
        if executable is None or not executable.is_absolute() or not executable.is_file():
            raise NativeMcpDeniedError("google_native_mcp_not_configured")
        if not self._google_project or not re.fullmatch(r"[0-9]+", account.external_account_id):
            raise NativeMcpDeniedError("google_native_mcp_invalid_configuration")
        credential = await self._credentials.get_credential(
            account.platform, account.external_account_id
        )
        app = self._apps.get_google_app_credentials()
        if credential is None or not credential.refresh_token or app is None:
            raise NativeMcpDeniedError("google_native_mcp_credentials_missing")
        await assert_egress_allowed("googleads.googleapis.com")
        with tempfile.TemporaryDirectory(prefix="safent-google-mcp-") as folder:
            adc = Path(folder) / "adc.json"
            with adc.open("x", encoding="utf-8") as handle:
                os.chmod(adc, 0o600)
                json.dump(
                    {
                        "type": "authorized_user",
                        "client_id": app.client_id,
                        "client_secret": app.client_secret,
                        "refresh_token": credential.refresh_token,
                        "quota_project_id": self._google_project,
                    },
                    handle,
                )
            env = {
                "GOOGLE_APPLICATION_CREDENTIALS": str(adc),
                "GOOGLE_PROJECT_ID": self._google_project,
                "GOOGLE_CLOUD_PROJECT": self._google_project,
                "FASTMCP_CHECK_FOR_UPDATES": "off",
            }
            if credential.login_customer_id:
                env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = credential.login_customer_id
            parameters = StdioServerParameters(command=str(executable), env=env, cwd=folder)
            # No third-party stderr (potential credentials) enters broker logs.
            with open(os.devnull, "w") as sink:
                async with stdio_client(parameters, errlog=sink) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=30) as session:
                        await session.initialize()
                        yield session

    @asynccontextmanager
    async def _meta_session(self, account: AccountRef) -> AsyncIterator[ClientSession]:
        if not self._meta_enabled:
            raise NativeMcpDeniedError("meta_native_mcp_not_enabled")
        if not re.fullmatch(r"act_[0-9]+", account.external_account_id):
            raise NativeMcpDeniedError("meta_native_mcp_invalid_account")
        credential = await self._credentials.get_credential(
            account.platform, account.external_account_id
        )
        if credential is None or not credential.access_token:
            raise NativeMcpDeniedError("meta_native_mcp_credentials_missing")
        await assert_egress_allowed("graph.facebook.com")
        await assert_egress_allowed("mcp.facebook.com")
        headers = {"Authorization": f"Bearer {credential.access_token}"}
        # `assert_egress_allowed` above is only a pre-flight resolve (CWE-367:
        # httpcore would resolve a second time at connect, DNS-rebinding
        # window); both clients below are pinned to the resolved IP at the
        # ACTUAL connection instead (M2, secreview-mac-integration.md).
        async with build_guarded_async_client(timeout=10, trust_env=False) as http:
            response = await http.get(_META_PERMISSIONS, headers=headers)
            response.raise_for_status()
            granted = {
                row["permission"]
                for row in response.json()["data"]
                if row.get("status") == "granted"
            }
        if not {"ads_read", "ads_mcp_management"} <= granted:
            raise NativeMcpDeniedError("meta_native_mcp_consent_required")
        async with build_guarded_async_client2(
            timeout=30, trust_env=False, headers=headers
        ) as http:
            async with streamable_http_client(_META_URL, http_client=http) as streams:
                async with ClientSession(*streams, read_timeout_seconds=30) as session:
                    await session.initialize()
                    yield session
