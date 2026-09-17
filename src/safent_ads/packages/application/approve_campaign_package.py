"""`ApproveCampaignPackage` (tasks.md T022; data-model.md "Revision 2" §R2.2/
§R2.5): verifica la huella viva, el freno y los guardarrailes con el MISMO
`GuardrailEvaluator` que `SubmitApproval`, construye y firma el sobre
(`PackageApprovalEnvelope`) y **una** `Authorization` `human_approval` de
sujeto `package` -- nunca `proposal_id`. Crea la `PackagePublication` en
estado `pending`, cursor 0, ANTES de devolver el resultado: el
`publication_id` viaja dentro del propio sobre que se firma.

Ninguna escritura de plataforma ocurre aqui (contracts/api.md §4): eso es
`RunPackagePublication` (T023), deliberadamente fuera de esta entrega."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ulid import ULID

from safent_ads.execution.application.ports import BrakeStatePort, GuardrailSetRepository
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailVerdict,
    ScopeKind,
    SpendLedger,
    brake_scope_from,
)
from safent_ads.packages.application.errors import (
    ChannelTypeNotEnabledError,
    PackageBrakeEngagedError,
    PackageChangedError,
    PackageExpiredError,
    PackageGuardrailBlockedError,
    PackageNotFoundError,
    PackageNotProposedError,
)
from safent_ads.packages.application.ports import (
    CampaignPackageRepository,
    PackageAuthorizationRepository,
    PackagePublicationRecord,
    PackagePublicationRepository,
)
from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    build_approval_envelope,
    compute_envelope_hash,
    package_approval_signing_payload,
)
from safent_ads.packages.domain.campaign_package import CampaignPackage, PackageState
from safent_ads.packages.domain.errors import (
    CampaignPackageInvariantError,
    PackageHashMismatchError,
)
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.packages.domain.package_hash import PackageHash, package_tree_payload
from safent_ads.packages.domain.planned_tree import GoogleCampaignNative
from safent_ads.packages.domain.publication_policy import PACKAGE_UNDO_GRACE_SECONDS
from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationKind,
    AuthorizationSubject,
    SignerPort,
    SubjectKind,
    sign_authorization,
)
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = [
    "ApproveCampaignPackage",
    "ApproveCampaignPackageCommand",
    "ApproveCampaignPackageResult",
]

# data-model.md Revision 2 §R2.C (AL-6): 30 minutos, constante propia -- NO
# `_AUTHORIZATION_TTL` de `submit_approval.py` (15 min no basta para una
# saga con reanudacion).
PACKAGE_APPROVAL_TTL = timedelta(minutes=30)


@dataclass(frozen=True, kw_only=True, slots=True)
class ApproveCampaignPackageCommand:
    business_id: BusinessId
    package_id: PackageId
    package_hash: str
    approved_by: str
    comment: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class ApproveCampaignPackageResult:
    publication_id: str
    authorization_id: str
    grace_seconds: int
    approval_expires_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class _SignedApproval:
    """Paquete interno de lo que `_sign_approval` produce -- evita repetir
    `compute_envelope_hash(envelope)` en `_persist` (ya se calculo una vez
    para firmar la `Authorization`)."""

    envelope: PackageApprovalEnvelope
    envelope_hash: str
    authorization: Authorization
    envelope_signature: bytes


class ApproveCampaignPackage:
    def __init__(
        self,
        *,
        packages: CampaignPackageRepository,
        publications: PackagePublicationRepository,
        authorizations: PackageAuthorizationRepository,
        brakes: BrakeStatePort,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        signer: SignerPort,
        clock: Clock,
        enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
            {GoogleAdvertisingChannelType.SEARCH}
        ),
    ) -> None:
        self._packages = packages
        self._publications = publications
        self._authorizations = authorizations
        self._brakes = brakes
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._signer = signer
        self._clock = clock
        self._enabled_google_channels = enabled_google_channels

    async def execute(
        self, command: ApproveCampaignPackageCommand
    ) -> ApproveCampaignPackageResult:
        package = await self._packages.get(command.package_id, business_id=command.business_id)
        if package is None:
            raise PackageNotFoundError(str(command.package_id))
        # T035 security re-check (CWE-284): the package could have been
        # PROPOSED while its channel was still enabled -- this installation's
        # `ADS_GOOGLE_CHANNELS_ENABLED` can have narrowed since. Fail-closed,
        # before any signature/guardrail work, same code
        # `ProposeCampaignPackage` already uses.
        self._require_enabled_channel(package)

        replay = await self._replay_if_already_approved(package, command.package_hash)
        if replay is not None:
            return replay
        if package.state is not PackageState.PROPOSED:
            raise PackageNotProposedError(package.state.value)

        now = self._clock.now()
        self._require_proposal_still_live(package, command.package_hash, now)
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(package.account_ref))
        await self._require_brake_not_engaged(scope)
        verdict = await self._evaluate_guardrails(scope, package)
        if not verdict.allowed:
            raise PackageGuardrailBlockedError(reasons=verdict.reasons)

        signed = await self._sign_approval(package, command, verdict, now)
        self._approve(package, command.package_hash, signed.authorization, now)
        await self._persist(package, signed, now)
        return ApproveCampaignPackageResult(
            publication_id=signed.envelope.publication_id,
            authorization_id=str(signed.authorization.authorization_id),
            grace_seconds=PACKAGE_UNDO_GRACE_SECONDS,
            approval_expires_at=signed.envelope.approval_expires_at,
        )

    async def _replay_if_already_approved(
        self, package: CampaignPackage, package_hash: str
    ) -> ApproveCampaignPackageResult | None:
        """ME-6 (revision de codigo): idempotente por `(package_id,
        package_hash)` -- un doble clic en «Aprobar y publicar» (o un
        reintento de red) nunca crea una segunda `PackagePublication`; la
        segunda llamada devuelve exactamente el mismo resultado que la
        primera.

        M4 (repaso de seguridad 0.2.23): solo aplica cuando el paquete
        sigue en `APPROVED` con esta MISMA huella -- un paquete que YA
        avanzo mas alla (`PUBLISHING`/`PUBLISHED`/`PARTIALLY_PUBLISHED`/
        `VERIFYING`) o que se cancelo/invalido (`INVALIDATED`) no es un
        reintento idempotente del MISMO `approve`, es un estado distinto
        que `approve` nunca debe reafirmar con un 200 silencioso; cae al
        `PackageNotProposedError` (409) de mas abajo, igual que `REJECTED`
        (que nunca llega aqui con una publicacion que repetir: `reject()`
        solo admite `DRAFT`/`PROPOSED`)."""
        if package.state is not PackageState.APPROVED or package.package_hash.value != package_hash:
            return None
        record = await self._publications.get_by_package_id(package.package_id)
        if record is None:
            return None
        return ApproveCampaignPackageResult(
            publication_id=record.publication_id,
            authorization_id=record.authorization_id,
            grace_seconds=PACKAGE_UNDO_GRACE_SECONDS,
            approval_expires_at=record.approval_expires_at,
        )

    def _require_enabled_channel(self, package: CampaignPackage) -> None:
        native = package.campaign.native
        if (
            isinstance(native, GoogleCampaignNative)
            and native.advertising_channel_type not in self._enabled_google_channels
        ):
            raise ChannelTypeNotEnabledError(channel=native.advertising_channel_type.value)

    def _require_proposal_still_live(
        self, package: CampaignPackage, package_hash: str, now: datetime
    ) -> None:
        if now >= package.expires_at:
            raise PackageExpiredError(str(package.package_id))
        if package_hash != package.package_hash.value:
            raise PackageChangedError(package.package_hash.value)

    async def _sign_approval(
        self,
        package: CampaignPackage,
        command: ApproveCampaignPackageCommand,
        verdict: GuardrailVerdict,
        now: datetime,
    ) -> _SignedApproval:
        publication_id = str(ULID())
        approval_expires_at = now + PACKAGE_APPROVAL_TTL
        envelope = build_approval_envelope(
            package,
            publication_id=publication_id,
            approved_by=command.approved_by,
            approved_at=now,
            approval_expires_at=approval_expires_at,
        )
        envelope_hash = compute_envelope_hash(envelope)
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            subject=AuthorizationSubject(kind=SubjectKind.PACKAGE, id=str(package.package_id)),
            kind=AuthorizationKind.HUMAN_APPROVAL,
            diff_hash=envelope_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by=command.approved_by,
            channel=AuthorizationChannel.PANEL,
            decided_at=now,
            expires_at=approval_expires_at,
            signer=self._signer,
            comment=command.comment,
        )
        await self._authorizations.save(authorization)
        # `SignedPackageApproval.signature` (contracts/api.md §R2.E): firma
        # DEDICADA sobre el sobre, no la firma de `authorization` -- esa
        # cubre ademas `guardrail_verdict_hash`, un dato que
        # `PackageApprovalProof` (lo que viaja al bróker) no transporta.
        # Misma clave, payload propio y autosuficiente (`package_approval.py`
        # docstring de `package_approval_signing_payload`).
        envelope_signature = self._signer.sign(
            canonical_json_bytes(package_approval_signing_payload(envelope))
        )
        return _SignedApproval(
            envelope=envelope,
            envelope_hash=envelope_hash,
            authorization=authorization,
            envelope_signature=envelope_signature,
        )

    def _approve(
        self,
        package: CampaignPackage,
        package_hash: str,
        authorization: Authorization,
        now: datetime,
    ) -> None:
        try:
            package.approve(
                PackageHash(package_hash),
                str(authorization.authorization_id),
                PACKAGE_UNDO_GRACE_SECONDS,
                now,
            )
        except (CampaignPackageInvariantError, PackageHashMismatchError) as exc:  # pragma: no cover
            raise PackageChangedError(str(exc)) from exc

    async def _persist(
        self, package: CampaignPackage, signed: _SignedApproval, now: datetime
    ) -> None:
        await self._packages.save(package)
        await self._publications.add(
            PackagePublicationRecord(
                publication_id=signed.envelope.publication_id,
                package_id=package.package_id,
                authorization_id=str(signed.authorization.authorization_id),
                state="pending",
                cursor=0,
                envelope=signed.envelope,
                envelope_hash=signed.envelope_hash,
                approval_signature=signed.envelope_signature,
                approval_expires_at=signed.envelope.approval_expires_at,
                approved_plan=_approved_plan(package),
                started_at=now,
            )
        )

    async def _require_brake_not_engaged(self, scope: GuardrailScope) -> None:
        brake = await self._brakes.get_effective(brake_scope_from(scope))
        if brake is not None and brake.engaged and brake.mode is BrakeMode.ALL:
            raise PackageBrakeEngagedError(str(scope.ref))

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, package: CampaignPackage
    ) -> GuardrailVerdict:
        campaign = package.campaign
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, package.account_ref)
        change = GuardrailChange(
            scope=scope,
            entity_ref=package.account_ref,
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.zero(campaign.daily_budget.currency),
            after=campaign.daily_budget,
            is_creation=True,
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)


def _approved_plan(package: CampaignPackage) -> dict[str, object]:
    return package_tree_payload(
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
