"""Adaptadores SQL de `CreativeBriefRepository`/`CreativeAssetRepository`/
`CreativeJobRepository` sobre `creative_briefs`/`creative_assets`/
`creative_jobs` (0021_creative_review) -- primera persistencia real de
`creative`, sustituye `infrastructure/in_memory_repositories.py` para el
carril de superficie.

SQL crudo via `text()`, mismo patron que
`brand.infrastructure.sql_brand_kit_repository` (JSONB para
subestructuras via `CAST(:x AS JSONB)` + `json.dumps`/`json.loads`).
Cada `Sql*Repository` vive dentro de la sesion que le pasan (sin
`commit()`); cada `RequestScoped*Repository` abre su propia sesion por
llamada y SI confirma -- mismo reparto que
`RequestScopedBrandKitRepository`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.creative.domain.brand_kit import BrandKit, SafeArea
from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.copy import AdCopy
from safent_ads.creative.domain.creative_asset import CreativeAsset, Provenance
from safent_ads.creative.domain.creative_job import CreativeJob, CreativeJobIdempotencyKey
from safent_ads.creative.domain.enums import (
    CallToAction,
    CampaignObjective,
    CreativeAssetState,
    CreativeJobState,
    CreativeOutcome,
    Format,
    GenerationStatus,
    Language,
    MediaKind,
    PolicySeverity,
    PolicyVerdictResult,
    RendererName,
)
from safent_ads.creative.domain.identifiers import (
    AssetId,
    BriefId,
    CalendarEventId,
    JobId,
    SignalId,
)
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.policy import PolicyFinding, PolicyVerdict
from safent_ads.creative.domain.shot import ShotDescription
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.ids import BusinessId

# --- CreativeBrief -----------------------------------------------------


_GET_BRIEF_SQL = text("""
    SELECT id, business_id, calendar_event_id, objective, audience_summary, hook,
           shots::text AS shots_text, on_screen_text::text AS on_screen_text_text, cta,
           voiceover_lines::text AS voiceover_lines_text, brand_kit::text AS brand_kit_text,
           source_signal_id, variant_count, language
    FROM creative_briefs
    WHERE id = :id
""")

_LIST_BRIEFS_FOR_BUSINESS_SQL = text("""
    SELECT id, business_id, calendar_event_id, objective, audience_summary, hook,
           shots::text AS shots_text, on_screen_text::text AS on_screen_text_text, cta,
           voiceover_lines::text AS voiceover_lines_text, brand_kit::text AS brand_kit_text,
           source_signal_id, variant_count, language
    FROM creative_briefs
    WHERE business_id = :business_id
    ORDER BY created_at
""")

_INSERT_BRIEF_SQL = text("""
    INSERT INTO creative_briefs
        (id, business_id, calendar_event_id, objective, audience_summary, hook, shots,
         on_screen_text, cta, voiceover_lines, brand_kit, source_signal_id, variant_count,
         language)
    VALUES
        (:id, :business_id, :calendar_event_id, :objective, :audience_summary, :hook,
         CAST(:shots AS JSONB), CAST(:on_screen_text AS JSONB), :cta,
         CAST(:voiceover_lines AS JSONB), CAST(:brand_kit AS JSONB), :source_signal_id,
         :variant_count, :language)
    ON CONFLICT (id) DO NOTHING
