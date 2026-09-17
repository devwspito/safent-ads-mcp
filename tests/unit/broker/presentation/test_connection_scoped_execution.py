"""Real broker dispatcher, signed write pipeline and credential resolution; fake SDK only."""

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.accounts.infrastructure.broker_client import _serialize_authorization
from safent_ads.broker.application.ports import CredentialRecord
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleAdsSearchClient
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.broker.presentation.dispatcher import handle_payload
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.platforms.test_google_ads_adapter import (
    _CAMPAIGN_RESOURCE,
    _CAMPAIGN_ROW,
    _CAPS_YAML,
    _CUSTOMER_ID,
    _NOW,
    _authorization,
    _config,
    _FakeSearchClient,
    _intent,
    _keypair,
)
from tests.unit.broker.presentation.test_oauth_socket_ops import _runtime


class _CredentialCheckedSdk(_FakeSearchClient):
    def __init__(self, live: LiveGoogleAdsSearchClient) -> None:
        super().__init__(rows_by_query={"SELECT campaign.resource_name": [_CAMPAIGN_ROW]})
        self.live = live
        self.read_identities: list[str] = []
        self.write_identities: list[str] = []

    def search_stream(self, customer_id, query):
        credential = self.live._resolve_credential(customer_id)
        self.read_identities.append(credential.refresh_token)
        return super().search_stream(customer_id, query)

    def mutate_status(self, customer_id, resource_name, level, status):
        credential = self.live._resolve_credential(customer_id)
        self.write_identities.append(credential.refresh_token)
        return super().mutate_status(customer_id, resource_name, level, status)


async def test_broker_reads_writes_receipts_and_revocation_bind_exact_connection(
    tmp_path: Path,
) -> None:
    store = EncryptedCredentialStore(tmp_path / "credentials", base64.b64encode(b"0" * 32).decode())
    connected = ConnectedCredentialStore(store, FixedClock(_NOW))
    live = LiveGoogleAdsSearchClient(
        client_id="fake", client_secret="fake", credential_store=connected
    )
    sdk = _CredentialCheckedSdk(live)
    signer, verifier = _keypair()
    adapter = GoogleAdsAdapter(
        _config(),
        sdk,
        FixedClock(_NOW),
        write_pipeline=WriteAuthorizationPipeline(
            verifier,
            parse_caps_config(_CAPS_YAML),
            WriteLedgerStore(tmp_path / "write.db"),
            scope_resolver=connected.resolve_write_scope,
        ),
    )
    runtime = replace(
        _runtime(tmp_path / "credentials"),
        adapters=PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter}),
    )
    business = uuid4()
    refs, credentials = [], []
    for index in range(2):
        connection, credential = uuid4(), CredentialRefId(uuid4())
        refs.append(
            EntityRef(
                PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, _CAMPAIGN_RESOURCE, business, connection
            )
        )
        credentials.append(credential)
        store.save_credential(
            credential,
            CredentialRecord(
                PlatformCode.GOOGLE,
                f"identity-{index}",
                "refresh_token",
                (),
                _NOW,
                None,
                business_id=str(business),
                connection_id=str(connection),
                owner_id=str(uuid4()),
            ),
        )
        store.bind_account_credential(
            PlatformCode.GOOGLE,
            _CUSTOMER_ID,
            credential,
            business_id=str(business),
            connection_id=str(connection),
        )

    async def exchange(payload):
        response = await handle_payload(json.dumps(payload).encode(), runtime)
        assert b"identity-" not in response
        return json.loads(response)

    reads = await asyncio.gather(
        *(
            exchange(
                {
                    "op": "fetch_account_inventory",
                    "platform": "google",
                    "external_account_id": _CUSTOMER_ID,
                    "business_id": str(business),
                    "connection_id": str(ref.connection_id),
                }
            )
            for ref in refs
        )
    )
    for index, response in enumerate(reads):
        assert response["ok"]
        assert response["result"][0]["entity_ref"] == str(refs[index])
    assert set(sdk.read_identities) == {"identity-0", "identity-1"}

    payloads = []
    for index, ref in enumerate(refs):
        intent = replace(_intent(ref), business_id=str(business))
        auth = _authorization(signer, diff_hash=intent.diff_hash)
        payload = {
            "op": "execute_write",
            "entity_ref": str(ref),
            "business_id": str(business),
            "operation": intent.operation.value,
            "parametro": intent.parametro,
            "valor_actual": intent.valor_actual,
            "valor_propuesto": intent.valor_propuesto,
            "diff_hash": intent.diff_hash,
            "expected_state_hash": intent.expected_state_hash,
            "authorization": _serialize_authorization(auth),
            "idempotency_key": f"write-{index}",
        }
        payloads.append(payload)
        response = await exchange(payload)
        assert response["result"]["outcome"] == "SUCCEEDED"
    assert sdk.write_identities == ["identity-0", "identity-1"]
    assert (await exchange({**payloads[0], "op": "read_write_receipt"}))["result"][
        "outcome"
    ] == "SUCCEEDED"
    wrong_receipt = await exchange(
        {**payloads[0], "op": "read_write_receipt", "entity_ref": str(refs[1])}
    )
    assert wrong_receipt.get("result") is None
    assert not (await exchange({**payloads[0], "business_id": str(uuid4())}))["ok"]
    store.revoke_credential(credentials[0], at=_NOW)
    denied = await exchange({"op": "read_entity_state", "entity_ref": str(refs[0])})
    assert not denied["ok"]
    assert (await exchange({"op": "read_entity_state", "entity_ref": str(refs[1])}))["ok"]
    assert sdk.write_identities == ["identity-0", "identity-1"]
