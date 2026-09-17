"""`packages.presentation.{panel_read,rest}` de extremo a extremo (T030/
T031): router real sobre `Container`, cookie de sesion real (mismo patron
que `tests/integration/composition/test_proposal_admin_rest.py`)."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import text

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.creative.domain.enums import MediaKind, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.domain.policy import PolicyVerdict
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.creative.infrastructure.sql_repositories import (
    SqlCreativeAssetRepository,
    SqlCreativeBriefRepository,
)
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.packages.domain.campaign_package import PackageState, PartialOutcome
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.packages.presentation.panel_read import build_package_read_router
from safent_ads.packages.presentation.rest import build_package_admin_router
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import BusinessId
from tests.integration.composition.conftest import AuthenticatedSession
from tests.integration.composition.conftest import (
    authenticated_session as authenticated_session,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.composition.factories import build_api_settings
from tests.unit.creative.domain.factories import make_brief
from tests.unit.creative.infrastructure.fakes import make_creative_asset
from tests.unit.packages.domain.conftest import propose_meta_package

pytestmark = pytest.mark.integration

_GUARDRAIL_SQL = text("""
    INSERT INTO guardrails
        (scope, business_id, currency, daily_cap_minor, monthly_cap_minor, budget_floor_minor,
         budget_ceiling_minor, max_step_pct, max_changes_per_entity_per_day)
    VALUES ('business', :business_id, 'EUR', 3000, 90000, 0, 3000, 30, 10)
""")

# `isolated_database_url` (tests/conftest.py) es una unica base por sesion de
# pytest, no por test: `tests/integration/notifications/test_telegram_brake_flow.py
# ::test_freno_on_confirmed_engages_the_brake_via_the_rest_use_case` engancha
# el freno GLOBAL a proposito y no lo libera (verifica justo eso), asi que un
# freno GLOBAL sigue activo para cualquier test posterior de la misma sesion
# que comparta esa base -- `get_effective` siempre lo consulta (chokepoint.py),
# sin importar el negocio/cuenta sembrados aqui. Empezar cada test de este
# fichero sin ningun freno activo es aislamiento de test, no un cambio de
# semantica del freno.
_RELEASE_STALE_BRAKES_SQL = text("""
    UPDATE emergency_brakes SET released_at = now(), released_by = 'test-isolation-reset'
     WHERE released_at IS NULL
