"""Incidente 2026-09-15 (companion 0.2.26): `get_google_keyword_ideas` moria en
la fachada de Composio con `composio_google_service_not_supported` porque
`KeywordPlanIdeaService` no estaba en `_GOOGLE_SERVICES`, mientras el cliente
nativo funcionaba. Ademas la fachada solo entendia kwargs planos y siempre
metia "/" antes del metodo, y `generateKeywordIdeas` es un metodo a nivel de
cliente (`customers/{id}:generateKeywordIdeas`) que el cliente real invoca
con `request=<mensaje>` e itera como un pager."""

from __future__ import annotations

import pytest

from safent_ads.broker.platforms.composio_sdk_clients import _GoogleSdkFacade
from safent_ads.broker.platforms.composio_transport import ComposioTransportError
from safent_ads.shared.ids import PlatformCode

_ACCOUNT = "1000000001"


class _FakeTransport:
    def __init__(self, response: object) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response

    def request(self, platform, account, *, endpoint, method, body=None, query=None):
        self.calls.append(
            {
                "platform": platform,
                "account": account,
                "endpoint": endpoint,
                "method": method,
                "body": body,
                "query": query,
            }
        )
        return self._response


def _keyword_request(sdk: _GoogleSdkFacade, customer_id: str):
    request = sdk.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.language = "languageConstants/1003"
    request.geo_target_constants.append("geoTargetConstants/2724")
    request.keyword_seed.keywords.extend(["veterinario", "tienda mascotas"])
    return request


def test_generate_keyword_ideas_uses_customer_level_path_and_iterates_results() -> None:
    transport = _FakeTransport(
        {
            "results": [
                {
                    "text": "veterinario",
                    "keywordIdeaMetrics": {"avgMonthlySearches": "1200", "competition": "LOW"},
                },
                {
                    "text": "tienda mascotas",
                    "keywordIdeaMetrics": {"avgMonthlySearches": "880", "competition": "MEDIUM"},
                },
            ]
        }
    )
    sdk = _GoogleSdkFacade(transport, _ACCOUNT)

    response = sdk.get_service("KeywordPlanIdeaService").generate_keyword_ideas(
        request=_keyword_request(sdk, _ACCOUNT)
    )
    rows = list(response)  # el cliente real hace `for result in response`

    call = transport.calls[0]
    assert call["platform"] is PlatformCode.GOOGLE
    assert call["account"] == _ACCOUNT
    assert call["endpoint"] == f"/v25/customers/{_ACCOUNT}:generateKeywordIdeas"  # sin "/"
    assert call["method"] == "POST"
    body = call["body"]
    assert "customerId" not in body
    assert body["keywordSeed"]["keywords"] == ["veterinario", "tienda mascotas"]
    assert body["geoTargetConstants"] == ["geoTargetConstants/2724"]
    assert [row.text for row in rows] == ["veterinario", "tienda mascotas"]
    assert rows[0].keyword_idea_metrics.avg_monthly_searches == 1200
    assert rows[1].keyword_idea_metrics.competition.name == "MEDIUM"


def test_generate_keyword_ideas_rejects_another_customer() -> None:
    transport = _FakeTransport({"results": []})
    sdk = _GoogleSdkFacade(transport, _ACCOUNT)
    with pytest.raises(ComposioTransportError, match="customer_mismatch"):
        sdk.get_service("KeywordPlanIdeaService").generate_keyword_ideas(
            request=_keyword_request(sdk, "999")
        )
    assert transport.calls == []


def test_unknown_services_and_methods_stay_rejected() -> None:
    sdk = _GoogleSdkFacade(_FakeTransport({}), _ACCOUNT)
    with pytest.raises(ComposioTransportError, match="service_not_supported"):
        sdk.get_service("KeywordPlanService")
    with pytest.raises(ComposioTransportError, match="method_not_supported"):
        _ = sdk.get_service("KeywordPlanIdeaService").generate_keyword_historical_metrics
