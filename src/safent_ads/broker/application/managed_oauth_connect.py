"""Broker-owned OAuth via Composio; no tokens are sent to the panel or LLM.

Opt-in deployment configuration, NOT a global key embedded in the desktop.
The existing one-use encrypted session, account admission and write pipeline
remain authoritative. All URLs, account discovery queries and IDs originate
here; callbacks are hints until independently verified against the provider.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.errors import (
    GoogleAccountAccessDeniedError,
    GoogleAccountNotEnabledError,
    GoogleAccountSelectionRequiredError,
    OAuthProviderDeniedError,
)
from safent_ads.broker.application.oauth_connect_flow import (
    BeginResult,
    CompleteResult,
    DiscoveredAccount,
)
from safent_ads.broker.application.oauth_state import generate_state, hash_state
from safent_ads.broker.application.ports import (
    ComposioAccountBinding,
    ConnectSessionRecord,
    CredentialRecord,
    OAuthConnectStorePort,
)
from safent_ads.broker.platforms.oauth_http import OAuthHttpClient, OAuthHttpError
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode
from safent_ads.shared.net.loopback import is_loopback_http_origin

_BASE = "https://backend.composio.dev/api/v3.1"
_TOOLKITS = {PlatformCode.GOOGLE: "googleads", PlatformCode.META: "metaads"}
_ADWORDS = "https://www.googleapis.com/auth/adwords"
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_MAX_ACCOUNTS = 5000
_MAX_CURSOR_BYTES = 4096
_HTTP_OK, _HTTP_REDIRECT = 200, 300
_CONNECTION_CREDENTIAL_TYPE = "composio_connection"


@dataclass(frozen=True, slots=True)
class ManagedOAuthConfig:
    api_key: str = field(repr=False)
    google_auth_config_id: str | None = None
    meta_auth_config_id: str | None = None

    def auth_config(self, provider: PlatformCode) -> str | None:
        return (
            self.google_auth_config_id
            if provider == PlatformCode.GOOGLE
            else self.meta_auth_config_id
        )


class ManagedOAuthConnectService:
    def __init__(
        self,
        config: ManagedOAuthConfig | Callable[[], ManagedOAuthConfig],
        http: OAuthHttpClient,
        store: OAuthConnectStorePort,
        clock: Clock,
        *,
        required: bool = False,
    ) -> None:
        self._config_source, self._http, self._store, self._clock = config, http, store, clock
        self.required = required

    @property
    def _config(self) -> ManagedOAuthConfig:
        return self._config_source() if callable(self._config_source) else self._config_source

    def configured(self, provider: PlatformCode) -> bool:
        config_id = self._config.auth_config(provider)
        return bool(
            self._config.api_key
            and config_id
            and re.fullmatch(r"ac_[A-Za-z0-9_-]{1,128}", config_id)
        )

    async def _get(self, path: str) -> Mapping[str, Any]:
        config = self._config
        if not config.api_key:
            raise OAuthProviderDeniedError("managed_configuration_missing")
        try:
            return await self._http.get_json(
                f"{_BASE}{path}", headers={"x-api-key": config.api_key}
            )
        except OAuthHttpError:
            raise OAuthProviderDeniedError("managed_connection_unavailable") from None

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        config = self._config
        if not config.api_key:
            raise OAuthProviderDeniedError("managed_configuration_missing")
        try:
            return await self._http.post_json(
                f"{_BASE}{path}",
                json_body=payload,
                headers={"x-api-key": config.api_key},
            )
        except OAuthHttpError:
            raise OAuthProviderDeniedError("managed_connection_unavailable") from None

    async def _validate_config(self, provider: PlatformCode, config_id: str) -> None:
        if not _ID.fullmatch(config_id):
            raise OAuthProviderDeniedError("managed_configuration_invalid")
        config = await self._get(f"/auth_configs/{config_id}")
        if (
            config.get("id") != config_id
            or config.get("status") != "ENABLED"
            or config.get("is_disabled") is True
            or str(config.get("auth_scheme", "")).upper() != "OAUTH2"
            or not isinstance(config.get("toolkit"), dict)
            or config["toolkit"].get("slug") != _TOOLKITS[provider]
        ):
            raise OAuthProviderDeniedError("managed_configuration_invalid")

    async def begin(
        self, *, provider: PlatformCode, business_id: str, redirect_uri: str, owner_id: str | None,
        google_customer_id: str | None = None,
    ) -> BeginResult:
        google_customer_id = normalize_google_customer_id(google_customer_id, provider=provider)
        if provider == PlatformCode.GOOGLE and not google_customer_id:
            raise GoogleAccountSelectionRequiredError("google_account_selection_required")
        config_id = self._config.auth_config(provider)
        if not self.configured(provider) or config_id is None or not owner_id:
            raise OAuthProviderDeniedError("managed_configuration_missing")
        await self._validate_config(provider, config_id)
        parsed = urlsplit(redirect_uri)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not (parsed.scheme == "https" or is_loopback_http_origin(redirect_uri))
        ):
            raise OAuthProviderDeniedError("managed_callback_invalid")
        state, connection_id = generate_state(), str(uuid.uuid4())
        # Unique per connection attempt: a valid account from another session
        # or installation cannot be substituted, even in the same project.
        user_id = f"safent-ads-{connection_id}"
        response = await self._post(
            "/connected_accounts/link",
            {
                "auth_config_id": config_id,
                "user_id": user_id,
                "callback_url": f"{redirect_uri}?{urlencode({'state': state, 'managed': '1'})}",
                **({"connection_data": {"generic_id": google_customer_id}}
                   if google_customer_id else {}),
            },
        )
        connected_id, url = response.get("connected_account_id"), response.get("redirect_url")
        target = urlsplit(url) if isinstance(url, str) else None
        if (
            not isinstance(connected_id, str)
            or not re.fullmatch(r"ca_[A-Za-z0-9_-]{1,128}", connected_id)
            or target is None
            or target.scheme != "https"
            or target.username
            or target.password
            or target.hostname not in {"connect.composio.dev", "backend.composio.dev"}
        ):
            raise OAuthProviderDeniedError("managed_link_invalid")
        now = self._clock.now()
        expires_at = now + timedelta(minutes=10)
        self._store.save_connect_session(
            hash_state(state),
            ConnectSessionRecord(
                provider=provider,
                business_id=business_id,
                redirect_uri=redirect_uri,
                pkce_verifier=None,
                created_at=now,
                expires_at=expires_at,
                connection_id=connection_id,
                owner_id=owner_id,
                managed_connection_id=connected_id,
                managed_user_id=user_id,
                managed_auth_config_id=config_id,
                google_customer_id=google_customer_id,
            ),
        )
        return BeginResult(str(url), state, expires_at, connection_id)

    async def complete(
        self, session: ConnectSessionRecord, *, connected_account_id: str
    ) -> CompleteResult:
        if (
            not session.managed_connection_id
            or connected_account_id != session.managed_connection_id
            or not session.managed_user_id
            or not session.managed_auth_config_id
            or self._config.auth_config(session.provider) != session.managed_auth_config_id
        ):
            raise OAuthProviderDeniedError("managed_connection_mismatch")
        binding = ComposioAccountBinding(
            connected_account_id, session.managed_user_id, session.managed_auth_config_id
        )
        await self._validate_config(session.provider, binding.auth_config_id)
        await self._verify_binding(session.provider, binding)
        scopes: tuple[str, ...]
        if session.provider == PlatformCode.GOOGLE:
            if not session.google_customer_id:
                raise GoogleAccountSelectionRequiredError("google_account_selection_required")
            accounts = await self._google_accounts(binding, session.google_customer_id)
            scopes = (_ADWORDS,)
        else:
            permissions = await self._proxy(
                binding, "https://graph.facebook.com/v26.0/me/permissions"
            )
            rows = self._rows(permissions, "data")
            scopes = tuple(
                sorted(
                    {
                        str(p["permission"])
                        for p in rows
                        if p.get("status") == "granted" and isinstance(p.get("permission"), str)
                    }
                )
            )
            if "ads_management" not in scopes:
                raise OAuthProviderDeniedError("managed_ads_write_permission_missing")
            accounts = await self._meta_accounts(binding)
        # Inventory is all-or-nothing; no bindings are saved on provider failure.
        await self._verify_binding(session.provider, binding)
        return CompleteResult(tuple(self._save(session, binding, row, scopes) for row in accounts))

    async def _verify_binding(
        self, provider: PlatformCode, binding: ComposioAccountBinding
    ) -> None:
        account = await self._get(f"/connected_accounts/{binding.connected_account_id}")
        if (
            account.get("id") != binding.connected_account_id
            or account.get("status") != "ACTIVE"
            or account.get("is_disabled") is True
            or account.get("user_id") != binding.user_id
            or not isinstance(account.get("toolkit"), dict)
            or account["toolkit"].get("slug") != _TOOLKITS[provider]
            or not isinstance(account.get("auth_config"), dict)
            or account["auth_config"].get("id") != binding.auth_config_id
            or account["auth_config"].get("is_disabled") is True
        ):
            raise OAuthProviderDeniedError("managed_connection_not_verified")

    async def _proxy(
        self,
        binding: ComposioAccountBinding,
        endpoint: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        # Only this module's fixed READ discovery endpoints call this helper.
        parameters = [{"name": k, "value": v, "type": "query"} for k, v in (query or {}).items()]
        if binding.login_customer_id:
            parameters.append(
                {"name": "login-customer-id", "value": binding.login_customer_id, "type": "header"}
            )
        payload: dict[str, Any] = {
            "connected_account_id": binding.connected_account_id,
            "endpoint": endpoint,
            "method": "POST" if body is not None else "GET",
            "parameters": parameters,
        }
        if body is not None:
            payload["body"] = dict(body)
        response = await self._post("/tools/execute/proxy", payload)
        if endpoint.startswith("https://googleads.googleapis.com/"):
            self._check_google_error(response)
        if (
            type(response.get("status")) is not int
            or not _HTTP_OK <= response["status"] < _HTTP_REDIRECT
            or "data" not in response
        ):
            raise OAuthProviderDeniedError("managed_account_discovery_failed")
        return response["data"]

    @staticmethod
    def _check_google_error(response: Mapping[str, Any]) -> None:
        """Map only Google's typed error enum, never provider text or credentials."""
        data = response.get("data")
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return
        codes = set()
        for detail in error.get("details", []) if isinstance(error.get("details"), list) else []:
            if not isinstance(detail, dict):
                continue
            for row in detail.get("errors", []) if isinstance(detail.get("errors"), list) else []:
                code = row.get("errorCode") if isinstance(row, dict) else None
                if isinstance(code, dict):
                    codes.add(str(code.get("authorizationError", "")))
        if "CUSTOMER_NOT_ENABLED" in codes:
            raise GoogleAccountNotEnabledError("google_account_not_enabled")
        if "USER_PERMISSION_DENIED" in codes:
            raise GoogleAccountAccessDeniedError("google_account_access_denied")

    @staticmethod
    def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise OAuthProviderDeniedError("managed_account_discovery_invalid")
        rows: list[dict[str, Any]] = payload[key]
        if len(rows) > _MAX_ACCOUNTS or any(not isinstance(row, dict) for row in rows):
            raise OAuthProviderDeniedError("managed_account_discovery_invalid")
        return rows

    async def _google_accounts(
        self, binding: ComposioAccountBinding, customer_id: str | None
    ) -> list[dict[str, Any]]:
        base = "https://googleads.googleapis.com/v25"
        customer_id = normalize_google_customer_id(customer_id, provider=PlatformCode.GOOGLE)
        if customer_id is None:
            raise GoogleAccountSelectionRequiredError("google_account_selection_required")
        # Connect only the account the human selected. listAccessibleCustomers
        # includes unrelated/closed accounts and can omit manager-mediated ones.
        result: dict[str, dict[str, Any]] = {}
        metadata = await self._proxy(
            binding,
            f"{base}/customers/{customer_id}/googleAds:search",
            body={"query": (
                "SELECT customer.id, customer.currency_code, customer.time_zone, "
                "customer.descriptive_name, customer.manager FROM customer LIMIT 1"
            )},
        )
        rows = self._rows(metadata, "results")
        if len(rows) != 1 or not isinstance(rows[0].get("customer"), dict):
            raise OAuthProviderDeniedError("managed_account_discovery_invalid")
        customer = dict(rows[0]["customer"])
        # Composio redacts generic_id even in non-secret customer metadata.
        # Restore only the known, human-selected customer on its fixed,
        # authenticated Google endpoint; never recover a provider credential.
        if (
            customer.get("id") == "***REDACTED***"
            and customer.get("resourceName") == "customers/***REDACTED***"
        ):
            customer["id"] = customer_id
        if str(customer.get("id")) != customer_id:
            raise OAuthProviderDeniedError("managed_account_discovery_invalid")
        if customer.get("manager") is True:
            children = await self._proxy(
                replace(binding, login_customer_id=customer_id),
                f"{base}/customers/{customer_id}/googleAds:search",
                body={"query": (
                    "SELECT customer_client.id, customer_client.currency_code, "
                    "customer_client.time_zone, customer_client.descriptive_name "
                    "FROM customer_client WHERE customer_client.manager = FALSE "
                    "AND customer_client.status = 'ENABLED'"
                )},
            )
            if children.get("nextPageToken"):
                raise OAuthProviderDeniedError("managed_account_discovery_limit")
            for child in self._rows(children, "results"):
                record = child.get("customerClient")
                if not isinstance(record, dict):
                    raise OAuthProviderDeniedError("managed_account_discovery_invalid")
                record = dict(record, login_customer_id=customer_id)
                result.setdefault(str(record.get("id")), record)
        else:
            result[customer_id] = customer
        if not result:
            raise GoogleAccountAccessDeniedError("google_account_inventory_empty")
        return [self._metadata(r, google=True) for r in result.values()]

    async def _meta_accounts(self, binding: ComposioAccountBinding) -> list[dict[str, Any]]:
        params = {"fields": "id,name,currency,timezone_name", "limit": "100"}
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            payload = await self._proxy(
                binding, "https://graph.facebook.com/v26.0/me/adaccounts", query=params
            )
            results.extend(self._metadata(row, google=False) for row in self._rows(payload, "data"))
            if len(results) > _MAX_ACCOUNTS:
                raise OAuthProviderDeniedError("managed_account_discovery_limit")
            paging = payload.get("paging", {})
            if not isinstance(paging, dict) or not paging.get("next"):
                return results
            cursors = paging.get("cursors", {})
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if (
                not isinstance(after, str)
                or not after
                or len(after) > _MAX_CURSOR_BYTES
                or after in seen
            ):
                raise OAuthProviderDeniedError("managed_account_discovery_invalid")
            seen.add(after)
            params["after"] = after  # Never follow provider-controlled paging URLs.

    @staticmethod
    def _metadata(row: Mapping[str, Any], *, google: bool) -> dict[str, Any]:
        account_id = str(row.get("id", ""))
        currency, timezone = (
            row.get("currencyCode" if google else "currency"),
            row.get("timeZone" if google else "timezone_name"),
        )
        login_id = row.get("login_customer_id")
        if (
            not re.fullmatch(r"[0-9]{1,20}" if google else r"act_[0-9]{1,30}", account_id)
            or (
                login_id is not None
                and (not isinstance(login_id, str) or not re.fullmatch(r"[0-9]{1,20}", login_id))
            )
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or not isinstance(timezone, str)
            or not timezone
        ):
            raise OAuthProviderDeniedError("managed_account_discovery_invalid")
        return {
            "id": account_id,
            "currency": currency,
            "timezone": timezone,
            "label": str(row.get("descriptiveName" if google else "name", account_id)),
            "login_customer_id": row.get("login_customer_id"),
        }

    def _save(
        self,
        session: ConnectSessionRecord,
        binding: ComposioAccountBinding,
        row: Mapping[str, Any],
        scopes: tuple[str, ...],
    ) -> DiscoveredAccount:
        ref, now = CredentialRefId(uuid.uuid4()), self._clock.now()
        bound = replace(binding, login_customer_id=row.get("login_customer_id"))
        self._store.save_credential(
            ref,
            CredentialRecord(
                platform=session.provider,
                token=json.dumps(asdict(bound)),
                token_type=_CONNECTION_CREDENTIAL_TYPE,
                scopes=scopes,
                obtained_at=now,
                expires_at=None,
                business_id=session.business_id,
                connection_id=session.connection_id,
                owner_id=session.owner_id,
            ),
        )
        self._store.bind_account_credential(
            session.provider,
            row["id"],
            ref,
            business_id=session.business_id,
            connection_id=session.connection_id,
        )
        return DiscoveredAccount(
            platform=session.provider,
            external_account_id=row["id"],
            label=row["label"],
            currency=row["currency"],
            timezone=row["timezone"],
            api_tier=ApiTier.GOOGLE_EXPLORER
            if session.provider == PlatformCode.GOOGLE
            else ApiTier.META_LIMITED,
            credential_ref_id=ref,
            scopes=scopes,
            obtained_at=now,
            expires_at=None,
            business_id=session.business_id,
            connection_id=session.connection_id,
            owner_id=session.owner_id,
        )
