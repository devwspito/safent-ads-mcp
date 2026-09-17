"""Adaptador SQL de `BrandKitRepository` sobre `brand_kits`
(0014_brand.py). SQL crudo via `text()`, no ORM (mismo patron que
`iam.infrastructure.sql_owner_repository`): `save()` es un UPSERT sobre el
UNIQUE de `business_id`, coherente con recargar
`config/brand/<business>.yaml` completo en vez de mutar campo a campo."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.claims_policy import normalize_forbidden_claims
from safent_ads.brand.domain.color_palette import ColorPalette, ColorRole, ColorSwatch
from safent_ads.brand.domain.identifiers import BrandKitId
from safent_ads.brand.domain.legal_disclaimer import LegalDisclaimer
from safent_ads.brand.domain.platform_constraint import PlatformConstraint
from safent_ads.brand.domain.tone_of_voice import ToneOfVoice
from safent_ads.brand.domain.typography import Typography
from safent_ads.shared.ids import BusinessId, PlatformCode

_GET_BY_BUSINESS_SQL = text("""
    SELECT id, business_id, primary_font, secondary_font, font_licence_note,
           font_weights::text AS font_weights_text, palette::text AS palette_text,
           tone_description, tone_adjectives::text AS tone_adjectives_text,
           tone_avoid::text AS tone_avoid_text, assets::text AS assets_text,
           claims_allowlist::text AS claims_allowlist_text,
           forbidden_claims::text AS forbidden_claims_text,
           legal_disclaimers::text AS legal_disclaimers_text,
           platform_constraints::text AS platform_constraints_text, is_confirmed,
           confirmed_website_host, updated_at
    FROM brand_kits
    WHERE business_id = :business_id
""")

_UPSERT_SQL = text("""
    INSERT INTO brand_kits (id, business_id, primary_font, secondary_font, font_licence_note,
                             font_weights, palette, tone_description, tone_adjectives,
                             tone_avoid, assets, claims_allowlist, forbidden_claims,
                             legal_disclaimers, platform_constraints, is_confirmed,
                             confirmed_website_host)
    VALUES (:id, :business_id, :primary_font, :secondary_font, :font_licence_note,
            CAST(:font_weights AS JSONB), CAST(:palette AS JSONB), :tone_description,
            CAST(:tone_adjectives AS JSONB), CAST(:tone_avoid AS JSONB),
            CAST(:assets AS JSONB), CAST(:claims_allowlist AS JSONB),
            CAST(:forbidden_claims AS JSONB), CAST(:legal_disclaimers AS JSONB),
            CAST(:platform_constraints AS JSONB), :is_confirmed, :confirmed_website_host)
    ON CONFLICT (business_id) DO UPDATE
    SET primary_font = EXCLUDED.primary_font,
        secondary_font = EXCLUDED.secondary_font,
        font_licence_note = EXCLUDED.font_licence_note,
        font_weights = EXCLUDED.font_weights,
        palette = EXCLUDED.palette,
        tone_description = EXCLUDED.tone_description,
        tone_adjectives = EXCLUDED.tone_adjectives,
        tone_avoid = EXCLUDED.tone_avoid,
        assets = EXCLUDED.assets,
        claims_allowlist = EXCLUDED.claims_allowlist,
        forbidden_claims = EXCLUDED.forbidden_claims,
        legal_disclaimers = EXCLUDED.legal_disclaimers,
        platform_constraints = EXCLUDED.platform_constraints,
        is_confirmed = EXCLUDED.is_confirmed,
        confirmed_website_host = EXCLUDED.confirmed_website_host
    RETURNING updated_at
