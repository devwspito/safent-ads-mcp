"""Business-scoped, operator-supplied launch packs and private video slots.

Packs live in the mounted kit. Assets stay in the backed-up creative volume.
Uploading a file never changes a provider plan or its approval hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.launches.approval import LaunchApprovalStore
from safent_ads.panel.presentation.deps import require_business_access

BusinessDep = Annotated[str, Depends(require_business_access)]
OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_MAX_VIDEO = 100 * 1024 * 1024
_MAX_TEXT = 262144
_MP4_PREFIX_SIZE = 12


def _error(status: int, message: str) -> ApiError:
    return ApiError(status_code=status, code="LAUNCH_REVIEW_ERROR", message=message)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class LaunchPackStore:
    def __init__(self, kit: Path | None, assets: Path) -> None:
        self.kit = kit.resolve() if kit else None
        self.assets = assets.resolve() / "launch-videos"

    def _root(self, slug: str) -> Path:
        if self.kit is None or not _KEY.fullmatch(slug):
            raise _error(404, "Plan no encontrado.")
        root = (self.kit / "launches" / slug).resolve()
        if not root.is_relative_to(self.kit) or not root.is_dir():
            raise _error(404, "Plan no encontrado.")
        return root

    def file(self, slug: str, name: str) -> Path:
        root = self._root(slug)
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise _error(404, "Archivo no encontrado.")
        return target

    def read(self, slug: str, business_id: str | None = None) -> dict[str, Any]:
        path = self.file(slug, "manifest.json")
        if path.stat().st_size > _MAX_TEXT:
            raise _error(422, "Plan demasiado grande.")
        data: dict[str, Any] = json.loads(path.read_text())
        if data.get("slug") != slug or (business_id and data.get("business_id") != business_id):
            raise _error(404, "Plan no encontrado.")
        return data

    def list(self, business_id: str) -> list[dict[str, Any]]:
        if self.kit is None or not (self.kit / "launches").is_dir():
            return []
        packs = []
        for root in sorted((self.kit / "launches").iterdir()):
            if root.is_dir() and _KEY.fullmatch(root.name):
                data = self.read(root.name)
                if data.get("business_id") == business_id:
                    packs.append(self.detail(root.name, business_id))
        return packs

    def detail(self, slug: str, business_id: str) -> dict[str, Any]:
        data = self.read(slug, business_id)
        documents = []
        for item in data.get("documents", []):
            path = self.file(slug, item["file"])
            if path.stat().st_size > _MAX_TEXT:
                raise _error(422, "Documento demasiado grande.")
            documents.append({"title": item["title"], "text": path.read_text()})
        slots = []
        for slot in data.get("video_slots", []):
            stored = self.video_path(slug, business_id, slot["id"])
            slots.append({**slot, "uploaded": stored.is_file()})
        return {
            "slug": slug,
            "title": data["title"],
            "summary": data["summary"],
            "proposal_id": data.get("proposal_id"),
            "landing_url": f"/eventos/{slug}",
            "blockers": data.get("blockers", []),
            "documents": documents,
            "video_slots": slots,
            "revision": self.revision(slug, business_id),
        }

    def revision(self, slug: str, business_id: str) -> str:
        data = self.read(slug, business_id)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode())
        files = {
            "landing.html",
            *data.get("public_assets", []),
            *(item["file"] for item in data.get("documents", [])),
        }
        for name in sorted(files):
            digest.update(name.encode())
            digest.update(self.file(slug, name).read_bytes())
        return digest.hexdigest()

    def video_path(self, slug: str, business_id: str, slot: str) -> Path:
        data = self.read(slug, business_id)
        if not _KEY.fullmatch(slot) or slot not in {s["id"] for s in data.get("video_slots", [])}:
            raise _error(404, "Hueco de vídeo no encontrado.")
        # business_id has already been resolved from the trusted manifest.
        scope = hashlib.sha256(business_id.encode()).hexdigest()
        return self.assets / scope / slug / f"{slot}.mp4"


def build_launch_review_router(
    kit: Path | None, assets: Path, approvals: LaunchApprovalStore | None = None
) -> APIRouter:
    router = APIRouter(tags=["launch-review"])
    store = LaunchPackStore(kit, assets)

    @router.get("/api/v1/launch-plans")
    async def list_plans(business_id: BusinessDep) -> dict[str, Any]:
        items = await asyncio.to_thread(store.list, business_id)
        for item in items:
            item["review"] = (
                await approvals.status(business_id, item["slug"], item["revision"])
                if approvals
                else {"approved": False, "approved_at": None}
            )
        return {"items": items}

    @router.post("/api/v1/launch-plans/{slug}/review")
    async def approve_plan(
        slug: str, business_id: BusinessDep, owner: OwnerDep, body: ReviewBody
    ) -> dict[str, Any]:
        if approvals is None:
            raise _error(503, "Revisión no disponible.")
        revision = await asyncio.to_thread(store.revision, slug, business_id)
        if revision != body.revision:
            raise _error(409, "El plan ha cambiado. Recarga y revisa la nueva versión.")
        return await approvals.approve(business_id, slug, revision, owner.owner_id)

    @router.get("/api/v1/launch-plans/{slug}/videos/{slot}")
    async def video(slug: str, slot: str, business_id: BusinessDep) -> FileResponse:
        path = store.video_path(slug, business_id, slot)
        if not path.is_file():
            raise _error(404, "Todavía no hay vídeo.")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    @router.put("/api/v1/launch-plans/{slug}/videos/{slot}")
    async def upload_video(
        slug: str,
        slot: str,
        request: Request,
        business_id: BusinessDep,
    ) -> dict[str, Any]:
        target = store.video_path(slug, business_id, slot)
        if request.headers.get("content-type", "").split(";")[0] != "video/mp4":
            raise _error(415, "Usa un vídeo MP4 (máximo 100 MB).")
        if target.is_file():
            raise _error(409, "Este hueco ya tiene un vídeo; se conserva la versión cargada.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{uuid4().hex}.upload"
        size = 0
        prefix = b""
        try:
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > _MAX_VIDEO:
                        raise _error(413, "El vídeo supera 100 MB.")
                    if len(prefix) < _MP4_PREFIX_SIZE:
                        prefix += chunk[: _MP4_PREFIX_SIZE - len(prefix)]
                    await asyncio.to_thread(output.write, chunk)
            if size < _MP4_PREFIX_SIZE or prefix[4:8] != b"ftyp":
                raise _error(415, "El archivo no contiene un vídeo MP4 reconocible.")
            # Exclusive hard link protects an existing slot against concurrent uploads.
            try:
                await asyncio.to_thread(os.link, temporary, target)
            except FileExistsError as exc:
                raise _error(409, "Otro vídeo ya ocupa este hueco.") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return {"uploaded": True, "bytes": size, "published_to_meta": False}

    _include_public_preview(router, store)
    return router


def _include_public_preview(router: APIRouter, store: LaunchPackStore) -> None:
    @router.get("/eventos/{slug}", response_class=HTMLResponse)
    async def landing(slug: str) -> HTMLResponse:
        data = store.read(slug)
        if data.get("public_preview") is not True:
            raise _error(404, "Página no encontrada.")
        page = store.file(slug, "landing.html").read_text()
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @router.get("/eventos/{slug}/assets/{name}")
    async def landing_asset(slug: str, name: str) -> FileResponse:
        data = store.read(slug)
        if data.get("public_preview") is not True or name not in data.get("public_assets", []):
            raise _error(404, "Archivo no encontrado.")
        return FileResponse(store.file(slug, name))
