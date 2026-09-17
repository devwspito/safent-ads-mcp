"""B-2 (revision de seguridad de las herramientas sensibles del MCP): el
`node` que manda el llamante de `get_meta_graph` nunca debe escoger la
credencial que usa `LiveMetaGraphClient` -- solo la cuenta autorizada
(`account_ref`) puede hacerlo. Un `node="act_<otro negocio>"` no debe
resolver ni una credencial ajena."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.ports import AccountRef
from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter, MetaAdsAdapterConfig
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
_OWN_ACCOUNT = "act_1111111111"
_OTHER_BUSINESS_ACCOUNT = "act_2222222222"


class _RecordingCredentialStore(InMemoryCredentialStore):
    def __init__(self, credentials: dict[tuple[PlatformCode, str], PlatformCredential]) -> None:
        super().__init__(credentials)
        self.requested_accounts: list[str] = []

    async def get_credential(
        self, platform: PlatformCode, external_account_id: str
    ) -> PlatformCredential | None:
        self.requested_accounts.append(external_account_id)
        return await super().get_credential(platform, external_account_id)


def _adapter(store: _RecordingCredentialStore) -> MetaAdsAdapter:
    graph_client = LiveMetaGraphClient(
        app_id="app-id", app_secret="app-secret", credential_store=store  # noqa: S106
    )
    config = MetaAdsAdapterConfig(
        app_id="app-id", app_secret="app-secret", system_user_token="system-user-token"
    )
    return MetaAdsAdapter(config, graph_client, FixedClock(_NOW))


async def test_un_node_de_otra_cuenta_no_resuelve_ninguna_credencial() -> None:
    other_business_credential = PlatformCredential(
        platform=PlatformCode.META,
        external_account_id=_OTHER_BUSINESS_ACCOUNT,
        access_token="token-del-otro-negocio",  # noqa: S106
    )
    store = _RecordingCredentialStore(
        {(PlatformCode.META, _OTHER_BUSINESS_ACCOUNT): other_business_credential}
    )
    adapter = _adapter(store)

    with pytest.raises(EntityNotFoundError):
        await adapter.get_meta_graph(
            AccountRef(PlatformCode.META, _OWN_ACCOUNT),
            node=_OTHER_BUSINESS_ACCOUNT,
            edge="",
            fields=("id", "name"),
            params={},
        )

    assert store.requested_accounts == []