""")


def _brand_kit_to_json(brand_kit: BrandKit) -> dict[str, Any]:
    return {
        "primary_font": brand_kit.primary_font,
        "secondary_font": brand_kit.secondary_font,
        "primary_color_hex": brand_kit.primary_color_hex,
        "secondary_color_hex": brand_kit.secondary_color_hex,
        "logo_asset_id": str(brand_kit.logo_asset_id),
        "safe_area": {
            "top": brand_kit.safe_area.top,
            "bottom": brand_kit.safe_area.bottom,
            "left": brand_kit.safe_area.left,
            "right": brand_kit.safe_area.right,
        },
    }


def _brand_kit_from_json(raw: dict[str, Any]) -> BrandKit:
    safe_area = raw.get("safe_area") or {}
    return BrandKit(
        primary_font=raw["primary_font"],
        secondary_font=raw["secondary_font"],
        primary_color_hex=raw["primary_color_hex"],
        secondary_color_hex=raw["secondary_color_hex"],
        logo_asset_id=AssetId.parse(raw["logo_asset_id"]),
        safe_area=SafeArea(
            top=safe_area.get("top", 0.0),
            bottom=safe_area.get("bottom", 0.0),
            left=safe_area.get("left", 0.0),
            right=safe_area.get("right", 0.0),
        ),
    )


def _shot_to_json(shot: ShotDescription) -> dict[str, Any]:
    return {"order": shot.order, "description": shot.description, "duration_s": shot.duration_s}


def _shot_from_json(raw: dict[str, Any]) -> ShotDescription:
    return ShotDescription(
        order=raw["order"], description=raw["description"], duration_s=raw.get("duration_s")
    )


def _row_to_brief(row: Any) -> CreativeBrief:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return CreativeBrief(
        business_id=BusinessId.parse(str(row.business_id)),
        calendar_event_id=(
            CalendarEventId.parse(str(row.calendar_event_id)) if row.calendar_event_id else None
        ),
        objective=CampaignObjective(row.objective),
        audience_summary=row.audience_summary,
        hook=row.hook,
        shots=[_shot_from_json(s) for s in json.loads(row.shots_text)],
        on_screen_text=json.loads(row.on_screen_text_text),
        cta=row.cta,
        voiceover_lines=json.loads(row.voiceover_lines_text),
        brand_kit=_brand_kit_from_json(json.loads(row.brand_kit_text)),
        source_signal_id=(
            SignalId.parse(str(row.source_signal_id)) if row.source_signal_id else None
        ),
        variant_count=row.variant_count,
        language=Language(row.language),
    )


class SqlCreativeBriefRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, brief_id: BriefId) -> CreativeBrief | None:
        result = await self._session.execute(_GET_BRIEF_SQL, {"id": str(brief_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_brief(row)

    async def add(self, brief_id: BriefId, brief: CreativeBrief) -> None:
        await self._session.execute(
            _INSERT_BRIEF_SQL,
            {
                "id": str(brief_id),
                "business_id": str(brief.business_id),
                "calendar_event_id": (
                    str(brief.calendar_event_id) if brief.calendar_event_id else None
                ),
                "objective": brief.objective.value,
                "audience_summary": brief.audience_summary,
                "hook": brief.hook,
                "shots": json.dumps([_shot_to_json(s) for s in brief.shots]),
                "on_screen_text": json.dumps(list(brief.on_screen_text)),
                "cta": brief.cta,
                "voiceover_lines": json.dumps(list(brief.voiceover_lines)),
                "brand_kit": json.dumps(_brand_kit_to_json(brief.brand_kit)),
                "source_signal_id": (
                    str(brief.source_signal_id) if brief.source_signal_id else None
                ),
                "variant_count": brief.variant_count,
                "language": brief.language.value,
            },
        )

    async def list_for_business(
        self, business_id: BusinessId
    ) -> Sequence[tuple[BriefId, CreativeBrief]]:
        result = await self._session.execute(
            _LIST_BRIEFS_FOR_BUSINESS_SQL, {"business_id": str(business_id)}
        )
        return tuple((BriefId.parse(str(row.id)), _row_to_brief(row)) for row in result.all())


class RequestScopedCreativeBriefRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, brief_id: BriefId) -> CreativeBrief | None:
        async with self._session_factory() as session:
            return await SqlCreativeBriefRepository(session).get(brief_id)

    async def add(self, brief_id: BriefId, brief: CreativeBrief) -> None:
        async with self._session_factory() as session:
            await SqlCreativeBriefRepository(session).add(brief_id, brief)
            await session.commit()

    async def list_for_business(
        self, business_id: BusinessId
    ) -> Sequence[tuple[BriefId, CreativeBrief]]:
        async with self._session_factory() as session:
            return await SqlCreativeBriefRepository(session).list_for_business(business_id)


# --- CreativeAsset -------------------------------------------------------


_ASSET_COLUMNS = """
    id, business_id, media_kind, format, duration_seconds, storage_uri, checksum,
    cost_estimate_amount, cost_estimate_currency, renderer_used, model_name, seed, brief_id,
    source_signal_id, generation_status, generated_at, ad_copy::text AS ad_copy_text,
    destination_url, state, policy_verdict, policy_findings::text AS policy_findings_text,
    outcome
