"""Adaptador SQL de `BrandDiscoveryDraftRepository` sobre
`brand_discovery_drafts` (0015_brand_discovery.py). SQL crudo via `text()`,
mismo patron que `SqlBrandKitRepository`: `save()` es un UPSERT sobre el
UNIQUE de `business_id`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.color_palette import ColorRole
from safent_ads.brand.domain.discovery import (
    BrandDiscoveryDraft,
    BusinessNameCandidate,
    ColorCandidate,
    ContactChannelCandidate,
    ContactChannelKind,
    CopySample,
    DiscoverySource,
    LogoCandidate,
    SocialLinkCandidate,
    SocialNetwork,
    TypographyCandidate,
)
from safent_ads.shared.ids import BusinessId

_GET_BY_BUSINESS_SQL = text("""
    SELECT id, business_id, source_url, discovered_at,
           logo_candidates::text AS logo_candidates_text,
           color_candidates::text AS color_candidates_text,
           typography_candidates::text AS typography_candidates_text,
           business_name_candidates::text AS business_name_candidates_text,
           social_links::text AS social_links_text,
           contact_channels::text AS contact_channels_text,
           copy_samples::text AS copy_samples_text
    FROM brand_discovery_drafts
    WHERE business_id = :business_id
""")

_UPSERT_SQL = text("""
    INSERT INTO brand_discovery_drafts (
        business_id, source_url, discovered_at, logo_candidates, color_candidates,
        typography_candidates, business_name_candidates, social_links, contact_channels,
        copy_samples
    )
    VALUES (
        :business_id, :source_url, :discovered_at, CAST(:logo_candidates AS JSONB),
        CAST(:color_candidates AS JSONB), CAST(:typography_candidates AS JSONB),
        CAST(:business_name_candidates AS JSONB), CAST(:social_links AS JSONB),
        CAST(:contact_channels AS JSONB), CAST(:copy_samples AS JSONB)
    )
    ON CONFLICT (business_id) DO UPDATE
    SET source_url = EXCLUDED.source_url,
        discovered_at = EXCLUDED.discovered_at,
        logo_candidates = EXCLUDED.logo_candidates,
        color_candidates = EXCLUDED.color_candidates,
        typography_candidates = EXCLUDED.typography_candidates,
        business_name_candidates = EXCLUDED.business_name_candidates,
        social_links = EXCLUDED.social_links,
        contact_channels = EXCLUDED.contact_channels,
        copy_samples = EXCLUDED.copy_samples
