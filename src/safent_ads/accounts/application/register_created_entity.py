"""`RegisterCreatedEntity` (003-entidades-creadas): registra en
`AdEntityRepository` la `AdEntity` que un flujo de escritura acaba de
confirmar contra la plataforma -- via SINCRONA, distinta de
`SyncAccountInventory` (que refleja el inventario REMOTO leido de forma
periodica y asincrona, plan.md §5).

`proposals_entity_exists()` (0027) exige que `entity_ref` viva ya en
`ad_entities`/`platform_accounts` antes de aceptar cualquier `Proposal`
contra el. Una campaña/conjunto/anuncio que la MISMA saga de `packages`
acaba de crear no puede esperar al proximo sync periodico para que su
propio hijo (`CREATE_AD_SET`/`CREATE_AD`/`ACTIVATE_CAMPAIGN`) pueda
proponerse -- este caso de uso es el punto de entrada de aplicacion, propio
de `accounts` (el dueño de `ad_entities`), que cualquier otro contexto que
acabe de confirmar una escritura de creacion puede llamar sin escribir SQL
propio ni construir el agregado `AdEntity` fuera de su contexto.

Idempotente por `entity_ref`: un recibo repetido (reconciliacion tras una
reanudacion) nunca reescribe una entidad ya registrada -- el sync periodico
sigue siendo la UNICA fuente que actualiza campos ya conocidos (nombre,
presupuesto, estado real de la plataforma)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.accounts.application.ports import AdEntityRepository
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["RegisterCreatedEntity", "RegisterCreatedEntityCommand"]


@dataclass(frozen=True, kw_only=True, slots=True)
class RegisterCreatedEntityCommand:
    business_id: BusinessId
    entity_ref: EntityRef
    parent_ref: EntityRef
    name: str
    status: AdEntityStatus
    platform_state_hash: str


class RegisterCreatedEntity:
    def __init__(self, entities: AdEntityRepository) -> None:
        self._entities = entities

    async def execute(self, command: RegisterCreatedEntityCommand) -> None:
        if await self._entities.get_by_ref(command.entity_ref) is not None:
            return
        await self._entities.save(
            AdEntity(
                business_id=command.business_id,
                entity_ref=command.entity_ref,
                parent_ref=command.parent_ref,
                name=command.name,
                status=command.status,
                platform_state_hash=PlatformStateHash(command.platform_state_hash),
                is_controllable=True,
                learning_state=LearningState.NOT_APPLICABLE,
            )
        )
