"""`SyncAccountInventory` (plan.md §5): trae el inventario remoto de una
`PlatformAccount` y lo refleja en `AdEntityRepository`. Solo lectura: US1 es
"ver la verdad", no gobierna escrituras (contracts/platform-port.md)."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.accounts.application.errors import AccountNotFoundError
from safent_ads.accounts.application.ports import (
    AccountRef,
    AccountRepository,
    AdEntityRepository,
    AdEntitySnapshot,
    AdsPlatformPort,
)
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class SyncAccountInventory:
    def __init__(
        self,
        platform_port: AdsPlatformPort,
        account_repository: AccountRepository,
        entity_repository: AdEntityRepository,
        clock: Clock,
    ) -> None:
        self._platform_port = platform_port
        self._account_repository = account_repository
        self._entity_repository = entity_repository
        self._clock = clock

    async def execute(self, account_ref: AccountRef) -> int:
        """Devuelve el numero de entidades sincronizadas."""
        account = await self._require_account(account_ref)
        snapshots = await self._platform_port.fetch_account_inventory(account_ref)

        for snapshot in snapshots:
            await self._upsert_entity(account.business_id, snapshot)
        await self._remove_entities_absent_from_inventory(account_ref, snapshots)

        account.record_synced(at=self._clock.now())
        await self._account_repository.save(account)
        return len(snapshots)

    async def _require_account(self, account_ref: AccountRef) -> PlatformAccount:
        account = await self._account_repository.get_by_ref(account_ref)
        if account is None:
            raise AccountNotFoundError(str(account_ref))
        return account

    async def _remove_entities_absent_from_inventory(
        self, account_ref: AccountRef, snapshots: Sequence[AdEntitySnapshot]
    ) -> None:
        """M1 (repaso 0.2.23): `proposals_entity_exists()` (0036) sigue
        abierto para un `entity_ref` que ya no existe en la plataforma
        (borrado/dado de baja fuera de este sistema) si su fila de
        `ad_entities` nunca se actualiza -- el inventario fresco es la
        UNICA fuente que sabe que desaparecio de verdad."""
        fetched_refs = {snapshot.entity_ref for snapshot in snapshots}
        known_entities = await self._entity_repository.list_by_account(account_ref)
        for entity in known_entities:
            if entity.entity_ref in fetched_refs or entity.status is AdEntityStatus.REMOVED:
                continue
            entity.mark_removed_from_platform()
            await self._entity_repository.save(entity)

    async def _upsert_entity(self, business_id: BusinessId, snapshot: AdEntitySnapshot) -> None:
        new_hash = PlatformStateHash.compute(snapshot.canonical_state)
        existing = await self._entity_repository.get_by_ref(snapshot.entity_ref)
        entity = existing or self._new_entity(business_id, snapshot, new_hash)
        if existing is not None:
            self._refresh_entity(existing, snapshot, new_hash)
        await self._entity_repository.save(entity)

    def _new_entity(
        self, business_id: BusinessId, snapshot: AdEntitySnapshot, state_hash: PlatformStateHash
    ) -> AdEntity:
        return AdEntity(
            business_id=business_id,
            entity_ref=snapshot.entity_ref,
            parent_ref=snapshot.parent_ref,
            name=snapshot.name,
            status=snapshot.status,
            platform_state_hash=state_hash,
            is_controllable=snapshot.is_controllable,
            learning_state=snapshot.learning_state,
            budget=snapshot.budget,
            bid_target=snapshot.bid_target,
            shared_budget_ref=snapshot.shared_budget_ref,
        )

    def _refresh_entity(
        self, entity: AdEntity, snapshot: AdEntitySnapshot, new_hash: PlatformStateHash
    ) -> None:
        entity.refresh_from_platform(
            name=snapshot.name,
            status=snapshot.status,
            is_controllable=snapshot.is_controllable,
            learning_state=snapshot.learning_state,
            budget=snapshot.budget,
            bid_target=snapshot.bid_target,
            shared_budget_ref=snapshot.shared_budget_ref,
            new_hash=new_hash,
            occurred_at=self._clock.now(),
        )