""")


def _logo_to_json(c: LogoCandidate) -> dict[str, Any]:
    return {
        "asset_id": c.asset_id,
        "kind": c.kind.value,
        "storage_uri": c.storage_uri,
        "sha256": c.sha256,
        "source": c.source.value,
        "confidence": c.confidence,
    }


def _logo_from_json(raw: dict[str, Any]) -> LogoCandidate:
    return LogoCandidate(
        asset_id=raw["asset_id"],
        kind=AssetKind(raw["kind"]),
        storage_uri=raw["storage_uri"],
        sha256=raw["sha256"],
        source=DiscoverySource(raw["source"]),
        confidence=float(raw["confidence"]),
    )


def _color_to_json(c: ColorCandidate) -> dict[str, Any]:
    return {
        "hex": c.hex,
        "source": c.source.value,
        "confidence": c.confidence,
        "role_hint": c.role_hint.value if c.role_hint is not None else None,
    }


def _color_from_json(raw: dict[str, Any]) -> ColorCandidate:
    role_hint = raw.get("role_hint")
    return ColorCandidate(
        hex=raw["hex"],
        source=DiscoverySource(raw["source"]),
        confidence=float(raw["confidence"]),
        role_hint=ColorRole(role_hint) if role_hint is not None else None,
    )


def _typography_to_json(c: TypographyCandidate) -> dict[str, Any]:
    return {"family": c.family, "source": c.source.value, "confidence": c.confidence}


def _typography_from_json(raw: dict[str, Any]) -> TypographyCandidate:
    return TypographyCandidate(
        family=raw["family"],
        source=DiscoverySource(raw["source"]),
        confidence=float(raw["confidence"]),
    )


def _business_name_to_json(c: BusinessNameCandidate) -> dict[str, Any]:
    return {"name": c.name, "source": c.source.value, "confidence": c.confidence}


def _business_name_from_json(raw: dict[str, Any]) -> BusinessNameCandidate:
    return BusinessNameCandidate(
        name=raw["name"], source=DiscoverySource(raw["source"]), confidence=float(raw["confidence"])
    )


def _social_link_to_json(c: SocialLinkCandidate) -> dict[str, Any]:
    return {"network": c.network.value, "url": c.url}


def _social_link_from_json(raw: dict[str, Any]) -> SocialLinkCandidate:
    return SocialLinkCandidate(network=SocialNetwork(raw["network"]), url=raw["url"])


def _contact_channel_to_json(c: ContactChannelCandidate) -> dict[str, Any]:
    return {"kind": c.kind.value, "page_url": c.page_url}


def _contact_channel_from_json(raw: dict[str, Any]) -> ContactChannelCandidate:
    return ContactChannelCandidate(kind=ContactChannelKind(raw["kind"]), page_url=raw["page_url"])


def _copy_sample_to_json(c: CopySample) -> dict[str, Any]:
    return {"source": c.source.value, "text": c.text}


def _copy_sample_from_json(raw: dict[str, Any]) -> CopySample:
    return CopySample(source=DiscoverySource(raw["source"]), text=raw["text"])


def _row_to_draft(row: Any) -> BrandDiscoveryDraft:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return BrandDiscoveryDraft(
        business_id=BusinessId.parse(str(row.business_id)),
        source_url=row.source_url,
        discovered_at=row.discovered_at,
        logo_candidates=tuple(_logo_from_json(c) for c in json.loads(row.logo_candidates_text)),
        color_candidates=tuple(
            _color_from_json(c) for c in json.loads(row.color_candidates_text)
        ),
        typography_candidates=tuple(
            _typography_from_json(c) for c in json.loads(row.typography_candidates_text)
        ),
        business_name_candidates=tuple(
            _business_name_from_json(c) for c in json.loads(row.business_name_candidates_text)
        ),
        social_links=tuple(_social_link_from_json(c) for c in json.loads(row.social_links_text)),
        contact_channels=tuple(
            _contact_channel_from_json(c) for c in json.loads(row.contact_channels_text)
        ),
        copy_samples=tuple(_copy_sample_from_json(c) for c in json.loads(row.copy_samples_text)),
    )


class SqlBrandDiscoveryDraftRepository:
    """Vive dentro de la transaccion del `AsyncSession` que le pasan; no
    hace `commit()` (mismo patron que `SqlBrandKitRepository`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_business(self, business_id: BusinessId) -> BrandDiscoveryDraft | None:
        result = await self._session.execute(
            _GET_BY_BUSINESS_SQL, {"business_id": str(business_id)}
        )
        row = result.one_or_none()
        return None if row is None else _row_to_draft(row)

    async def save(self, draft: BrandDiscoveryDraft) -> None:
        await self._session.execute(
            _UPSERT_SQL,
            {
                "business_id": str(draft.business_id),
                "source_url": draft.source_url,
                "discovered_at": draft.discovered_at,
                "logo_candidates": json.dumps([_logo_to_json(c) for c in draft.logo_candidates]),
                "color_candidates": json.dumps(
                    [_color_to_json(c) for c in draft.color_candidates]
                ),
                "typography_candidates": json.dumps(
                    [_typography_to_json(c) for c in draft.typography_candidates]
                ),
                "business_name_candidates": json.dumps(
                    [_business_name_to_json(c) for c in draft.business_name_candidates]
                ),
                "social_links": json.dumps([_social_link_to_json(c) for c in draft.social_links]),
                "contact_channels": json.dumps(
                    [_contact_channel_to_json(c) for c in draft.contact_channels]
                ),
                "copy_samples": json.dumps([_copy_sample_to_json(c) for c in draft.copy_samples]),
            },
        )


class RequestScopedBrandDiscoveryDraftRepository:
    """`BrandDiscoveryDraftRepository` real, una sesion por llamada
    (`container.session_factory()`), mismo patron que
    `RequestScopedBrandKitRepository`: `build_brand_router` construye los
    casos de uso una vez al arrancar la app, asi que esta envoltura es la
    que hace que cada peticion HTTP tenga su propia sesion en vez de
    compartir una entre todas."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_business(self, business_id: BusinessId) -> BrandDiscoveryDraft | None:
        async with self._session_factory() as session:
            return await SqlBrandDiscoveryDraftRepository(session).get_by_business(business_id)

    async def save(self, draft: BrandDiscoveryDraft) -> None:
        async with self._session_factory() as session:
            await SqlBrandDiscoveryDraftRepository(session).save(draft)
            await session.commit()
