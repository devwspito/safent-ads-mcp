"""`WriteAuthorizationPipeline`: orquesta (I/O incluido) los controles 3-6
y 8 de `contracts/platform-port.md`, comunes a cualquier plataforma. Cada
adaptador (`google_ads_adapter.py`/`meta_ads_adapter.py`) sigue siendo
quien resuelve `platform_account_id`, lee el estado remoto (comprobacion
7) y muta via el SDK (comprobacion 8's "mutate") -- este modulo evita
duplicar entre los dos la parte que NO depende de la plataforma: recomputar
`diff_hash`, verificar la firma, resolver `caps.yaml` y aplicar el tope
duro, y la idempotencia persistida."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import cast

from safent_ads.accounts.application.ports import (
    AccountRef,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.broker.domain.ledger_scope import (
    LedgerScope,
    LedgerScopeError,
    LegacyLedgerScopeError,
)
from safent_ads.broker.domain.operation_semantics import (
    DEFENSIVE_OPERATIONS,
    matches_signed_transition,
)
from safent_ads.broker.domain.package_admission import (
    admit_package_step,
    admit_package_upload,
    build_package_step_idempotency_key,
)
from safent_ads.broker.domain.write_authorization import (
    WriteDenialCode,
    authorization_signing_payload,
    check_hard_caps,
    check_state_drift,
    money_minor_units,
    outcome_literal_for_denial,
    recompute_diff_hash,
    verify_authorization,
)
from safent_ads.broker.infrastructure.caps_config import (
    AccountCaps,
    CapsConfigError,
    CapsResolverPort,
)
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.iam.application.managed_ads_authority import (
    ManagedAdsBindingAuthorityPort,
    ManagedAdsDenied,
    ManagedAdsUnavailable,
)
from safent_ads.observability.metrics import record_write_denial
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.crypto.ed25519 import ApprovalVerifier


def denial_outcome(code: WriteDenialCode) -> WriteOutcome:
    record_write_denial(code.value)
    return WriteOutcome(
        outcome=outcome_literal_for_denial(code),
        applied_value=None,
        state_hash_after=None,
        error_code=code.value,
        platform_request_id=None,
    )


class PackageUploadDeniedError(Exception):
    """H1 (revision de seguridad 0.2.23): `WriteAuthorizationPipeline.
    admit_upload` denego un paso `UPLOAD_CREATIVE` -- `error_code` es un
    `WriteDenialCode.value`. A diferencia de `execute_write` (que nunca
    lanza, siempre devuelve un `WriteOutcome`), `upload_asset` no tiene ese
    tipo de retorno uniforme (devuelve `PlatformAssetHandle`, no
    `WriteOutcome`) -- una denegacion viaja como excepcion, exactamente el
    mismo contrato que `ChokepointStepExecutor._execute_upload_creative` ya
    esperaba de cualquier otro rechazo del bróker (`BrokerRequestDeniedError`
    en el lado `ads-api`) antes de esta revision."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class WriteAuthorizationPipeline:
    def __init__(
        self,
        verifier: ApprovalVerifier,
        caps: CapsResolverPort,
        ledger: WriteLedgerStore,
        *,
        scope_resolver: Callable[[WriteIntent, str], LedgerScope],
        require_owner_approval: bool = True,
        managed_authority: ManagedAdsBindingAuthorityPort | None = None,
        require_managed_binding: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self._verifier = verifier
        self._caps = caps
        self._ledger = ledger
        self._require_owner_approval = require_owner_approval
        self._scope_resolver = scope_resolver
        # Server-owned dependency; no wire field or local-owner shortcut enables it.
        self._managed_authority = managed_authority
        self._require_managed_binding = require_managed_binding
        self._clock = clock or SystemClock()

    def authorize(  # noqa: PLR0911 - independent fail-closed authorization gates
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        *,
        platform_account_id: str,
        remote_state_hash: str,
        now: datetime,
    ) -> WriteOutcome | None:
        """`None`: los controles 3-7 pasaron, el adaptador puede comprobar
        reserva e idempotencia (`begin_write`) antes de mutar. Cualquier otro valor es el
        veredicto final -- se devuelve tal cual, sin mutar ni persistir
        (persistir el veredicto es responsabilidad de `finalize`, que solo
        se alcanza si tambien hubo intento de mutacion)."""
        denial = self._verify(intent, authorization, now)
        if denial is not None:
            return denial_outcome(denial)
        try:
            account_caps = self._caps.resolve(platform_account_id)
        except CapsConfigError:
            return denial_outcome(WriteDenialCode.ACCOUNT_NOT_CONFIGURED)
        try:
            scope = self._scope_resolver(intent, platform_account_id)
            if not self._matches_managed_scope(intent, scope):
                return denial_outcome(WriteDenialCode.MANAGED_BINDING_MISMATCH)
            denial = self._check_caps(intent, account_caps, scope, now)
        except LegacyLedgerScopeError:
            return denial_outcome(WriteDenialCode.LEGACY_LEDGER_SCOPE_UNRESOLVED)
        except LedgerScopeError:
            return denial_outcome(WriteDenialCode.LEDGER_SCOPE_UNVERIFIED)
        if denial is not None:
            return denial_outcome(denial)
        if intent.operation == WriteOperation.CREATE_CAMPAIGN:
            # _verify validated ACCOUNT + absent prior state + native PAUSED plan.
            # There is deliberately no nonexistent remote campaign to read.
            return None
        denial = check_state_drift(remote_state_hash, intent.expected_state_hash)
        return None if denial is None else denial_outcome(denial)

    def begin_write(
        self,
        key: str,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        account: str,
        now: datetime,
    ) -> WriteOutcome | None:
        """Atomic local reservation; no SQLite transaction crosses network I/O."""
        if intent.managed_binding is not None:
            return denial_outcome(WriteDenialCode.MANAGED_ADMISSION_UNAVAILABLE)
        return self._begin_reserved_write(key, intent, authorization, account, now)

    async def begin_admitted_write(  # noqa: PLR0911 - fail-closed gates before any reservation
        self,
        key: str,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        account: str,
        now: datetime,
    ) -> WriteOutcome | None:
        """Admit exact human-signed context outside SQLite, then reserve locally.

        Production keeps this dependency absent until the Enterprise human
        authentication flow exists. A live grant is never a human approval.
        The decision is used only in this invocation, never cached or serialized.
        """
        binding = intent.managed_binding
        if binding is None:
            return self.begin_write(key, intent, authorization, account, now)
        denial = self._verify(intent, authorization, self._clock.now())
        if denial is not None:
            return denial_outcome(denial)
        try:
            scope = self._scope_resolver(intent, account)
        except LedgerScopeError:
            return denial_outcome(WriteDenialCode.LEDGER_SCOPE_UNVERIFIED)
        if not self._matches_managed_scope(intent, scope):
            return denial_outcome(WriteDenialCode.MANAGED_BINDING_MISMATCH)
        authority = self._managed_authority
        if authority is None:
            return denial_outcome(WriteDenialCode.MANAGED_ADMISSION_UNAVAILABLE)
        try:
            admitted = await authority.admit_binding(binding, operation="execute")
        except ManagedAdsDenied:
            return denial_outcome(WriteDenialCode.MANAGED_ADMISSION_DENIED)
        except ManagedAdsUnavailable:
            return denial_outcome(WriteDenialCode.MANAGED_ADMISSION_UNAVAILABLE)
        if admitted != binding:
            return denial_outcome(WriteDenialCode.MANAGED_BINDING_MISMATCH)
        # No await from here to the reservation commit. Recheck signature,
        # mutable JSON, credentials and expiry after acquiring the write lock.
        return self._begin_reserved_write(key, intent, authorization, account, now)

    def _begin_reserved_write(
        self,
        key: str,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        account: str,
        now: datetime,
    ) -> WriteOutcome | None:
        self._ledger.begin_transaction()
        try:
            fresh_expiry = intent.managed_binding is not None or intent.operation in {
                WriteOperation.CREATE_AD_SET,
                WriteOperation.CREATE_AD,
            }
            checked_at = self._clock.now() if fresh_expiry else now
            result = self._reserve_write(
                key, intent, authorization, account, now, verification_time=checked_at
            )
            self._ledger.commit()
            return result
        except BaseException:
            self._ledger.rollback()
            raise

    def _reserve_write(  # noqa: PLR0911 - explicit fail-closed authorization gates
        self,
        key: str,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        account: str,
        now: datetime,
        *,
        verification_time: datetime,
    ) -> WriteOutcome | None:
        prior = self._ledger.get_outcome(key)
        if prior is not None:
            if not self._ledger.receipt_matches(key, intent, authorization.authorization_id):
                return denial_outcome(WriteDenialCode.DIFF_HASH_MISMATCH)
            # Existing receipts are readable without granting fresh authority.
            # New attempts must still prove the live server-side connection.
        # Accounting retains the adapter's operation timestamp, also used by
        # finalize. Only authority expiry uses fresh wall time after lock wait.
        denial = self._verify(intent, authorization, verification_time)
        if denial is not None:
            return denial_outcome(denial)
        try:
            self._ledger.assert_key_not_orphaned(key)
            scope = self._scope_resolver(intent, account)
            if not self._matches_managed_scope(intent, scope):
                return denial_outcome(WriteDenialCode.MANAGED_BINDING_MISMATCH)
            if prior is not None:
                if self._ledger.finalization_scope(key, intent, account) != scope:
                    return denial_outcome(WriteDenialCode.LEDGER_SCOPE_UNVERIFIED)
            else:
                denial = self._check_caps(intent, self._caps.resolve(account), scope, now)
        except LegacyLedgerScopeError:
            return denial_outcome(WriteDenialCode.LEGACY_LEDGER_SCOPE_UNRESOLVED)
        except LedgerScopeError:
            return denial_outcome(WriteDenialCode.LEDGER_SCOPE_UNVERIFIED)
        except CapsConfigError:
            return denial_outcome(WriteDenialCode.ACCOUNT_NOT_CONFIGURED)
        if prior is not None:
            return prior
        if denial is not None:
            return denial_outcome(denial)
        before = money_minor_units(intent.valor_actual) or 0
        after = money_minor_units(intent.valor_propuesto) or 0
        self._ledger.begin_receipt(
            key, intent, authorization.authorization_id, scope, max(0, after - before)
        )
        return None

    def read_receipt(
        self, key: str, intent: WriteIntent, authorization: SignedAuthorization
    ) -> WriteOutcome | None:
        # Signature authenticates the original request, but expiry is irrelevant to
        # a read of its past receipt. This grants no new execution authority.
        if intent.managed_binding != authorization.managed_binding:
            raise ValueError("receipt_payload_mismatch")
        if intent.managed_binding is not None and intent.business_id != str(
            intent.managed_binding.account.business_id
        ):
            raise ValueError("receipt_payload_mismatch")
        if not intent.business_id or recompute_diff_hash(intent) != authorization.diff_hash:
            raise ValueError("receipt_payload_mismatch")
        if intent.diff_hash != authorization.diff_hash:
            raise ValueError("receipt_payload_mismatch")
        if not self._verifier.verify(
            authorization_signing_payload(authorization), bytes.fromhex(authorization.signature)
        ):
            raise ValueError("receipt_signature_invalid")
        return self._ledger.read_receipt(key, intent, authorization.authorization_id)

    async def admit_upload(
        self,
        *,
        account_ref: AccountRef,
        media: bytes,
        mime_type: str,
        width: int | None,
        height: int | None,
        binding: Mapping[str, object] | None,
        approval: Mapping[str, object] | None,
        upload: Callable[[], Awaitable[PlatformAssetHandle]],
        now: datetime,
    ) -> PlatformAssetHandle:
        """H1 (revision de seguridad 0.2.23): admite un paso `UPLOAD_CREATIVE`
        con las MISMAS R1-R6 que `admit_package_step` (`package_admission.
        admit_package_upload`) mas el cotejo de `sha256(media)` contra la
        plantilla firmada, y anota el manejador confirmado bajo
        `pkg-<publication_id>-<step_index>` -- la MISMA clave que
        `_verify_payload_reproducible`/R5 ya resuelven para el resto de
        pasos, en el MISMO libro (`WriteLedgerStore`). Sin reserva previa en
        `write_receipts`: un `UPLOAD_CREATIVE` no compite por tope de gasto
        ni tiene padre que confirmar (R5 no aplica) -- la unica idempotencia
        que hace falta es "misma clave -> mismo manejador, nunca una segunda
        subida", que `idempotent_writes` ya garantiza (T108). Una subida SIN
        paquete (`binding`/`approval` los dos `None`) nunca toca el libro:
        comportamiento identico al de antes de esta revision."""
        if binding is None and approval is None:
            return await upload()
        if binding is None or approval is None:
            raise PackageUploadDeniedError(WriteDenialCode.PACKAGE_BINDING_REQUIRED.value)
        if width is None or height is None:
            raise PackageUploadDeniedError(WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE.value)
        denial = admit_package_upload(
            account_ref=account_ref,
            media=media,
            mime_type=mime_type,
            width=width,
            height=height,
            binding=binding,
            approval=approval,
            verifier=self._verifier,
            now=now,
        )
        if denial is not None:
            record_write_denial(denial.value)
            raise PackageUploadDeniedError(denial.value)
        # R4 (`_verify_derivable`, ya superada arriba) garantiza que
        # `step_index` es un `int` dentro de rango -- el `cast` documenta esa
        # garantia de frontera en vez de reventar con un `assert` (que
        # desaparece con `python -O`).
        step_index = cast(int, binding["step_index"])
        key = build_package_step_idempotency_key(str(binding["publication_id"]), step_index)
        cached = self._ledger.created_resource(key)
        if cached is not None:
            return PlatformAssetHandle(platform_asset_id=cached, preview_url=None)
        handle = await upload()
        self._ledger.record_outcome_if_absent(
            key, WriteOutcome("SUCCEEDED", None, None, None, handle.platform_asset_id)
        )
        return handle

    def finalize(
        self,
        idempotency_key: str,
        platform_account_id: str,
        intent: WriteIntent,
        outcome: WriteOutcome,
        *,
        now: datetime,
    ) -> WriteOutcome:
        """Persiste el desenlace (exito o fallo) bajo la clave de
        idempotencia y, solo si aplico de verdad, anota el delta en el
        contador propio del dia (comprobacion 6). `now` debe ser el MISMO
        instante pasado a `authorize` para esta misma escritura -- evita
        que un cambio de dia entre la comprobacion y el registro desplace
        el apunte a una fecha distinta de la que se conto contra el tope."""
        self._ledger.begin_transaction()
        try:
            scope = self._ledger.finalization_scope(idempotency_key, intent, platform_account_id)
            persisted = self._ledger.record_outcome_if_absent(idempotency_key, outcome)
            if persisted.outcome == "SUCCEEDED":
                self._record_applied_delta(intent, scope, idempotency_key, now)
            self._ledger.commit()
            return persisted
        except BaseException:
            self._ledger.rollback()
            raise

    def _verify(  # noqa: PLR0911 - independent cryptographic and identity gates
        self, intent: WriteIntent, authorization: SignedAuthorization, now: datetime
    ) -> WriteDenialCode | None:
        live_diff_hash = recompute_diff_hash(intent)
        denial = verify_authorization(intent, authorization, live_diff_hash, now, self._verifier)
        if denial is not None:
            return denial
        if self._require_managed_binding and intent.managed_binding is None:
            return WriteDenialCode.MANAGED_BINDING_MISMATCH
        if intent.managed_binding is not None:
            if self._managed_authority is None:
                return WriteDenialCode.MANAGED_ADMISSION_UNAVAILABLE
            if authorization.kind != "human_approval":
                return WriteDenialCode.OWNER_APPROVAL_REQUIRED
            if authorization.issued_by != str(intent.managed_binding.user_id):
                return WriteDenialCode.MANAGED_BINDING_MISMATCH
            try:
                intent.managed_binding.validate_entity(intent.entity_ref)
            except ValueError:
                return WriteDenialCode.MANAGED_BINDING_MISMATCH
        if not matches_signed_transition(intent):
            return WriteDenialCode.OPERATION_DIFF_MISMATCH
        # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3): R1-R7,
        # SIEMPRE evaluadas para un `package_step` -- nunca condicionadas a
        # `require_owner_approval` (esa bandera decide si un `kind` sin sobre
        # humano basta; la integridad estructural del propio sobre no es
        # opcional). Para cualquier otro `kind`, esta llamada solo comprueba
        # la mitad "prohibido" de R1 (INV-13) y devuelve `None` si no hay
        # sobre/binding colado.
        denial = admit_package_step(
            intent=intent,
            authorization=authorization,
            verifier=self._verifier,
            now=now,
            resolve_created_resource=self._ledger.created_resource,
        )
        if denial is not None:
            return denial
        if self._require_owner_approval and authorization.kind not in {
            "human_approval",
            "package_step",
        }:
            return WriteDenialCode.OWNER_APPROVAL_REQUIRED
        if (
            authorization.kind == "rule_authorization"
            and intent.operation not in DEFENSIVE_OPERATIONS
        ):
            return WriteDenialCode.RULE_OPERATION_NOT_DEFENSIVE
        return None

    @staticmethod
    def _matches_managed_scope(intent: WriteIntent, scope: LedgerScope) -> bool:
        binding = intent.managed_binding
        return binding is None or (
            scope.business_id == binding.account.business_id
            and scope.platform == binding.account.platform
            and scope.external_account_id == binding.provider_account.external_account_id
        )

    def _check_caps(
        self,
        intent: WriteIntent,
        account_caps: AccountCaps,
        platform_account_id: LedgerScope,
        now: datetime,
    ) -> WriteDenialCode | None:
        today = now.date()
        daily = self._ledger.snapshot_today(platform_account_id, today)
        monthly_delta = self._ledger.month_to_date_delta(platform_account_id, today)
        pending_count, pending_delta = self._ledger.pending_totals(platform_account_id)
        return check_hard_caps(
            account_caps,
            before_minor_units=0
            if intent.operation == WriteOperation.CREATE_CAMPAIGN
            else money_minor_units(intent.valor_actual),
            after_minor_units=money_minor_units(intent.valor_propuesto),
            changes_count_today=daily.changes_count + pending_count,
            applied_delta_today_minor_units=daily.applied_delta_minor_units + pending_delta,
            applied_delta_month_to_date_minor_units=monthly_delta + pending_delta,
            is_creation=intent.operation == WriteOperation.CREATE_CAMPAIGN,
        )

    def _record_applied_delta(
        self,
        intent: WriteIntent,
        platform_account_id: LedgerScope,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        before = (
            0
            if intent.operation == WriteOperation.CREATE_CAMPAIGN
            else money_minor_units(intent.valor_actual)
        )
        after = money_minor_units(intent.valor_propuesto)
        delta = 0 if before is None or after is None else after - before
        self._ledger.record_applied_change(platform_account_id, now.date(), idempotency_key, delta)


__all__ = ["PackageUploadDeniedError", "WriteAuthorizationPipeline", "denial_outcome"]
