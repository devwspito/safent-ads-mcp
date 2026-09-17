"""Native SDK-shaped transport facades; no duplicate campaign/write engine.

Google SDK protobufs remain the request/response schema authority. Meta uses
the existing LiveMetaGraphClient ownership and confirmation checks unchanged.
Only the transport changes for an explicitly Composio-backed account.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials
from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.platforms.composio_transport import (
    ComposioAdsTransport,
    ComposioTransportError,
)
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleAdsSearchClient
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryIdentityRequiredError
from safent_ads.broker.platforms.meta_scope import split_meta_scope
from safent_ads.shared.ids import PlatformCode

# fix/ad-library-identity-reason: Meta's documented response on `ads_archive`
# when the Facebook user behind the token has not completed the Ad Library
# identity/location confirmation (facebook.com/ID) -- HTTP 400,
# `error.type = "OAuthException"`, `error.code = 10` ("Application does not
# have permission for this action"). The ONLY signal this client special-
# cases; every other upstream failure stays the generic, honest bucket.
_IDENTITY_REQUIRED_UPSTREAM_STATUS = 400
# Meta: OAuthException code 10 + error_subcode 2332002 ("Authorization and login
# needed": confirma tu identidad en facebook.com/ID). Un 10 sin ese subcodigo es
# otra denegacion de permiso y NO se traduce a "identidad".
_IDENTITY_REQUIRED_UPSTREAM_ERROR_CODE = "OAuthException/10/2332002"

_GOOGLE_SERVICES = {
    "GoogleAdsService": {
        "search_stream": (
            "googleAds:searchStream",
            "SearchGoogleAdsStreamRequest",
            "SearchGoogleAdsStreamResponse",
        ),
        "mutate": ("googleAds:mutate", "MutateGoogleAdsRequest", "MutateGoogleAdsResponse"),
    },
    "CampaignBudgetService": {
        "mutate_campaign_budgets": (
            "campaignBudgets:mutate",
            "MutateCampaignBudgetsRequest",
            "MutateCampaignBudgetsResponse",
        ),
    },
    "CampaignService": {
        "mutate_campaigns": (
            "campaigns:mutate",
            "MutateCampaignsRequest",
            "MutateCampaignsResponse",
        ),
    },
    "AdGroupService": {
        "mutate_ad_groups": ("adGroups:mutate", "MutateAdGroupsRequest", "MutateAdGroupsResponse"),
    },
    "AdGroupAdService": {
        "mutate_ad_group_ads": (
            "adGroupAds:mutate",
            "MutateAdGroupAdsRequest",
            "MutateAdGroupAdsResponse",
        ),
    },
    "AdGroupCriterionService": {
        "mutate_ad_group_criteria": (
            "adGroupCriteria:mutate",
            "MutateAdGroupCriteriaRequest",
            "MutateAdGroupCriteriaResponse",
        ),
    },
    # `LiveGoogleAssetUploadClient.mutate_image_asset` (google_asset_upload.py,
    # threat-model.md #17): a single `ImageAsset` mutate, same request/response
    # shape as the other mutate services above.
    "AssetService": {
        "mutate_assets": ("assets:mutate", "MutateAssetsRequest", "MutateAssetsResponse"),
    },
    # Customer-level custom method (`customers/{id}:generateKeywordIdeas`):
    # the endpoint starts with ":" and hangs off the customer, no "/" before it.
    "KeywordPlanIdeaService": {
        "generate_keyword_ideas": (
            ":generateKeywordIdeas",
            "GenerateKeywordIdeasRequest",
            "GenerateKeywordIdeaResponse",
        ),
    },
}


class ComposioGoogleAdsSearchClient(LiveGoogleAdsSearchClient):
    """Hybrid native client: a binding chooses transport, not an agent argument."""

    def __init__(self, *, composio_transport: ComposioAdsTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._composio_transport = composio_transport

    def _resolve_credential(self, customer_id: str) -> PlatformCredential:
        credential = asyncio.run(
            self._credential_store.get_credential(PlatformCode.GOOGLE, customer_id)
        )
        if credential is None or not (credential.composio or credential.refresh_token):
            raise CredentialNotConnectedError("google_connection_not_verified")
        return credential

    def _build_sdk_client(self, credential: PlatformCredential) -> Any:
        if credential.composio is not None:
            return _GoogleSdkFacade(self._composio_transport, credential.external_account_id)
        if not self._client_id:
            raise CredentialNotConnectedError("google_native_app_not_configured")
        return super()._build_sdk_client(credential)


class _GoogleSdkFacade:
    def __init__(self, transport: ComposioAdsTransport, account: str) -> None:
        self._transport = transport
        self._account = account
        # Credentials are deliberately anonymous: this SDK instance only builds
        # and parses protobufs. It never creates a network service or sees tokens.
        self._schema = GoogleAdsClient(
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
            use_proto_plus=True,
            version="v25",
        )

    def get_type(self, name: str) -> Any:
        return self._schema.get_type(name)

    def copy_from(self, destination: Any, source: Any) -> Any:
        return self._schema.copy_from(destination, source)

    def get_service(self, name: str) -> _GoogleServiceFacade:
        if name not in _GOOGLE_SERVICES:
            raise ComposioTransportError("composio_google_service_not_supported")
        return _GoogleServiceFacade(self, name)


class _GoogleServiceFacade:
    def __init__(self, sdk: _GoogleSdkFacade, service: str) -> None:
        self._sdk = sdk
        self._service = service

    def __getattr__(self, name: str) -> Any:
        if name not in _GOOGLE_SERVICES[self._service]:
            raise ComposioTransportError("composio_google_method_not_supported")

        def call(**kwargs: Any) -> Any:
            return self._invoke(name, kwargs)

        return call

    def _invoke(self, method: str, kwargs: dict[str, Any]) -> Any:
        if kwargs.pop("retry", None) is not None:
            raise ComposioTransportError("composio_retry_not_supported")
        endpoint, request_type, response_type = _GOOGLE_SERVICES[self._service][method]
        try:
            request = self._build_request(request_type, kwargs)
            if str(getattr(request, "customer_id", "")) != self._sdk._account:
                raise ComposioTransportError("composio_google_customer_mismatch")
            body = MessageToDict(request._pb)
            body.pop("customerId", None)
        except ComposioTransportError:
            raise
        except Exception:
            raise ComposioTransportError("composio_google_request_invalid") from None
        # Customer-level custom methods (":generateKeywordIdeas") hang off the
        # customer itself: no "/" between the id and the method.
        separator = "" if endpoint.startswith(":") else "/"
        response = self._sdk._transport.request(
            PlatformCode.GOOGLE,
            self._sdk._account,
            endpoint=f"/v25/customers/{self._sdk._account}{separator}{endpoint}",
            method="POST",
            body=body,
        )
        if method == "search_stream":
            if not isinstance(response, list):
                raise ComposioTransportError("composio_google_response_invalid")
            return [self._parse(response_type, batch) for batch in response]
        parsed = self._parse(response_type, response)
        # The native pager iterates results directly ("for result in response");
        # keep that contract so LiveGoogleKeywordIdeaClient stays unchanged.
        return parsed.results if method == "generate_keyword_ideas" else parsed

    def _build_request(self, request_type: str, kwargs: dict[str, Any]) -> Any:
        # The SDK accepts flat fields or a single "request=<message>" (the
        # shape LiveGoogleKeywordIdeaClient uses); both must resolve here.
        if set(kwargs) == {"request"}:
            return kwargs["request"]
        return type(self._sdk.get_type(request_type))(**kwargs)

    def _parse(self, name: str, payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise ComposioTransportError("composio_google_response_invalid")
        message = self._sdk.get_type(name)
        try:
            ParseDict(payload, message._pb)
        except Exception:
            raise ComposioTransportError("composio_google_response_invalid") from None
        return message


class ComposioMetaGraphClient(LiveMetaGraphClient):
    def __init__(self, *, composio_transport: ComposioAdsTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._composio_transport = composio_transport

    def _api_for(self, node_id: str) -> Any:
        account, _ = split_meta_scope(node_id)
        credential = asyncio.run(self._credential_store.get_credential(PlatformCode.META, account))
        if credential is None:
            raise CredentialNotConnectedError("meta_connection_not_verified")
        if credential.composio is not None:
            return _MetaApiFacade(self._composio_transport, account)
        if not self._app_id or not self._app_secret:
            raise CredentialNotConnectedError("meta_native_app_not_configured")
        return super()._api_for(node_id)


class _MetaResponse:
    def __init__(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise ComposioTransportError("composio_meta_response_invalid")
        self._body = body

    def json(self) -> Mapping[str, Any]:
        return self._body


class _MetaApiFacade:
    def __init__(self, transport: ComposioAdsTransport, account: str) -> None:
        self._transport = transport
        self._account = account

    def call(self, method: str, path: Sequence[str], *, params: Mapping[str, Any]) -> _MetaResponse:
        # GET complex query values use the same JSON representation as Meta SDK.
        query = (
            {
                key: value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
                for key, value in params.items()
            }
            if method == "GET"
            else None
        )
        return _MetaResponse(
            self._transport.request(
                PlatformCode.META,
                self._account,
                endpoint="/v26.0/" + "/".join(path),
                method=method,
                query=query,
                body=params if method == "POST" else None,
            )
        )


class ComposioMetaAdLibraryClient:
    """Composio-backed `MetaAdLibraryClient` (`meta_ad_library.py` Protocol):
    the companion/managed deployment has no native Meta app
    (`composition/broker.py::_build_meta_adapter`), so `LiveMetaAdLibraryClient`
    (app-token based) always fails closed there. `/ads_archive` itself is
    account-agnostic (`live_meta_ad_library_client.py` docstring), but the
    Composio proxy still needs a connected account to authenticate the
    call: the caller resolves the business's own active Meta connection and
    passes it in as `external_account_id`, one call at a time -- never a
    fixed account baked in at construction, this client is shared by every
    business `MetaAdsAdapter` serves. Reuses `_MetaApiFacade`/`_MetaResponse`
    above: same GET param-to-query serialization Composio already proxies
    for every other Meta read (`ComposioMetaGraphClient`). fix/ad-library-
    identity-reason: distinguishes Meta's one documented, stable failure
    (`_IDENTITY_REQUIRED_UPSTREAM_STATUS`/`_IDENTITY_REQUIRED_UPSTREAM_ERROR_CODE`)
    from every other upstream error via `ComposioTransportError`'s already-
    sanitized status/code triplet -- never a raw message."""

    def __init__(self, *, composio_transport: ComposioAdsTransport) -> None:
        self._transport = composio_transport

    def search_ads_archive(
        self,
        *,
        country: str,
        search_terms: str | None,
        search_page_ids: str | None,
        active_status: str,
        fields: Sequence[str],
        limit: int,
        external_account_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        if not external_account_id:
            # Fail closed: the caller (`meta_ads_adapter.py::search_ads_archive`)
            # should never omit it once a business resolved an active Meta
            # connection -- an absent value here is a wiring bug, not a
            # legitimate "no account" outcome (that one is decided upstream,
            # before this client is even reached).
            raise CredentialNotConnectedError("meta_ads_archive_account_missing")
        params: dict[str, Any] = {
            "ad_reached_countries": [country],
            "ad_active_status": active_status,
            "fields": ",".join(fields),
            "limit": limit,
        }
        if search_terms:
            params["search_terms"] = search_terms
        if search_page_ids:
            params["search_page_ids"] = [search_page_ids]
        facade = _MetaApiFacade(self._transport, external_account_id)
        try:
            body = facade.call("GET", ["ads_archive"], params=params).json()
        except ComposioTransportError as exc:
            if (
                exc.upstream_status == _IDENTITY_REQUIRED_UPSTREAM_STATUS
                and exc.upstream_error_code == _IDENTITY_REQUIRED_UPSTREAM_ERROR_CODE
            ):
                raise MetaAdLibraryIdentityRequiredError(
                    "meta_ad_library_identity_confirmation_required"
                ) from None
            raise
        data = body.get("data")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ComposioTransportError("composio_meta_response_invalid")
        return data
