"""004 tasks-2.md I1: las cuatro ops nuevas del bróker (`meta_reference_read`,
`google_reference_read`, `meta_graph_get`, `meta_ads_archive`) enrutadas de
extremo a extremo -- `handle_payload()` con un `PlatformAdapterRegistry` de
verdad (mismo `MetaAdsAdapter`/`GoogleAdsAdapter` de produccion, SDK
sustituido por un doble, contracts/platform-port.md "SDK mocked in
tests"). Ningun handler nuevo se prueba solo contra el adaptador: se prueba
contra el sobre JSON completo, como lo vera `ads-api` de verdad."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from safent_ads.broker.application.connection_scope import (
    ConnectionScope,
    current_connection_scope,
)
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter, GoogleAdsAdapterConfig
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryIdentityRequiredError
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter, MetaAdsAdapterConfig
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_AD_ACCOUNT_ID = "act_1234567890"
_CUSTOMER_ID = "1234567890"


class _FakeGraphClient:
    def __init__(
        self,
        edges: dict[tuple[str, str], list[Mapping[str, Any]]] | None = None,
        nodes: dict[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.edges = edges or {}
        self.nodes = nodes or {}

    def get_node(self, node_id: str, fields: Sequence[str]) -> Mapping[str, Any]:  # noqa: ARG002
        return self.nodes[node_id]

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],  # noqa: ARG002
        params: Mapping[str, Any] | None = None,  # noqa: ARG002
        *,
        paginate: bool = True,  # noqa: ARG002 - forma exacta del puerto (A-1)
    ) -> Sequence[Mapping[str, Any]]:
        return self.edges.get((node_id, edge), [])

    def update_node(self, node_id: str, fields: Mapping[str, Any]) -> None:  # noqa: ARG002
        raise AssertionError("no write op is exercised in this test module")


class _FakeSearchClient:
    def search_stream(self, customer_id: str, query: str):  # noqa: ARG002
        yield from ()


class _FakeKeywordIdeaClient:
    def generate_keyword_ideas(
        self,
        customer_id: str,  # noqa: ARG002
        *,
        seed_keywords: Sequence[str],  # noqa: ARG002
        geo_target_constant: str,  # noqa: ARG002
        language_constant: str,  # noqa: ARG002
        limit: int,  # noqa: ARG002
    ) -> list[Mapping[str, Any]]:
        return [{"text": "pienso perro", "avg_monthly_searches": 500, "competition": "LOW"}]


class _FakeAdLibraryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_ads_archive(
        self,
        *,
        country: str,  # noqa: ARG002
        search_terms: str | None,  # noqa: ARG002
        search_page_ids: str | None,  # noqa: ARG002
        active_status: str,  # noqa: ARG002
        fields: Sequence[str],  # noqa: ARG002
        limit: int,  # noqa: ARG002
        external_account_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        self.calls.append(
            {
                "external_account_id": external_account_id,
                "connection_scope": current_connection_scope(),
            }
        )
        return [{"page_name": "Acme", "ad_creative_bodies": ["Hola"], "publisher_platforms": []}]


def _meta_adapter(
    graph_client: _FakeGraphClient, *, ad_library: _FakeAdLibraryClient | None = None
) -> MetaAdsAdapter:
    config = MetaAdsAdapterConfig(app_id="a", app_secret="s", system_user_token="t")
    return MetaAdsAdapter(config, graph_client, FixedClock(_NOW), ad_library_client=ad_library)


def _google_adapter(keyword_client: _FakeKeywordIdeaClient) -> GoogleAdsAdapter:
    config = GoogleAdsAdapterConfig(
        client_id="c", client_secret="s", refresh_token="r", login_customer_id=_CUSTOMER_ID
    )
    return GoogleAdsAdapter(
        config, _FakeSearchClient(), FixedClock(_NOW), keyword_idea_client=keyword_client
    )


def _runtime(
    *, meta: MetaAdsAdapter | None = None, google: GoogleAdsAdapter | None = None
) -> BrokerRuntime:
    adapters: dict[PlatformCode, object] = {}
    if meta is not None:
        adapters[PlatformCode.META] = meta
    if google is not None:
        adapters[PlatformCode.GOOGLE] = google
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry(adapters),  # type: ignore[arg-type]
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type]
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
    )


async def test_meta_reference_read_end_to_end() -> None:
    pixel_row = {"id": "px1", "name": "Pixel"}
    graph_client = _FakeGraphClient(edges={(_AD_ACCOUNT_ID, "adspixels"): [pixel_row]})
    runtime = _runtime(meta=_meta_adapter(graph_client))
    payload = json.dumps(
        {
            "op": "meta_reference_read",
            "platform": "meta",
            "external_account_id": _AD_ACCOUNT_ID,
            "tool": "list_meta_pixels",
            "arguments": {},
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {"ok": True, "result": {"rows": [{"id": "px1", "name": "Pixel"}]}}


async def test_google_reference_read_end_to_end() -> None:
    runtime = _runtime(google=_google_adapter(_FakeKeywordIdeaClient()))
    payload = json.dumps(
        {
            "op": "google_reference_read",
            "platform": "google",
            "external_account_id": _CUSTOMER_ID,
            "tool": "get_google_keyword_ideas",
            "arguments": {
                "seed_keywords": ["perros"],
                "geo_target": "2724",
                "language": "1003",
            },
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response["ok"] is True
    assert response["result"]["rows"][0]["text"] == "pienso perro"


async def test_meta_graph_get_end_to_end() -> None:
    account_node = {"id": _AD_ACCOUNT_ID, "name": "Cuenta"}
    graph_client = _FakeGraphClient(nodes={_AD_ACCOUNT_ID: account_node})
    runtime = _runtime(meta=_meta_adapter(graph_client))
    payload = json.dumps(
        {
            "op": "meta_graph_get",
            "platform": "meta",
            "external_account_id": _AD_ACCOUNT_ID,
            "node": _AD_ACCOUNT_ID,
            "edge": "",
            "fields": ["id", "name"],
            "params": {},
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {
        "ok": True,
        "result": {"rows": [{"id": _AD_ACCOUNT_ID, "name": "Cuenta"}]},
    }


async def test_meta_graph_get_foreign_node_is_entity_not_found_end_to_end() -> None:
    graph_client = _FakeGraphClient(nodes={"999": {"account_id": "other", "id": "999"}})
    runtime = _runtime(meta=_meta_adapter(graph_client))
    payload = json.dumps(
        {
            "op": "meta_graph_get",
            "platform": "meta",
            "external_account_id": _AD_ACCOUNT_ID,
            "node": "999",
            "edge": "",
            "fields": ["id"],
            "params": {},
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {"ok": False, "error_code": "ENTITY_NOT_FOUND", "reason": "denied"}


class _SpyGraphClient(_FakeGraphClient):
    """B-3: espia si el adaptador llego a tocar el SDK -- una peticion
    denegada por politica no debe hacer ninguna llamada al proveedor."""

    def __init__(self) -> None:
        super().__init__()
        self.called = False

    def get_node(self, node_id: str, fields: Sequence[str]) -> Mapping[str, Any]:
        self.called = True
        return super().get_node(node_id, fields)

    def get_edge(
        self,
        node_id: str,
        edge: str,
        fields: Sequence[str],
        params: Mapping[str, Any] | None = None,
        *,
        paginate: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        self.called = True
        return super().get_edge(node_id, edge, fields, params, paginate=paginate)


@pytest.mark.parametrize(
    ("edge", "fields", "params"),
    [
        ("adaccounts", ["id"], {}),  # arista fuera de la lista blanca
        ("campaigns", ["access_token"], {}),  # campo de la lista negra
        ("campaigns", ["id"], {"limit": 500}),  # clave de params vetada
        ("campaigns", ["id"], {"note": {"nested": "dict"}}),  # M-2: valor no escalar
    ],
)
async def test_meta_graph_get_deniega_arista_campo_y_params_fuera_de_politica_aunque_el_cliente_los_mande(  # noqa: E501
    edge: str, fields: list[str], params: dict[str, Any]
) -> None:
    graph_client = _SpyGraphClient()
    runtime = _runtime(meta=_meta_adapter(graph_client))
    payload = json.dumps(
        {
            "op": "meta_graph_get",
            "platform": "meta",
            "external_account_id": _AD_ACCOUNT_ID,
            "node": _AD_ACCOUNT_ID,
            "edge": edge,
            "fields": fields,
            "params": params,
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {"ok": False, "error_code": "GRAPH_POLICY_DENIED", "reason": "denied"}
    assert graph_client.called is False


async def test_meta_ads_archive_end_to_end() -> None:
    runtime = _runtime(meta=_meta_adapter(_FakeGraphClient(), ad_library=_FakeAdLibraryClient()))
    payload = json.dumps(
        {
            "op": "meta_ads_archive",
            "country": "ES",
            "search_terms": "acme",
            "search_page_ids": None,
            "active_status": "ACTIVE",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response["ok"] is True
    assert response["result"]["ads"][0]["advertiser_name"] == "Acme"


async def test_meta_ads_archive_carries_the_connection_scope_and_account_end_to_end() -> None:
    """fix/ad-library-over-composio: `ComposioMetaAdLibraryClient` resolves
    its Composio binding through `current_connection_scope()` -- the
    dispatcher must fix that scope from `connection_id`/`business_id`
    BEFORE calling the adapter, and thread `external_account_id` through
    unchanged, exactly like `meta_reference_read`/`meta_graph_get` above."""
    ad_library = _FakeAdLibraryClient()
    runtime = _runtime(meta=_meta_adapter(_FakeGraphClient(), ad_library=ad_library))
    business_id = str(uuid4())
    connection_id = str(uuid4())
    payload = json.dumps(
        {
            "op": "meta_ads_archive",
            "business_id": business_id,
            "connection_id": connection_id,
            "external_account_id": _AD_ACCOUNT_ID,
            "country": "ES",
            "search_terms": "acme",
            "search_page_ids": None,
            "active_status": "ACTIVE",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response["ok"] is True
    call = ad_library.calls[0]
    assert call["external_account_id"] == _AD_ACCOUNT_ID
    assert call["connection_scope"] == ConnectionScope(UUID(business_id), UUID(connection_id))
    # The scope is torn down again once the op returns: never leaks past the
    # single request that set it.
    assert current_connection_scope() is None


async def test_meta_ads_archive_without_client_fails_closed_end_to_end() -> None:
    runtime = _runtime(meta=_meta_adapter(_FakeGraphClient()))
    payload = json.dumps(
        {
            "op": "meta_ads_archive",
            "country": "ES",
            "search_terms": "acme",
            "search_page_ids": None,
            "active_status": "ACTIVE",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response["ok"] is False
    # 2026-09-15: sigue fallando cerrado, pero con la denegacion precisa en vez
    # del FAILED/adapter_error generico (PlatformCapabilityNotImplementedError
    # es ahora un codigo conocido del dispatcher).
    assert response["error_code"] == "PLATFORM_CAPABILITY_NOT_IMPLEMENTED"


class _IdentityRequiredAdLibraryClient:
    def search_ads_archive(
        self,
        *,
        country: str,  # noqa: ARG002
        search_terms: str | None,  # noqa: ARG002
        search_page_ids: str | None,  # noqa: ARG002
        active_status: str,  # noqa: ARG002
        fields: Sequence[str],  # noqa: ARG002
        limit: int,  # noqa: ARG002
        external_account_id: str | None = None,  # noqa: ARG002
    ) -> list[Mapping[str, Any]]:
        raise MetaAdLibraryIdentityRequiredError("meta_ad_library_identity_confirmation_required")


async def test_meta_ads_archive_identity_required_end_to_end() -> None:
    """fix/ad-library-identity-reason: Meta's documented (400,
    `OAuthException/10`) response must reach `ads-api` as its own denial
    code -- never the generic `FAILED`/`adapter_error` bucket every other
    `MetaAdLibraryError` still falls into."""
    runtime = _runtime(
        meta=_meta_adapter(_FakeGraphClient(), ad_library=_IdentityRequiredAdLibraryClient())
    )
    payload = json.dumps(
        {
            "op": "meta_ads_archive",
            "country": "ES",
            "search_terms": "acme",
            "search_page_ids": None,
            "active_status": "ACTIVE",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {
        "ok": False,
        "error_code": "META_AD_LIBRARY_IDENTITY_REQUIRED",
        "reason": "denied",
    }


class _DatedAdLibraryClient:
    """A row with real Graph API delivery timestamps -- `_FakeAdLibraryClient`
    above never sets these, so it never exercised `map_ads_archive_row`'s
    date mapping through an actual `json.dumps` (`_ok_response`)."""

    def search_ads_archive(
        self,
        *,
        country: str,  # noqa: ARG002
        search_terms: str | None,  # noqa: ARG002
        search_page_ids: str | None,  # noqa: ARG002
        active_status: str,  # noqa: ARG002
        fields: Sequence[str],  # noqa: ARG002
        limit: int,  # noqa: ARG002
        external_account_id: str | None = None,  # noqa: ARG002
    ) -> list[Mapping[str, Any]]:
        return [
            {
                "page_name": "Acme",
                "ad_creative_bodies": ["Hola"],
                "ad_delivery_start_time": "2026-01-01T00:00:00+0000",
                "ad_delivery_stop_time": "2026-02-01T00:00:00+0000",
                "publisher_platforms": ["facebook"],
            }
        ]


async def test_meta_ads_archive_with_delivery_dates_end_to_end() -> None:
    """Regression (fix/broker-ad-library-typeerror): `map_ads_archive_row`
    used to leave `start_date`/`stop_date` as raw `datetime.date` objects.
    `_dispatch` builds `_ok_response(result)` OUTSIDE its own try/except, so
    `json.dumps` raising `TypeError: Object of type date is not JSON
    serializable` escaped `handle_payload` entirely -- and from there
    `_handle_connection` (no try/except around it either) -- killing the
    client's connection instead of answering `ok: false`. Same failure
    signature production saw: `Unhandled exception in client_connected_cb`,
    no upstream-failure log (there was no upstream failure, the fetch
    succeeded)."""
    runtime = _runtime(meta=_meta_adapter(_FakeGraphClient(), ad_library=_DatedAdLibraryClient()))
    payload = json.dumps(
        {
            "op": "meta_ads_archive",
            "country": "ES",
            "search_terms": "acme",
            "search_page_ids": None,
            "active_status": "ACTIVE",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response["ok"] is True
    ad = response["result"]["ads"][0]
    assert ad["start_date"] == "2026-01-01"
    assert ad["stop_date"] == "2026-02-01"
