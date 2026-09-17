"""`Authorization` — unifica `human_approval` y `rule_authorization`
(data-model.md tabla `approvals`; T058). Firmada Ed25519 sobre
`proposal_id + diff_hash + guardrail_verdict_hash + expires_at` por
`ads-api`; el broker solo tiene la clave publica (threat-model.md C-3).

`shared/crypto/ed25519.py` (otra rama) construira el `ApprovalSigner`/
`ApprovalVerifier` reales. Aqui se declara el `Protocol` con la misma forma
(`sign(bytes) -> bytes`, `verify(bytes, sig) -> bool`) para que el cableado
futuro sea un cambio de import, no de contrato."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import DomainError
from safent_ads.shared.managed_ads import ManagedAdsBinding


class AuthorizationKind(StrEnum):
    HUMAN_APPROVAL = "human_approval"
    RULE_AUTHORIZATION = "rule_authorization"
    # `003-paquete-de-campana` data-model.md Revision 2 §R2.9 (AL-1) /
    # contracts/api.md §R2.E (BL-3): la autorizacion que firma
    # `chokepoint_step_executor` para UN paso de publicacion, encadenada a
    # la `human_approval` de sujeto `package` via `derived_from_authorization_id`.
    # Nunca sustituye a `human_approval`/`rule_authorization`: el freno la
    # trata como autonoma en ambos modos (`EmergencyBrake.blocks`).
    PACKAGE_STEP = "package_step"


class AuthorizationDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class AuthorizationChannel(StrEnum):
    PANEL = "panel"
    TELEGRAM = "telegram"
    RULE_ENGINE = "rule_engine"


class SignerPort(Protocol):
    """Puerto local: misma forma que `ApprovalSigner` de `shared/crypto`.
    Solo `ads-api` lo implementa de verdad (Ed25519 privada)."""

    def sign(self, payload: bytes) -> bytes: ...


class VerifierPort(Protocol):
    """Puerto local: misma forma que `ApprovalVerifier` de `shared/crypto`.
    El broker lo implementa con la clave publica."""

    def verify(self, payload: bytes, signature: bytes) -> bool: ...


class AuthorizationVerificationError(DomainError):
    """Base de todo fallo de verificacion — la ejecucion debe denegar."""


class AuthorizationNotApprovedError(AuthorizationVerificationError):
    pass


class AuthorizationExpiredError(AuthorizationVerificationError):
    pass


class AuthorizationDiffMismatchError(AuthorizationVerificationError):
    """`diff_hash` recalculado del payload vivo no coincide (invariante 2)."""


class AuthorizationGuardrailMismatchError(AuthorizationVerificationError):
    """El veredicto de guardarrailes recalculado ya no coincide con el que se
    firmo — un recorte cambio el resultado desde que se autorizo."""


class AuthorizationSignatureInvalidError(AuthorizationVerificationError):
    pass


class RuleAuthorizationNotEligibleError(DomainError):
    """FR-11/FR-12 (reforzado por la migracion `0008_proposals` de la rama
    de BD): una `rule_authorization` (motor de reglas, AUTO) solo puede
    emitirse para propuestas `ROUTINE`. `IMPORTANT`/`CRITICAL` exigen
    `human_approval` siempre."""


class AuthorizationSubjectInvariantError(DomainError):
    """`003-paquete-de-campana` data-model.md Revision 2 §R2.5 (BL-7):
    exactamente uno de `proposal_id`/`subject` no es nulo, y `subject !=
    None` exige `kind == human_approval` y `channel == PANEL` — un paquete
    no es una propuesta y su autorizacion nunca se disfraza de una."""


class PackageStepAuthorizationInvariantError(DomainError):
    """`003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3):
    `kind == package_step` exige, todos a la vez, `proposal_id` (el paso
    materializa una `Proposal` real), `derived_from_authorization_id`
    (la `human_approval` de la que deriva), `package_approval` (el sobre
    firmado por el humano, integro) y `package_binding` (a que paso ata);
    y ningun otro `kind` lleva ninguno de los cuatro."""


class SubjectKind(StrEnum):
    """`AuthorizationSubject.kind` (R2.5). Solo `PACKAGE` se construye hoy:
    una autorizacion de propuesta sigue usando `proposal_id` a secas
    (`subject=None`), el mismo truco condicional que ya usa
    `managed_binding` — `PROPOSAL` queda declarado por simetria con el
    contrato pero ningun llamador lo produce todavia."""

    PROPOSAL = "proposal"
    PACKAGE = "package"


@dataclass(frozen=True, slots=True)
class AuthorizationSubject:
    """`003-paquete-de-campana` data-model.md Revision 2 §R2.5: sujeto
    explicito de una `Authorization` que NO decide sobre una `Proposal`
    (hoy, exclusivamente `kind=package`, `id=package_id`)."""

    kind: SubjectKind
    id: str


@dataclass(frozen=True, slots=True)
class PackageApprovalProof:
    """`003-paquete-de-campana` contracts/api.md §R2.E `SignedPackageApproval`:
    el sobre COMPLETO que firmo el humano (`PackageApprovalEnvelope`,
    `packages.domain.approval_envelope`), adjunto a la `Authorization`
    `package_step` para que el broker verifique la firma humana **sin**
    preguntarle nada a `ads-api` (BL-2, BL-3). `envelope`/`package_binding`
    viajan como `Mapping` puro (la forma canonica ya serializada por
    `packages.domain`) para que este modulo -- base de `packages` -- no
    importe ese contexto (aciclicidad, data-model.md "Bounded contexts")."""

    envelope: Mapping[str, object]
    authorization_id: str
    issued_by: str
    expires_at: datetime
    signature: bytes

    def as_claims(self) -> dict[str, object]:
        return {
            "envelope": dict(self.envelope),
            "authorization_id": self.authorization_id,
            "issued_by": self.issued_by,
            "expires_at": self.expires_at.isoformat(),
            "signature": self.signature.hex(),
        }


@dataclass(frozen=True, slots=True)
class Authorization:
    """Decision firmada, ligada a un `diff_hash` y a un veredicto de
    guardarrailes exactos (data-model.md `approvals`).

    `proposal_id`/`subject` (Revision 2 §R2.5, BL-7): exactamente uno no es
    `None`. Una autorizacion de paquete no tiene `Proposal` — meter su
    `package_id` en `proposal_id` seria la confusion de tipos que el
    dueño prohibe explicitamente."""

    authorization_id: AuthorizationId
    proposal_id: ProposalId | None
    kind: AuthorizationKind
    decision: AuthorizationDecision
    diff_hash: str
    guardrail_verdict_hash: str
    issued_by: str
    channel: AuthorizationChannel
    signature: bytes
    decided_at: datetime
    expires_at: datetime
    comment: str | None = None
    managed_binding: ManagedAdsBinding | None = None
    subject: AuthorizationSubject | None = None
    derived_from_authorization_id: AuthorizationId | None = None
    package_approval: PackageApprovalProof | None = None
    package_binding: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if (self.proposal_id is None) == (self.subject is None):
            raise AuthorizationSubjectInvariantError(
                "exactamente uno de proposal_id/subject debe estar presente"
            )
        if self.subject is not None and (
            self.kind is not AuthorizationKind.HUMAN_APPROVAL
            or self.channel is not AuthorizationChannel.PANEL
        ):
            raise AuthorizationSubjectInvariantError(
                "subject exige kind=human_approval y channel=panel"
            )
        self._require_package_step_shape()

    def _require_package_step_shape(self) -> None:
        package_step_fields = (
            self.proposal_id,
            self.derived_from_authorization_id,
            self.package_approval,
            self.package_binding,
        )
        if self.kind is AuthorizationKind.PACKAGE_STEP:
            if any(field is None for field in package_step_fields) or self.subject is not None:
                raise PackageStepAuthorizationInvariantError(
                    "package_step exige proposal_id, derived_from_authorization_id, "
                    "package_approval y package_binding, y nunca subject"
                )
        elif any(
            field is not None
            for field in (
                self.derived_from_authorization_id,
                self.package_approval,
                self.package_binding,
            )
        ):
            raise PackageStepAuthorizationInvariantError(
                "solo package_step lleva derived_from_authorization_id/"
                "package_approval/package_binding"
            )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def signing_payload(self) -> bytes:
        payload: dict[str, object] = {
            "authorization_id": str(self.authorization_id),
            "proposal_id": str(self.proposal_id) if self.proposal_id is not None else None,
            "kind": self.kind.value,
            "diff_hash": self.diff_hash,
            "guardrail_verdict_hash": self.guardrail_verdict_hash,
            "issued_by": self.issued_by,
            "expires_at": self.expires_at.isoformat(),
        }
        if self.managed_binding is not None:
            payload["managed_binding"] = self.managed_binding.as_claims()
        if self.subject is not None:
            payload["subject"] = {"kind": self.subject.kind.value, "id": self.subject.id}
        if self.derived_from_authorization_id is not None:
            payload["derived_from_authorization_id"] = str(self.derived_from_authorization_id)
        if self.package_approval is not None:
            payload["package_approval"] = self.package_approval.as_claims()
        if self.package_binding is not None:
            payload["package_binding"] = dict(self.package_binding)
        return canonical_json_bytes(payload)


def sign_authorization(
    *,
    authorization_id: AuthorizationId,
    kind: AuthorizationKind,
    diff_hash: str,
    guardrail_verdict_hash: str,
    issued_by: str,
    channel: AuthorizationChannel,
    decided_at: datetime,
    expires_at: datetime,
    signer: SignerPort,
    proposal_id: ProposalId | None = None,
    subject: AuthorizationSubject | None = None,
    proposal_classification: Classification | None = None,
    comment: str | None = None,
    managed_binding: ManagedAdsBinding | None = None,
    derived_from_authorization_id: AuthorizationId | None = None,
    package_approval: PackageApprovalProof | None = None,
    package_binding: Mapping[str, object] | None = None,
) -> Authorization:
    """Construye y firma una `Authorization` en estado `approved`. Unica
    forma de producir una instancia con firma valida — evita que un llamador
    fabrique una `Authorization` "a mano" con una firma inventada.

    `proposal_classification` se exige (no solo declarada) unicamente para
    `rule_authorization`, unico `kind` que puede degradar automaticamente
    de `IMPORTANT`/`CRITICAL` a `ROUTINE` sin humano delante — un sujeto de
    paquete (Revision 2 §R2.5) nunca es `rule_authorization`, asi que no le
    hace falta declarar una clasificacion que no tiene.

    `derived_from_authorization_id`/`package_approval`/`package_binding`
    (contracts/api.md §R2.E, C-E1): `PACKAGE_STEP` los exige los tres a la
    vez -- `Authorization.__post_init__` ya lo comprueba -- y ademas nunca
    se firma un paso sobre un sobre ya caducado, aunque el paso se firme
    en el instante exacto en que expira (`ApproveCampaignPackage` firma el
    sobre con la misma regla: `now >= expires_at` es caducado)."""
    if kind is AuthorizationKind.RULE_AUTHORIZATION and (
        proposal_classification is not Classification.ROUTINE or managed_binding is not None
    ):
        raise RuleAuthorizationNotEligibleError(
            f"rule_authorization no permitida para classification={proposal_classification}"
        )
    if (
        kind is AuthorizationKind.PACKAGE_STEP
        and package_approval is not None
        and decided_at >= package_approval.expires_at
    ):
        raise PackageStepAuthorizationInvariantError(
            f"sobre caducado en el momento de firmar: decided_at={decided_at} "
            f"expires_at={package_approval.expires_at}"
        )
    unsigned = Authorization(
        authorization_id=authorization_id,
        proposal_id=proposal_id,
        kind=kind,
        decision=AuthorizationDecision.APPROVED,
        diff_hash=diff_hash,
        guardrail_verdict_hash=guardrail_verdict_hash,
        issued_by=issued_by,
        channel=channel,
        signature=b"",
        decided_at=decided_at,
        expires_at=expires_at,
        comment=comment,
        managed_binding=managed_binding,
        subject=subject,
        derived_from_authorization_id=derived_from_authorization_id,
        package_approval=package_approval,
        package_binding=package_binding,
    )
    signature = signer.sign(unsigned.signing_payload())
    return replace(unsigned, signature=signature)


class AuthorizationVerifier:
    """Recalcula `diff_hash` y veredicto de guardarrailes desde datos vivos
    y comprueba firma + caducidad (T058). Nunca confia en el `diff_hash`
    almacenado en la propia `Authorization`."""

    def __init__(self, verifier: VerifierPort, clock: Clock) -> None:
        self._verifier = verifier
        self._clock = clock

    def verify(
        self,
        authorization: Authorization,
        live_diff_hash: str,
        live_guardrail_verdict_hash: str,
    ) -> None:
        """Lanza una `AuthorizationVerificationError` (o subclase) ante
        cualquier discrepancia. No devuelve un booleano a proposito: el
        default es denegar, y una excepcion no clasificada tambien deniega."""
        if authorization.decision is not AuthorizationDecision.APPROVED:
            raise AuthorizationNotApprovedError(f"decision={authorization.decision}")
        if authorization.is_expired(self._clock.now()):
            raise AuthorizationExpiredError(f"expired_at={authorization.expires_at}")
        if authorization.diff_hash != live_diff_hash:
            raise AuthorizationDiffMismatchError(
                f"authorized={authorization.diff_hash} live={live_diff_hash}"
            )
        if authorization.guardrail_verdict_hash != live_guardrail_verdict_hash:
            raise AuthorizationGuardrailMismatchError(
                f"authorized={authorization.guardrail_verdict_hash} "
                f"live={live_guardrail_verdict_hash}"
            )
        if not self._verifier.verify(authorization.signing_payload(), authorization.signature):
            raise AuthorizationSignatureInvalidError("firma invalida")
