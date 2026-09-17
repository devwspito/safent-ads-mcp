"""Incidente 2026-09-15: `get_google_keyword_ideas` fallaba en produccion con
RESOURCE_NAME_MALFORMED porque el cliente enviaba a Google los ids pelados que
recibe del MCP ("1003", "2724") en vez de nombres de recurso. Ademas, sin
`page_size` Google devolvia miles de ideas (2 MB medidos en vivo)."""

from __future__ import annotations

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleKeywordIdeaClient


class _RecordingService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def generate_keyword_ideas(self, *, request):
        self.requests.append(request)
        return []


class _FakeSdk:
    def __init__(self, service: _RecordingService) -> None:
        self._schema = GoogleAdsClient(
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
            use_proto_plus=True,
            version="v25",
        )
        self._service = service

    def get_type(self, name: str):
        return self._schema.get_type(name)

    def get_service(self, name: str):
        assert name == "KeywordPlanIdeaService"
        return self._service


class _FakeSearchClient:
    def __init__(self, sdk: _FakeSdk) -> None:
        self._sdk = sdk

    def build_sdk_client(self, customer_id: str):
        del customer_id  # el doble sirve el mismo SDK para cualquier cliente
        return self._sdk


def _run(language: str, geo: str) -> object:
    service = _RecordingService()
    client = LiveGoogleKeywordIdeaClient(_FakeSearchClient(_FakeSdk(service)))  # type: ignore[arg-type]
    client.generate_keyword_ideas(
        "1000000001",
        seed_keywords=("veterinario",),
        geo_target_constant=geo,
        language_constant=language,
        limit=200,
    )
    return service.requests[0]


@pytest.mark.parametrize(
    "language,geo",
    [("1003", "2724"), ("languageConstants/1003", "geoTargetConstants/2724"), (" 1003 ", "2724")],
)
def test_bare_ids_and_full_names_both_become_resource_names(language, geo) -> None:
    request = _run(language, geo)
    assert request.customer_id == "1000000001"
    assert request.language == "languageConstants/1003"
    assert list(request.geo_target_constants) == ["geoTargetConstants/2724"]


def test_page_size_is_bounded_to_the_limit() -> None:
    request = _run("1003", "2724")
    assert request.page_size == 200
    assert list(request.keyword_seed.keywords) == ["veterinario"]
