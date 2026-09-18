import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.launches.review import LaunchPackStore, build_launch_review_router
from safent_ads.panel.presentation.deps import require_business_access


@pytest.fixture
def pack(tmp_path: Path) -> tuple[Path, Path]:
    kit = tmp_path / "kit"
    root = kit / "launches" / "opening"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "slug": "opening",
                "business_id": "business-a",
                "title": "Opening",
                "summary": "Meet the team",
                "public_preview": True,
                "public_assets": ["page.css"],
                "documents": [{"title": "Plan", "file": "plan.md"}],
                "video_slots": [{"id": "welcome", "title": "Welcome"}],
            }
        )
    )
    (root / "plan.md").write_text("Private strategy")
    (root / "landing.html").write_text("<!doctype html><h1>Preview</h1>")
    (root / "page.css").write_text("body { color: black; }")
    return kit, tmp_path / "assets"


def client_for(pack: tuple[Path, Path], business: str | None, approvals=None) -> TestClient:
    app = FastAPI()

    async def access() -> str:
        if business is None:
            raise HTTPException(status_code=401)
        return business

    app.dependency_overrides[require_business_access] = access
    if business is not None:
        app.dependency_overrides[current_owner] = lambda: AuthenticatedOwner(
            uuid4(), "owner@example.com"
        )
    app.add_exception_handler(
        ApiError,
        lambda _request, error: JSONResponse(
            {"error": error.detail}, status_code=error.status_code
        ),
    )
    app.include_router(build_launch_review_router(*pack, approvals=approvals))
    return TestClient(app, raise_server_exceptions=False)


def test_scoped_pack_and_public_asset_allowlist(pack: tuple[Path, Path]) -> None:
    client = client_for(pack, "business-b")
    assert client.get("/api/v1/launch-plans").json() == {"items": []}
    assert client.get("/eventos/opening").status_code == 200
    assert client.get("/eventos/opening/assets/page.css").status_code == 200
    assert client.get("/eventos/opening/assets/plan.md").status_code == 404
    assert client.get("/eventos/opening/assets/manifest.json").status_code == 404
    assert client_for(pack, None).get("/api/v1/launch-plans").status_code == 401


def test_path_escape_and_cross_business_video_denied(pack: tuple[Path, Path]) -> None:
    store = LaunchPackStore(*pack)
    with pytest.raises(Exception, match="Plan no encontrado"):
        store.read("../opening")
    with pytest.raises(Exception, match="Plan no encontrado"):
        store.video_path("opening", "business-b", "welcome")
    (pack[0] / "launches" / "opening" / "escape").symlink_to(pack[0].parent)
    with pytest.raises(Exception, match="Archivo no encontrado"):
        store.file("opening", "escape/outside.txt")


def test_upload_private_bounded_and_not_overwritten(pack: tuple[Path, Path]) -> None:
    client = client_for(pack, "business-a")
    url = "/api/v1/launch-plans/opening/videos/welcome"
    headers = {"content-type": "video/mp4"}
    assert client.put(url, content=b"not a video", headers=headers).status_code != 200
    payload = b"\x00\x00\x00\x18ftypmp42" + b"test" * 10
    response = client.put(url, content=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["published_to_meta"] is False
    assert client.get(url).content == payload
    assert client_for(pack, None).get(url).status_code == 401
    assert client_for(pack, "business-b").get(url).status_code != 200
    assert client.put(url, content=payload, headers=headers).status_code != 200
    assert not list(pack[1].rglob("*.upload"))


def test_oversized_upload_removed(pack: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("safent_ads.launches.review._MAX_VIDEO", 16)
    client = client_for(pack, "business-a")
    assert (
        client.put(
            "/api/v1/launch-plans/opening/videos/welcome",
            content=b"x" * 17,
            headers={"content-type": "video/mp4"},
        ).status_code
        != 200
    )
    assert not list(pack[1].rglob("*.upload"))
    assert not list(pack[1].rglob("*.mp4"))


def test_review_requires_session_scope_and_current_revision(pack: tuple[Path, Path]) -> None:
    approvals = AsyncMock()
    approvals.approve.return_value = {"approved": True, "approved_at": "2026-09-18T00:00:00Z"}
    revision = LaunchPackStore(*pack).revision("opening", "business-a")
    url = "/api/v1/launch-plans/opening/review"
    assert (
        client_for(pack, None, approvals).post(url, json={"revision": revision}).status_code == 401
    )
    assert (
        client_for(pack, "business-b", approvals).post(url, json={"revision": revision}).status_code
        == 404
    )
    client = client_for(pack, "business-a", approvals)
    assert client.post(url, json={"revision": "0" * 64}).status_code == 409
    approvals.approve.assert_not_called()
    assert client.post(url, json={"revision": revision}).json()["approved"] is True
    (pack[0] / "launches/opening/plan.md").write_text("A different plan")
    assert client.post(url, json={"revision": revision}).status_code == 409
    assert approvals.approve.call_count == 1