"""

_GET_ASSET_SQL = text(f"SELECT {_ASSET_COLUMNS} FROM creative_assets WHERE id = :id")  # noqa: S608

_LIST_ASSETS_FOR_BUSINESS_SQL = text(f"""
    SELECT {_ASSET_COLUMNS} FROM creative_assets
    WHERE business_id = :business_id
    ORDER BY created_at DESC
""")  # noqa: S608

_LIST_ASSETS_FOR_BUSINESS_AND_MEDIA_KIND_SQL = text(f"""
    SELECT {_ASSET_COLUMNS} FROM creative_assets
    WHERE business_id = :business_id AND media_kind = :media_kind
    ORDER BY created_at DESC
""")  # noqa: S608

_INSERT_ASSET_SQL = text("""
    INSERT INTO creative_assets
        (id, business_id, media_kind, format, duration_seconds, storage_uri, checksum,
         cost_estimate_amount, cost_estimate_currency, renderer_used, model_name, seed,
         brief_id, source_signal_id, generation_status, generated_at, ad_copy, destination_url,
         state, policy_verdict, policy_findings, outcome)
    VALUES
        (:id, :business_id, :media_kind, :format, :duration_seconds, :storage_uri, :checksum,
         :cost_estimate_amount, :cost_estimate_currency, :renderer_used, :model_name, :seed,
         :brief_id, :source_signal_id, :generation_status, :generated_at,
         CAST(:ad_copy AS JSONB), :destination_url, :state, :policy_verdict,
         CAST(:policy_findings AS JSONB), :outcome)
""")

_UPDATE_ASSET_SQL = text("""
    UPDATE creative_assets
    SET format = :format, ad_copy = CAST(:ad_copy AS JSONB), destination_url = :destination_url,
        state = :state, policy_verdict = :policy_verdict,
        policy_findings = CAST(:policy_findings AS JSONB), outcome = :outcome
    WHERE id = :id
