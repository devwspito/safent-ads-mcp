"""Repositorios en memoria de `creative`. `contracts/creative-port.md` no
declara puertos de persistencia de agregado (solo render/composicion de
contenido binario); estos implementan los puertos anadidos en
`application/ports.py` para que los casos de uso y el borde MCP/REST sean
demostrables sin Postgres en este carril. `database-engineer` los sustituye
por adaptadores SQLAlchemy detras del mismo puerto (DIP) cuando aterrice
`0011_creative` (T098)."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.creative.domain.brief import CreativeBrief
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.creative_job import CreativeJob, CreativeJobIdempotencyKey
from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.identifiers import AssetId, BriefId, JobId
from safent_ads.shared.ids import BusinessId


class InMemoryCreativeBriefRepository:
    def __init__(self) -> None:
        self._briefs: dict[BriefId, CreativeBrief] = {}

    async def get(self, brief_id: BriefId) -> CreativeBrief | None:
        return self._briefs.get(brief_id)

    async def add(self, brief_id: BriefId, brief: CreativeBrief) -> None:
        self._briefs[brief_id] = brief

    async def list_for_business(
        self, business_id: BusinessId
    ) -> Sequence[tuple[BriefId, CreativeBrief]]:
        return tuple(
            (brief_id, brief)
            for brief_id, brief in self._briefs.items()
            if brief.business_id == business_id
        )


class InMemoryCreativeAssetRepository:
    def __init__(self) -> None:
        self._assets: dict[AssetId, CreativeAsset] = {}

    async def get(self, asset_id: AssetId) -> CreativeAsset | None:
        return self._assets.get(asset_id)

    async def add(self, asset: CreativeAsset) -> None:
        self._assets[asset.asset_id] = asset

    async def update(self, asset: CreativeAsset) -> None:
        self._assets[asset.asset_id] = asset

    async def list_for_business(
        self, business_id: BusinessId, *, media_kind: MediaKind | None = None
    ) -> Sequence[CreativeAsset]:
        return tuple(
            asset
            for asset in self._assets.values()
            if asset.business_id == business_id
            and (media_kind is None or asset.media_kind == media_kind)
        )


class InMemoryCreativeJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[JobId, CreativeJob] = {}
        self._by_idempotency_key: dict[CreativeJobIdempotencyKey, JobId] = {}

    async def get(self, job_id: JobId) -> CreativeJob | None:
        return self._jobs.get(job_id)

    async def get_by_idempotency_key(self, key: CreativeJobIdempotencyKey) -> CreativeJob | None:
        job_id = self._by_idempotency_key.get(key)
        return self._jobs.get(job_id) if job_id is not None else None

    async def add(self, job: CreativeJob) -> None:
        self._jobs[job.job_id] = job
        self._by_idempotency_key[job.idempotency_key] = job.job_id

    async def update(self, job: CreativeJob) -> None:
        self._jobs[job.job_id] = job
