"""H-follow-up (revision de codigo 2026-09-15): `CredentialNotConnectedError`
(`broker/platforms/errors.py`) escapaba sin traducir en las lecturas que
NO envuelven la excepcion del SDK en un tipo propio (`google_reference_read`
via `GoogleAdsAdapter.read_reference_data` -- a diferencia de `run_gaql`,
cuyo `_run_gaql` ya reenvuelve TODO en `GoogleAdsAdapterError` antes de que
el dispatcher la vea, fuera de esta rama): `ads-api` la veia como
`FAILED`/`adapter_error` generico en vez de `CREDENTIAL_NOT_CONNECTED`, que
`mcp.application.errors.broker_denial_error` ya sabe traducir a un error
tipado (mismo incidente de produccion que `PLATFORM_APP_NOT_CONFIGURED`,
companion 0.2.21)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter, GoogleAdsAdapterConfig
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_CUSTOMER_ID = "1234567890"


class _FakeSearchClient:
    def search_stream(self, customer_id: str, query: str):  # noqa: ARG002
        yield from ()


class _CredentialNotConnectedKeywordIdeaClient:
    def generate_keyword_ideas(
        self,
        customer_id: str,  # noqa: ARG002
        *,
        seed_keywords,  # noqa: ARG002
        geo_target_constant,  # noqa: ARG002
        language_constant,  # noqa: ARG002
        limit,  # noqa: ARG002
    ):
        raise CredentialNotConnectedError("sin credencial de cliente para esta cuenta")


def _runtime() -> BrokerRuntime:
    config = GoogleAdsAdapterConfig(
        client_id="c", client_secret="s", refresh_token="r", login_customer_id=_CUSTOMER_ID
    )
    adapter = GoogleAdsAdapter(
        config,
        _FakeSearchClient(),
        FixedClock(_NOW),
        keyword_idea_client=_CredentialNotConnectedKeywordIdeaClient(),
    )
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter}),  # type: ignore[arg-type]
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type]
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
    )


async def test_google_reference_read_translates_credential_not_connected_end_to_end() -> None:
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

    response = json.loads(await handle_payload(payload, _runtime()))

    assert response == {"ok": False, "error_code": "CREDENTIAL_NOT_CONNECTED", "reason": "denied"}
