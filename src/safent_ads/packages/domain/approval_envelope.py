"""`PackageApprovalEnvelope` -- lo que el dueño firma **una vez**, al pulsar
"Aprobar y publicar" (data-model.md "Revision 2" §R2.2; tasks.md T102).

No es un diff y no es una propuesta: es una decision sobre un paquete
entero, con su plan de pasos ORDENADO y DENSO. `project_step_template` es
pura, determinista, sin reloj y sin E/S -- produce la carga exacta que se
va a escribir, con cada valor que la saga resolvera en ejecucion
sustituido por un hueco simbolico. Solo existen DOS huecos (R2.2):

1. El `entity_ref` del padre viaja en `WriteIntent.entity_ref`, **nunca**
   dentro de la carga -- por eso ningun payload de esta capa contiene un
   simbolo `"{parent}"`: el padre no es un campo del paso proyectado.
2. `"{creative_of:<local_ref>}"` para el manejador de plataforma de una
   imagen subida en un paso `UPLOAD_CREATIVE` anterior del mismo paquete.

Cualquier otro valor que dependiera de la ejecucion seria un error de
diseño: `project_step_template` lanza `UnresolvableStepTemplateError` en
vez de inventar un hueco nuevo.

Alcance de esta entrega (T102, absorbido en T015): los tipos de valor
(`StepTemplate`, `PackageApprovalEnvelope`), la derivacion pura del plan de
pasos (`derive_step_plan`) y la proyeccion/sustitucion/reapertura de huecos.
Las siete reglas de admision que VERIFICAN un `PackageStepBinding` contra
un recibo confirmado (R2.4/T103, `step_binding.py`) y el cableado con
chokepoint/broker (T104, T106-T113) quedan fuera de este fichero -- ver la
nota de alcance en `step_binding.py`."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.packages.domain.holes import creative_hole, image_local_ref
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.packages.domain.planned_tree import (
    CAMPAIGN_LOCAL_REF,
    GoogleAssetGroupNative,
    PlannedAd,
    PlannedAdSet,
)
from safent_ads.packages.domain.platform_completeness import (
    ad_set_wire_plan,
    ad_wire_plan,
    campaign_wire_plan,
)
from safent_ads.packages.domain.values import ImageCreativeRef
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

# data-model.md §R2.2 dice "3..17 entradas", pero ese techo solo cuadra
# contando CREATE_CAMPAIGN(1) + CREATE_AD_SET(<=3) + CREATE_AD(<=12) +
# ACTIVATE_CAMPAIGN(1) = 17 y OMITE los pasos UPLOAD_CREATIVE que R2.7
# anade delante (uno por checksum distinto, hasta 12 en el peor caso: cada
# anuncio con una imagen propia). Un paquete al limite estructural
# (invariante 2: <=3 conjuntos, <=12 anuncios) puede necesitar hasta
# 12 + 1 + 3 + 12 + 1 = 29 pasos. Se amplia el techo a ese numero derivado
# de las cotas ya vivas en `campaign_package.py` en vez de dejar que un
# paquete legal (ya aceptado por `CampaignPackage.propose`) no pueda
# aprobarse -- nota de implementacion para el proximo repaso del contrato.
#
# D-4/AL-7 (tasks.md T044, threat-model.md): Maximo Rendimiento anade un
# SEGUNDO peor caso, no una suma sobre el de arriba -- las tres imagenes
# del grupo de recursos viajan en `native.assets`, nunca en `PlannedAd.
# creative`, asi que nunca comparten paquete con anuncios de Meta (unica
# plataforma cuyo anuncio lleva imagen; RSA/Display/Demand Gen de Google
# son texto puro, `TextOnlyCreativeRef`, forzado por `_require_ads_match_
# platform`/`_require_ad_set_platform_matches_campaign`). Peor caso Maximo
# Rendimiento: <=3 grupos de recursos x 3 imagenes = 9 UPLOAD_CREATIVE +
# CREATE_CAMPAIGN(1) + CREATE_AD_SET(<=3, sin CREATE_AD, `ads_per_node`
# vale (0, 0)) + ACTIVATE_CAMPAIGN(1) = 14 pasos. `max(27, 14) + 2 = 29`:
# el techo de hoy sigue siendo el correcto, sin ampliarlo -- se deja
# escrita la aritmetica del segundo caso para que el proximo canal cuyo
# anuncio SI lleve imagen (Demand Gen/Display, threat-model.md nota T044)
# tenga que recalcular esta cota explicitamente, nunca asumir que "cabe".
_MIN_STEP_PLAN_LENGTH = 3
_MAX_STEP_PLAN_LENGTH = 29
_ENVELOPE_VERSION = 2


class UnresolvableStepTemplateError(PackageDomainError):
    """`project_step_template` no sabe representar el paso pedido -- nunca
    inventa un hueco nuevo (R2.2)."""


class StepKind(StrEnum):
    UPLOAD_CREATIVE = "UPLOAD_CREATIVE"
    CREATE_CAMPAIGN = "CREATE_CAMPAIGN"
    CREATE_AD_SET = "CREATE_AD_SET"
    CREATE_AD = "CREATE_AD"
    ACTIVATE_CAMPAIGN = "ACTIVATE_CAMPAIGN"


@dataclass(frozen=True, slots=True)
class StepTemplate:
    """Una entrada del plan de pasos firmado (R2.2). `depends_on` no esta
    en la lista literal de `data-model.md`, pero es necesaria para que
    `PackageStepBinding.creative_sources` (R2.3) sea derivable **solo**
    del sobre firmado sin volver a consultar el plan vivo -- si no,
    "todo campo salvo `parent_entity_ref` es derivable del sobre" seria
    falso para ese campo."""

    step_index: int
    step_kind: StepKind
    local_ref: str
    parent_local_ref: str | None
    payload_template_hash: str
    expected_done_steps: int | None = None
    depends_on: tuple[str, ...] = ()

    def to_canonical(self) -> dict[str, object]:
        return {
            "step_index": self.step_index,
            "step_kind": self.step_kind.value,
            "local_ref": self.local_ref,
            "parent_local_ref": self.parent_local_ref,
            "payload_template_hash": self.payload_template_hash,
            "expected_done_steps": self.expected_done_steps,
            "depends_on": list(self.depends_on),
        }


# ---------------------------------------------------------------------------
# project_step_template -- pura, determinista, sin reloj, sin E/S
# ---------------------------------------------------------------------------


def project_step_template(
    package: CampaignPackage, step_kind: StepKind, local_ref: str
) -> dict[str, object]:
    if step_kind is StepKind.UPLOAD_CREATIVE:
        return _upload_creative_template(package, local_ref)
    if step_kind is StepKind.CREATE_CAMPAIGN:
        return campaign_wire_plan(package.campaign)
    if step_kind is StepKind.CREATE_AD_SET:
        ad_set = _find_ad_set(package, local_ref)
        return ad_set_wire_plan(ad_set, package.account_ref.platform)
    if step_kind is StepKind.CREATE_AD:
        return _create_ad_template(package, local_ref)
    if step_kind is StepKind.ACTIVATE_CAMPAIGN:
        return {"status": "ACTIVE"}
    raise UnresolvableStepTemplateError(f"step_kind desconocido: {step_kind!r}")  # pragma: no cover


def _find_ad_set(package: CampaignPackage, local_ref: str) -> PlannedAdSet:
    for ad_set in package.ad_sets:
        if ad_set.local_ref.value == local_ref:
            return ad_set
    raise UnresolvableStepTemplateError(f"ad_set no encontrado: {local_ref!r}")


def _find_ad(package: CampaignPackage, local_ref: str) -> PlannedAd:
    for ad_set in package.ad_sets:
        for ad in ad_set.ads:
            if ad.local_ref.value == local_ref:
                return ad
    raise UnresolvableStepTemplateError(f"anuncio no encontrado: {local_ref!r}")


def _images(package: CampaignPackage) -> list[ImageCreativeRef]:
    """Todas las imagenes firmadas del paquete: las de los anuncios y,
    para Maximo Rendimiento (T024), las del propio grupo de recursos --
    el grupo ES el anuncio, asi que sus tres imagenes tambien necesitan un
    paso `UPLOAD_CREATIVE` (uno por `checksum` distinto, R2.7 sin
    excepcion de canal)."""
    return [
        ad.creative
        for ad_set in package.ad_sets
        for ad in ad_set.ads
        if isinstance(ad.creative, ImageCreativeRef)
    ] + [image for ad_set in package.ad_sets for image in asset_group_images(ad_set)]


def asset_group_images(ad_set: PlannedAdSet) -> tuple[ImageCreativeRef, ...]:
    """Las tres imagenes del grupo de recursos (T024), publica (T044) para
    que `chokepoint_step_executor._find_asset_id_by_checksum` resuelva el
    mismo `checksum` sin reimplementar este recorrido."""
    if isinstance(ad_set.native, GoogleAssetGroupNative):
        return ad_set.native.assets.images
    return ()


def ad_equivalent_local_refs(package: CampaignPackage) -> frozenset[str]:
    """Los `local_ref` de los nodos que SON el anuncio (tasks.md T043,
    BL-3): un `PlannedAd` normal, o el propio grupo de recursos en Maximo
    Rendimiento (0 `PlannedAd`, el nodo de segundo nivel ES el anuncio).
    Fuente UNICA para `expected_done_steps` (`derive_step_plan`, abajo) y
    para la reverificacion de `RunPackagePublication` antes de
    `ACTIVATE_CAMPAIGN` -- nunca dos formas de contar lo mismo."""
    refs: set[str] = set()
    for ad_set in package.ad_sets:
        if isinstance(ad_set.native, GoogleAssetGroupNative):
            refs.add(ad_set.local_ref.value)
        else:
            refs.update(ad.local_ref.value for ad in ad_set.ads)
    return frozenset(refs)


def _upload_creative_template(package: CampaignPackage, local_ref: str) -> dict[str, object]:
    for creative in _images(package):
        if image_local_ref(creative.checksum) == local_ref:
            return {
                "checksum": creative.checksum,
                "mime_type": creative.mime_type,
                "width": creative.width,
                "height": creative.height,
            }
    raise UnresolvableStepTemplateError(f"creatividad no encontrada: {local_ref!r}")


def _create_ad_template(package: CampaignPackage, local_ref: str) -> dict[str, object]:
    """`ad_wire_plan` (`platform_completeness.py`) es la forma exacta que
    `ad_child_creation.validate_child_payload` exige: la misma que
    `chokepoint_step_executor` firma y ejecuta, con el `{creative_of:X}`
    todavia sin resolver -- ninguna diferencia de forma entre lo firmado
    y lo escrito (R7)."""
    ad = _find_ad(package, local_ref)
    return ad_wire_plan(
        ad,
        package.account_ref.platform,
        page_id=package.publish_as.page_id if package.publish_as is not None else None,
        image_hash=_image_hash_hole(ad),
    )


def _image_hash_hole(ad: PlannedAd) -> str | None:
    if isinstance(ad.creative, ImageCreativeRef):
        return creative_hole(image_local_ref(ad.creative.checksum))
    return None


def compute_payload_template_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# substitute_holes / reopen_holes -- reversibilidad exigida por INV-12
# ---------------------------------------------------------------------------


def substitute_holes(payload: object, resolutions: dict[str, str]) -> object:
    if isinstance(payload, dict):
        return {key: substitute_holes(value, resolutions) for key, value in payload.items()}
    if isinstance(payload, list):
        return [substitute_holes(item, resolutions) for item in payload]
    if isinstance(payload, str) and payload in resolutions:
        return resolutions[payload]
    return payload


def reopen_holes(payload: object, resolutions: dict[str, str]) -> object:
    reverse = {resolved: hole for hole, resolved in resolutions.items()}
    return substitute_holes(payload, reverse)


def project_step(
    package: CampaignPackage, step_kind: StepKind, local_ref: str, resolutions: dict[str, str]
) -> object:
    return substitute_holes(project_step_template(package, step_kind, local_ref), resolutions)


# ---------------------------------------------------------------------------
# derive_step_plan -- orden fijo y verificable (R2.2)
# ---------------------------------------------------------------------------


def derive_step_plan(package: CampaignPackage) -> tuple[StepTemplate, ...]:
    """Todas las `UPLOAD_CREATIVE` (una por `checksum` distinto, ordenadas
    por `checksum`) -> `CREATE_CAMPAIGN` -> `CREATE_AD_SET` en orden de
    `local_ref` -> `CREATE_AD` en orden de `local_ref` -> `ACTIVATE_CAMPAIGN`,
    siempre la ultima. Pura: mismo paquete -> mismo plan, siempre."""
    steps: list[StepTemplate] = []
    _append_upload_creative_steps(package, steps)
    _append_step(steps, package, StepKind.CREATE_CAMPAIGN, CAMPAIGN_LOCAL_REF, None)
    for ad_set in package.ad_sets:
        _append_step(
            steps,
            package,
            StepKind.CREATE_AD_SET,
            ad_set.local_ref.value,
            CAMPAIGN_LOCAL_REF,
            depends_on=_ad_set_depends_on(ad_set),
        )
    for ad_set in package.ad_sets:
        for ad in ad_set.ads:
            _append_ad_step(steps, package, ad, ad_set.local_ref.value)
    # T043/BL-3: nunca vale 0 -- `ad_equivalent_local_refs` cuenta el
    # grupo de recursos como el anuncio en Maximo Rendimiento, donde
    # `len(ad_set.ads)` (el recuento de antes) siempre daba cero.
    expected_done_steps = len(ad_equivalent_local_refs(package))
    _append_step(
        steps,
        package,
        StepKind.ACTIVATE_CAMPAIGN,
        CAMPAIGN_LOCAL_REF,
        CAMPAIGN_LOCAL_REF,
        expected_done_steps=expected_done_steps,
    )
    return tuple(steps)


def _unique_sorted_checksums(package: CampaignPackage) -> tuple[str, ...]:
    return tuple(sorted({creative.checksum for creative in _images(package)}))


def _append_upload_creative_steps(package: CampaignPackage, steps: list[StepTemplate]) -> None:
    for checksum in _unique_sorted_checksums(package):
        _append_step(steps, package, StepKind.UPLOAD_CREATIVE, image_local_ref(checksum), None)


def _ad_depends_on(ad: PlannedAd) -> tuple[str, ...]:
    if isinstance(ad.creative, ImageCreativeRef):
        return (image_local_ref(ad.creative.checksum),)
    return ()


def _ad_set_depends_on(ad_set: PlannedAdSet) -> tuple[str, ...]:
    """Maximo Rendimiento (T024): el paso `CREATE_AD_SET` del grupo de
    recursos depende de sus tres `UPLOAD_CREATIVE`, igual que un anuncio
    depende del suyo (`_ad_depends_on`) -- mismo hueco, mismo mecanismo,
    ningun tipo de hueco nuevo."""
    return tuple(
        sorted({image_local_ref(image.checksum) for image in asset_group_images(ad_set)})
    )


def _append_ad_step(
    steps: list[StepTemplate], package: CampaignPackage, ad: PlannedAd, ad_set_local_ref: str
) -> None:
    _append_step(
        steps,
        package,
        StepKind.CREATE_AD,
        ad.local_ref.value,
        ad_set_local_ref,
        depends_on=_ad_depends_on(ad),
    )


def _append_step(
    steps: list[StepTemplate],
    package: CampaignPackage,
    step_kind: StepKind,
    local_ref: str,
    parent_local_ref: str | None,
    *,
    expected_done_steps: int | None = None,
    depends_on: tuple[str, ...] = (),
) -> None:
    template = project_step_template(package, step_kind, local_ref)
    steps.append(
        StepTemplate(
            step_index=len(steps),
            step_kind=step_kind,
            local_ref=local_ref,
            parent_local_ref=parent_local_ref,
            payload_template_hash=compute_payload_template_hash(template),
            expected_done_steps=expected_done_steps,
            depends_on=depends_on,
        )
    )


# ---------------------------------------------------------------------------
# PackageApprovalEnvelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageApprovalEnvelope:
    """La carga que el propietario firma, una vez, al pulsar «Aprobar y
    publicar» (R2.2). `publication_id`/`approved_by`/`approved_at`/
    `approval_expires_at` los resuelve la capa de aplicacion (sesion,
    reloj, `PackagePublication` ya creada) -- no son datos del arbol
    declarado, por eso no vienen de `CampaignPackage`."""

    package_id: PackageId
    package_hash: str
    business_id: BusinessId
    platform: PlatformCode
    account_ref: EntityRef
    publication_id: str
    approved_by: str
    approved_at: datetime
    approval_expires_at: datetime
    step_plan: tuple[StepTemplate, ...]
    envelope_version: int = field(default=_ENVELOPE_VERSION, init=False)

    def __post_init__(self) -> None:
        _require_dense_step_plan(self.step_plan)
        _require_activate_campaign_last_and_unique(self.step_plan)

    def to_canonical(self) -> dict[str, object]:
        return {
            "envelope_version": self.envelope_version,
            "package_id": str(self.package_id),
            "package_hash": self.package_hash,
            "business_id": str(self.business_id),
            "platform": self.platform.value,
            "account_ref": str(self.account_ref),
            "publication_id": self.publication_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "approval_expires_at": self.approval_expires_at.isoformat(),
            "step_plan": [step.to_canonical() for step in self.step_plan],
        }


def _require_dense_step_plan(step_plan: tuple[StepTemplate, ...]) -> None:
    if not (_MIN_STEP_PLAN_LENGTH <= len(step_plan) <= _MAX_STEP_PLAN_LENGTH):
        raise PackageDomainError("package_approval_envelope_step_plan_size_invalid")
    for expected_index, step in enumerate(step_plan):
        if step.step_index != expected_index:
            raise PackageDomainError("package_approval_envelope_step_plan_not_dense")


def _require_activate_campaign_last_and_unique(step_plan: tuple[StepTemplate, ...]) -> None:
    if step_plan[-1].step_kind is not StepKind.ACTIVATE_CAMPAIGN:
        raise PackageDomainError("package_approval_envelope_activate_not_last")
    if any(step.step_kind is StepKind.ACTIVATE_CAMPAIGN for step in step_plan[:-1]):
        raise PackageDomainError("package_approval_envelope_activate_not_unique")


def compute_envelope_hash(envelope: PackageApprovalEnvelope) -> str:
    return hashlib.sha256(canonical_json_bytes(envelope.to_canonical())).hexdigest()


def package_approval_signing_payload(envelope: PackageApprovalEnvelope) -> dict[str, object]:
    """contracts/api.md §R2.E: `SignedPackageApproval.signature` es "Ed25519
    sobre `package_approval_signing_payload(envelope)`" -- una firma
    DEDICADA sobre el sobre, deliberadamente distinta de
    `Authorization.signing_payload()` (que ademas mete
    `guardrail_verdict_hash`, un dato que `PackageApprovalProof` no
    transporta: el bróker no puede reconstruirla sin él). Firmar el sobre
    aparte, con la MISMA clave que ya firma `Authorization`
    (`ApproveCampaignPackage._signer`), es autosuficiente: el bróker
    verifica sin preguntarle nada a `ads-api` y sin depender de un campo que
    no viaja. Identico a `to_canonical()` hoy -- funcion propia para que el
    significado ("esto es lo que el sobre firma") no dependa de que
    `to_canonical()` siga usandose tambien para el `envelope_hash`."""
    return envelope.to_canonical()


def build_approval_envelope(
    package: CampaignPackage,
    *,
    publication_id: str,
    approved_by: str,
    approved_at: datetime,
    approval_expires_at: datetime,
) -> PackageApprovalEnvelope:
    """Deriva el sobre completo de un paquete `PROPOSED`/`APPROVED`. Pura
    salvo por los cuatro datos que la aplicacion ya resolvio (sesion,
    reloj, `PackagePublication`); el plan de pasos siempre sale de
    `derive_step_plan`, nunca se acepta uno ya construido."""
    return PackageApprovalEnvelope(
        package_id=package.package_id,
        package_hash=package.package_hash.value,
        business_id=package.business_id,
        platform=package.account_ref.platform,
        account_ref=package.account_ref,
        publication_id=publication_id,
        approved_by=approved_by,
        approved_at=approved_at,
        approval_expires_at=approval_expires_at,
        step_plan=derive_step_plan(package),
    )