""")


def _asset_to_json(asset: BrandAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "kind": asset.kind.value,
        "storage_uri": asset.storage_uri,
        "usage_rule": asset.usage_rule,
    }


def _asset_from_json(raw: dict[str, Any]) -> BrandAsset:
    return BrandAsset(
        asset_id=raw["asset_id"],
        kind=AssetKind(raw["kind"]),
        storage_uri=raw["storage_uri"],
        usage_rule=raw["usage_rule"],
    )


def _swatch_to_json(swatch: ColorSwatch) -> dict[str, Any]:
    return {
        "role": swatch.role.value,
        "hex": swatch.hex,
        "contrast_ratio_on_white": swatch.contrast_ratio_on_white,
    }


def _swatch_from_json(raw: dict[str, Any]) -> ColorSwatch:
    return ColorSwatch(
        role=ColorRole(raw["role"]),
        hex=raw["hex"],
        contrast_ratio_on_white=float(raw["contrast_ratio_on_white"]),
    )


def _disclaimer_to_json(disclaimer: LegalDisclaimer) -> dict[str, Any]:
    return {
        "text": disclaimer.text,
        "applies_to": [p.value for p in disclaimer.applies_to]
        if disclaimer.applies_to is not None
        else None,
    }


def _disclaimer_from_json(raw: dict[str, Any]) -> LegalDisclaimer:
    applies_to = raw.get("applies_to")
    return LegalDisclaimer(
        text=raw["text"],
        applies_to=tuple(PlatformCode(p) for p in applies_to) if applies_to is not None else None,
    )


def _constraint_to_json(constraint: PlatformConstraint) -> dict[str, Any]:
    return {
        "platform": constraint.platform.value,
        "max_headline_chars": constraint.max_headline_chars,
        "requires_disclaimer": constraint.requires_disclaimer,
        "notes": constraint.notes,
    }


def _constraint_from_json(raw: dict[str, Any]) -> PlatformConstraint:
    return PlatformConstraint(
        platform=PlatformCode(raw["platform"]),
        max_headline_chars=raw.get("max_headline_chars"),
        requires_disclaimer=bool(raw.get("requires_disclaimer", False)),
        notes=raw.get("notes", ""),
    )


def _row_to_brand_kit(row: Any) -> BrandKit:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return BrandKit(
        brand_kit_id=BrandKitId.parse(str(row.id)),
        business_id=BusinessId.parse(str(row.business_id)),
        typography=Typography(
            primary_family=row.primary_font,
            secondary_family=row.secondary_font,
            licence_note=row.font_licence_note,
            weights=tuple(json.loads(row.font_weights_text)),
        ),
        palette=ColorPalette(
            swatches=tuple(_swatch_from_json(s) for s in json.loads(row.palette_text))
        ),
        tone_of_voice=ToneOfVoice(
            description=row.tone_description,
            adjectives=tuple(json.loads(row.tone_adjectives_text)),
            avoid=tuple(json.loads(row.tone_avoid_text)),
        ),
        updated_at=row.updated_at,
        assets=tuple(_asset_from_json(a) for a in json.loads(row.assets_text)),
        claims_allowlist=frozenset(json.loads(row.claims_allowlist_text)),
        forbidden_claims=normalize_forbidden_claims(json.loads(row.forbidden_claims_text)),
        legal_disclaimers=tuple(
            _disclaimer_from_json(d) for d in json.loads(row.legal_disclaimers_text)
        ),
        platform_constraints=tuple(
            _constraint_from_json(c) for c in json.loads(row.platform_constraints_text)
        ),
        is_confirmed=bool(row.is_confirmed),
        confirmed_website_host=row.confirmed_website_host,
    )


class SqlBrandKitRepository:
    """Vive dentro de la transaccion del `AsyncSession` que le pasan; no
    hace `commit()` (mismo patron que `SqlDecisionLogRepository`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_business(self, business_id: BusinessId) -> BrandKit | None:
        result = await self._session.execute(
            _GET_BY_BUSINESS_SQL, {"business_id": str(business_id)}
        )
        row = result.one_or_none()
        return None if row is None else _row_to_brand_kit(row)

    async def save(self, brand_kit: BrandKit) -> None:
        await self._session.execute(
            _UPSERT_SQL,
            {
                "id": str(brand_kit.brand_kit_id),
                "business_id": str(brand_kit.business_id),
                "primary_font": brand_kit.typography.primary_family,
                "secondary_font": brand_kit.typography.secondary_family,
                "font_licence_note": brand_kit.typography.licence_note,
                "font_weights": json.dumps(list(brand_kit.typography.weights)),
                "palette": json.dumps([_swatch_to_json(s) for s in brand_kit.palette.swatches]),
                "tone_description": brand_kit.tone_of_voice.description,
                "tone_adjectives": json.dumps(list(brand_kit.tone_of_voice.adjectives)),
                "tone_avoid": json.dumps(list(brand_kit.tone_of_voice.avoid)),
                "assets": json.dumps([_asset_to_json(a) for a in brand_kit.assets]),
                "claims_allowlist": json.dumps(sorted(brand_kit.claims_allowlist)),
                "forbidden_claims": json.dumps(sorted(brand_kit.forbidden_claims)),
                "legal_disclaimers": json.dumps(
                    [_disclaimer_to_json(d) for d in brand_kit.legal_disclaimers]
                ),
                "platform_constraints": json.dumps(
                    [_constraint_to_json(c) for c in brand_kit.platform_constraints]
                ),
                "is_confirmed": brand_kit.is_confirmed,
                "confirmed_website_host": brand_kit.confirmed_website_host,
            },
        )


class RequestScopedBrandKitRepository:
    """`BrandKitRepository` real, una sesion por llamada
    (`container.session_factory()`), mismo patron que
    `panel.infrastructure.sql_read_model.RequestScopedPanelReadPort`:
    `build_brand_router` construye `GetBrandKit`/`ListBrandAssets` una vez
    al arrancar la app, asi que esta envoltura es la que hace que cada
    peticion HTTP tenga su propia sesion en vez de compartir una entre
    todas."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_business(self, business_id: BusinessId) -> BrandKit | None:
        async with self._session_factory() as session:
            return await SqlBrandKitRepository(session).get_by_business(business_id)

    async def save(self, brand_kit: BrandKit) -> None:
        async with self._session_factory() as session:
            await SqlBrandKitRepository(session).save(brand_kit)
            await session.commit()
