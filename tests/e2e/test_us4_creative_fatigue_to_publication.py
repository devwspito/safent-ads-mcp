"""T111 (tasks.md): fatiga -> piezas -> propuesta de publicacion -> nada
llega a la plataforma hasta aprobar. `signals/application/
score_creative_fatigue.py` existe (M16, CTR cae >=10% frente a la linea
base de 14D) pero ningun banco lo encadenaba de punta a punta con
`GenerateCreativeAssets` (piezas), `ProposeCreative` (propuesta) y el
`ExecutionChokepoint` real (quickstart.md §8 solo prueba el tramo F4 suelto:
"POST /creative-jobs [...] Nada se publica sin propuesta aprobada", sin la
senal FATIGUE real que lo dispara ni el desenlace tras aprobar).

Real Postgres para: metricas -> `ScoreCreativeFatigue` -> `signals`;
`GenerateCreativeAssets` -> `creative_jobs`/`creative_assets`; `ProposeCreative`
-> `proposals` real; `POST /proposals/{id}/approve` -> `ExecutionChokepoint`
real. Solo el renderizador de imagen esta doblado, en el borde exacto que
contracts/platform-port.md permite ("SDK mocked in tests"):
`_FakeImageRenderer`, el mismo doble que
`tests/integration/creative/test_authorization_integration.py`. Este banco
NO levanta ningun bróker (ni real ni doble): sin `PlatformReaderPort`
configurado, `PlatformStateRevalidator.has_drifted` trata el fallo de
lectura como deriva -- default-deny, threat-model.md C-16 -- y el
chokepoint nunca llega a intentar una escritura (`SKIPPED_DRIFT`). Un
segundo hallazgo, documentado pero no ejercido por este banco (exigiria
levantar un bróker de verdad solo para demostrarlo dos veces): aunque el
estado remoto SI se confirmase, `execution/infrastructure/
broker_platform.py::_OPERATION_BY_PARAMETER` no tiene entrada para
`creative_publication` -- la escritura tampoco llegaria a la plataforma,
mismo limite honesto que `CREATE_CAMPAIGN` (quickstart.md §8.5).

Gap confirmado leyendo el codigo, no adivinado: no existe ninguna ruta REST
ni caso de uso para adjuntar `ad_copy` a un `CreativeAsset` ya renderizado
(`LocalPolicyChecker.check` exige `asset.ad_copy is not None` antes de
evaluar politica, `creative/infrastructure/policy_checker.py:32`, pero
`GenerateCreativeAssets._build_asset` siempre construye el activo con
`ad_copy=None`, `creative/application/generate_creative_assets.py:232-242`
-- el copy RSA queda para `copycat`, profitability-engine.md §9). Mismo
limite que ya asumio `test_authorization_integration.py::
test_propose_publication_creates_a_real_proposal`: este banco adjunta el
copy releyendo/reescribiendo el agregado por el repositorio (una
transaccion propia, fuera de cualquier endpoint), nunca inventando un
endpoint que no existe."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.creative_proposal_gateway import ContainerCreativeProposalGateway
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.propose_creative import ProposeCreative
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.creative.infrastructure.sql_repositories import (
    RequestScopedCreativeAssetRepository,
    RequestScopedCreativeBriefRepository,
    RequestScopedCreativeJobRepository,
    SqlCreativeAssetRepository,
)
from safent_ads.creative.presentation.router import build_creative_router
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.application.score_creative_fatigue import (
    ScoreCreativeFatigue,
    ScoreCreativeFatigueRequest,
)
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import CreativeSignalKind
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlCreativeSignalRepository,
    SqlMetricWindowRepository,
)
from tests.contracts.execution.conftest import GuardrailLimits, seed_guardrails
from tests.integration.creative.test_authorization_integration import _FakeImageRenderer
from tests.unit.composition.factories import build_api_settings
from tests.unit.creative.domain.factories import make_ad_copy

pytestmark = pytest.mark.integration

_RAW_TOKEN = "qa-us4-fatigue-session-token"  # noqa: S105 - fixture, no secreto real
_AS_OF = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
_PASSING_GATES = (GateVerdict.ok(GateName.LEARNING),)
_STATE_HASH = "a" * 64
_OLDER_WEEK_DAILY_CLICKS = 300  # CTR 3.0% -- linea base sana de 14D
_RECENT_WEEK_DAILY_CLICKS = 100  # CTR 1.0% -- caida del 50%, muy por encima del 10% de M16
_DAILY_IMPRESSIONS = 10_000


@dataclass(frozen=True, slots=True)
class _FatiguedCreative:
    business_id: uuid.UUID
    creative_ref: EntityRef
    ad_ref: EntityRef


async def _seed_fatigued_creative_chain(session: AsyncSession) -> _FatiguedCreative:
    """Jerarquia completa `campaign -> ad_set -> ad -> creative`
    (`ad_entities_parent_level_check`, alembic/versions/0003_ad_entities.py)
    mas 14 dias de `metrics_daily` sobre la CREATIVE con el patron de M16:
    semana antigua a CTR 3.0%, semana reciente a CTR 1.0% -- caida del 50%,
    muy por encima del umbral de aviso (10%, `signal_engine.py:_M16_WARN_DROP_PCT`)."""
    business_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = business_id.hex[:12]
    await session.execute(
        text(
            "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
            "VALUES (:id, :slug, 'Negocio con fatiga creativa', 'Europe/Madrid', 'EUR')"
        ),
        {"id": business_id, "slug": f"fatiga-{suffix}"},
    )
    await session.execute(
        text(
            "INSERT INTO credential_refs (id, platform, alias) "
            "VALUES (:id, 'meta', :alias)"
        ),
        {"id": credential_id, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                           currency, timezone, api_tier, credential_ref_id,
                                           status)
            VALUES (:id, :business_id, 'meta', :external_account_id, 'EUR',
                    'Europe/Madrid', 'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "external_account_id": f"act_{suffix}",
            "credential_ref_id": credential_id,
        },
    )

    chain = [
        (EntityLevel.CAMPAIGN, f"cmp-{suffix}", None),
        (EntityLevel.AD_SET, f"ads-{suffix}", EntityLevel.CAMPAIGN),
        (EntityLevel.AD, f"ad-{suffix}", EntityLevel.AD_SET),
        (EntityLevel.CREATIVE, f"cr-{suffix}", EntityLevel.AD),
    ]
    parent_id: uuid.UUID | None = None
    refs_by_level: dict[EntityLevel, EntityRef] = {}
    for level, external_id, _parent_level in chain:
        entity_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO ad_entities (id, business_id, platform_account_id, platform, level,
                                         external_id, name, status, platform_state_hash, parent_id)
                VALUES (:id, :business_id, :account_id, 'meta', :level, :external_id,
                        :name, 'ACTIVE', :state_hash, :parent_id)
                """
            ),
            {
                "id": entity_id,
                "business_id": business_id,
                "account_id": account_id,
                "level": level.value,
                "external_id": external_id,
                "name": f"{level.value} de contrato",
                "state_hash": _STATE_HASH,
                "parent_id": parent_id,
            },
        )
        parent_id = entity_id
        refs_by_level[level] = EntityRef(
            platform=PlatformCode.META, level=level, external_id=external_id
        )
    await session.flush()

    creative_ref = refs_by_level[EntityLevel.CREATIVE]
    end_date = _AS_OF.date()
    for offset in range(14):
        stat_date = end_date - timedelta(days=offset)
        is_recent_week = offset < 7
        clicks = _RECENT_WEEK_DAILY_CLICKS if is_recent_week else _OLDER_WEEK_DAILY_CLICKS
        await _insert_metrics_daily_row(
            session,
            business_id=business_id,
            entity_ref=creative_ref,
            account_id=account_id,
            stat_date=stat_date,
            impressions=_DAILY_IMPRESSIONS,
            clicks=clicks,
        )

    # `_pass_gates` del chokepoint exige un `GuardrailSet` que alcance al
    # `entity_ref` de la propuesta (aunque el diff no sea monetario, ver
    # `money_pair_from_diff`): sin ninguna fila de `guardrails`,
    # `SqlGuardrailSets.get_effective` lanza `MissingGuardrailSetError`
    # antes de llegar a evaluar nada de negocio.
    await seed_guardrails(
        session,
        scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(refs_by_level[EntityLevel.AD])),
        limits=GuardrailLimits(),
        level="business",
    )

    # `POST /proposals/{id}/approve` exige `_OwnerDep` (`CURRENT_OWNER`,
    # `iam.presentation.dependencies`), una sesion REAL por cookie -- no
    # basta con el override de `get_authenticated_caller` que usa el resto
    # del banco (mismo patron que
    # `tests/e2e/test_us3_approve_without_fatigue.py::seeded_owner_session`).
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    await session.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": owner_id,
            "email": f"owner-{suffix}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
        ),
        {
            "id": session_id,
            "owner_id": owner_id,
            "token_hash": hashlib.sha256(_RAW_TOKEN.encode()).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        },
    )
    await session.flush()

    return _FatiguedCreative(
        business_id=business_id, creative_ref=creative_ref, ad_ref=refs_by_level[EntityLevel.AD]
    )


