"""Esquema pydantic de `config/brand/<business>.yaml` y su parseo puro
(mismo patron que `broker.infrastructure.caps_config.parse_caps_config`:
sin tocar el sistema de ficheros, para poder probarlo con una cadena)."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorPalette, ColorRole, ColorSwatch
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.domain.platform_constraint import PlatformConstraint
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.brand.infrastructure.errors import BrandKitYamlError
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import BusinessId, PlatformCode


class _AssetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    kind: AssetKind
    storage_uri: str
    usage_rule: str


class _SwatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ColorRole
    hex: str
    contrast_ratio_on_white: float


class _TypographyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_family: str
    licence_note: str
    secondary_family: str | None = None
    weights: list[str] = Field(default_factory=list)


class _ToneOfVoicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    adjectives: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class _DisclaimerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    applies_to: list[PlatformCode] | None = None


class _PlatformConstraintPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: PlatformCode
    max_headline_chars: int | None = None
    requires_disclaimer: bool = False
    notes: str = ""


class BrandKitPayload(BaseModel):
    """Raiz de `config/brand/<business>.yaml` ya validada. `business_id`
    NO vive en el YAML: lo pasa el llamador de `parse_brand_kit_yaml`
    (el operador del comando, nunca el contenido del fichero), para que un
    YAML mal editado no pueda escribir el kit de marca de otro negocio."""

    model_config = ConfigDict(extra="forbid")

    typography: _TypographyPayload
    tone_of_voice: _ToneOfVoicePayload
    palette: list[_SwatchPayload] = Field(default_factory=list)
    assets: list[_AssetPayload] = Field(default_factory=list)
    claims_allowlist: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    legal_disclaimers: list[_DisclaimerPayload] = Field(default_factory=list)
    platform_constraints: list[_PlatformConstraintPayload] = Field(default_factory=list)


def parse_brand_kit_yaml(raw_yaml: str, *, business_id: BusinessId, clock: Clock) -> BrandKit:
    document = _safe_load(raw_yaml)
    payload = _validate_payload(document)
    return _to_brand_kit(payload, business_id=business_id, clock=clock)


def _safe_load(raw_yaml: str) -> object:
    try:
        document = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise BrandKitYamlError(f"YAML invalido: {exc}") from exc
    if not isinstance(document, dict):
        raise BrandKitYamlError(f"la raiz debe ser un mapeo YAML, no {type(document).__name__}")
    return document


def _validate_payload(document: object) -> BrandKitPayload:
    try:
        return BrandKitPayload.model_validate(document)
    except (TypeError, ValueError) as exc:
        raise BrandKitYamlError(f"esquema de kit de marca invalido: {exc}") from exc


def _to_brand_kit(payload: BrandKitPayload, *, business_id: BusinessId, clock: Clock) -> BrandKit:
    try:
        return BrandKit(
            brand_kit_id=BrandKitId.new(),
            business_id=business_id,
            typography=Typography(
                primary_family=payload.typography.primary_family,
                secondary_family=payload.typography.secondary_family,
                licence_note=payload.typography.licence_note,
                weights=tuple(payload.typography.weights),
            ),
            palette=ColorPalette(
                swatches=tuple(
                    ColorSwatch(
                        role=s.role, hex=s.hex, contrast_ratio_on_white=s.contrast_ratio_on_white
                    )
                    for s in payload.palette
                )
            ),
            tone_of_voice=ToneOfVoice(
                description=payload.tone_of_voice.description,
                adjectives=tuple(payload.tone_of_voice.adjectives),
                avoid=tuple(payload.tone_of_voice.avoid),
            ),
            updated_at=clock.now(),
            assets=tuple(
                BrandAsset(
                    asset_id=a.asset_id,
                    kind=a.kind,
                    storage_uri=a.storage_uri,
                    usage_rule=a.usage_rule,
                )
                for a in payload.assets
            ),
            claims_allowlist=frozenset(payload.claims_allowlist),
            forbidden_claims=normalize_forbidden_claims(payload.forbidden_claims),
            legal_disclaimers=tuple(
                LegalDisclaimer(
                    text=d.text,
                    applies_to=tuple(d.applies_to) if d.applies_to is not None else None,
                )
                for d in payload.legal_disclaimers
            ),
            platform_constraints=tuple(
                PlatformConstraint(
                    platform=c.platform,
                    max_headline_chars=c.max_headline_chars,
                    requires_disclaimer=c.requires_disclaimer,
                    notes=c.notes,
                )
                for c in payload.platform_constraints
            ),
        )
    except DomainError as exc:
        raise BrandKitYamlError(f"kit de marca invalido: {exc}") from exc
