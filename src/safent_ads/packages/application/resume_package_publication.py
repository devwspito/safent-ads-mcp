"""`ResumePackagePublication` (T025; contracts/api.md §R2.C): reanuda por
el primer paso no hecho, con el MISMO sobre firmado -- "reanudar es
decidir otra vez" (R2.10): exige la huella viva del paquete (anti-CSRF de
hecho, igual que `approve`/`reject`) y, si el sobre humano ya caduco,
deniega en vez de re-acuñar la firma en silencio (AL-2).

Solo transiciona la fila de publicacion (`halted` -> `running`): el propio
`RunPackagePublication` retoma por `cursor`, y la clave de idempotencia de
cada paso (`pkg-<publication_id>-<step_index>`) es estable, asi que
reanudar nunca duplica un paso ya hecho."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from safent_ads.packages.application.errors import (
    PackageApprovalExpiredError,
    PackageChangedError,
    PackageNotFoundError,
    PackageNotResumableError,
    PublicationNotFoundError,
)
from safent_ads.packages.application.ports import (
    CampaignPackageRepository,
    PackagePublicationRecord,
    PackagePublicationRepository,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = [
    "ResumePackagePublication",
    "ResumePackagePublicationCommand",
    "ResumePackagePublicationResult",
]

logger = structlog.get_logger(__name__)

_RESUMABLE_PACKAGE_STATES = frozenset({PackageState.PARTIALLY_PUBLISHED, PackageState.VERIFYING})


@dataclass(frozen=True, kw_only=True, slots=True)
class ResumePackagePublicationCommand:
    business_id: BusinessId
    package_id: PackageId
    package_hash: str
    resumed_by: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ResumePackagePublicationResult:
    publication_id: str
    approval_expires_at: datetime


class ResumePackagePublication:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        publications: PackagePublicationRepository,
        clock: Clock,
    ) -> None:
        self._packages = packages
        self._publications = publications
        self._clock = clock

    async def execute(
        self, command: ResumePackagePublicationCommand
    ) -> ResumePackagePublicationResult:
        package = await self._packages.get(command.package_id, business_id=command.business_id)
        if package is None:
            raise PackageNotFoundError(str(command.package_id))
        if package.state not in _RESUMABLE_PACKAGE_STATES:
            raise PackageNotResumableError(package.state.value)
        if command.package_hash != package.package_hash.value:
            raise PackageChangedError(package.package_hash.value)

        record = await self._publications.get_by_package_id(command.package_id)
        if record is None:  # pragma: no cover - un paquete approved+ siempre tiene publicacion
            raise PublicationNotFoundError(str(command.package_id))
        if not _is_resumable(record, package):
            raise PackageNotResumableError(record.state)

        now = self._clock.now()
        if now >= record.envelope.approval_expires_at:
            raise PackageApprovalExpiredError(str(command.package_id))

        if package.state is PackageState.PARTIALLY_PUBLISHED:
            package.resume_publishing(now)
            await self._packages.save(package)

        await self._publications.advance(
            record.publication_id,
            cursor=record.cursor,
            state="running",
            halt_reason=None,
            failed_step_index=None,
            finished_at=None,
        )
        # H-S1 (revision de codigo): "resume records the acting person" --
        # el sobre firmado NO se re-acuña aqui (sigue vivo, misma firma),
        # asi que `approved_by` del sobre no cambia; quien pulso "Continuar"
        # se deja en la traza de auditoria, no en el sobre.
        logger.info(
            "package_publication_resumed",
            business_id=str(command.business_id),
            package_id=str(command.package_id),
            publication_id=record.publication_id,
            resumed_by=command.resumed_by,
        )
        return ResumePackagePublicationResult(
            publication_id=record.publication_id,
            approval_expires_at=record.envelope.approval_expires_at,
        )


def _is_resumable(record: PackagePublicationRecord, package: CampaignPackage) -> bool:
    """M2 (revision de codigo): un paso `unknown` deja la PUBLICACION en
    `running` a proposito (`RunPackagePublication._stay_running`, BL-4/
    INV-4: nunca se reconcilia por reescritura) mientras el PAQUETE pasa a
    `verifying`. Contracts/api.md §4 permite reanudar desde `verifying`,
    pero exigir `record.state == "halted"` a secas lo dejaba inalcanzable:
    ninguna publicacion `verifying` llega jamas a `halted` por si sola. El
    `resume` sobre una publicacion `running` en este estado es un no-op de
    confirmacion (el cursor/estado ya son los correctos): sirve para que el
    dueño pueda forzar el reintento si el bucle del worker tardase."""
    if record.state == "halted":
        return True
    return record.state == "running" and package.state is PackageState.VERIFYING
