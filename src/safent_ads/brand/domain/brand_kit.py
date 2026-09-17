"""`BrandKit`: agregado raiz de `brand`, uno por negocio (tool-surface.md
§2.2, §6). Reconstruido entero en cada lectura/escritura (mismo patron que
`catalog.CalendarEvent`): el propietario reemplaza el YAML completo en vez
de editar campo a campo, asi que casi no hay metodos de mutacion parcial,
solo invariantes de construccion y consultas puras. `replace_claims` es la
unica excepcion (`UpdateBrandClaims`, `PUT /brand/claims`): gobierna solo
los tres campos de politica de reclamos, con el mismo reemplazo entero
-nunca fusion campo a campo- que `ConfirmBrandDraft` ya aplica a
typography/palette/tono."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.claims_policy import (
    DEFAULT_FORBIDDEN_CLAIMS,
    contains_forbidden_claim,
    find_allow_forbid_conflicts,
    normalize_claims_allowlist,
    normalize_forbidden_claims,
)
from safent_ads.brand.domain.color_palette import ColorPalette
from safent_ads.brand.domain.errors import (
    AllowedClaimConflictsWithForbiddenError,
    MissingBaselineForbiddenClaimsError,
)
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.domain.platform_constraint import PlatformConstraint
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.shared.ids import BusinessId

PLACEHOLDER_MARKER = "REEMPLAZAR"
"""Marca que `config/brand/<business>.yaml` usa en cada valor que el
propietario debe rellenar (nunca inventamos su marca real). `is_complete()`
la busca para no reportar un kit a medias como si fuera utilizable."""

_PLACEHOLDER_MARKER_CF = PLACEHOLDER_MARKER.casefold()


@dataclass(frozen=True, kw_only=True, slots=True)
class BrandKit:
    brand_kit_id: BrandKitId
    business_id: BusinessId
    typography: Typography
    palette: ColorPalette
    tone_of_voice: ToneOfVoice
    updated_at: datetime
    assets: tuple[BrandAsset, ...] = ()
    claims_allowlist: frozenset[str] = frozenset()
    forbidden_claims: frozenset[str] = frozenset()
    legal_disclaimers: tuple[LegalDisclaimer, ...] = ()
    platform_constraints: tuple[PlatformConstraint, ...] = ()
    is_confirmed: bool = True
    """`False` solo para el kit que produce `BrandDiscoveryDraft.merge_into_kit`
    (discovery.py, tool-surface.md ampliado: "El MCP debe pedir el sitio
    web... el usuario puede subir manual o poner el enlace"): un rastreo
    automatico nunca se usa como fuente de verdad hasta que el propietario
    lo confirma (`ConfirmBrandDraft`). Por defecto `True` porque todo kit
    cargado desde `config/brand/<business>.yaml` o construido a mano ya es
    autoria directa del propietario, no una conjetura del rastreador."""
    confirmed_website_host: str | None = None
    """F-8 (checklists/website-brand-extractor-review.md): el host que la
    herramienta MCP `ingest_brand_from_website` puede rastrear SIN URL
    libre -- lo fija `IngestBrandFromWebsite.execute` con el host de la
    URL que un propietario autenticado acaba de teclear por REST (nunca
    el MCP, que nunca acepta una URL -- solo relee este campo). `None`
    hasta el primer rastreo exitoso por REST: la herramienta MCP debe
    reportarlo y detenerse, nunca inventar un dominio."""

    def __post_init__(self) -> None:
        baseline = {claim.casefold() for claim in DEFAULT_FORBIDDEN_CLAIMS}
        present = {claim.casefold() for claim in self.forbidden_claims}
        if not baseline <= present:
            missing = baseline - present
            raise MissingBaselineForbiddenClaimsError(
                f"forbidden_claims no incluye el suelo de seguridad: {sorted(missing)}"
            )

    def logos(self) -> tuple[BrandAsset, ...]:
        return tuple(asset for asset in self.assets if asset.is_logo())

    def assets_of_kind(self, kind: AssetKind | None) -> tuple[BrandAsset, ...]:
        if kind is None:
            return self.assets
        return tuple(asset for asset in self.assets if asset.kind == kind)

    def asset_by_id(self, asset_id: str) -> BrandAsset | None:
        """Mismo patron que `BrandDiscoveryDraft.logo_by_asset_id`
        (discovery.py): usado por `GetBrandAssetPreview` para resolver un
        `asset_id` opaco a su `storage_uri` sin que la peticion HTTP toque
        nunca una ruta de fichero directamente."""
        return next((asset for asset in self.assets if asset.asset_id == asset_id), None)

    def is_claim_forbidden(self, text: str) -> bool:
        return contains_forbidden_claim(text, self.forbidden_claims)

    def replace_claims(
        self,
        *,
        claims_allowlist: Iterable[str],
        forbidden_claims: Iterable[str],
        legal_disclaimers: tuple[LegalDisclaimer, ...],
        updated_at: datetime,
    ) -> BrandKit:
        """`UpdateBrandClaims` (`PUT /brand/claims`): reemplaza entera la
        politica de reclamos y avisos legales, nunca fusiona campo a
        campo. `forbidden_claims` pasa por `normalize_forbidden_claims`
        para que el suelo de seguridad siga presente sin que quien llama
        tenga que acordarse -- el dominio nunca confia en que la capa de
        arriba ya lo hizo."""
        allowlist = normalize_claims_allowlist(claims_allowlist)
        effective_forbidden = normalize_forbidden_claims(forbidden_claims)
        conflicts = find_allow_forbid_conflicts(allowlist, effective_forbidden)
        if conflicts:
            raise AllowedClaimConflictsWithForbiddenError(
                f"reclamos permitidos en conflicto con prohibidos: {sorted(conflicts)}"
            )
        return replace(
            self,
            claims_allowlist=allowlist,
            forbidden_claims=effective_forbidden,
            legal_disclaimers=legal_disclaimers,
            updated_at=updated_at,
        )

    def is_complete(self) -> bool:
        """`False` si faltan bloques obligatorios o si alguno todavia lleva
        el marcador de plantilla -- una fuente honesta para
        `get_project_context`/`get_capabilities` (tool-surface.md §0: "la
        capacidad se declara, nunca se finge")."""
        if not self.is_confirmed:
            return False
        if not self.assets or not self.palette.swatches:
            return False
        return not self._has_placeholder_marker()

    def _has_placeholder_marker(self) -> bool:
        texts = [
            self.typography.primary_family,
            self.typography.licence_note,
            self.tone_of_voice.description,
            *(disclaimer.text for disclaimer in self.legal_disclaimers),
            *(asset.usage_rule for asset in self.assets),
            *(asset.storage_uri for asset in self.assets),
        ]
        return any(_PLACEHOLDER_MARKER_CF in text.casefold() for text in texts)
