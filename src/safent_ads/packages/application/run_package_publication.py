"""`RunPackagePublication` (T023): avanza UN paso de una publicacion por
invocacion -- `ads-worker` la llama una vez por publicacion abierta y por
ciclo (T029), igual que `ExecutionChokepoint.run_once` para propuestas
sueltas.

Antes de tocar nada: recomputa `package_hash` en vivo y lo compara con el
firmado (R3) y relee el freno (AL-1) -- las dos comprobaciones que
`ApproveCampaignPackage` ya hizo al aprobar pueden haber cambiado desde
entonces. Resuelve el `entity_ref` del padre (y de cada creatividad que el
anuncio dependa) desde su PROPIO recibo confirmado en
`campaign_package_steps` -- nunca de lo que el paso anterior "dijera" en
memoria (R5, cierra C-T1: sustitucion de padre). Tras ejecutar, relee
SIEMPRE el desenlace de `PackageStepExecutorPort` -- que a su vez lo relee
de la base -- nunca decide por un valor en memoria (AL-3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.accounts.application.errors import AdEntityParentNotFoundError, EntityNotFoundError
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
    RefreshRegisteredEntityStateCommand,
)
from safent_ads.accounts.application.register_created_entity import (
    RegisterCreatedEntity,
    RegisterCreatedEntityCommand,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.execution.application.ports import BrakeStatePort
from safent_ads.execution.domain.guardrails import BrakeScope, BrakeScopeKind
from safent_ads.packages.application.errors import (
    PackageNotFoundError,
    PublicationNotFoundError,
)
from safent_ads.packages.application.ports import (
    CampaignPackageRepository,
    PackagePublicationRecord,
    PackagePublicationRepository,
    PackageStepExecutorPort,
    PackageStepRecord,
    PackageStepRepository,
    StepExecutionOutcome,
)
from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    StepKind,
    StepTemplate,
    ad_equivalent_local_refs,
)
from safent_ads.packages.domain.campaign_package import (
    ActivatedOutcome,
    CampaignPackage,
    NoneCreatedOutcome,
    PackageState,
    PartialOutcome,
    UncertainOutcome,
)
from safent_ads.packages.domain.holes import creative_hole
from safent_ads.packages.domain.package_hash import compute_package_hash, package_tree_payload
from safent_ads.packages.domain.planned_tree import GoogleCampaignNative
from safent_ads.packages.domain.publication_policy import PACKAGE_UNDO_GRACE
from safent_ads.packages.domain.step_binding import (
    PackageStepBinding,
    derive_step_binding,
    parent_step_index_of,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import EntityRef

__all__ = ["RunPackagePublication", "RunPackagePublicationResult"]

_VISIBLE_STEP_KINDS = frozenset(
    {"create_campaign", "create_ad_set", "create_ad", "activate_campaign"}
)
# 003-entidades-creadas: los tres pasos que crean una entidad nueva --
# `ACTIVATE_CAMPAIGN` MODIFICA una ya registrada por su propio
# `CREATE_CAMPAIGN`, nunca crea una (ver `_refresh_activated_campaign`,
# item 2 del repaso 0.2.23).
_ENTITY_CREATING_STEP_KINDS = frozenset(
    {StepKind.CREATE_CAMPAIGN, StepKind.CREATE_AD_SET, StepKind.CREATE_AD}
)
_ENTITY_REGISTRATION_PARENT_MISSING = "package_entity_registration_parent_missing"
_ENTITY_ACTIVATION_REFRESH_MISSING = "package_entity_activation_refresh_missing"


class PublicationCorruptError(ApplicationError):
    """Un paso del sobre firmado no se puede reproducir -- nunca deberia
    ocurrir si `ApproveCampaignPackage` archivo el plan correctamente."""


@dataclass(frozen=True, kw_only=True, slots=True)
class RunPackagePublicationResult:
    publication_id: str
    publication_state: str
    package_state: str
    next_step_hint: str | None


class RunPackagePublication:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        publications: PackagePublicationRepository,
        steps: PackageStepRepository,
        step_executor: PackageStepExecutorPort,
        brakes: BrakeStatePort,
        entity_registration: RegisterCreatedEntity,
        entity_activation_refresh: RefreshRegisteredEntityState,
        clock: Clock,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._packages = packages
        self._publications = publications
        self._steps = steps
        self._step_executor = step_executor
        self._brakes = brakes
        self._entity_registration = entity_registration
        self._entity_activation_refresh = entity_activation_refresh
        self._clock = clock
        self._enabled_google_channels = enabled_google_channels

    async def execute(self, publication_id: str) -> RunPackagePublicationResult:
        record = await self._publications.get_by_id(publication_id)
        if record is None:
            raise PublicationNotFoundError(publication_id)
        envelope = record.envelope
        package = await self._packages.get(record.package_id, business_id=envelope.business_id)
        if package is None:
            raise PackageNotFoundError(str(record.package_id))
        if record.state in {"completed", "halted"}:
            # Terminal para este caso de uso: una publicacion `halted` solo
            # avanza de nuevo por `ResumePackagePublication` (decidir otra
            # vez, R2.5) -- reprocesar el mismo paso aqui repetiria el
            # `record_publication_outcome` sobre un paquete ya en estado
            # terminal (partially_published/failed), que `CampaignPackage`
            # rechaza por diseño.
            return RunPackagePublicationResult(
                publication_id=publication_id,
                publication_state=record.state,
                package_state=package.state.value,
                next_step_hint=None,
            )

        now = self._clock.now()
        if package.state is PackageState.APPROVED and now < record.started_at + PACKAGE_UNDO_GRACE:
            # FR-08: dentro de los 45 s previos, "Deshacer" cancela la
            # publicacion entera sin haber escrito nada -- el primer paso
            # no se ejecuta hasta que la gracia pase, para que esa promesa
            # sea literalmente cierta y no una carrera con el worker.
            return RunPackagePublicationResult(
                publication_id=publication_id,
                publication_state=record.state,
                package_state=package.state.value,
                next_step_hint=None,
            )
        if package.state is PackageState.APPROVED:
            package.begin_publishing(now)
            await self._packages.save(package)

        halted = await self._pre_flight_halt_reason(package, envelope, now)
        if halted is not None:
            # M6 (revision de codigo): un halt PRE-FLIGHT (huella cambiada,
            # freno puesto) ocurre ANTES de intentar ningun paso -- ninguno
            # ha fallado, asi que `failed_step_index` es `None`, nunca
            # `record.cursor` (el valor por defecto de `_halt`, correcto
            # solo cuando el halt SI corresponde a un paso concreto).
            return await self._halt(record, package, now, halted, failed_step_index=None)

        return await self._execute_step(record, package, envelope, now)

    async def _execute_step(
        self,
        record: PackagePublicationRecord,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        now: datetime,
    ) -> RunPackagePublicationResult:
        publication_id = record.publication_id
        step_index = record.cursor
        step_template = envelope.step_plan[step_index]
        if step_template.step_kind is StepKind.ACTIVATE_CAMPAIGN:
            structure_ready = await self._structure_complete_before_activation(
                publication_id, package, envelope, step_template
            )
            if not structure_ready:
                return await self._halt(
                    record,
                    package,
                    now,
                    "structure_incomplete_before_activation",
                    failed_step_index=step_index,
                )
        binding = await self._derive_admitted_binding(record, envelope, step_index)
        if binding is None:
            return await self._halt(
                record, package, now, "package_parent_unconfirmed", failed_step_index=step_index
            )

        resolutions = await self._resolve_creative_sources(publication_id, envelope, step_template)
        if resolutions is None:
            return await self._halt(
                record, package, now, "package_parent_unconfirmed", failed_step_index=step_index
            )

        existing_step = await self._steps.get(publication_id, step_index)
        current_step_state = existing_step.state if existing_step is not None else "pending"
        if existing_step is None:
            pending = _step_record(publication_id, step_index, step_template, "pending")
            await self._steps.upsert(pending)

        outcome = await self._step_executor.execute_step(
            package=package,
            envelope=envelope,
            binding=binding,
            resolutions=resolutions,
            human_authorization_id=record.authorization_id,
            human_approval_signature=record.approval_signature,
            # B2 (revision de codigo): un intento previo del MISMO paso ya
            # materializo esta `Proposal` -- nunca se vuelve a proponer.
            existing_proposal_id=existing_step.proposal_id if existing_step is not None else None,
        )
        await self._persist_step_outcome(
            publication_id,
            step_index,
            step_template,
            current_step_state,
            outcome,
            existing_proposal_id=existing_step.proposal_id if existing_step is not None else None,
        )
        registration_halt_reason = await self._register_created_entity(
            package, binding, step_template, outcome
        )
        if registration_halt_reason is not None:
            # T126/003-entidades-creadas: el paso YA quedo `done` de verdad
            # arriba (la escritura en la plataforma es real y no se
            # desdice) -- lo que se para AQUI es la saga, antes de que el
            # HIJO pueda proponerse contra un padre que `ad_entities`
            # todavia no conoce. `_halt` marca `partially_published` (hay
            # algo creado) en vez de `failed`, y una reanudacion vuelve a
            # intentar el registro sobre el MISMO paso.
            return await self._halt(
                record, package, now, registration_halt_reason, failed_step_index=step_index
            )
        activation_halt_reason = await self._refresh_activated_campaign(
            binding, step_template, outcome
        )
        if activation_halt_reason is not None:
            # Mismo criterio que `registration_halt_reason`: la activacion
            # SI ocurrio de verdad en la plataforma -- lo que para aqui es
            # la saga, antes de que `DetectPlatformDrift`/el siguiente paso
            # trabajen sobre una fila de `ad_entities` que aun no lo sabe.
            return await self._halt(
                record, package, now, activation_halt_reason, failed_step_index=step_index
            )
        return await self._apply_outcome(
            record=record,
            package=package,
            envelope=envelope,
            step_index=step_index,
            outcome=outcome,
            now=now,
        )

    async def _persist_step_outcome(
        self,
        publication_id: str,
        step_index: int,
        step_template: StepTemplate,
        current_state: str,
        outcome: StepExecutionOutcome,
        *,
        existing_proposal_id: str | None,
    ) -> None:
        """0042's `campaign_package_steps_guard` solo admite `done`/`unknown`
        viniendo de `running` -- nunca directo desde `pending`/`blocked`/
        `failed`. Aqui se abre ese salto con un upsert de transito por
        `running` ANTES de archivar el desenlace real (invariante 1 del
        trigger, "sin pasos anteriores sin terminar", ya la exige
        `RunPackagePublication` en orden por diseño).

        `failed -> blocked` no tiene salto legal en ese trigger (tampoco via
        `running`, que no admite `blocked`): se archiva como `failed` con el
        MISMO `outcome_code` -- `_apply_outcome` ya trata ambos igual (paran
        la publicacion por la misma razon), asi que no se pierde
        informacion de cara al dueño."""
        target_state = (
            "failed"
            if current_state == "failed" and outcome.state == "blocked"
            else outcome.state
        )
        # B2: el `proposal_id` una vez conocido nunca se pierde -- cada
        # `upsert` REEMPLAZA la fila entera (`SqlPackageStepRepository`
        # docstring), asi que un transito intermedio sin este campo la
        # borraria y la siguiente reanudacion volveria a proponer.
        proposal_id = existing_proposal_id or outcome.proposal_id
        if target_state in {"done", "unknown"} and current_state != "unknown":
            await self._steps.upsert(
                _step_record(
                    publication_id, step_index, step_template, "running", proposal_id=proposal_id
                )
            )
        await self._steps.upsert(
            PackageStepRecord(
                publication_id=publication_id,
                step_index=step_index,
                kind=step_template.step_kind.value.lower(),
                local_ref=step_template.local_ref,
                parent_local_ref=step_template.parent_local_ref,
                state=target_state,
                created_entity_ref=outcome.created_entity_ref,
                outcome_code=outcome.outcome_code,
                proposal_id=proposal_id,
                confirmed_state_hash=outcome.confirmed_state_hash,
            )
        )

    async def _register_created_entity(
        self,
        package: CampaignPackage,
        binding: PackageStepBinding,
        step_template: StepTemplate,
        outcome: StepExecutionOutcome,
    ) -> str | None:
        """T126/003-entidades-creadas: `proposals_entity_exists()` (0027)
        exige que `entity_ref` viva ya en `ad_entities`/`platform_accounts`
        antes de aceptar la `Proposal` del HIJO de este paso
        (`CREATE_AD_SET`/`CREATE_AD`/`ACTIVATE_CAMPAIGN`) -- se registra
        AQUI, antes de devolver el control, para que la proxima invocacion
        (que propone el paso siguiente) nunca encuentre un padre fantasma.

        Sin `confirmed_state_hash` no hay recibo real que registrar todavia
        (mismo criterio de tolerancia que `_resolve_parent`, T123/AL-4: un
        paso `done` sin el -- doble de prueba, o anterior a la columna 0049
        -- no falla aqui, lo hace mas adelante quien SI lo necesita)."""
        if step_template.step_kind not in _ENTITY_CREATING_STEP_KINDS:
            return None
        if (
            outcome.state != "done"
            or not outcome.created_entity_ref
            or not outcome.confirmed_state_hash
        ):
            return None
        parent_ref = (
            package.account_ref
            if step_template.step_kind is StepKind.CREATE_CAMPAIGN
            else binding.parent_entity_ref
        )
        # ChokepointStepExecutor ya exige un padre antes de escribir nada.
        if parent_ref is None:  # pragma: no cover
            return None
        try:
            await self._entity_registration.execute(
                RegisterCreatedEntityCommand(
                    business_id=package.business_id,
                    entity_ref=EntityRef.parse(outcome.created_entity_ref),
                    parent_ref=parent_ref,
                    name=f"{step_template.step_kind.value} ({step_template.local_ref})",
                    # `platform_completeness.campaign_wire_plan`/`ad_set_wire_plan`/
                    # `ad_wire_plan` firman `status="PAUSED"` para los tres pasos
                    # de `_ENTITY_CREATING_STEP_KINDS`: la plataforma nunca crea
                    # una entidad ya activa. `ACTIVATE_CAMPAIGN` la sube a ACTIVE
                    # despues, sobre esta MISMA fila (`_refresh_activated_campaign`).
                    status=AdEntityStatus.PAUSED,
                    platform_state_hash=outcome.confirmed_state_hash,
                )
            )
        except AdEntityParentNotFoundError:
            return _ENTITY_REGISTRATION_PARENT_MISSING
        return None

    async def _refresh_activated_campaign(
        self,
        binding: PackageStepBinding,
        step_template: StepTemplate,
        outcome: StepExecutionOutcome,
    ) -> str | None:
        """Item 2 (repaso 0.2.23): `ACTIVATE_CAMPAIGN` MODIFICA la campaña
        que su propio `CREATE_CAMPAIGN` ya registro PAUSED -- sin esto,
        `DetectPlatformDrift` la marca DRIFTED nada mas publicarse (compara
        contra el hash de creacion, nunca actualizado) y el siguiente ciclo
        firma un `expected_state_hash` obsoleto. `binding.parent_entity_ref`
        es el `entity_ref` de la propia campaña (el padre declarado de
        `ACTIVATE_CAMPAIGN` es su propio `CREATE_CAMPAIGN`, `derive_step_plan`).

        Mismo criterio de tolerancia que `_register_created_entity`: sin
        `confirmed_state_hash` no hay recibo real que aplicar todavia."""
        if step_template.step_kind is not StepKind.ACTIVATE_CAMPAIGN:
            return None
        if outcome.state != "done" or not outcome.confirmed_state_hash:
            return None
        campaign_ref = binding.parent_entity_ref
        if campaign_ref is None:  # pragma: no cover - el sobre exige padre para ACTIVATE_CAMPAIGN
            return None
        try:
            await self._entity_activation_refresh.execute(
                RefreshRegisteredEntityStateCommand(
                    entity_ref=campaign_ref,
                    confirmed_state_hash=outcome.confirmed_state_hash,
                )
            )
        except EntityNotFoundError:
            return _ENTITY_ACTIVATION_REFRESH_MISSING
        return None

    async def _structure_complete_before_activation(
        self,
        publication_id: str,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        step_template: StepTemplate,
    ) -> bool:
        """T043/BL-3 (003 AL-4 generalizado): `_resolve_parent` (mas abajo)
        solo reverifica el PADRE directo de `ACTIVATE_CAMPAIGN` -- la propia
        campaña -- nunca a sus hermanos (los `PlannedAd`/grupos de recursos
        que SI son "lo aprobado"). Sin esto, un recibo de un anuncio o de un
        grupo de recursos alterado directamente en la base (fuera de esta
        saga) nunca se vuelve a comprobar antes de encender el gasto. Se
        recuenta contra el sobre FIRMADO (`expected_done_steps`), nunca
        contra `package.ad_sets` en vivo (eso ya lo cubre R3/`package_hash`
        en `_pre_flight_halt_reason`)."""
        expected = step_template.expected_done_steps
        if expected is None or expected <= 0:
            return False
        ad_equivalent_refs = ad_equivalent_local_refs(package)
        done = 0
        for step in envelope.step_plan:
            if step.local_ref not in ad_equivalent_refs:
                continue
            record = await self._steps.get(publication_id, step.step_index)
            if record is not None and record.state == "done":
                done += 1
        return done == expected

    async def _pre_flight_halt_reason(
        self, package: CampaignPackage, envelope: PackageApprovalEnvelope, now: datetime
    ) -> str | None:
        # AL-6/T125 (revision de codigo): el broker ya deniega un paso con
        # el sobre caducado (`package_approval_expired`, R2 con SU propio
        # reloj) -- comprobarlo tambien aqui evita materializar una
        # `Proposal`/`Authorization` derivada entera solo para que la
        # deniegue mas tarde, y le da al halt el mismo codigo estable que
        # `resume` ya usa (`PackageApprovalExpiredError`).
        if now >= envelope.approval_expires_at:
            return "package_approval_expired"
        native = package.campaign.native
        if (
            isinstance(native, GoogleCampaignNative)
            and native.advertising_channel_type not in self._enabled_google_channels
        ):
            # T035 security re-check (CWE-284): `ApproveCampaignPackage`
            # already gates this at approval time, but `ADS_GOOGLE_CHANNELS_
            # ENABLED` can narrow between approval and a worker tick that
            # resumes a `partially_published`/`verifying` package -- PRE-
            # FLIGHT, before any step is attempted, so nothing new is ever
            # written for a channel this installation stopped allowing.
            return "package_channel_not_enabled"
        live_hash = compute_package_hash(
            package_tree_payload(
                business_id=package.business_id,
                platform=package.account_ref.platform,
                account_ref=package.account_ref,
                publish_as=package.publish_as,
                offering_id=package.offering_id,
                campaign=package.campaign,
                ad_sets=package.ad_sets,
                daily_budget=package.budget.daily,
                rationale=package.rationale,
                research=package.research,
            )
        )
        if live_hash.value != envelope.package_hash:
            return "package_changed_mid_publication"
        scope = BrakeScope(kind=BrakeScopeKind.PLATFORM_ACCOUNT, ref=str(envelope.account_ref))
        brake = await self._brakes.get_effective(scope)
        if brake is not None and brake.blocks(AuthorizationKind.PACKAGE_STEP):
            return "brake_engaged"
        return None

    async def _derive_admitted_binding(
        self, record: PackagePublicationRecord, envelope: PackageApprovalEnvelope, step_index: int
    ) -> PackageStepBinding | None:
        parent_confirmed, parent_entity_ref, parent_state_hash = await self._resolve_parent(
            record.publication_id, envelope, step_index
        )
        if not parent_confirmed:
            return None
        return derive_step_binding(
            envelope, record.envelope_hash, step_index, parent_entity_ref, parent_state_hash
        )

    async def _apply_outcome(
        self,
        *,
        record: PackagePublicationRecord,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        step_index: int,
        outcome: StepExecutionOutcome,
        now: datetime,
    ) -> RunPackagePublicationResult:
        if outcome.state == "done":
            return await self._advance(
                record=record,
                package=package,
                envelope=envelope,
                step_index=step_index,
                outcome=outcome,
                now=now,
            )
        if outcome.state == "unknown":
            return await self._stay_running(record, package, now)
        reason = outcome.outcome_code or "step_failed"
        return await self._halt(record, package, now, reason, failed_step_index=step_index)

    async def _stay_running(
        self, record: PackagePublicationRecord, package: CampaignPackage, now: datetime
    ) -> RunPackagePublicationResult:
        await self._publications.advance(
            record.publication_id,
            cursor=record.cursor,
            state="running",
            halt_reason=None,
            failed_step_index=None,
            finished_at=None,
        )
        if package.state is PackageState.PUBLISHING:
            package.record_publication_outcome(UncertainOutcome(), now)
            await self._packages.save(package)
        return RunPackagePublicationResult(
            publication_id=record.publication_id,
            publication_state="running",
            package_state=package.state.value,
            next_step_hint="Comprobando el desenlace del ultimo paso.",
        )

    # ------------------------------------------------------------------
    # Resolucion de padre / creatividad -- SOLO desde recibos confirmados
    # ------------------------------------------------------------------

    async def _resolve_parent(
        self, publication_id: str, envelope: PackageApprovalEnvelope, step_index: int
    ) -> tuple[bool, EntityRef | None, str | None]:
        """`(confirmado, entity_ref, confirmed_state_hash)`. `entity_ref is
        None` con `confirmado=True` es un paso raiz (sin padre, p. ej.
        `CREATE_CAMPAIGN`/`UPLOAD_CREATIVE`) -- distinto de "padre sin
        recibo todavia", que deniega en vez de devolver `None`.

        T123/AL-4: `confirmed_state_hash` puede volver `None` incluso con
        `confirmado=True` (recibos anteriores a la columna 0049, o un paso
        padre que -- por lo que sea -- nunca lo archivo). No se falla aqui:
        `ChokepointStepExecutor` es quien falla cerrado antes de proponer
        nada, con una razon limpia (`package_parent_state_unconfirmed`),
        igual que ya hace con `payload_template_hash`."""
        step = envelope.step_plan[step_index]
        if step.parent_local_ref is None:
            return True, None, None
        parent_index = parent_step_index_of(envelope, step_index)
        if parent_index is None:  # pragma: no cover - ya lo valido PackageApprovalEnvelope
            return False, None, None
        parent_record = await self._steps.get(publication_id, parent_index)
        if (
            parent_record is None
            or parent_record.state != "done"
            or not parent_record.created_entity_ref
        ):
            return False, None, None
        return (
            True,
            EntityRef.parse(parent_record.created_entity_ref),
            parent_record.confirmed_state_hash,
        )

    async def _resolve_creative_sources(
        self, publication_id: str, envelope: PackageApprovalEnvelope, step_template: StepTemplate
    ) -> dict[str, str] | None:
        resolutions: dict[str, str] = {}
        for creative_local_ref in step_template.depends_on:
            upload_index = _find_step_index_by_local_ref(envelope, creative_local_ref)
            if upload_index is None:  # pragma: no cover - garantizado por derive_step_plan
                return None
            upload_record = await self._steps.get(publication_id, upload_index)
            if (
                upload_record is None
                or upload_record.state != "done"
                or not upload_record.created_entity_ref
            ):
                return None
            resolutions[creative_hole(creative_local_ref)] = upload_record.created_entity_ref
        return resolutions

    # ------------------------------------------------------------------
    # Avance / parada
    # ------------------------------------------------------------------

    async def _advance(
        self,
        *,
        record: PackagePublicationRecord,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        step_index: int,
        outcome: StepExecutionOutcome,
        now: datetime,
    ) -> RunPackagePublicationResult:
        new_cursor = step_index + 1
        is_last = new_cursor >= len(envelope.step_plan)
        await self._publications.advance(
            record.publication_id,
            cursor=new_cursor,
            state="completed" if is_last else "running",
            halt_reason=None,
            failed_step_index=None,
            finished_at=now if is_last else None,
        )
        if not is_last:
            return RunPackagePublicationResult(
                publication_id=record.publication_id,
                publication_state="running",
                package_state=package.state.value,
                next_step_hint=_next_step_hint(envelope, new_cursor),
            )
        campaign_ref = await self._campaign_entity_ref(record.publication_id, envelope)
        package.record_publication_outcome(
            ActivatedOutcome(
                campaign_entity_ref=campaign_ref or "",
                activated_at=now,
                undo_deadline=outcome.undo_deadline or now,
            ),
            now,
        )
        await self._packages.save(package)
        return RunPackagePublicationResult(
            publication_id=record.publication_id,
            publication_state="completed",
            package_state=package.state.value,
            next_step_hint=None,
        )

    async def _campaign_entity_ref(
        self, publication_id: str, envelope: PackageApprovalEnvelope
    ) -> str | None:
        for step in envelope.step_plan:
            if step.step_kind is StepKind.CREATE_CAMPAIGN:
                record = await self._steps.get(publication_id, step.step_index)
                return record.created_entity_ref if record is not None else None
        return None  # pragma: no cover - todo plan firmado lleva CREATE_CAMPAIGN

    async def _halt(
        self,
        record: PackagePublicationRecord,
        package: CampaignPackage,
        now: datetime,
        reason: str,
        *,
        failed_step_index: int | None,
    ) -> RunPackagePublicationResult:
        await self._publications.advance(
            record.publication_id,
            cursor=record.cursor,
            state="halted",
            halt_reason=reason,
            failed_step_index=failed_step_index,
            finished_at=now,
        )
        created_count = await self._count_visible_done(record.publication_id, record.envelope)
        # B1 (revision de codigo): `CampaignPackage._TRANSITIONS[VERIFYING]`
        # solo admite {PUBLISHED, PARTIALLY_PUBLISHED} -- nunca FAILED. Es
        # deliberado, no un descuido: `VERIFYING` significa que un paso dio
        # `unknown` alguna vez (BL-4/INV-4, "nunca se reconcilia por
        # reescritura"), asi que el sistema ya no puede afirmar "no se creo
        # nada" con certeza. Tratar ese halt como `PARTIALLY_PUBLISHED` con
        # `created_count=0` (en vez de `NONE_CREATED`) es lo seguro: deja el
        # paquete en el camino de "Continuar" (relee recibos), nunca en el
        # de "vuelve a proponer" (que arriesgaria una segunda campaña si algo
        # SI llego a escribirse en la plataforma).
        came_from_verifying = package.state is PackageState.VERIFYING
        if created_count > 0 or came_from_verifying:
            # `PartialOutcome.failed_step_index` es el hint de cara al
            # dueño ("por que paso se detuvo"), no la columna persistida
            # de la publicacion (`None` arriba para un halt PRE-FLIGHT,
            # M6): cuando no hubo un paso concreto que fallara, el paso
            # siguiente pendiente (`record.cursor`) sigue siendo la
            # informacion util para "Continuar".
            package.record_publication_outcome(
                PartialOutcome(
                    created_count=created_count,
                    failed_step_index=(
                        failed_step_index if failed_step_index is not None else record.cursor
                    ),
                    next_step_hint=_halt_hint(reason),
                ),
                now,
            )
        else:
            package.record_publication_outcome(NoneCreatedOutcome(outcome_code=reason), now)
        await self._packages.save(package)
        return RunPackagePublicationResult(
            publication_id=record.publication_id,
            publication_state="halted",
            package_state=package.state.value,
            next_step_hint=_halt_hint(reason),
        )

    async def _count_visible_done(
        self, publication_id: str, envelope: PackageApprovalEnvelope
    ) -> int:
        count = 0
        for step in envelope.step_plan:
            if step.step_kind.value.lower() not in _VISIBLE_STEP_KINDS:
                continue
            record = await self._steps.get(publication_id, step.step_index)
            if record is not None and record.state == "done":
                count += 1
        return count


def _step_record(
    publication_id: str,
    step_index: int,
    step_template: StepTemplate,
    state: str,
    *,
    proposal_id: str | None = None,
) -> PackageStepRecord:
    return PackageStepRecord(
        publication_id=publication_id,
        step_index=step_index,
        kind=step_template.step_kind.value.lower(),
        local_ref=step_template.local_ref,
        parent_local_ref=step_template.parent_local_ref,
        state=state,
        proposal_id=proposal_id,
    )


def _find_step_index_by_local_ref(envelope: PackageApprovalEnvelope, local_ref: str) -> int | None:
    for step in envelope.step_plan:
        if step.local_ref == local_ref:
            return step.step_index
    return None


def _next_step_hint(envelope: PackageApprovalEnvelope, cursor: int) -> str:
    if cursor >= len(envelope.step_plan):
        return "Activando la campaña."
    return f"Siguiente: {envelope.step_plan[cursor].step_kind.value.lower()}."


def _halt_hint(reason: str) -> str:
    hints = {
        "package_changed_mid_publication": "El paquete cambio a mitad de publicacion.",
        "brake_engaged": "Los cambios estan parados.",
        "package_parent_unconfirmed": "Esperando confirmacion del paso anterior.",
        "package_approval_expired": "La aprobacion caduco. Vuelve a aprobar para continuar.",
        "package_channel_not_enabled": (
            "El canal de esta campaña ya no esta habilitado en esta instalacion."
        ),
        "structure_incomplete_before_activation": (
            "Falta estructura por confirmar antes de activar. Reintenta."
        ),
        _ENTITY_REGISTRATION_PARENT_MISSING: "No se pudo registrar la entidad creada. Reintenta.",
        _ENTITY_ACTIVATION_REFRESH_MISSING: "No se pudo confirmar la activacion. Reintenta.",
    }
    return hints.get(reason, "No se pudo continuar. Revisa el detalle del paquete.")