""")


@dataclass(frozen=True, slots=True)
class _Seeded:
    container: Container
    scope: SeededScope


@pytest.fixture
async def seeded(
    isolated_database_url: str, authenticated_session: AuthenticatedSession
) -> AsyncIterator[_Seeded]:
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    business_id = uuid.uuid4()
    async with container.session_factory() as session:
        await session.execute(_RELEASE_STALE_BRAKES_SQL)
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de paquete', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"pkg-{uuid.uuid4().hex[:10]}"},
        )
        scope = await seed_package_scope(
            session, owner_id=authenticated_session.owner_id, business_id=business_id
        )
        await session.execute(_GUARDRAIL_SQL, {"business_id": business_id})
        await session.commit()
    try:
        yield _Seeded(container=container, scope=scope)
    finally:
        await container.aclose()


def _app(container: Container, asset_store: LocalAssetStorage) -> FastAPI:
    application = FastAPI()
    application.state.container = container
    application.add_exception_handler(ApiError, _handle_api_error)
    application.include_router(build_package_read_router(container.session_factory, asset_store))
    application.include_router(
        build_package_admin_router(
            container.session_factory,
            SystemClock(),
            asset_store,
            container.approval_key_pair.signer,
            lambda session: container.build_execution_use_cases(session).pause_entity,
        )
    )
    return application


def _client(
    container: Container, asset_store: LocalAssetStorage, cookies: dict[str, str]
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(container, asset_store))
    return httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies)


async def _seed_package(container: Container, scope: SeededScope):
    package = propose_meta_package(
        # La fabrica usa un NOW fijo (14-sep) + 72 h: con el reloj REAL del caso de uso
        # el paquete nacia caducado desde el 17-sep 10:00 UTC. Aqui nace ahora.
        now=SystemClock().now(),
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )
    async with container.session_factory() as session:
        await SqlCampaignPackageRepository(session).add(package)
        await session.commit()
    return package


async def _owner_email(container: Container, owner_id: uuid.UUID) -> str:
    async with container.session_factory() as session:
        row = (
            await session.execute(
                text("SELECT email FROM owners WHERE id = :id"), {"id": owner_id}
            )
        ).mappings().one()
    return str(row["email"])


async def _last_decision(container: Container, business_id: BusinessId, event_type: str) -> dict:
    """M3 (repaso de seguridad 0.2.23): `payload::text` (nunca la
    decodificacion automatica del driver), mismo criterio que
    `SqlDecisionLogRepository` -- ver su docstring."""
    async with container.session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT actor_id, payload::text AS payload_text FROM decision_log "
                    "WHERE business_id = :business_id AND event_type = :event_type "
                    "ORDER BY seq DESC LIMIT 1"
                ),
                {"business_id": business_id.value, "event_type": event_type},
            )
        ).mappings().one()
    return {"actor_id": row["actor_id"], "payload": json.loads(row["payload_text"])}


def _tiny_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buffer, format="PNG")
    return buffer.getvalue()


async def _seed_usable_asset(container: Container, business_id: BusinessId, storage_dir: Path):
    asset_store = LocalAssetStorage(storage_dir, b"0" * 32, SystemClock())
    storage_uri = await asset_store.put(_tiny_png(), MediaKind.IMAGE)
    async with container.session_factory() as session:
        brief_id = BriefId.new()
        await SqlCreativeBriefRepository(session).add(brief_id, make_brief(business_id=business_id))
        asset = make_creative_asset(
            business_id=business_id, storage_uri=storage_uri, brief_id=brief_id
        )
        asset.mark_ready(PolicyVerdict(verdict=PolicyVerdictResult.PASS_, findings=()))
        await SqlCreativeAssetRepository(session).add(asset)
        await session.commit()
    return asset, asset_store


class TestApprove:
    async def test_happy_path_signs_one_authorization_and_creates_the_publication(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/approve?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["grace_seconds"] == 45
        assert body["publication_id"]
        assert body["authorization_id"]

        async with seeded.container.session_factory() as session:
            reloaded = await SqlCampaignPackageRepository(session).get(
                package.package_id, business_id=seeded.scope.business_id
            )
            record = await SqlPackagePublicationRepository(session).get_by_package_id(
                package.package_id
            )
            authorization_row = (
                await session.execute(
                    text(
                        "SELECT subject_kind, subject_id, proposal_id FROM approvals "
                        "WHERE id = :id"
                    ),
                    {"id": body["authorization_id"]},
                )
            ).mappings().one()
        assert reloaded is not None
        assert reloaded.state is PackageState.APPROVED
        assert record is not None
        assert record.state == "pending"
        assert authorization_row["subject_kind"] == "package"
        assert authorization_row["subject_id"] == str(package.package_id)
        assert authorization_row["proposal_id"] is None

    async def test_hash_mismatch_is_409(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/approve?business_id={seeded.scope.business_id}",
                json={"package_hash": "f" * 64},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PACKAGE_CHANGED"

    async def test_unknown_package_is_404(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{uuid.uuid4()}/approve?business_id={seeded.scope.business_id}",
                json={"package_hash": "f" * 64},
            )

        assert response.status_code == 404


class TestReject:
    async def test_happy_path_transitions_to_rejected(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/reject?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value, "comment": "no aplica"},
            )

        assert response.status_code == 200
        async with seeded.container.session_factory() as session:
            reloaded = await SqlCampaignPackageRepository(session).get(
                package.package_id, business_id=seeded.scope.business_id
            )
        assert reloaded is not None
        assert reloaded.state is PackageState.REJECTED

    async def test_records_who_rejected_it_and_why(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        # M3 (repaso de seguridad 0.2.23): `reject` no dejaba ningun rastro
        # queryable de quien ni por que -- ni siquiera leia al dueño
        # autenticado (`_owner` llegaba sin usar).
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())
        owner_email = await _owner_email(seeded.container, authenticated_session.owner_id)

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/reject?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value, "comment": "no aplica"},
            )

        assert response.status_code == 200
        decision = await _last_decision(
            seeded.container, seeded.scope.business_id, "package_rejected"
        )
        assert decision["actor_id"] == owner_email
        assert decision["payload"]["package_id"] == str(package.package_id)
        assert decision["payload"]["comment"] == "no aplica"


class TestResume:
    async def test_a_proposed_package_is_not_resumable(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/resume"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PACKAGE_NOT_RESUMABLE"

    async def test_hash_mismatch_is_409(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{package.package_id}/resume"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": "f" * 64},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] in {"PACKAGE_CHANGED", "PACKAGE_NOT_RESUMABLE"}

    async def test_records_who_resumed_it(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        # M3 (repaso de seguridad 0.2.23): `resume` solo dejaba un log de
        # structlog -- ephemero, nunca queryable como el resto de
        # decisiones del dueño.
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())
        owner_email = await _owner_email(seeded.container, authenticated_session.owner_id)

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            approved = await client.post(
                f"/api/v1/packages/{package.package_id}/approve"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )
            assert approved.status_code == 200, approved.text
            await _halt_mid_publication(seeded.container, seeded.scope, package)

            response = await client.post(
                f"/api/v1/packages/{package.package_id}/resume"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )

        assert response.status_code == 200, response.text
        decision = await _last_decision(
            seeded.container, seeded.scope.business_id, "package_resumed"
        )
        assert decision["actor_id"] == owner_email
        assert decision["payload"]["package_id"] == str(package.package_id)
        assert decision["payload"]["publication_id"] == response.json()["publication_id"]


async def _halt_mid_publication(container: Container, scope: SeededScope, package) -> None:
    """Deja el paquete en `PARTIALLY_PUBLISHED`/la publicacion en `halted`
    sin correr la saga entera -- lo unico que `resume` necesita para
    aceptar reanudarla (`_RESUMABLE_PACKAGE_STATES`), igual que un halt
    real a mitad de camino."""
    now = SystemClock().now()
    async with container.session_factory() as session:
        packages_repo = SqlCampaignPackageRepository(session)
        publications_repo = SqlPackagePublicationRepository(session)
        reloaded = await packages_repo.get(package.package_id, business_id=scope.business_id)
        assert reloaded is not None
        record = await publications_repo.get_by_package_id(package.package_id)
        assert record is not None
        reloaded.begin_publishing(now)
        # El trigger `campaign_packages_guard` (0042) solo admite saltos de
        # UN paso -- `approved -> partially_published` directo lo rechaza,
        # igual que le pasaria a `RunPackagePublication` si no guardase
        # `PUBLISHING` primero.
        await packages_repo.save(reloaded)
        reloaded.record_publication_outcome(
            PartialOutcome(created_count=1, failed_step_index=1, next_step_hint="Continuar."), now
        )
        await packages_repo.save(reloaded)
        await publications_repo.advance(
            record.publication_id,
            cursor=record.cursor,
            state="halted",
            halt_reason="brake_engaged",
            failed_step_index=1,
            finished_at=now,
        )
        await session.commit()


class TestUndo:
    async def test_cancels_within_the_forty_five_second_grace_window(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            approved = await client.post(
                f"/api/v1/packages/{package.package_id}/approve"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )
            assert approved.status_code == 200, approved.text

            response = await client.post(
                f"/api/v1/packages/{package.package_id}/undo"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value, "reason": "me arrepenti"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["undo_kind"] == "cancelled_publication"
        async with seeded.container.session_factory() as session:
            reloaded = await SqlCampaignPackageRepository(session).get(
                package.package_id, business_id=seeded.scope.business_id
            )
            record = await SqlPackagePublicationRepository(session).get_by_package_id(
                package.package_id
            )
        assert reloaded is not None
        assert reloaded.state is PackageState.INVALIDATED
        assert record is not None
        assert record.state == "halted"

    async def test_records_who_undid_it_and_why(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        # M3 (repaso de seguridad 0.2.23): `UndoPackagePublication` nunca
        # persistia `reason` en ningun sitio -- `_cancel` archiva un motivo
        # fijo ("cancelled_by_owner"), nunca el que el dueño escribio.
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())
        owner_email = await _owner_email(seeded.container, authenticated_session.owner_id)

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            approved = await client.post(
                f"/api/v1/packages/{package.package_id}/approve"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value},
            )
            assert approved.status_code == 200, approved.text

            response = await client.post(
                f"/api/v1/packages/{package.package_id}/undo"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": package.package_hash.value, "reason": "me arrepenti"},
            )

        assert response.status_code == 200, response.text
        decision = await _last_decision(seeded.container, seeded.scope.business_id, "undo")
        assert decision["actor_id"] == owner_email
        assert decision["payload"]["package_id"] == str(package.package_id)
        assert decision["payload"]["reason"] == "me arrepenti"
        assert decision["payload"]["undo_kind"] == "cancelled_publication"

    async def test_unknown_package_is_404(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.post(
                f"/api/v1/packages/{uuid.uuid4()}/undo?business_id={seeded.scope.business_id}",
                json={"package_hash": "f" * 64, "reason": "x"},
            )

        assert response.status_code == 404


class TestOwnerContext:
    async def test_happy_path_sets_the_text(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/packages/{package.package_id}/owner-context"
                f"?business_id={seeded.scope.business_id}",
                json={"text": "Esperar a que el dueño confirme el horario."},
            )

        assert response.status_code == 200
        async with seeded.container.session_factory() as session:
            reloaded = await SqlCampaignPackageRepository(session).get(
                package.package_id, business_id=seeded.scope.business_id
            )
        assert reloaded is not None
        assert reloaded.owner_context == "Esperar a que el dueño confirme el horario."

    async def test_over_max_length_is_422(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.put(
                f"/api/v1/packages/{package.package_id}/owner-context"
                f"?business_id={seeded.scope.business_id}",
                json={"text": "a" * 501},
            )

        assert response.status_code == 422


class TestGetPackagePreview:
    async def test_happy_path_returns_the_full_preview_shape(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.get(
                f"/api/v1/packages/{package.package_id}?business_id={seeded.scope.business_id}"
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["package_id"] == str(package.package_id)
        assert body["approvable"] is True
        assert body["platform"]["account"]["entity_ref"] == str(package.account_ref)
        assert body["platform"]["account"]["name"] == package.account_ref.external_id
        assert body["platform"]["publish_as"]["page_name"] == "Clinica X"
        assert len(body["campaign"]["ad_sets"]) == 1
        ad = body["campaign"]["ad_sets"][0]["ads"][0]
        assert ad["image"] is not None
        assert ad["image"]["preview_url"].startswith("/api/v1/creative-previews/")
        assert ad["cta_label"] == "Más información"
        assert body["on_approve"]["creates"] == {"campaigns": 1, "ad_sets": 1, "ads": 1}

    async def test_unknown_package_is_404(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.get(
                f"/api/v1/packages/{uuid.uuid4()}?business_id={seeded.scope.business_id}"
            )

        assert response.status_code == 404


class TestListPackages:
    async def test_unknown_state_filter_is_422_not_500(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        """M3 (revision de codigo): `PackageState("bogus")` lanzaba
        `ValueError` sin capturar -- entrada de cliente invalida es `422`,
        nunca un `500`."""
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.get(
                f"/api/v1/packages?business_id={seeded.scope.business_id}&state=bogus"
            )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_valid_state_filter_lists_only_matching_packages(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset_store = LocalAssetStorage(tmp_path, b"0" * 32, SystemClock())

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            response = await client.get(
                f"/api/v1/packages?business_id={seeded.scope.business_id}&state=proposed"
            )

        assert response.status_code == 200, response.text
        assert [item["package_id"] for item in response.json()["items"]] == [
            str(package.package_id)
        ]


class TestCreativeCandidatesAndPatch:
    async def test_candidate_appears_and_patch_recalculates_the_hash(
        self, seeded: _Seeded, authenticated_session: AuthenticatedSession, tmp_path: Path
    ) -> None:
        package = await _seed_package(seeded.container, seeded.scope)
        asset, asset_store = await _seed_usable_asset(
            seeded.container, seeded.scope.business_id, tmp_path
        )
        original_hash = package.package_hash.value

        async with _client(seeded.container, asset_store, authenticated_session.cookies) as client:
            candidates_response = await client.get(
                f"/api/v1/packages/{package.package_id}/creative-candidates"
                f"?business_id={seeded.scope.business_id}&ad_local_ref=as%231/ad%231"
            )
            assert candidates_response.status_code == 200
            candidate_ids = {item["asset_id"] for item in candidates_response.json()["items"]}
            assert str(asset.asset_id) in candidate_ids

            patch_response = await client.patch(
                f"/api/v1/packages/{package.package_id}/ads/as%231/ad%231/creative"
                f"?business_id={seeded.scope.business_id}",
                json={"package_hash": original_hash, "creative_asset_id": str(asset.asset_id)},
            )

        assert patch_response.status_code == 200, patch_response.text
        new_hash = patch_response.json()["package_hash"]
        assert new_hash != original_hash
        async with seeded.container.session_factory() as session:
            reloaded = await SqlCampaignPackageRepository(session).get(
                package.package_id, business_id=seeded.scope.business_id
            )
        assert reloaded is not None
        assert reloaded.package_hash.value == new_hash