""")

_CTA_LABEL_TO_NAME: dict[str, CallToAction] = {cta.value: cta for cta in CallToAction}


def _ad_copy_to_json(ad_copy: AdCopy) -> dict[str, Any]:
    return {
        "headline": ad_copy.headline,
        "primary_text": ad_copy.primary_text,
        "cta": ad_copy.cta.value,
        "language": ad_copy.language.value,
    }


def _ad_copy_from_json(raw: dict[str, Any]) -> AdCopy:
    return AdCopy(
        headline=raw["headline"],
        primary_text=raw["primary_text"],
        cta=_CTA_LABEL_TO_NAME[raw["cta"]],
        language=Language(raw.get("language", Language.ES_ES.value)),
    )


def _policy_finding_to_json(finding: PolicyFinding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "severity": finding.severity.value,
        "human_message": finding.human_message,
    }


def _policy_finding_from_json(raw: dict[str, Any]) -> PolicyFinding:
    return PolicyFinding(
        code=raw["code"],
        severity=PolicySeverity(raw["severity"]),
        human_message=raw["human_message"],
    )


def _row_to_asset(row: Any) -> CreativeAsset:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    policy_verdict: PolicyVerdict | None = None
    if row.policy_verdict is not None:
        findings = (
            tuple(_policy_finding_from_json(f) for f in json.loads(row.policy_findings_text))
            if row.policy_findings_text is not None
            else ()
        )
        policy_verdict = PolicyVerdict(
            verdict=PolicyVerdictResult(row.policy_verdict), findings=findings
        )
    provenance = Provenance(
        renderer_used=RendererName(row.renderer_used),
        model_name=row.model_name,
        seed=row.seed,
        brief_id=BriefId.parse(str(row.brief_id)),
        source_signal_id=(
            SignalId.parse(str(row.source_signal_id)) if row.source_signal_id else None
        ),
        generation_status=GenerationStatus(row.generation_status),
        generated_at=row.generated_at,
    )
    return CreativeAsset._reconstitute(  # noqa: SLF001 - unico consumidor sancionado
        asset_id=AssetId.parse(str(row.id)),
        business_id=BusinessId.parse(str(row.business_id)),
        media_kind=MediaKind(row.media_kind),
        format=Format(row.format) if row.format is not None else None,
        duration_seconds=(
            float(row.duration_seconds) if row.duration_seconds is not None else None
        ),
        storage_uri=StorageUri(row.storage_uri),
        checksum=row.checksum,
        cost_estimate=Money(Decimal(row.cost_estimate_amount), row.cost_estimate_currency),
        provenance=provenance,
        ad_copy=_ad_copy_from_json(json.loads(row.ad_copy_text)) if row.ad_copy_text else None,
        destination_url=row.destination_url,
        state=CreativeAssetState(row.state),
        policy_verdict=policy_verdict,
        outcome=CreativeOutcome(row.outcome),
    )


class SqlCreativeAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, asset_id: AssetId) -> CreativeAsset | None:
        result = await self._session.execute(_GET_ASSET_SQL, {"id": str(asset_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_asset(row)

    async def add(self, asset: CreativeAsset) -> None:
        await self._session.execute(_INSERT_ASSET_SQL, self._params(asset))

    async def update(self, asset: CreativeAsset) -> None:
        params = self._params(asset)
        await self._session.execute(_UPDATE_ASSET_SQL, params)

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> Sequence[CreativeAsset]:
        if media_kind is None:
            result = await self._session.execute(
                _LIST_ASSETS_FOR_BUSINESS_SQL, {"business_id": str(business_id)}
            )
        else:
            result = await self._session.execute(
                _LIST_ASSETS_FOR_BUSINESS_AND_MEDIA_KIND_SQL,
                {"business_id": str(business_id), "media_kind": media_kind.value},
            )
        return tuple(_row_to_asset(row) for row in result.all())

    def _params(self, asset: CreativeAsset) -> dict[str, Any]:
        policy_verdict = asset.policy_verdict
        return {
            "id": str(asset.asset_id),
            "business_id": str(asset.business_id),
            "media_kind": asset.media_kind.value,
            "format": asset.format.value if asset.format is not None else None,
            "duration_seconds": asset.duration_seconds,
            "storage_uri": str(asset.storage_uri),
            "checksum": asset.checksum,
            "cost_estimate_amount": asset.cost_estimate.amount,
            "cost_estimate_currency": asset.cost_estimate.currency,
            "renderer_used": asset.provenance.renderer_used.value,
            "model_name": asset.provenance.model_name,
            "seed": asset.provenance.seed,
            "brief_id": str(asset.provenance.brief_id),
            "source_signal_id": (
                str(asset.provenance.source_signal_id)
                if asset.provenance.source_signal_id
                else None
            ),
            "generation_status": asset.provenance.generation_status.value,
            "generated_at": asset.provenance.generated_at,
            "ad_copy": json.dumps(_ad_copy_to_json(asset.ad_copy)) if asset.ad_copy else None,
            "destination_url": asset.destination_url,
            "state": asset.state.value,
            "policy_verdict": policy_verdict.verdict.value if policy_verdict else None,
            "policy_findings": (
                json.dumps([_policy_finding_to_json(f) for f in policy_verdict.findings])
                if policy_verdict
                else None
            ),
            "outcome": asset.outcome.value,
        }


class RequestScopedCreativeAssetRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, asset_id: AssetId) -> CreativeAsset | None:
        async with self._session_factory() as session:
            return await SqlCreativeAssetRepository(session).get(asset_id)

    async def add(self, asset: CreativeAsset) -> None:
        async with self._session_factory() as session:
            await SqlCreativeAssetRepository(session).add(asset)
            await session.commit()

    async def update(self, asset: CreativeAsset) -> None:
        async with self._session_factory() as session:
            await SqlCreativeAssetRepository(session).update(asset)
            await session.commit()

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> Sequence[CreativeAsset]:
        async with self._session_factory() as session:
            return await SqlCreativeAssetRepository(session).list_for_business(
                business_id, media_kind=media_kind
            )


# --- CreativeJob -----------------------------------------------------------


_JOB_COLUMNS = """
    id, business_id, brief_id, brief_hash, variant_index, state, progress,
    asset_ids::text AS asset_ids_text, failure_reason
