"""`LiveMetaGraphClient`: solo el borde fail-closed cuando
`CredentialStorePort` no tiene la cuenta conectada -- el resto (llamada
real al Graph API) no se prueba aqui, igual que `MetaAdsAdapter` sustituye
el SDK por un doble (`test_meta_ads_adapter.py`)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms import live_meta_graph_client
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.shared.ids import PlatformCode


def _client() -> LiveMetaGraphClient:
    return LiveMetaGraphClient(
        app_id="app-id",
        app_secret="app-secret",  # noqa: S106
        credential_store=InMemoryCredentialStore(),
    )


def test_get_edge_on_account_fails_closed_without_connected_credential() -> None:
    with pytest.raises(CredentialNotConnectedError):
        _client().get_edge("act_123", "campaigns", ("id",))


def test_get_node_without_prior_account_context_fails_closed() -> None:
    """Sin un `get_edge(act_...)` previo en la misma instancia, un
    `get_node` sobre un nodo anidado (campana, conjunto de anuncios) no
    tiene forma de saber que cuenta resolver -- ver la limitacion conocida
    documentada en el modulo."""
    with pytest.raises(CredentialNotConnectedError):
        _client().get_node("120000000000001", ("id", "name"))


def test_sdk_client_is_explicitly_pinned_to_graph_api_v26(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sdk_client = object()

    def fake_init(session: object, **kwargs: object) -> object:
        captured["session"] = session
        captured.update(kwargs)
        return sdk_client

    monkeypatch.setattr(live_meta_graph_client, "FacebookAdsApi", fake_init)
    credential = PlatformCredential(
        platform=PlatformCode.META,
        external_account_id="act_123",
        access_token="meta-access-token",
    )
    client = LiveMetaGraphClient(
        app_id="app-id",
        app_secret="app-secret",
        credential_store=InMemoryCredentialStore(
            {(PlatformCode.META, "act_123"): credential}
        ),
    )

    assert client._api_for("act_123") is sdk_client
    assert captured["api_version"] == "v26.0"


def test_params_nunca_pisa_los_fields_de_la_lista_blanca(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-1: `params={"fields": "id,access_token"}` no puede sustituir la
    lista blanca de campos que el llamante ya valido -- `fields` se aplica
    siempre el ultimo en el diccionario de la peticion real a Meta."""
    captured_params: list[dict[str, Any]] = []

    class _FakeResponse:
        def json(self) -> dict[str, Any]:
            return {"data": [], "paging": {}}

    class _FakeApi:
        def call(
            self, method: str, path: list[str], params: dict[str, Any]  # noqa: ARG002
        ) -> _FakeResponse:
            captured_params.append(params)
            return _FakeResponse()

    monkeypatch.setattr(live_meta_graph_client, "FacebookAdsApi", lambda *a, **k: _FakeApi())  # noqa: ARG005
    credential = PlatformCredential(
        platform=PlatformCode.META, external_account_id="act_123", access_token="meta-token"
    )
    client = LiveMetaGraphClient(
        app_id="app-id",
        app_secret="app-secret",
        credential_store=InMemoryCredentialStore({(PlatformCode.META, "act_123"): credential}),
    )

    client.get_edge(
        "act_123", "campaigns", ("id", "name"), params={"fields": "id,access_token"}
    )

    assert captured_params[-1]["fields"] == "id,name"


def test_el_paso_a_traves_no_sigue_el_cursor_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-1: `paginate=False` (usado por `get_meta_graph`, el paso a
    traves) hace UNA sola llamada, con `limit` fijado por el servidor, y
    nunca sigue `paging.next` -- a diferencia de `paginate=True` (el resto
    de usos del adaptador: inventario, insights), que si pagina."""
    call_count = 0

    class _FakeResponse:
        def json(self) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {
                "data": [{"id": "1"}],
                "paging": {"next": "https://graph.facebook.com/next", "cursors": {"after": "c1"}},
            }

    class _FakeApi:
        def __init__(self) -> None:
            self.last_params: dict[str, Any] = {}

        def call(
            self, method: str, path: list[str], params: dict[str, Any]  # noqa: ARG002
        ) -> _FakeResponse:
            self.last_params = params
            return _FakeResponse()

    fake_api = _FakeApi()
    monkeypatch.setattr(live_meta_graph_client, "FacebookAdsApi", lambda *a, **k: fake_api)  # noqa: ARG005
    credential = PlatformCredential(
        platform=PlatformCode.META, external_account_id="act_123", access_token="meta-token"
    )
    client = LiveMetaGraphClient(
        app_id="app-id",
        app_secret="app-secret",
        credential_store=InMemoryCredentialStore({(PlatformCode.META, "act_123"): credential}),
    )

    rows = client.get_edge("act_123", "campaigns", ("id",), paginate=False)

    assert rows == [{"id": "1"}]
    assert call_count == 1
    assert fake_api.last_params["limit"] == 200
    assert "after" not in fake_api.last_params


def test_create_image_sends_base64_bytes_and_name_with_no_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meta's Marketing API accepts the image inline as base64 (`bytes`
    field) plus `name` on `POST /act_<id>/adimages`; dropping `files=`
    (multipart) means this exact same request also works over the
    Composio JSON proxy, which has no multipart support
    (`composio_sdk_clients.py::_MetaApiFacade.call` only takes `params`)."""
    captured: dict[str, Any] = {}
    media = b"\x89PNG\r\n\x1a\nfake-bytes"

    class _FakeResponse:
        def json(self) -> dict[str, Any]:
            return {"images": {"photo.jpg": {"hash": "abc123hash", "url": "https://x/preview"}}}

    class _FakeApi:
        def call(self, method: str, path: list[str], params: dict[str, Any]) -> _FakeResponse:
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr(live_meta_graph_client, "FacebookAdsApi", lambda *a, **k: _FakeApi())  # noqa: ARG005
    credential = PlatformCredential(
        platform=PlatformCode.META, external_account_id="act_123", access_token="meta-token"
    )
    client = LiveMetaGraphClient(
        app_id="app-id",
        app_secret="app-secret",
        credential_store=InMemoryCredentialStore({(PlatformCode.META, "act_123"): credential}),
    )

    result = client.create_image("act_123", "photo.jpg", media)

    assert captured["method"] == "POST"
    assert captured["path"] == ["act_123", "adimages"]
    assert captured["params"] == {
        "bytes": base64.b64encode(media).decode("ascii"),
        "name": "photo.jpg",
    }
    assert result == {"hash": "abc123hash", "url": "https://x/preview"}
