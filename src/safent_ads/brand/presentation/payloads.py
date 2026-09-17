"""Patrones y modelos compartidos por `mcp_tools.py` y `router.py` (mismo
reparto que `creative.presentation.payloads`): la misma validacion
estricta (threat-model.md C-11) para las dos superficies de `brand`.

A diferencia de `creative.presentation.payloads.CreativeBriefPayload`
(que lleva `business_id` dentro del cuerpo), aqui `business_id` NUNCA
viaja en el payload compartido: en `brand` siempre llega por el
`business_id` que ya paso `require_business_access` (query en REST, campo
de nivel superior en MCP) -- un unico punto de autorizacion por peticion,
nunca dos copias del mismo dato que puedan divergir. `mcp_tools.py`
extiende estos payloads anadiendo `business_id`; `router.py` los usa tal
cual como cuerpo de la peticion."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraftRequest
from safent_ads.brand.application.update_brand_claims import UpdateBrandClaimsRequest
from safent_ads.brand.domain.color_palette import ColorRole, ColorSwatch
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.shared.ids import BusinessId, PlatformCode

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
OPAQUE_ASSET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
# `validate_discovery_url` (domain) exige el resto (host real, sin
# credenciales, sin literal IP); el patron de superficie solo aplica la
# defensa en profundidad de threat-model.md C-11: nunca un esquema libre.
DISCOVERY_URL_PATTERN = r"^https?://[^\s]{1,2000}$"

_MAX_TEXT_FIELD_LEN = 200
_MAX_LIST_ITEMS = 20
# `PUT /brand/claims` (rest-api.md §Marca): un reclamo de una sola palabra
# ("gratis") o un parrafo entero no son lo que ese campo modela -- un
# reclamo corto y citable, tan estrecho a proposito como el resto de
# limites de esta superficie (defensa en profundidad, threat-model.md C-11).
_MIN_CLAIM_LEN = 2
_MAX_CLAIM_LEN = 80
_MAX_CLAIMS_PER_LIST = 50


class StrictModel(BaseModel):
    """Base comun: rechaza campos no declarados (threat-model.md C-11)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoverBrandPayload(StrictModel):
    """Cuerpo compartido de `ingest_brand_from_website` (MCP) y
    `POST /brand/discover` (REST): el unico argumento libre de `brand`
    (owner request), `business_id` fuera del payload (ver docstring del
    modulo)."""

    url: str = Field(pattern=DISCOVERY_URL_PATTERN)


ClaimText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=_MIN_CLAIM_LEN, max_length=_MAX_CLAIM_LEN
    ),
]


class ColorSwatchPayload(StrictModel):
    role: ColorRole
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    contrast_ratio_on_white: float = Field(ge=1.0, le=21.0)

    def to_domain(self) -> ColorSwatch:
        return ColorSwatch(
            role=self.role, hex=self.hex, contrast_ratio_on_white=self.contrast_ratio_on_white
        )


class ConfirmBrandDraftPayload(StrictModel):
    """Cuerpo compartido de `confirm_brand_draft` (MCP) y
    `POST /brand/confirm` (REST): mismo reemplazo entero de
    typography/palette/tono/assets que documenta
    `application/confirm_brand_draft.py`."""

    primary_font: str = Field(min_length=1, max_length=_MAX_TEXT_FIELD_LEN)
    font_licence_note: str = Field(min_length=1, max_length=_MAX_TEXT_FIELD_LEN)
    tone_description: str = Field(min_length=1, max_length=_MAX_TEXT_FIELD_LEN)
    secondary_font: str | None = Field(default=None, max_length=_MAX_TEXT_FIELD_LEN)
    font_weights: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST_ITEMS)
    palette: tuple[ColorSwatchPayload, ...] = Field(default=(), max_length=_MAX_LIST_ITEMS)
    tone_adjectives: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST_ITEMS)
    tone_avoid: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST_ITEMS)
    selected_asset_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST_ITEMS)

    def to_request(self, business_id: BusinessId) -> ConfirmBrandDraftRequest:
        return ConfirmBrandDraftRequest(
            business_id=business_id,
            primary_font=self.primary_font,
            font_licence_note=self.font_licence_note,
            tone_description=self.tone_description,
            secondary_font=self.secondary_font,
            font_weights=self.font_weights,
            palette=tuple(swatch.to_domain() for swatch in self.palette),
            tone_adjectives=self.tone_adjectives,
            tone_avoid=self.tone_avoid,
            selected_asset_ids=self.selected_asset_ids,
        )


class LegalDisclaimerPayload(StrictModel):
    """Un aviso legal de `PUT /brand/claims`: `applies_to=None` es "todas
    las plataformas conectadas" (`LegalDisclaimer.applies_to_platform`).
    Sin limite de 80 caracteres a proposito -- a diferencia de un
    reclamo, un aviso legal real suele ser una frase larga."""

    text: str = Field(min_length=1, max_length=_MAX_TEXT_FIELD_LEN)
    applies_to: tuple[PlatformCode, ...] | None = Field(default=None, max_length=_MAX_LIST_ITEMS)

    def to_domain(self) -> LegalDisclaimer:
        return LegalDisclaimer(text=self.text, applies_to=self.applies_to)


class UpdateBrandClaimsPayload(StrictModel):
    """Cuerpo de `PUT /brand/claims` (rest-api.md §Marca): reemplazo
    entero de la politica de reclamos y avisos legales, el unico bloque
    que `POST /brand/confirm` deliberadamente no gestiona."""

    claims_allowlist: tuple[ClaimText, ...] = Field(default=(), max_length=_MAX_CLAIMS_PER_LIST)
    forbidden_claims: tuple[ClaimText, ...] = Field(default=(), max_length=_MAX_CLAIMS_PER_LIST)
    legal_disclaimers: tuple[LegalDisclaimerPayload, ...] = Field(
        default=(), max_length=_MAX_LIST_ITEMS
    )

    def to_request(self, business_id: BusinessId, *, actor_email: str) -> UpdateBrandClaimsRequest:
        return UpdateBrandClaimsRequest(
            business_id=business_id,
            actor_email=actor_email,
            claims_allowlist=self.claims_allowlist,
            forbidden_claims=self.forbidden_claims,
            legal_disclaimers=tuple(d.to_domain() for d in self.legal_disclaimers),
        )
