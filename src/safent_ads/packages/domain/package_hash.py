"""`PackageHash` -- huella canonica del arbol declarado del paquete
(data-model.md `PackageHash`; T013). Reutiliza `canonical_json_bytes`/
`to_jsonable` de `proposals.domain.diff_hash` -- no reimplementa la
canonicalizacion (claves ordenadas, separadores compactos, `Money`/`Decimal`
resueltos por `to_jsonable`).

data-model.md "Revision 2" §R2.1 (threat-model.md BL-1) es la fuente de
verdad de la forma exacta: **sustituye** la regla original "nunca contiene
identificadores de plataforma" por "excluye unicamente los identificadores
que crea la propia saga". La huella cubre `business_id`, `platform`,
`account_ref` (forma larga, con `connection_id`), `publish_as` (la pagina
Meta ya existente desde la que se publica), `offering_id`, el dinero
declarado (`daily` + `duration_days` -- `total_cap`/`monthly_equivalent`
son derivados y quedan fuera), el arbol completo y el porque. Cambiar
`account_ref`, `connection_id` o `publish_as.page_id` entre proponer y
aprobar cambia la huella y dispara `409 PACKAGE_CHANGED` en el borde HTTP
(INV-11)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.domain.planned_tree import PlannedAdSet, PlannedCampaign
from safent_ads.packages.domain.values import MetaPublishAs, PackageRationale, ResearchSummary
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

_HASH_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_PACKAGE_HASH_SCHEMA_VERSION = 2


class PackageHashFormatError(ValueError):
    """La cadena no es una huella SHA-256 valida (64 caracteres hex)."""


@dataclass(frozen=True, slots=True)
class PackageHash:
    """Firma del contenido declarado. **Nunca** se confia en un valor
    almacenado: se recalcula siempre desde el payload vivo (invariante 8)."""

    value: str

    def __post_init__(self) -> None:
        if len(self.value) != _HASH_LENGTH or any(char not in _HEX_DIGITS for char in self.value):
            raise PackageHashFormatError(f"PackageHash invalido: {self.value!r}")

    def __str__(self) -> str:
        return self.value


def compute_package_hash(payload: dict[str, object]) -> PackageHash:
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return PackageHash(digest)


def package_tree_payload(
    *,
    business_id: BusinessId,
    platform: PlatformCode,
    account_ref: EntityRef,
    publish_as: MetaPublishAs | None,
    offering_id: OfferingId,
    campaign: PlannedCampaign,
    ad_sets: tuple[PlannedAdSet, ...],
    daily_budget: Money,
    rationale: PackageRationale,
    research: ResearchSummary | None,
) -> dict[str, object]:
    """Proyeccion canonica exacta de `data-model.md §R2.1`. Nunca incluye
    identificadores creados por la propia saga (`entity_ref` de campana/
    conjunto/anuncio, manejador de plataforma de la imagen, `publication_id`,
    `proposal_id`, `execution_id`) ni valores derivados o de presentacion
    (`total_cap`, `monthly_equivalent`, `*_plain` calculados de `native`,
    `expires_at`) -- solo lo que el dueño vio y firmo."""
    return {
        "schema_version": _PACKAGE_HASH_SCHEMA_VERSION,
        "business_id": str(business_id),
        "platform": platform.value,
        "account_ref": str(account_ref),
        "publish_as": publish_as.to_canonical() if publish_as is not None else None,
        "offering_id": str(offering_id),
        "budget": {"daily": daily_budget, "duration_days": campaign.duration_days},
        "plan": {
            "campaign": _campaign_plan_payload(campaign),
            "ad_sets": [ad_set.to_canonical() for ad_set in ad_sets],
        },
        "rationale": {
            "owner_request": rationale.owner_request,
            "why": rationale.why,
            "success_criterion": campaign.success_criterion,
            "kill_criterion": campaign.kill_criterion,
        },
        "research": research.to_canonical() if research is not None else None,
    }


def _campaign_plan_payload(campaign: PlannedCampaign) -> dict[str, object]:
    return {
        "name": campaign.name,
        "objective": campaign.objective.value,
        "native": campaign.native.to_canonical(),
    }
