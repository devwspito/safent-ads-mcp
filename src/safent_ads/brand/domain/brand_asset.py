"""`BrandAsset`: logo o foto de referencia aprobada (tool-surface.md §2.2
`list_brand_assets`: "Logotipos y fotos aprobadas como referencia de
edicion"). `LOGO_VECTOR`/`LOGO_RASTER` son los "logos"; `REFERENCE_PHOTO`
e `ICON` completan el set que `import_creative_asset`/`edit_product_image`
(fuera de este lane) usaran como referencia."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.brand.domain.errors import BlankFieldError


class AssetKind(StrEnum):
    LOGO_VECTOR = "logo_vector"
    LOGO_RASTER = "logo_raster"
    REFERENCE_PHOTO = "reference_photo"
    ICON = "icon"
    FONT_FILE = "font_file"
    """Tipografia con licencia como fichero (`.woff2/.woff/.ttf/.otf`),
    subida manual (`UploadBrandAsset`) — distinta de `Typography.primary_family`
    (solo el nombre de la familia mas la nota de licencia)."""
    PALETTE_DEFINITION = "palette_definition"
    """Paleta como JSON subido a mano (`UploadBrandAsset`); referencia
    guardada para trazabilidad, no reemplaza `ColorPalette.swatches`, que
    el propietario confirma campo a campo en `ConfirmBrandDraft`."""


_LOGO_KINDS = frozenset({AssetKind.LOGO_VECTOR, AssetKind.LOGO_RASTER})


@dataclass(frozen=True, slots=True, kw_only=True)
class BrandAsset:
    """`usage_rule` es texto para el modelo ("no deformar", "espacio libre
    minimo 2x altura del logo", "solo sobre fondo claro"): la regla vive
    aqui, no en el nombre del fichero."""

    asset_id: str
    kind: AssetKind
    storage_uri: str
    usage_rule: str

    def __post_init__(self) -> None:
        if not self.asset_id.strip():
            raise BlankFieldError("asset_id vacio")
        if not self.storage_uri.strip():
            raise BlankFieldError("storage_uri vacio")

    def is_logo(self) -> bool:
        return self.kind in _LOGO_KINDS
