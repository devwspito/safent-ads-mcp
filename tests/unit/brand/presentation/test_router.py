"""`build_brand_router`: kit confirmado (`GET /brand`, `GET /brand/assets`)
mas el descubrimiento de marca desde un sitio web opcional
(`POST /brand/discover`, `GET /brand/draft`, `POST /brand/confirm`,
`POST /brand/assets` subida manual, `PUT /brand/claims`). `TestClient`
sobre una `FastAPI` desnuda, sobre repos en memoria (CRUD del caso de
uso, no de persistencia).

La autorizacion real (T046/integracion, cableada sobre `iam`) se prueba
aparte: `tests/unit/test_brand_mounted.py` cubre el cableado en
`composition/app.py` (falla cerrado sin sesion) y
`tests/integration/brand/test_authorization_integration.py` cubre
`require_business_access` contra Postgres real (401 sin sesion, 404 con
un `business_id` que no existe en ninguna de las nuevas rutas) -- mismo
patron que `tests/integration/panel/test_authorization_integration.py`.
Aqui se sobrescribe `require_business_access` por un caller sin
restriccion que preserva el 422 de forma invalida ya cubierto por
`Query(pattern=UUID_PATTERN)` (mismo patron que
`creative.presentation.router`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraft
from safent_ads.brand.application.get_brand_asset_preview import GetBrandAssetPreview
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.application.ingest_brand_from_website import IngestBrandFromWebsite
from safent_ads.brand.application.list_brand_assets import ListBrandAssets
from safent_ads.brand.application.update_brand_claims import UpdateBrandClaims
from safent_ads.brand.application.upload_brand_asset import UploadBrandAsset
from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.brand.presentation.payloads import UUID_PATTERN
from safent_ads.brand.presentation.router import build_brand_router, require_business_access
from safent_ads.brand.testing.in_memory_brand_asset_storage import InMemoryBrandAssetStorage
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.brand.testing.recording_brand_claims_decision_recorder import (
    RecordingBrandClaimsDecisionRecorder,
)
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit

_OWNER = AuthenticatedOwner(owner_id=uuid.uuid4(), email="owner@safent.example")

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SAFE_SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='1' height='1'/></svg>"
_HOSTILE_SVG_BYTES = (
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
)

_LOGO = LogoCandidate(
    asset_id="logo-1",
    kind=AssetKind.LOGO_RASTER,
    storage_uri="brand/logo-1.png",
    sha256="a" * 64,
    source=DiscoverySource.OG_IMAGE,
    confidence=0.7,
)


class _FakeWebsiteBrandDiscovery:
    def __init__(self, draft: BrandDiscoveryDraft | None = None, *, fails: bool = False) -> None:
        self._draft = draft
        self._fails = fails

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:  # noqa: ARG002
        if self._fails:
            raise InfrastructureError("no se pudo descargar la pagina de inicio")
        return self._draft or BrandDiscoveryDraft(
            business_id=business_id, source_url=url, discovered_at=_NOW
        )


async def _unrestricted_business_access(
    business_id: Annotated[str, Query(pattern=UUID_PATTERN)],
) -> BusinessId:
    return BusinessId.parse(business_id)


def _fake_owner() -> AuthenticatedOwner:
    return _OWNER


def _client(
    *,
    kits: InMemoryBrandKitRepository | None = None,
    drafts: InMemoryBrandDiscoveryDraftRepository | None = None,
    storage: InMemoryBrandAssetStorage | None = None,
    discovery: _FakeWebsiteBrandDiscovery | None = None,
    decision_recorder: RecordingBrandClaimsDecisionRecorder | None = None,
) -> TestClient:
    kits = kits or InMemoryBrandKitRepository()
    drafts = drafts or InMemoryBrandDiscoveryDraftRepository()
    storage = storage or InMemoryBrandAssetStorage()
    decision_recorder = decision_recorder or RecordingBrandClaimsDecisionRecorder()
    clock = FixedClock(_NOW)
    router = build_brand_router(
        get_brand_kit=GetBrandKit(kits),
        list_brand_assets=ListBrandAssets(kits),
        ingest_brand_from_website=IngestBrandFromWebsite(
            discovery=discovery or _FakeWebsiteBrandDiscovery(),
            drafts=drafts,
            brand_kits=kits,
            clock=clock,
        ),
        get_brand_draft=GetBrandDraft(drafts),
        confirm_brand_draft=ConfirmBrandDraft(drafts=drafts, brand_kits=kits, clock=clock),
        upload_brand_asset=UploadBrandAsset(drafts=drafts, storage=storage, clock=clock),
        get_brand_asset_preview=GetBrandAssetPreview(
            brand_kits=kits, drafts=drafts, storage=storage
        ),
        update_brand_claims=UpdateBrandClaims(
            brand_kits=kits, clock=clock, decision_recorder=decision_recorder
        ),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_business_access] = _unrestricted_business_access
    app.dependency_overrides[current_owner] = _fake_owner
    return TestClient(app)


class TestGetBrand:
    def test_requires_valid_business_id(self) -> None:
        client = _client()

        response = client.get("/api/v1/brand", params={"business_id": "not-a-uuid"})

        assert response.status_code == 422

    def test_returns_404_when_business_has_no_kit(self) -> None:
        client = _client()

        response = client.get("/api/v1/brand", params={"business_id": str(BusinessId.new())})

        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_returns_the_stored_kit(self) -> None:
        business_id = BusinessId.new()
        kit = make_brand_kit(business_id=business_id)
        client = _client(kits=InMemoryBrandKitRepository([kit]))

        response = client.get("/api/v1/brand", params={"business_id": str(business_id)})

        assert response.status_code == 200
        body = response.json()
        assert body["business_id"] == str(business_id)
        assert body["typography"]["primary_family"] == kit.typography.primary_family


class TestListBrandAssets:
    def test_filters_by_kind(self) -> None:
        business_id = BusinessId.new()
        logo = BrandAsset(
            asset_id="logo-1",
            kind=AssetKind.LOGO_VECTOR,
            storage_uri="s3://logo.svg",
            usage_rule="x",
        )
        photo = BrandAsset(
            asset_id="photo-1",
            kind=AssetKind.REFERENCE_PHOTO,
            storage_uri="s3://photo.jpg",
            usage_rule="x",
        )
        kit = make_brand_kit(business_id=business_id, assets=(logo, photo))
        client = _client(kits=InMemoryBrandKitRepository([kit]))

        response = client.get(
            "/api/v1/brand/assets",
            params={"business_id": str(business_id), "kind": "reference_photo"},
        )

        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["asset_id"] for item in items] == ["photo-1"]


class TestUploadBrandAsset:
    def test_stores_the_payload_and_returns_the_candidate(self) -> None:
        business_id = BusinessId.new()
        storage = InMemoryBrandAssetStorage()
        client = _client(storage=storage)

        response = client.post(
            "/api/v1/brand/assets",
            params={"business_id": str(business_id), "kind": "logo_raster"},
            files={"file": ("logo.png", _PNG_BYTES, "image/png")},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["kind"] == "logo_raster"
        assert body["source"] == "manual_upload"
        assert body["preview_url"] == f"/api/v1/brand/assets/{body['asset_id']}/preview"
        assert storage.stored

    def test_rejects_a_file_over_the_size_cap(self) -> None:
        client = _client()
        oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1)

        response = client.post(
            "/api/v1/brand/assets",
            params={"business_id": str(BusinessId.new()), "kind": "logo_raster"},
            files={"file": ("logo.png", oversized, "image/png")},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"


class TestGetBrandAssetPreview:
    def test_returns_404_when_the_asset_id_is_unknown(self) -> None:
        client = _client()

        response = client.get(
            "/api/v1/brand/assets/unknown-asset/preview",
            params={"business_id": str(BusinessId.new())},
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_streams_a_raster_asset_sniffed_by_magic_bytes_never_by_the_stored_extension(
        self,
    ) -> None:
        business_id = BusinessId.new()
        asset = BrandAsset(
            asset_id="logo-1",
            kind=AssetKind.LOGO_VECTOR,
            storage_uri="logo_vector/actually-a-png.svg",
            usage_rule="x",
        )
        kit = make_brand_kit(business_id=business_id, assets=(asset,))
        storage = InMemoryBrandAssetStorage(seed={"logo_vector/actually-a-png.svg": _PNG_BYTES})
        client = _client(kits=InMemoryBrandKitRepository([kit]), storage=storage)

        response = client.get(
            "/api/v1/brand/assets/logo-1/preview", params={"business_id": str(business_id)}
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, max-age=300"
        assert response.content == _PNG_BYTES
        assert response.headers.get("content-security-policy") != "sandbox"

    def test_returns_304_when_if_none_match_matches_the_current_etag(self) -> None:
        business_id = BusinessId.new()
        asset = BrandAsset(
            asset_id="logo-1",
            kind=AssetKind.LOGO_RASTER,
            storage_uri="logo_raster/key-1",
            usage_rule="x",
        )
        kit = make_brand_kit(business_id=business_id, assets=(asset,))
        storage = InMemoryBrandAssetStorage(seed={"logo_raster/key-1": _PNG_BYTES})
        client = _client(kits=InMemoryBrandKitRepository([kit]), storage=storage)
        first = client.get(
            "/api/v1/brand/assets/logo-1/preview", params={"business_id": str(business_id)}
        )
        etag = first.headers["etag"]

        second = client.get(
            "/api/v1/brand/assets/logo-1/preview",
            params={"business_id": str(business_id)},
            headers={"if-none-match": etag},
        )

        assert second.status_code == 304
        assert second.content == b""

    def test_serves_a_safe_svg_with_a_sandbox_content_security_policy(self) -> None:
        business_id = BusinessId.new()
        asset = BrandAsset(
            asset_id="logo-1",
            kind=AssetKind.LOGO_VECTOR,
            storage_uri="logo_vector/key-1",
            usage_rule="x",
        )
        kit = make_brand_kit(business_id=business_id, assets=(asset,))
        storage = InMemoryBrandAssetStorage(seed={"logo_vector/key-1": _SAFE_SVG_BYTES})
        client = _client(kits=InMemoryBrandKitRepository([kit]), storage=storage)

        response = client.get(
            "/api/v1/brand/assets/logo-1/preview", params={"business_id": str(business_id)}
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert response.headers["content-security-policy"] == "sandbox"

    def test_rejects_a_hostile_unsanitized_svg_with_415(self) -> None:
        """Defensa en profundidad: `UploadBrandAsset.from_bytes` (subida
        manual) nunca aplica `sanitize_remote_svg` antes de escribir, asi
        que un SVG con `<script>` puede llegar a estar en disco -- este
        endpoint es el que impide que salga por HTTP igualmente."""
        business_id = BusinessId.new()
        asset = BrandAsset(
            asset_id="logo-1",
            kind=AssetKind.LOGO_VECTOR,
            storage_uri="logo_vector/key-1",
            usage_rule="x",
        )
        kit = make_brand_kit(business_id=business_id, assets=(asset,))
        storage = InMemoryBrandAssetStorage(seed={"logo_vector/key-1": _HOSTILE_SVG_BYTES})
        client = _client(kits=InMemoryBrandKitRepository([kit]), storage=storage)

        response = client.get(
            "/api/v1/brand/assets/logo-1/preview", params={"business_id": str(business_id)}
        )

        assert response.status_code == 415
        assert response.json()["detail"]["error"]["code"] == "UNSUPPORTED_ASSET_TYPE"

    def test_finds_a_draft_logo_candidate_not_yet_confirmed(self) -> None:
        business_id = BusinessId.new()
        candidate = LogoCandidate(
            asset_id="candidate-1",
            kind=AssetKind.LOGO_RASTER,
            storage_uri="logo_raster/key-1",
            sha256="a" * 64,
            source=DiscoverySource.FAVICON,
            confidence=0.5,
        )
        draft = BrandDiscoveryDraft(
            business_id=business_id,
            source_url=None,
            discovered_at=_NOW,
            logo_candidates=(candidate,),
        )
        storage = InMemoryBrandAssetStorage(seed={"logo_raster/key-1": _PNG_BYTES})
        client = _client(
            drafts=InMemoryBrandDiscoveryDraftRepository([draft]), storage=storage
        )

        response = client.get(
            "/api/v1/brand/assets/candidate-1/preview", params={"business_id": str(business_id)}
        )

        assert response.status_code == 200
        assert response.content == _PNG_BYTES


class TestDiscoverBrand:
    def test_rejects_an_invalid_url(self) -> None:
        client = _client()

        response = client.post(
            "/api/v1/brand/discover",
            params={"business_id": str(BusinessId.new())},
            json={"url": "not a url"},
        )

        assert response.status_code == 422

    def test_maps_unreachable_website_to_a_validation_error(self) -> None:
        client = _client(discovery=_FakeWebsiteBrandDiscovery(fails=True))

        response = client.post(
            "/api/v1/brand/discover",
            params={"business_id": str(BusinessId.new())},
            json={"url": "https://example-business.test"},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "DISCOVERY_UNREACHABLE"

    def test_saves_and_returns_the_draft(self) -> None:
        business_id = BusinessId.new()
        drafts = InMemoryBrandDiscoveryDraftRepository()
        client = _client(drafts=drafts)

        response = client.post(
            "/api/v1/brand/discover",
            params={"business_id": str(business_id)},
            json={"url": "https://example-business.test"},
        )

        assert response.status_code == 200
        assert response.json()["source_url"] == "https://example-business.test"


class TestGetBrandDraft:
    def test_returns_404_when_no_draft_exists(self) -> None:
        client = _client()

        response = client.get("/api/v1/brand/draft", params={"business_id": str(BusinessId.new())})

        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_returns_the_stored_draft(self) -> None:
        business_id = BusinessId.new()
        draft = BrandDiscoveryDraft(
            business_id=business_id,
            source_url="https://example-business.test",
            discovered_at=_NOW,
            logo_candidates=(_LOGO,),
        )
        client = _client(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

        response = client.get("/api/v1/brand/draft", params={"business_id": str(business_id)})

        assert response.status_code == 200
        body = response.json()
        assert body["logo_candidates"][0]["asset_id"] == "logo-1"


class TestConfirmBrandDraft:
    def _confirm_body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "primary_font": "Poppins",
            "font_licence_note": "Google Fonts, SIL OFL 1.1",
            "tone_description": "Cercano y claro.",
            "palette": [
                {"role": "primary", "hex": "#112233", "contrast_ratio_on_white": 10.0},
            ],
            "selected_asset_ids": ["logo-1"],
        }
        body.update(overrides)
        return body

    def test_returns_404_when_no_draft_exists(self) -> None:
        client = _client()

        response = client.post(
            "/api/v1/brand/confirm",
            params={"business_id": str(BusinessId.new())},
            json=self._confirm_body(),
        )

        assert response.status_code == 404

    def test_returns_422_when_selected_asset_is_stale(self) -> None:
        business_id = BusinessId.new()
        draft = BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
        client = _client(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

        response = client.post(
            "/api/v1/brand/confirm",
            params={"business_id": str(business_id)},
            json=self._confirm_body(),
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

    def test_confirms_the_draft_into_an_is_confirmed_kit(self) -> None:
        business_id = BusinessId.new()
        draft = BrandDiscoveryDraft(
            business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
        )
        client = _client(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

        response = client.post(
            "/api/v1/brand/confirm",
            params={"business_id": str(business_id)},
            json=self._confirm_body(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_confirmed"] is True
        assert body["typography"]["primary_family"] == "Poppins"


class TestUpdateBrandClaims:
    def _claims_body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "claims_allowlist": ["Envio gratis"],
            "forbidden_claims": ["mejor del mercado"],
            "legal_disclaimers": [{"text": "Aviso legal nuevo", "applies_to": None}],
        }
        body.update(overrides)
        return body

    def test_returns_404_when_business_has_no_kit(self) -> None:
        client = _client()

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(BusinessId.new())},
            json=self._claims_body(),
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_replaces_the_claims_and_returns_the_updated_kit(self) -> None:
        business_id = BusinessId.new()
        decision_recorder = RecordingBrandClaimsDecisionRecorder()
        client = _client(
            kits=InMemoryBrandKitRepository([make_brand_kit(business_id=business_id)]),
            decision_recorder=decision_recorder,
        )

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(business_id)},
            json=self._claims_body(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["claims_allowlist"] == ["Envio gratis"]
        assert {"claim": "mejor del mercado", "is_floor": False} in body["forbidden_claims"]
        assert {"claim": "garantizado", "is_floor": True} in body["forbidden_claims"]
        assert body["legal_disclaimers"] == [{"text": "Aviso legal nuevo", "applies_to": None}]
        assert len(decision_recorder.recorded) == 1
        assert decision_recorder.recorded[0].actor_email == _OWNER.email

    def test_works_on_an_unconfirmed_draft_preview_kit(self) -> None:
        business_id = BusinessId.new()
        kit = make_brand_kit(business_id=business_id, is_confirmed=False)
        client = _client(kits=InMemoryBrandKitRepository([kit]))

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(business_id)},
            json=self._claims_body(),
        )

        assert response.status_code == 200
        assert response.json()["is_confirmed"] is False

    def test_returns_422_when_an_allowed_claim_conflicts_with_the_safety_floor(self) -> None:
        business_id = BusinessId.new()
        client = _client(kits=InMemoryBrandKitRepository([make_brand_kit(business_id=business_id)]))

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(business_id)},
            json=self._claims_body(claims_allowlist=["Garantizado"], forbidden_claims=[]),
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

    def test_rejects_a_claim_shorter_than_two_characters(self) -> None:
        business_id = BusinessId.new()
        client = _client(kits=InMemoryBrandKitRepository([make_brand_kit(business_id=business_id)]))

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(business_id)},
            json=self._claims_body(claims_allowlist=["a"]),
        )

        assert response.status_code == 422

    def test_rejects_more_than_fifty_claims_in_a_list(self) -> None:
        business_id = BusinessId.new()
        client = _client(kits=InMemoryBrandKitRepository([make_brand_kit(business_id=business_id)]))

        response = client.put(
            "/api/v1/brand/claims",
            params={"business_id": str(business_id)},
            json=self._claims_body(claims_allowlist=[f"reclamo {i}" for i in range(51)]),
        )

        assert response.status_code == 422
