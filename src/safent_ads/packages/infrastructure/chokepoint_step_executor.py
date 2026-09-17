"""`ChokepointStepExecutor`: implementa `PackageStepExecutorPort` (T024).

Un paso de tipo `UPLOAD_CREATIVE` sube bytes directamente por
`AdsPlatformPort.upload_asset` (BL-6): no hay diff que aplicar sobre una
entidad existente, asi que no hay `Proposal` que materializar. Los cuatro
tipos restantes (`CREATE_CAMPAIGN`/`CREATE_AD_SET`/`CREATE_AD`/
`ACTIVATE_CAMPAIGN`) siguen el MISMO camino que cualquier propuesta ya
aprobada (`ApplyDefensiveAction._propose_authorize_and_run`, plan.md §6):
materializar una `Proposal` (`ProposeAction`, nace `pending`), aprobarla y
programarla con gracia cero, firmar la `Authorization` derivada
`package_step` -- encadenada a la humana via `derived_from_authorization_id`
y acompañada del `PackageApprovalProof` integro -- encolar el
`ExecutionAttempt` con la clave de idempotencia PROPIA del paso
(`build_package_step_idempotency_key`, estable entre reintentos y
reanudaciones) y ejecutar por el MISMO `ExecutionChokepoint` que ya
evalua freno, guardarrailes y verifica la firma.

Nota de alcance de esta entrega (documentada, no oculta): el bróker
(`WriteAuthorizationPipeline`) todavia no verifica de forma independiente
el sobre humano embebido (`SignedPackageApproval`/`WriteIntent.
package_binding`, contracts/api.md §R2.E) -- eso es T107/T108, fuera de
esta rama. Hoy sigue denegando `package_step` con `OWNER_APPROVAL_REQUIRED`
(fail-closed, seguro); las comprobaciones de este fichero (envoltura
firmada, encadenado a la humana, huella del sobre reproducida) son la
parte que SI corre en `ads-api`/`ads-worker`."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.ports import AdsPlatformPort, AssetUploadRequest
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.execution.application.chokepoint import ExecutionChokepoint
from safent_ads.execution.application.ports import ExecutionQueuePort, GuardrailSetRepository
from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    ExecutionStatus,
)
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailVerdict,
    ScopeKind,
    SpendLedger,
)
from safent_ads.packages.application.ports import CreativeAssetBytesPort, StepExecutionOutcome
from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    StepKind,
    asset_group_images,
    compute_payload_template_hash,
    project_step_template,
    substitute_holes,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.step_binding import PackageStepBinding
from safent_ads.packages.domain.values import ImageCreativeRef
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    PackageApprovalProof,
    SignerPort,
    sign_authorization,
)
from safent_ads.proposals.domain.campaign_creation import (
    CampaignCreationError,
    creation_budget,
)
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.ids import EntityLevel, EntityRef

__all__ = ["ChokepointStepExecutor", "CreativeChecksumMismatchError", "PackageStepProjectionError"]

_PAUSED_VALUE = "PAUSED"
_ACTIVE_VALUE = "ACTIVE"
_STATUS_PARAMETER = "status"
_PARAMETER_PREFIX = {
    StepKind.CREATE_CAMPAIGN: "new_campaign:",
    StepKind.CREATE_AD_SET: "new_ad_set:",
    StepKind.CREATE_AD: "new_ad:",
}
_WIRE_WRAPPER_KEY = {
    StepKind.CREATE_CAMPAIGN: "creation_plan",
    StepKind.CREATE_AD_SET: "child_plan",
    StepKind.CREATE_AD: "child_plan",
}
_PROPOSAL_KIND = {
    StepKind.CREATE_CAMPAIGN: ProposalKind.CREATE_CAMPAIGN,
    StepKind.CREATE_AD_SET: ProposalKind.CREATE_AD_SET,
    StepKind.CREATE_AD: ProposalKind.CREATE_AD,
    StepKind.ACTIVATE_CAMPAIGN: ProposalKind.RESUME,
}
_ENTITY_LEVEL = {
    StepKind.CREATE_CAMPAIGN: EntityLevel.CAMPAIGN,
    StepKind.CREATE_AD_SET: EntityLevel.AD_SET,
    StepKind.CREATE_AD: EntityLevel.AD,
}
_PARAMETER_HASH_LENGTH = 16
# T123/AL-4 (data-model.md R2.8, generalizado de la activacion a todo paso
# con padre): estos tres tipos escriben SOBRE un padre que la MISMA saga
# acaba de crear -- su `expected_state_hash` nunca puede venir de
# `ad_entities` (el padre no vive alli todavia), solo del `confirmed_state_
# hash` que el chokepoint archivo al confirmar la escritura del padre.
# `CREATE_CAMPAIGN` queda fuera a proposito: no tiene padre, y
# `matches_signed_transition`/`_verify` (broker) exigen que `new_campaign:`
# viaje SIN `expected_state_hash` (no hay campaña remota previa que leer).
_STEPS_REQUIRING_PARENT_STATE_HASH = frozenset(
    {StepKind.CREATE_AD_SET, StepKind.CREATE_AD, StepKind.ACTIVATE_CAMPAIGN}
)
_PARENT_STATE_UNCONFIRMED = "package_parent_state_unconfirmed"


class CreativeChecksumMismatchError(InfrastructureError):
    """BL-6: los bytes recuperados del activo no coinciden con el
    `checksum` firmado en la plantilla del paso -- se deniega antes de
    llamar a ninguna plataforma."""


class PackageStepProjectionError(InfrastructureError):
    """Invariante interna de `_to_write_step`: `derive_step_binding`/
    `project_step_template` garantizan la forma de `binding`/`resolved_
    template` para cada `step_kind` -- si esta clase se dispara hay un bug
    real aguas arriba y debe fallar alto (nunca `assert`, que desaparece
    con `python -O`, ni en silencio)."""


@dataclass(frozen=True, slots=True)
class _WriteStep:
    parameter: str
    before: object
    after: object
    entity_ref: EntityRef


class ChokepointStepExecutor:
    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        authorizations: AuthorizationRepository,
        propose_action: ProposeAction,
        execution_queue: ExecutionQueuePort,
        chokepoint: ExecutionChokepoint,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        platform: AdsPlatformPort,
        creative_bytes: CreativeAssetBytesPort,
        signer: SignerPort,
        clock: Clock,
    ) -> None:
        self._proposals = proposals
        self._authorizations = authorizations
        self._propose_action = propose_action
        self._execution_queue = execution_queue
        self._chokepoint = chokepoint
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._platform = platform
        self._creative_bytes = creative_bytes
        self._signer = signer
        self._clock = clock

    async def execute_step(
        self,
        *,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        binding: PackageStepBinding,
        resolutions: dict[str, str],
        human_authorization_id: str,
        human_approval_signature: bytes,
        existing_proposal_id: str | None = None,
    ) -> StepExecutionOutcome:
        if binding.step_kind is StepKind.UPLOAD_CREATIVE:
            return await self._execute_upload_creative(
                package, envelope, binding, human_authorization_id, human_approval_signature
            )
        if existing_proposal_id is not None:
            return await self._reconcile_write_step(
                ProposalId.parse(existing_proposal_id), binding.step_kind, package
            )
        return await self._execute_write_step(
            package=package,
            envelope=envelope,
            binding=binding,
            resolutions=resolutions,
            human_authorization_id=human_authorization_id,
            human_approval_signature=human_approval_signature,
        )

    async def _reconcile_write_step(
        self, proposal_id: ProposalId, step_kind: StepKind, package: CampaignPackage
    ) -> StepExecutionOutcome:
        """B2 (revision de codigo): un intento previo de este paso YA
        materializo esta `Proposal` -- reconciliar NUNCA vuelve a proponer
        ni a firmar una `Authorization` nueva, solo pide al chokepoint que
        reevalue el intento reservado bajo la clave de idempotencia estable
        del paso. Evita `DuplicateExecutionError` (`executions_idempotency_
        key_unique`) y respeta BL-4/INV-4: un paso `unknown` se reconcilia
        por lectura, nunca por una segunda escritura."""
        await self._chokepoint.run_once(proposal_id=proposal_id)
        # AL-3: el desenlace SIEMPRE se relee de la base, nunca del valor de
        # retorno de `run_once`.
        attempt = await self._execution_queue.get_for_proposal(proposal_id)
        if attempt is None:  # pragma: no cover - el chokepoint acaba de resolverlo
            return StepExecutionOutcome(state="unknown", created_entity_ref=None, outcome_code=None)
        return _to_step_outcome(attempt, step_kind, package, proposal_id=str(proposal_id))

    # ------------------------------------------------------------------
    # UPLOAD_CREATIVE -- fuera del chokepoint generico (BL-6)
    # ------------------------------------------------------------------

    async def _execute_upload_creative(
        self,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        binding: PackageStepBinding,
        human_authorization_id: str,
        human_approval_signature: bytes,
    ) -> StepExecutionOutcome:
        template = project_step_template(package, StepKind.UPLOAD_CREATIVE, binding.local_ref)
        checksum = str(template["checksum"])
        mime_type = str(template["mime_type"])
        # `_upload_creative_template` fija estas dos claves como `int`
        # (`ImageCreativeRef.width`/`height`) -- el `cast` documenta esa
        # garantia de frontera, `project_step_template` solo tipa `object`.
        width = cast(int, template["width"])
        height = cast(int, template["height"])
        asset_id = _find_asset_id_by_checksum(package, checksum)
        if asset_id is None:
            return StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="creative_not_found"
            )
        media = await self._creative_bytes.get_bytes(
            business_id=package.business_id, asset_id=asset_id
        )
        if media is None:
            return StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="creative_not_usable"
            )
        if hashlib.sha256(media).hexdigest() != checksum:
            return StepExecutionOutcome(
                state="failed",
                created_entity_ref=None,
                outcome_code="creative_checksum_mismatch",
            )
        # H1 (revision de seguridad 0.2.23): el bróker ya no confia en que
        # este paso llegue fuera de la tuberia de escritura -- viaja con el
        # MISMO `package_binding`/`package_approval` que `execute_write`
        # (`admit_package_step`), para que `WriteAuthorizationPipeline.
        # admit_upload` pueda aplicar R1-R6 y anotar el manejador en su
        # propio libro antes de subir nada a Meta.
        package_approval = PackageApprovalProof(
            envelope=envelope.to_canonical(),
            authorization_id=human_authorization_id,
            issued_by=envelope.approved_by,
            expires_at=envelope.approval_expires_at,
            signature=human_approval_signature,
        ).as_claims()
        try:
            handle = await self._platform.upload_asset(
                AssetUploadRequest(
                    account_ref=AccountRef.parse(str(package.account_ref)),
                    file_name=asset_id,
                    mime_type=mime_type,
                    media=media,
                    width=width,
                    height=height,
                    package_binding=binding.to_canonical(),
                    package_approval=package_approval,
                )
            )
        except BrokerRequestDeniedError as exc:
            # H1 (revision de codigo): el bróker rechazo la subida de
            # forma PERMANENTE (capacidad no soportada, credencial ausente,
            # payload invalido) -- tratarlo como `unknown` reintentaria para
            # siempre el mismo rechazo en cada ciclo de 5 s. `failed` para la
            # saga entera (no se activa nada), igual que cualquier otro paso.
            return StepExecutionOutcome(
                state="failed",
                created_entity_ref=None,
                outcome_code=f"upload_denied:{exc.error_code}",
            )
        except Exception:  # noqa: BLE001 - fallo de red/proveedor: la subida pudo aplicarse
            return StepExecutionOutcome(
                state="unknown", created_entity_ref=None, outcome_code="upload_outcome_unknown"
            )
        # T112/R2.7: `{creative_of:X}` resuelve a `link_data.image_hash`
        # (Meta) -- el manejador OPACO que el proveedor confirma, nunca la
        # URL de previsualizacion. `handle.preview_url` (si el proveedor la
        # da) es solo informativo -- publicar nunca depende de ella, y el
        # bróker (`meta_ads_adapter.py::_allowed_creative_preview_url`) ya
        # es quien decide si es segura de enseñar en una vista previa.
        return StepExecutionOutcome(
            state="done", created_entity_ref=handle.platform_asset_id, outcome_code=None
        )

    # ------------------------------------------------------------------
    # CREATE_CAMPAIGN / CREATE_AD_SET / CREATE_AD / ACTIVATE_CAMPAIGN
    # ------------------------------------------------------------------

    async def _execute_write_step(
        self,
        *,
        package: CampaignPackage,
        envelope: PackageApprovalEnvelope,
        binding: PackageStepBinding,
        resolutions: dict[str, str],
        human_authorization_id: str,
        human_approval_signature: bytes,
    ) -> StepExecutionOutcome:
        step_kind = binding.step_kind
        template = project_step_template(package, step_kind, binding.local_ref)
        if compute_payload_template_hash(template) != binding.payload_template_hash:
            # R7 en el lado ads-api: el binding se deriva del sobre firmado
            # (step_binding.py); esto solo se dispara si `package` cambio
            # de forma incompatible con `binding.payload_template_hash`
            # entre que `RunPackagePublication` valido R3 (huella del
            # arbol) y esta llamada -- no deberia ocurrir nunca en el mismo
            # ciclo de ejecucion, pero se comprueba explicito, fail-closed.
            return StepExecutionOutcome(
                state="failed",
                created_entity_ref=None,
                outcome_code="package_payload_not_reproducible",
            )
        expected_state_hash = binding.parent_receipt_state_hash
        if step_kind in _STEPS_REQUIRING_PARENT_STATE_HASH and not expected_state_hash:
            # T123/AL-4: sin el `confirmed_state_hash` del padre no hay
            # precondicion que firmar -- ni `ad_child_creation.
            # validate_child_diff` (CREATE_AD_SET/CREATE_AD) ni
            # `check_state_drift` (activacion) pueden verificar nada a
            # ciegas. Falla ANTES de proponer nada: nunca se materializa una
            # `Proposal` sobre una precondicion inventada.
            return StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code=_PARENT_STATE_UNCONFIRMED
            )
        resolved = substitute_holes(template, resolutions)
        write = _to_write_step(step_kind, resolved, package, binding)

        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(write.entity_ref))
        verdict = await self._evaluate_guardrails(scope, write, step_kind)
        if not verdict.allowed:
            return StepExecutionOutcome(
                state="blocked", created_entity_ref=None, outcome_code=",".join(verdict.reasons)
            )

        now = self._clock.now()
        propose_result = await self._propose_action.execute(
            ProposeActionCommand(
                business_id=package.business_id,
                diff=_build_diff(write),
                kind=_PROPOSAL_KIND[step_kind],
                cause=Cause(text="Paso de publicacion del paquete de campaña."),
                cause_type=f"package_step:{step_kind.value.lower()}",
                evidence=(),
                estimated_impact=_estimated_impact(write),
                urgency=Urgency.RECOMMENDED,
                expected_state_hash=expected_state_hash,
            )
        )
        proposal = await self._proposals.get(propose_result.proposal_id)
        if proposal is None:  # pragma: no cover - se acaba de guardar en la misma sesion
            return StepExecutionOutcome(
                state="failed", created_entity_ref=None, outcome_code="proposal_not_found"
            )

        # Revision de codigo (T123, descubierto por el journey de Postgres):
        # el trigger de `proposals` (0008) valida transiciones de UN salto
        # contra lo YA persistido -- aprobar y programar en el MISMO objeto
        # en memoria y guardar UNA vez intenta escribir `pending ->
        # scheduled` directamente, que el trigger rechaza. `ApplyDefensive
        # Action._propose_authorize_and_run` evita esto porque
        # `AuthorizeRuleAction` persiste el salto a `approved` en su propia
        # transaccion; aqui no hay un caso de uso intermedio, asi que se
        # guarda cada salto por separado.
        proposal.approve(proposal.diff.diff_hash, now)
        await self._proposals.save(proposal)
        proposal.schedule_execution(0, now)
        await self._proposals.save(proposal)

        derived_from = AuthorizationId.parse(human_authorization_id)
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            proposal_id=proposal.proposal_id,
            kind=AuthorizationKind.PACKAGE_STEP,
            diff_hash=proposal.diff.diff_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by="ads-worker",
            channel=AuthorizationChannel.RULE_ENGINE,
            decided_at=now,
            expires_at=envelope.approval_expires_at,
            signer=self._signer,
            derived_from_authorization_id=derived_from,
            package_approval=PackageApprovalProof(
                envelope=envelope.to_canonical(),
                authorization_id=human_authorization_id,
                issued_by=envelope.approved_by,
                expires_at=envelope.approval_expires_at,
                signature=human_approval_signature,
            ),
            package_binding=binding.to_canonical(),
        )
        await self._authorizations.save(authorization)

        await self._execution_queue.save(
            ExecutionAttempt.claim_for_package_step(
                business_id=package.business_id,
                proposal_id=proposal.proposal_id,
                authorization_id=authorization.authorization_id,
                publication_id=binding.publication_id,
                step_index=binding.step_index,
            )
        )
        await self._chokepoint.run_once(proposal_id=proposal.proposal_id)
        # AL-3: el desenlace SIEMPRE se relee de la base, nunca del valor de
        # retorno de `run_once` (que aqui ni se guarda).
        attempt = await self._execution_queue.get_for_proposal(proposal.proposal_id)
        proposal_id = str(proposal.proposal_id)
        if attempt is None:  # pragma: no cover - el chokepoint acaba de resolverlo
            return StepExecutionOutcome(
                state="unknown", created_entity_ref=None, outcome_code=None, proposal_id=proposal_id
            )
        return _to_step_outcome(attempt, step_kind, package, proposal_id=proposal_id)

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, write: _WriteStep, step_kind: StepKind
    ) -> GuardrailVerdict:
        is_creation = step_kind is StepKind.CREATE_CAMPAIGN
        before, after = _money_pair(write, is_creation)
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, write.entity_ref)
        change = GuardrailChange(
            scope=scope,
            entity_ref=write.entity_ref,
            authorization_kind=AuthorizationKind.PACKAGE_STEP,
            before=before,
            after=after,
            is_creation=is_creation,
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)


def _find_asset_id_by_checksum(package: CampaignPackage, checksum: str) -> str | None:
    """T044/T046 (BL-4/D-3): tambien busca en las imagenes del grupo de
    recursos (`asset_group_images`, T024/T044) -- sin esto, el paso
    `UPLOAD_CREATIVE` de Maximo Rendimiento nunca encuentra su `asset_id`
    y falla con `creative_not_found`, aunque el activo si sea del negocio y
    este `READY`/`PASS`."""
    for ad_set in package.ad_sets:
        for ad in ad_set.ads:
            if isinstance(ad.creative, ImageCreativeRef) and ad.creative.checksum == checksum:
                return str(ad.creative.asset_id)
        for image in asset_group_images(ad_set):
            if image.checksum == checksum:
                return str(image.asset_id)
    return None


def _to_write_step(
    step_kind: StepKind,
    resolved_template: object,
    package: CampaignPackage,
    binding: PackageStepBinding,
) -> _WriteStep:
    if step_kind is StepKind.ACTIVATE_CAMPAIGN:
        if binding.parent_entity_ref is None:
            raise PackageStepProjectionError("activate_campaign_sin_padre")
        return _WriteStep(
            parameter=_STATUS_PARAMETER,
            before=_PAUSED_VALUE,
            after=_ACTIVE_VALUE,
            entity_ref=binding.parent_entity_ref,
        )
    if not isinstance(resolved_template, dict):
        raise PackageStepProjectionError("plantilla_resuelta_no_es_un_diccionario")
    prefix = _PARAMETER_PREFIX[step_kind]
    parameter = f"{prefix}{binding.payload_template_hash[:_PARAMETER_HASH_LENGTH]}"
    after = {_WIRE_WRAPPER_KEY[step_kind]: resolved_template}
    entity_ref = (
        package.account_ref if step_kind is StepKind.CREATE_CAMPAIGN else binding.parent_entity_ref
    )
    if entity_ref is None:
        raise PackageStepProjectionError("create_ad_set_o_create_ad_sin_padre")
    return _WriteStep(parameter=parameter, before=None, after=after, entity_ref=entity_ref)


def _build_diff(write: _WriteStep) -> ProposedDiff:
    return ProposedDiff.build(write.entity_ref, write.parameter, write.before, write.after)


def _money_pair(write: _WriteStep, is_creation: bool) -> tuple[Money, Money]:
    """Un solo lector del presupuesto (005 T016): `creation_budget` valida la
    forma completa del plan; aqui solo se estima el impacto, y un plan
    ilegible vale cero porque el broker ya lo deniega (BL-5)."""
    if not is_creation:
        return Money.zero(), Money.zero()
    try:
        after = creation_budget(write.after)
    except CampaignCreationError:
        return Money.zero(), Money.zero()
    return Money.zero(after.currency), after


def _estimated_impact(write: _WriteStep) -> Money:
    _, after = _money_pair(write, is_creation=write.parameter.startswith("new_campaign:"))
    return after if after.is_positive() else Money.zero()


def _to_step_outcome(
    attempt: ExecutionAttempt,
    step_kind: StepKind,
    package: CampaignPackage,
    *,
    proposal_id: str | None = None,
) -> StepExecutionOutcome:
    if attempt.status is ExecutionStatus.EXECUTED:
        created_ref = _created_entity_ref(step_kind, package, attempt.created_external_id)
        return StepExecutionOutcome(
            state="done",
            created_entity_ref=created_ref,
            outcome_code=None,
            undo_deadline=attempt.undo_deadline,
            proposal_id=proposal_id,
            # T123/AL-4: el recibo confirmado de ESTE paso, para que su
            # HIJO (si lo tiene) firme su propio `expected_state_hash`.
            confirmed_state_hash=attempt.platform_state_hash_after,
        )
    if attempt.status is ExecutionStatus.UNKNOWN:
        return StepExecutionOutcome(
            state="unknown",
            created_entity_ref=None,
            outcome_code=attempt.error_code,
            proposal_id=proposal_id,
        )
    if attempt.status is ExecutionStatus.BLOCKED_BRAKE:
        return StepExecutionOutcome(
            state="blocked",
            created_entity_ref=None,
            outcome_code="brake_engaged",
            proposal_id=proposal_id,
        )
    if attempt.status is ExecutionStatus.BLOCKED_GUARDRAIL:
        return StepExecutionOutcome(
            state="blocked",
            created_entity_ref=None,
            outcome_code=attempt.error_code or "guardrail_blocked",
            proposal_id=proposal_id,
        )
    return StepExecutionOutcome(
        state="failed",
        created_entity_ref=None,
        outcome_code=attempt.error_code or "step_failed",
        proposal_id=proposal_id,
    )


def _created_entity_ref(
    step_kind: StepKind, package: CampaignPackage, external_id: str | None
) -> str | None:
    if step_kind is StepKind.ACTIVATE_CAMPAIGN or external_id is None:
        return None
    scoped = EntityRef(
        platform=package.account_ref.platform,
        level=_ENTITY_LEVEL[step_kind],
        external_id=external_id,
        business_id=package.business_id.value,
        connection_id=package.account_ref.connection_id,
    )
    return str(scoped)
