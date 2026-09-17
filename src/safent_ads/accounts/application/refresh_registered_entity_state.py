"""`RefreshRegisteredEntityState` (003-entidades-creadas, item 2 del repaso
0.2.23): tras un `ACTIVATE_CAMPAIGN` confirmado, sube a ACTIVE y refresca la
huella de la `AdEntity` que su propio `CREATE_CAMPAIGN` ya registro
(`RegisterCreatedEntity`). Distinta de esta: `RegisterCreatedEntity` es
idempotente por diseño (un recibo repetido nunca reescribe una fila ya
conocida) -- aqui SI hay que sobreescribir el estado y el hash del recibo
mas reciente sobre la MISMA fila, nunca crear una nueva.

Fail-closed como `RegisterCreatedEntity`: si la campaña no esta registrada
todavia (nunca deberia pasar -- `ACTIVATE_CAMPAIGN` solo llega tras su
propio `CREATE_CAMPAIGN` ya registrado), no la crea a ciegas -- lanza
`EntityNotFoundError` y deja que `RunPackagePublication` pare la saga en
vez de fingir que la activacion se reflejo."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.ports import AdEntityRepository
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.ids import EntityRef

__all__ = ["RefreshRegisteredEntityState", "RefreshRegisteredEntityStateCommand"]


@dataclass(frozen=True, kw_only=True, slots=True)
class RefreshRegisteredEntityStateCommand:
    entity_ref: EntityRef
    confirmed_state_hash: str


class RefreshRegisteredEntityState:
    def __init__(self, entities: AdEntityRepository) -> None:
        self._entities = entities

    async def execute(self, command: RefreshRegisteredEntityStateCommand) -> None:
        entity = await self._entities.get_by_ref(command.entity_ref)
        if entity is None:
            raise EntityNotFoundError(str(command.entity_ref))
        entity.confirm_activation(PlatformStateHash(command.confirmed_state_hash))
        await self._entities.save(entity)
