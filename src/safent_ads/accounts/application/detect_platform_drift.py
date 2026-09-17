"""`DetectPlatformDrift` (plan.md §5): compara el `platform_state_hash`
conocido contra una lectura fresca y marca la entidad `drifted` si diverge
(data-model.md invariante de `AdEntity`; threat-model.md C-16)."""

from __future__ import annotations

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.ports import AdEntityRepository, AdsPlatformPort
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.clock import Clock
from safent_ads.shared.events import DomainEventBus
from safent_ads.shared.ids import EntityRef


class DetectPlatformDrift:
    def __init__(
        self,
        platform_port: AdsPlatformPort,
        entity_repository: AdEntityRepository,
        event_bus: DomainEventBus,
        clock: Clock,
    ) -> None:
        self._platform_port = platform_port
        self._entity_repository = entity_repository
        self._event_bus = event_bus
        self._clock = clock

    async def execute(self, entity_ref: EntityRef) -> bool:
        """Devuelve `True` si detecto y marco deriva."""
        entity = await self._entity_repository.get_by_ref(entity_ref)
        if entity is None:
            raise EntityNotFoundError(str(entity_ref))

        remote_state = await self._platform_port.read_entity_state(entity_ref)
        observed_hash = PlatformStateHash.compute(remote_state.canonical_state)
        if observed_hash == entity.platform_state_hash:
            return False

        entity.mark_drifted(observed_hash, occurred_at=self._clock.now())
        await self._entity_repository.save(entity)
        for event in entity.pull_events():
            await self._event_bus.publish(event)
        return True
