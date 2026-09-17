"""`LiveMetaAdLibraryClient`: el mapeo de parametros de `/ads_archive` y el
borde fail-closed sin app de Meta configurada -- la llamada real al Graph
API no se prueba aqui, igual que `LiveMetaGraphClient`
(`test_live_meta_graph_client.py`)."""

from __future__ import annotations

from typing import Any

import pytest

from safent_ads.broker.platforms import live_meta_ad_library_client
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.live_meta_ad_library_client import LiveMetaAdLibraryClient


def test_search_ads_archive_fails_closed_without_app_credentials() -> None:
    client = LiveMetaAdLibraryClient(app_id="", app_secret="")

    with pytest.raises(CredentialNotConnectedError):
        client.search_ads_archive(
            country="ES",
            search_terms="zapatillas",
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
        )


def test_search_ads_archive_uses_the_app_access_token_never_a_client_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_session(**kwargs: object) -> object:
        captured["session_kwargs"] = kwargs
        return object()

    def fake_api(session: object, **kwargs: object) -> object:
        captured["api_version"] = kwargs.get("api_version")
        return _FakeApi([])

    monkeypatch.setattr(live_meta_ad_library_client, "FacebookSession", fake_session)
    monkeypatch.setattr(live_meta_ad_library_client, "FacebookAdsApi", fake_api)
    client = LiveMetaAdLibraryClient(app_id="app-id", app_secret="app-secret")

    client.search_ads_archive(
        country="ES",
        search_terms=None,
        search_page_ids=None,
        active_status="ACTIVE",
        fields=("page_name",),
        limit=50,
    )

    assert captured["session_kwargs"] == {
        "app_id": "app-id",
        "app_secret": "app-secret",
        "access_token": "app-id|app-secret",
    }
    assert captured["api_version"] == "v26.0"


class _FakeApi:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self._data = data
        self.calls: list[dict[str, Any]] = []

    def call(self, method: str, path: list[str], params: dict[str, Any]) -> _FakeResponse:
        self.calls.append({"method": method, "path": path, "params": params})
        return _FakeResponse(self._data)


class _FakeResponse:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


def _client_over(monkeypatch: pytest.MonkeyPatch, api: _FakeApi) -> LiveMetaAdLibraryClient:
    monkeypatch.setattr(live_meta_ad_library_client, "FacebookSession", lambda **_kwargs: object())
    monkeypatch.setattr(live_meta_ad_library_client, "FacebookAdsApi", lambda *_a, **_k: api)
    return LiveMetaAdLibraryClient(app_id="app-id", app_secret="app-secret")


def test_search_ads_archive_builds_the_official_ads_archive_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi([{"page_name": "Acme"}])
    client = _client_over(monkeypatch, api)

    rows = client.search_ads_archive(
        country="ES",
        search_terms="zapatillas",
        search_page_ids="123456",
        active_status="ACTIVE",
        fields=("page_name", "ad_snapshot_url"),
        limit=50,
    )

    assert rows == [{"page_name": "Acme"}]
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == ["ads_archive"]
    assert call["params"] == {
        "ad_reached_countries": ["ES"],
        "ad_active_status": "ACTIVE",
        "fields": "page_name,ad_snapshot_url",
        "limit": 50,
        "search_terms": "zapatillas",
        "search_page_ids": ["123456"],
    }


def test_search_ads_archive_omits_optional_filters_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi([])
    client = _client_over(monkeypatch, api)

    client.search_ads_archive(
        country="ES",
        search_terms=None,
        search_page_ids=None,
        active_status="ALL",
        fields=("page_name",),
        limit=50,
    )

    params = api.calls[0]["params"]
    assert "search_terms" not in params
    assert "search_page_ids" not in params


def test_search_ads_archive_rejects_a_response_without_a_valid_data_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi([])
    api.call = lambda *_a, **_k: _FakeResponse(None)  # type: ignore[assignment]
    client = _client_over(monkeypatch, api)

    with pytest.raises(ValueError, match="respuesta Meta"):
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
        )
