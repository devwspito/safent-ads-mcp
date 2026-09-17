"""`UndoPackagePublication` (T025; contracts/api.md §5): dos ventanas,
nunca una tercera via de borrado.

1. **Antes de la primera escritura** (paquete `approved`, dentro de los
   45 s de gracia, FR-08): cancela la publicacion entera sin haber
   escrito nada -- invalida el paquete (`APPROVED -> INVALIDATED`) y
   detiene la fila de publicacion para que el worker nunca la retome.
2. **Tras la activacion** (`published`/`partially_published`, con la
   campaña ya confirmada por recibo): pausa por el MISMO camino que
   `PauseEntity` -- el clic en «Deshacer» ES la autorizacion humana
   explicita, exactamente como pausar desde Campañas. Sin ventana de
   caducidad propia (decision del integrador, `plan.md` R2.5: "Pausar es
   la accion normal de Campañas y esta siempre disponible").

`Ningun camino de este caso de uso emite `WriteOperation.DELETE`. Nunca."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.execution.application.entity_lifecycle_actions import (
    EntityActionDeniedError,
    PauseEntity,
    PauseEntityCommand,
    UnknownEntityError,
)
from safent_ads.packages.application.errors import (
    PackageChangedError,
    PackageNotFoundError,
    UndoNoConfirmedCampaignError,
    UndoPauseDeniedError,
    UndoWindowClosedError,
)
from safent_ads.packages.application.ports import (
    CampaignPackageRepository,
    PackagePublicationRecord,
    PackagePublicationRepository,
    PackageStepRepository,
)
from safent_ads.packages.domain.approval_envelope import PackageApprovalEnvelope, StepKind
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.packages.domain.publication_policy import PACKAGE_UNDO_GRACE
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef, EntityRefFormatError

__all__ = [
    "UndoPackagePublication",
    "UndoPackagePublicationCommand",
    "UndoPackagePublicationResult",
]

_PUBLISHED_STATES = frozenset({PackageState.PUBLISHED, PackageState.PARTIALLY_PUBLISHED})


@dataclass(frozen=True, kw_only=True, slots=True)
class UndoPackagePublicationCommand:
    business_id: BusinessId
    package_id: PackageId
    package_hash: str
    owner_email: str
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class UndoPackagePublicationResult:
    undo_kind: str  # "cancelled_publication" | "campaign_paused"
    campaign_entity_ref: str | None = None
    execution_id: str | None = None


class UndoPackagePublication:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        publications: PackagePublicationRepository,
        steps: PackageStepRepository,
        pause_entity: PauseEntity,
        clock: Clock,
    ) -> None:
        self._packages = packages
        self._publications = publications
        self._steps = steps
        self._pause_entity = pause_entity
        self._clock = clock

    async def execute(self, command: UndoPackagePublicationCommand) -> UndoPackagePublicationResult:
        package = await self._packages.get(command.package_id, business_id=command.business_id)
        if package is None:
            raise PackageNotFoundError(str(command.package_id))
        if command.package_hash != package.package_hash.value:
            raise PackageChangedError(package.package_hash.value)

        record = await self._publications.get_by_package_id(command.package_id)
        if record is None:
            raise UndoWindowClosedError(str(command.package_id))
        now = self._clock.now()

        if package.state is PackageState.APPROVED and now < record.started_at + PACKAGE_UNDO_GRACE:
            return await self._cancel(package, record, now)
        if package.state in _PUBLISHED_STATES:
            return await self._pause(command, record)
        # Decision de contrato (revision de codigo, contracts/api.md §R2.C):
        # `undo` NO cubre `PUBLISHING`/`VERIFYING` a proposito. Toda entidad
        # creada durante la saga nace PAUSED (invariante 7, data-model.md)
        # -- nada gasta ni entrega todavia, asi que no hay urgencia de
        # intervenir, y "cancelled_publication" prometeria "no se ha creado
        # nada" cuando pudiera ya haber estructura real (violaria la
        # auditoria: nunca decirle al dueño algo falso). El mecanismo de
        # parada de emergencia para este tramo YA existe y es otro: el
        # freno (`EmergencyBrake`, AL-1/T120) detiene la saga entre pasos
        # sin necesidad de fingir un estado que no es cierto. El dueño
        # recupera el control de "Deshacer" en el primer estado estable
        # que la saga alcance (`published`/`partially_published`/`failed`).
        raise UndoWindowClosedError(str(command.package_id))

    async def _cancel(
        self, package: CampaignPackage, record: PackagePublicationRecord, now: datetime
    ) -> UndoPackagePublicationResult:
        package.invalidate("cancelled_by_owner", now)
        await self._packages.save(package)
        await self._publications.advance(
            record.publication_id,
            cursor=record.cursor,
            state="halted",
            halt_reason="cancelled_by_owner",
            failed_step_index=None,
            finished_at=now,
        )
        return UndoPackagePublicationResult(undo_kind="cancelled_publication")

    async def _pause(
        self, command: UndoPackagePublicationCommand, record: PackagePublicationRecord
    ) -> UndoPackagePublicationResult:
        campaign_ref = await self._confirmed_campaign_ref(record.publication_id, record.envelope)
        if campaign_ref is None:
            # M5 (revision de codigo): la ventana de deshacer SIGUE
            # abierta (el paquete ya esta published/partially_published);
            # lo que falta es el recibo `done` de la campana, no tiempo.
            raise UndoNoConfirmedCampaignError(str(command.package_id))
        try:
            entity_ref = EntityRef.parse(campaign_ref)
        except EntityRefFormatError as exc:  # pragma: no cover - defensive
            raise UndoNoConfirmedCampaignError(str(command.package_id)) from exc
        try:
            result = await self._pause_entity.execute(
                PauseEntityCommand(entity_ref=entity_ref, owner_email=command.owner_email)
            )
        except (EntityActionDeniedError, UnknownEntityError) as exc:
            # M5: la campana SI esta confirmada -- `PauseEntity` la denego
            # por su propia razon (freno, guardarrail, ya pausada), que no
            # es "se te cerro la ventana para deshacer".
            raise UndoPauseDeniedError(str(command.package_id)) from exc
        return UndoPackagePublicationResult(
            undo_kind="campaign_paused",
            campaign_entity_ref=campaign_ref,
            execution_id=str(result.execution_id),
        )

    async def _confirmed_campaign_ref(
        self, publication_id: str, envelope: PackageApprovalEnvelope
    ) -> str | None:
        for step in envelope.step_plan:
            if step.step_kind is StepKind.CREATE_CAMPAIGN:
                record = await self._steps.get(publication_id, step.step_index)
                # M5: `created_entity_ref` solo se fia con `state == "done"`
                # explicito -- nunca por la mera presencia del campo, que
                # hoy tambien deberia ser `None` fuera de `done` (invariante
                # de `RunPackagePublication._persist_step_outcome`), pero
                # este metodo no debe depender de que esa disciplina se
                # mantenga en otro fichero para ser correcto.
                if record is not None and record.state == "done" and record.created_entity_ref:
                    return record.created_entity_ref
                return None
        return None  # pragma: no cover - todo plan firmado lleva CREATE_CAMPAIGN