async def _insert_metrics_daily_row(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: EntityRef,
    account_id: uuid.UUID,
    stat_date: date,
    impressions: int,
    clicks: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO metrics_daily (business_id, entity_ref, entity_level,
                                       platform_account_id, stat_date, account_timezone,
                                       currency, spend, impressions, clicks)
            VALUES (:business_id, :entity_ref, :entity_level, :account_id, :stat_date,
                    'Europe/Madrid', 'EUR', 50, :impressions, :clicks)
            """
        ),
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "entity_level": entity_ref.level.value,
            "account_id": account_id,
            "stat_date": stat_date,
            "impressions": impressions,
            "clicks": clicks,
        },
    )


@pytest.fixture
async def fatigued_creative(isolated_database_url: str) -> AsyncIterator[_FatiguedCreative]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            fixture = await _seed_fatigued_creative_chain(session)
            await session.commit()
        yield fixture
    finally:
        await engine.dispose()


def _build_app(container: Container) -> FastAPI:
    briefs = RequestScopedCreativeBriefRepository(container.session_factory)
    assets = RequestScopedCreativeAssetRepository(container.session_factory)
    jobs = RequestScopedCreativeJobRepository(container.session_factory)
    asset_store = LocalAssetStorage(
        Path("/tmp/creative-fatigue-e2e-assets"),
        signing_key=b"e2e-test-signing-key-not-a-secret",
        clock=container.clock,
    )
    generate = GenerateCreativeAssets(
        briefs=briefs,
        jobs=jobs,
        assets=assets,
        image_renderers={RendererName.GPT_IMAGE_1_5: _FakeImageRenderer()},
        renderer_selector=RendererSelector(),
        gpu_lease=InProcessGpuQueue(container.clock),
    )
    router = build_creative_router(
        briefs=briefs,
        assets=assets,
        jobs=jobs,
        asset_store=asset_store,
        generate_creative_assets=generate,
        run_policy_check=RunPolicyCheck(LocalPolicyChecker(assets), assets),
        propose_creative=ProposeCreative(ContainerCreativeProposalGateway(container), assets),
        clock=container.clock,
    )
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(router)
    app.include_router(build_execution_router(container))
    return app


def _client_scoped_to(app: FastAPI, business_id: uuid.UUID) -> httpx.AsyncClient:
    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(business_id)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


def _brief_payload(
    *, business_id: str, source_signal_id: str, logo_asset_id: str
) -> dict[str, object]:
    return {
        "business_id": business_id,
        "calendar_event_id": None,
        "objective": "lead",
        "audience_summary": "Adultos 25-45",
        "hook": "Renueva la creatividad antes de que siga cayendo",
        "shots": [
            {"order": 1, "description": "Aula"},
            {"order": 2, "description": "Presentador"},
            {"order": 3, "description": "Estudiante feliz"},
        ],
        "on_screen_text": [],
        "cta": "Apúntate ya",
        "voiceover_lines": [],
        "brand_kit": {
            "primary_font": "Inter",
            "secondary_font": "Inter",
            "primary_color_hex": "#112233",
            "secondary_color_hex": "#FFFFFF",
            "logo_asset_id": logo_asset_id,
        },
        "source_signal_id": source_signal_id,
        "variant_count": 1,
    }


async def _attach_ad_copy(session: AsyncSession, asset_id: AssetId) -> None:
    """Unico punto del banco donde se toca el agregado fuera de un
    endpoint -- ver el docstring del modulo: no existe ruta REST ni caso de
    uso que adjunte `ad_copy` a un activo ya renderizado."""
    repo = SqlCreativeAssetRepository(session)
    asset = await repo.get(asset_id)
    assert asset is not None
    with_copy = CreativeAsset(
        asset_id=asset.asset_id,
        business_id=asset.business_id,
        media_kind=asset.media_kind,
        format=asset.format,
        duration_seconds=asset.duration_seconds,
        storage_uri=asset.storage_uri,
        checksum=asset.checksum,
        cost_estimate=asset.cost_estimate,
        provenance=asset.provenance,
        ad_copy=make_ad_copy(),
        destination_url=asset.destination_url,
    )
    await repo.update(with_copy)


async def test_fatigue_signal_pieces_publication_proposal_and_approval_gate(  # noqa: PLR0915 - e2e de 6 pasos, mismo criterio que test_write_path_end_to_end.py
    fatigued_creative: _FatiguedCreative, isolated_database_url: str
) -> None:
    fixture = fatigued_creative

    # ------------------------------------------------------------------
    # 1) `score_creative_fatigue.py` REAL sobre metricas REALES: M16 detecta
    # la caida de CTR del 50% (>=10%) y persiste una senal FATIGUE de
    # verdad en `signals`.
    # ------------------------------------------------------------------
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            use_case = ScoreCreativeFatigue(
                SqlMetricWindowRepository(session),
                SqlCreativeSignalRepository(session, cycle_id=uuid.uuid4()),
            )
            signal = await use_case.execute(
                ScoreCreativeFatigueRequest(
                    entity_ref=fixture.creative_ref,
                    currency="EUR",
                    gate_verdicts=_PASSING_GATES,
                    as_of=_AS_OF,
                )
            )
            await session.commit()
        assert signal is not None
        assert signal.kind is CreativeSignalKind.FATIGUE

        async with AsyncSession(engine, expire_on_commit=False) as session:
            persisted = await SqlCreativeSignalRepository(
                session, cycle_id=uuid.uuid4()
            ).find_latest_for_entity(entity_ref=fixture.creative_ref)
        assert persisted is not None and persisted.signal_id is not None
        source_signal_id = persisted.signal_id
    finally:
        await engine.dispose()

    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(_AS_OF)
    try:
        app = _build_app(container)
        async with _client_scoped_to(app, fixture.business_id) as client:
            # --------------------------------------------------------
            # 2) Piezas solicitadas: `POST /creative-jobs` con el
            # renderizador de imagen DOBLADO en el puerto (`ImageRendererPort`,
            # "SDK mocked in tests"), disparado por la senal FATIGUE.
            # --------------------------------------------------------
            create_response = await client.post(
                "/api/v1/creative-jobs",
                json={
                    "brief": _brief_payload(
                        business_id=str(fixture.business_id),
                        source_signal_id=source_signal_id,
                        logo_asset_id=str(AssetId.new()),
                    )
                },
            )
            assert create_response.status_code == 202, create_response.text
            job_id = create_response.json()["job_id"]

            job_response = await client.get(f"/api/v1/creative-jobs/{job_id}")
            assert job_response.status_code == 200
            job_body = job_response.json()
            assert job_body["state"] == "READY"
            assert len(job_body["assets"]) == 1
            asset_id_str = job_body["assets"][0]["asset_id"]

            # ad_copy no tiene ruta REST propia todavia (docstring del
            # modulo): se adjunta por el repositorio, en su propia
            # transaccion, antes de que el policy-check REAL pueda evaluar
            # nada (`LocalPolicyChecker.check` exige `ad_copy` presente).
            asset_id = AssetId.parse(asset_id_str)
            async with container.session_factory() as session:
                await _attach_ad_copy(session, asset_id)
                await session.commit()

            policy_response = await client.post(
                f"/api/v1/creatives/{asset_id_str}/policy-check",
                json={"platform": "meta", "placement": "feed"},
            )
            assert policy_response.status_code == 200, policy_response.text
            assert policy_response.json()["verdict"] == "PASS"

            # --------------------------------------------------------
            # 3) `propose-publication`: crea una `Proposal` IMPORTANT
            # `pending` -- nunca publica ella misma (FR-33).
            # --------------------------------------------------------
            propose_response = await client.post(
                f"/api/v1/creatives/{asset_id_str}/propose-publication",
                json={
                    "ad_set_ref": str(fixture.ad_ref),
                    "ad_copy": {
                        "headline": "Nueva creatividad, mismo objetivo",
                        "primary_text": "Renovamos el anuncio para seguir rindiendo.",
                        "cta": "Apúntate",
                    },
                    "typed_confirmation": "PUBLICAR",
                },
            )
            assert propose_response.status_code == 201, propose_response.text
            proposal_id = propose_response.json()["proposal_id"]

            async with container.session_factory() as session:
                row = (
                    await session.execute(
                        text("SELECT state, classification FROM proposals WHERE id = :id"),
                        {"id": proposal_id},
                    )
                ).mappings().one()
            assert row["state"] == "pending"
            assert row["classification"] == "important"

            # --------------------------------------------------------
            # 4) Nada llego a la plataforma TODAVIA: sin fila alguna en
            # `executions` para esta propuesta.
            # --------------------------------------------------------
            async with container.session_factory() as session:
                before_approval = (
                    await session.execute(
                        text("SELECT count(*) FROM executions WHERE proposal_id = :id"),
                        {"id": proposal_id},
                    )
                ).scalar_one()
            assert before_approval == 0, "nada debe tocar la plataforma antes de aprobar"

            # --------------------------------------------------------
            # 5) Aprobar por REST, mismo `ExecutionChokepoint` que
            # cualquier otra escritura -- SOLO ahora existe un intento de
            # ejecucion.
            # --------------------------------------------------------
            async with container.session_factory() as session:
                diff_hash = (
                    await session.execute(
                        text("SELECT diff_hash FROM proposals WHERE id = :id"), {"id": proposal_id}
                    )
                ).scalar_one()
            approve_response = await client.post(
                f"/api/v1/proposals/{proposal_id}/approve", json={"diff_hash": diff_hash}
            )
            assert approve_response.status_code == 200, approve_response.text
            grace_seconds = approve_response.json()["grace_seconds"]

            async with container.session_factory() as session:
                after_approval_count = (
                    await session.execute(
                        text("SELECT count(*) FROM executions WHERE proposal_id = :id"),
                        {"id": proposal_id},
                    )
                ).scalar_one()
            assert after_approval_count == 1, "la aprobacion debe agendar un unico intento"

            # --------------------------------------------------------
            # 6) Correr el chokepoint real (pasado el margen de gracia, T083).
            # Sin un bróker configurado (este banco no levanta ninguno --
            # solo dobla el renderizador de imagen), `PlatformStateRevalidator.
            # has_drifted` (execution/application/platform_state_revalidator.py:29-33)
            # trata el fallo de lectura como deriva, default-deny: la
            # plataforma nunca llega a CONTESTAR, y mucho menos a recibir
            # una escritura. `SKIPPED_DRIFT` dice lo mismo que
            # `test_learning_gated_entity_gets_a_hold_signal_via_real_cycle`
            # de otra forma: sin confirmar el estado remoto, no se ejecuta
            # a ciegas (threat-model.md C-16).
            #
            # Hallazgo documentado, no ejercido en este banco: aun con un
            # bróker en pie que SI confirmase el estado, la publicacion de
            # creatividad seguiria sin llegar a la plataforma -- `
            # execution/infrastructure/broker_platform.py::_OPERATION_BY_PARAMETER`
            # (linea 60) no tiene entrada para el parametro
            # `creative_publication` que `composition/mcp_write_adapter.py:213`
            # usa, asi que `_operation()` lanzaria `UnsupportedWriteParameterError`
            # antes de tocar el puerto de plataforma -- mismo limite honesto
            # ya documentado para `CREATE_CAMPAIGN` en quickstart.md §8.5.
            # --------------------------------------------------------
            container.clock.advance_to(  # type: ignore[attr-defined]
                _AS_OF + timedelta(seconds=grace_seconds + 1)
            )
            async with container.session_factory() as session:
                use_cases = container.build_execution_use_cases(session)
                outcome = await use_cases.chokepoint.run_once()
                await session.commit()
        assert outcome is ExecutionStatus.SKIPPED_DRIFT

        async with container.session_factory() as session:
            outcome_row = (
                await session.execute(
                    text("SELECT outcome FROM executions WHERE proposal_id = :id"),
                    {"id": proposal_id},
                )
            ).mappings().one()
        assert outcome_row["outcome"] == "SKIPPED_DRIFT"
    finally:
        await container.aclose()