"""

_GET_JOB_SQL = text(f"SELECT {_JOB_COLUMNS} FROM creative_jobs WHERE id = :id")  # noqa: S608

_GET_JOB_BY_IDEMPOTENCY_KEY_SQL = text(f"""
    SELECT {_JOB_COLUMNS} FROM creative_jobs
    WHERE brief_hash = :brief_hash AND variant_index = :variant_index
""")  # noqa: S608

_INSERT_JOB_SQL = text("""
    INSERT INTO creative_jobs
        (id, business_id, brief_id, brief_hash, variant_index, state, progress, asset_ids,
         failure_reason)
    VALUES
        (:id, :business_id, :brief_id, :brief_hash, :variant_index, :state, :progress,
         CAST(:asset_ids AS JSONB), :failure_reason)
""")

_UPDATE_JOB_SQL = text("""
    UPDATE creative_jobs
    SET state = :state, progress = :progress, asset_ids = CAST(:asset_ids AS JSONB),
        failure_reason = :failure_reason
    WHERE id = :id
""")


def _row_to_job(row: Any) -> CreativeJob:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return CreativeJob._reconstitute(  # noqa: SLF001 - unico consumidor sancionado
        job_id=JobId.parse(str(row.id)),
        business_id=BusinessId.parse(str(row.business_id)),
        brief_id=BriefId.parse(str(row.brief_id)),
        idempotency_key=CreativeJobIdempotencyKey(
            brief_hash=row.brief_hash, variant_index=row.variant_index
        ),
        state=CreativeJobState(row.state),
        progress=float(row.progress),
        asset_ids=tuple(AssetId.parse(a) for a in json.loads(row.asset_ids_text)),
        failure_reason=row.failure_reason,
    )


class SqlCreativeJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: JobId) -> CreativeJob | None:
        result = await self._session.execute(_GET_JOB_SQL, {"id": str(job_id)})
        row = result.one_or_none()
        return None if row is None else _row_to_job(row)

    async def get_by_idempotency_key(
        self, key: CreativeJobIdempotencyKey
    ) -> CreativeJob | None:
        result = await self._session.execute(
            _GET_JOB_BY_IDEMPOTENCY_KEY_SQL,
            {"brief_hash": key.brief_hash, "variant_index": key.variant_index},
        )
        row = result.one_or_none()
        return None if row is None else _row_to_job(row)

    async def add(self, job: CreativeJob) -> None:
        await self._session.execute(_INSERT_JOB_SQL, self._params(job))

    async def update(self, job: CreativeJob) -> None:
        await self._session.execute(_UPDATE_JOB_SQL, self._params(job))

    def _params(self, job: CreativeJob) -> dict[str, Any]:
        return {
            "id": str(job.job_id),
            "business_id": str(job.business_id),
            "brief_id": str(job.brief_id),
            "brief_hash": job.idempotency_key.brief_hash,
            "variant_index": job.idempotency_key.variant_index,
            "state": job.state.value,
            "progress": job.progress,
            "asset_ids": json.dumps([str(a) for a in job.asset_ids]),
            "failure_reason": job.failure_reason,
        }


class RequestScopedCreativeJobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, job_id: JobId) -> CreativeJob | None:
        async with self._session_factory() as session:
            return await SqlCreativeJobRepository(session).get(job_id)

    async def get_by_idempotency_key(
        self, key: CreativeJobIdempotencyKey
    ) -> CreativeJob | None:
        async with self._session_factory() as session:
            return await SqlCreativeJobRepository(session).get_by_idempotency_key(key)

    async def add(self, job: CreativeJob) -> None:
        async with self._session_factory() as session:
            await SqlCreativeJobRepository(session).add(job)
            await session.commit()

    async def update(self, job: CreativeJob) -> None:
        async with self._session_factory() as session:
            await SqlCreativeJobRepository(session).update(job)
            await session.commit()
