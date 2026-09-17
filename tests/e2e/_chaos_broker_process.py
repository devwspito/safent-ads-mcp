"""Standalone `ads-broker` process for the chaos bank
(`test_chaos_worker_crash_mid_execution.py`, tasks.md T074: "matar el
worker a media ejecucion"). Runs the SAME real wiring as
`tests/integration/composition/test_write_path_end_to_end.py::_running_broker`
(`GoogleAdsAdapter` + `WriteAuthorizationPipeline`, Ed25519 signature
verification, `caps.yaml`, `WriteLedgerStore`) but as its OWN OS process --
the chaos test needs to SIGKILL the *worker* while it is blocked waiting on
this broker's response, which only works if the broker's event loop is not
the same one running the test (a synchronous sleep inside the doubled SDK
would otherwise freeze the test's own polling).

Usage: `python _chaos_broker_process.py <socket_path> <customer_id>
<amount_micros> <sentinel_path>`. The doubled `mutate_campaign_budget`
(contracts/platform-port.md: "SDK mocked in tests") touches
`sentinel_path` the instant it is called, then sleeps for
`QA_CHAOS_MUTATE_DELAY_SECONDS` (env-controlled hook, default 3s) before
returning -- the deterministic window the chaos test polls for before
sending SIGKILL to the worker subprocess."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from safent_ads.accounts.application.ports import WriteIntent
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.domain.ledger_scope import LedgerScope
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.google_ads_adapter import (
    GoogleAdsAdapter,
    GoogleAdsAdapterConfig,
)
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
)
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapter, MetaOAuthAdapterConfig
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import PlatformCode

_APPROVAL_SEED_B64 = base64.b64encode(b"e" * 32).decode()
_CREDENTIAL_MASTER_KEY_B64 = base64.b64encode(b"0" * 32).decode()


def _fake_scope(intent: WriteIntent, account: str) -> LedgerScope:
    # Standalone subprocess fixture: no provider credentials or test-package imports.
    return LedgerScope(UUID(intent.business_id), intent.entity_ref.platform, account)


def _approval_public_key_b64() -> str:
    return ApprovalSigner.from_seed_b64(_APPROVAL_SEED_B64).public_key_b64()


class _UnreachableHttpClient:
    """Ningun `op` de OAuth se ejercita en este bróker de caos; si algo
    llamara a este cliente HTTP seria un fallo del banco, no un doble
    legitimo (mismo criterio que el flagship)."""

    async def post_form(
        self, url: str, *, data: dict[str, str]  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def get_json(
        self, url: str, *, headers: object = None, params: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def post_json(
        self, url: str, *, json_body: object, headers: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError


def _campaign_row(*, customer_id: str, amount_micros: int) -> dict[str, Any]:
    """MISMA forma que `test_write_path_end_to_end.py::_campaign_row`
    (campaign_id="1"): el hash de estado (`PlatformStateHash.compute`
    sobre todos los campos salvo `.resource_name`) tiene que coincidir
    byte a byte con el que calcula el test al sembrar `expected_state_hash`
    -- hasta el nombre de la campana cuenta."""
    return {
        "campaign.resource_name": f"customers/{customer_id}/campaigns/1",
        "campaign.id": 1,
        "campaign.name": "Campana de contrato",
        "campaign.status": "ENABLED",
        "campaign_budget.resource_name": f"customers/{customer_id}/campaignBudgets/1",
        "campaign_budget.amount_micros": amount_micros,
        "campaign_budget.type": "STANDARD",
        "campaign_budget.explicitly_shared": False,
        "customer.currency_code": "EUR",
    }


class _SentinelDelaySearchClient:
    """El SDK doblado: escribe `sentinel_path` en cuanto empieza a mutar y
    duerme `QA_CHAOS_MUTATE_DELAY_SECONDS` antes de devolver -- la ventana
    deterministica que el test usa para matar al worker a media escritura."""

    def __init__(self, row: Mapping[str, Any], sentinel_path: Path) -> None:
        self._row = row
        self._sentinel_path = sentinel_path

    def search_stream(self, customer_id: str, query: str) -> Iterator[Mapping[str, Any]]:  # noqa: ARG002
        yield self._row

    def mutate_campaign_budget(
        self, customer_id: str, budget_resource_name: str, amount_micros: int  # noqa: ARG002
    ) -> str:
        self._sentinel_path.write_text("in_flight")
        delay = float(os.environ.get("QA_CHAOS_MUTATE_DELAY_SECONDS", "3"))
        time.sleep(delay)
        return budget_resource_name

    def mutate_status(self, customer_id, resource_name, level, status) -> str:  # noqa: ANN001
        raise NotImplementedError

    def mutate_negative_keyword(self, customer_id, ad_group_resource_name, keyword_text) -> str:  # noqa: ANN001
        raise NotImplementedError


def _caps_yaml(customer_id: str) -> str:
    return f"""
defaults:
  max_step_pct: 100
  max_changes_per_day: 5
  autonomy_enabled: true
accounts:
  "{customer_id}":
    daily_cap_minor: 100000000
    monthly_cap_minor: 1000000000
    floor_minor: 100
    ceiling_minor: 100000000
"""


async def main() -> None:
    socket_path = Path(sys.argv[1])
    customer_id = sys.argv[2]
    amount_micros = int(sys.argv[3])
    sentinel_path = Path(sys.argv[4])
    ledger_path = Path(sys.argv[5])
    credentials_dir = Path(sys.argv[6])

    row = _campaign_row(customer_id=customer_id, amount_micros=amount_micros)
    search_client = _SentinelDelaySearchClient(row, sentinel_path)
    verifier = ApprovalVerifier.from_public_key_b64(_approval_public_key_b64())
    caps = parse_caps_config(_caps_yaml(customer_id))
    ledger = WriteLedgerStore(ledger_path)
    pipeline = WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=_fake_scope)
    clock = SystemClock()
    config = GoogleAdsAdapterConfig(
        client_id="client-id", client_secret="client-secret",
        refresh_token="refresh-token", login_customer_id="",
    )
    adapter = GoogleAdsAdapter(config, search_client, clock, write_pipeline=pipeline)
    registry = PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter})
    store = EncryptedCredentialStore(credentials_dir, _CREDENTIAL_MASTER_KEY_B64)
    google_oauth = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"),
        _UnreachableHttpClient(),  # type: ignore[arg-type]
        clock,
    )
    meta_oauth = MetaOAuthAdapter(
        MetaOAuthAdapterConfig(app_id="a", app_secret="s"), _UnreachableHttpClient(), clock  # type: ignore[arg-type]
    )
    runtime = BrokerRuntime(
        adapters=registry,
        oauth_flow=OAuthConnectFlow(store, google_oauth, meta_oauth, clock),
        app_credentials=AppCredentialsService(store, clock),
    )
    server = await serve(socket_path, runtime, frozenset({os.getuid()}))
    print("BROKER_READY", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
