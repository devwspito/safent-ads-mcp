"""Persistencia propia del broker para los controles 6 y 8 de
`contracts/platform-port.md` (threat-model.md C-8, C-17): SQLite bajo
`BrokerSettings.credential_store_dir`, sin relacion con `spend_ledger` de
`ads-api` -- el "doble libro" a proposito (C-17): aunque el ledger de
`ads-api` este mal calculado o la base este comprometida, el tope diario y
mensual del broker se vuelve a contar aqui, desde cero, con sus propias
filas.

Un unico fichero con `idempotent_writes` (una clave ya vista
devuelve el resultado anterior sin volver a mutar, comprobacion 8) y
`applied_changes` (contador propio de cambios e importe aplicado por
cuenta y dia, comprobacion 6); `write_receipts` conserva reservas y
resultados ligados al payload original. `scope_key` identifica negocio,
plataforma y cuenta fisica, nunca la conexion OAuth. `sqlite3` (stdlib) en vez de una dependencia
nueva -- mismo criterio que `EncryptedCredentialStore` sobre AES-GCM: un
fichero por broker, nunca una base compartida con `ads-api`."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from safent_ads.accounts.application.ports import WriteIntent, WriteOutcome
from safent_ads.broker.domain.ledger_scope import (
    LedgerScope,
    LedgerScopeError,
    LegacyLedgerScopeError,
)
from safent_ads.shared.ids import EntityRef

_DB_DIR_MODE = 0o700
_DB_FILE_MODE = 0o600
_MONTH_PREFIX_LENGTH: Final = 7  # "YYYY-MM" de "YYYY-MM-DD"

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS idempotent_writes (
    idempotency_key TEXT PRIMARY KEY,
    outcome_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_changes (
    platform_account_id TEXT NOT NULL,
    scope_key TEXT,
    change_date TEXT NOT NULL,
    delta_minor_units INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    PRIMARY KEY (platform_account_id, change_date, idempotency_key)
);
CREATE TABLE IF NOT EXISTS write_receipts (
    idempotency_key TEXT PRIMARY KEY,
    business_id TEXT,
    entity_ref TEXT NOT NULL,
    diff_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    authorization_id TEXT NOT NULL,
    platform_account_id TEXT NOT NULL,
    scope_key TEXT,
    positive_delta_minor INTEGER NOT NULL,
    outcome_json TEXT,
    pending INTEGER NOT NULL DEFAULT 1
);
"""


@dataclass(frozen=True, slots=True)
class DailyChangesSnapshot:
    changes_count: int
    applied_delta_minor_units: int


class WriteLedgerStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=_DB_DIR_MODE)
        self._connection = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._upgrade_scope_schema()
        db_path.chmod(_DB_FILE_MODE)

    def _upgrade_scope_schema(self) -> None:
        # Additive, serialized, and no inferred tenant backfill. Old binaries
        # still insert NULL: these rows remain a fail-closed legacy barrier.
        self.begin_transaction()
        try:
            for table in ("applied_changes", "write_receipts"):
                columns = {
                    row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")
                }
                if "scope_key" not in columns:
                    self._connection.execute(f"ALTER TABLE {table} ADD COLUMN scope_key TEXT")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_applied_scope_date "
                "ON applied_changes(scope_key, change_date)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_receipt_scope_pending "
                "ON write_receipts(scope_key, pending)"
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_applied_scoped_key "
                "ON applied_changes(idempotency_key) WHERE scope_key IS NOT NULL"
            )
            self.commit()
        except BaseException:
            self.rollback()
            self._connection.close()
            raise

    def created_resource(self, idempotency_key: str) -> str | None:
        """T108 (`003-paquete-de-campana` contracts/api.md §R2.E, R5/R7): el
        recurso de plataforma (`platform_request_id` -- `campaign_resource`/
        `child_resource` en `broker/platforms/{campaign,ad_child}_creation.py`)
        de una escritura YA confirmada `SUCCEEDED` bajo esta clave, o `None`
        si no existe o no tuvo exito. Fuente unica para que el bróker
        resuelva el padre/las creatividades de un paso de paquete SIN
        preguntarle nada a `ads-api`: nunca se fia de una fila de
        `campaign_package_steps` (que vive en la base de `ads-api`, fuera de
        su confianza)."""
        row = self._connection.execute(
            "SELECT outcome_json FROM idempotent_writes WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        outcome = json.loads(row[0])
        if outcome.get("outcome") != "SUCCEEDED":
            return None
        resource = outcome.get("platform_request_id")
        return resource if isinstance(resource, str) and resource else None

    def get_outcome(self, idempotency_key: str) -> WriteOutcome | None:
        row = self._connection.execute(
            "SELECT outcome_json FROM idempotent_writes WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is not None:
            return _outcome_from_json(json.loads(row[0]))
        pending = self._connection.execute(
            "SELECT 1 FROM write_receipts WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return (
            None
            if pending is None
            else WriteOutcome("UNKNOWN", None, None, "remote_outcome_unknown", None)
        )

    def read_receipt(
        self, key: str, intent: WriteIntent, authorization_id: str
    ) -> WriteOutcome | None:
        # `receipt_matches` ya compara la huella COMPLETA (T110, BL-5): para
        # un paso de paquete no incluye `authorization_id` a proposito (se
        # re-acuña en cada reanudacion). Filtrar la fila TAMBIEN por ese
        # `authorization_id` aqui reintroduciria el mismo atasco que T110
        # cierra -- la fila se identifica solo por `idempotency_key` una vez
        # que la huella ya caso.
        if not self.receipt_matches(key, intent, authorization_id):
            return None
        row = self._connection.execute(
            "SELECT outcome_json FROM write_receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        return None if row is None or row[0] is None else _outcome_from_json(json.loads(row[0]))

    def receipt_matches(self, key: str, intent: WriteIntent, authorization_id: str) -> bool:
        row = self._connection.execute(
            "SELECT fingerprint FROM write_receipts WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is not None and row[0] == _fingerprint(intent, authorization_id)

    def assert_key_not_orphaned(self, key: str) -> None:
        # A surviving applied row proves this key was already used, even when
        # the legacy writer never recorded a receipt/outcome. Never remutate it
        # against another account merely because that account has a clean cap.
        row = self._connection.execute(
            "SELECT 1 FROM applied_changes a WHERE a.idempotency_key = ? "
            "AND NOT EXISTS (SELECT 1 FROM write_receipts r "
            "WHERE r.idempotency_key=a.idempotency_key)",
            (key,),
        ).fetchone()
        if row is not None:
            raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved")

    def begin_receipt(
        self,
        key: str,
        intent: WriteIntent,
        authorization_id: str,
        account: LedgerScope,
        positive_delta: int,
    ) -> bool:
        result = self._connection.execute(
            "INSERT OR IGNORE INTO write_receipts "
            "(idempotency_key, business_id, entity_ref, diff_hash, authorization_id, "
            "platform_account_id, positive_delta_minor, fingerprint, scope_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                intent.business_id,
                str(intent.entity_ref),
                intent.diff_hash,
                authorization_id,
                account.external_account_id,
                positive_delta,
                _fingerprint(intent, authorization_id),
                account.key,
            ),
        )
        return result.rowcount == 1

    def pending_totals(self, account: LedgerScope) -> tuple[int, int]:
        self._assert_legacy_unambiguous(account)
        row = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(positive_delta_minor), 0) FROM write_receipts "
            "WHERE scope_key = ? AND pending = 1",
            (account.key,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def begin_transaction(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._connection.execute("COMMIT")

    def rollback(self) -> None:
        self._connection.execute("ROLLBACK")

    def record_outcome_if_absent(self, idempotency_key: str, outcome: WriteOutcome) -> WriteOutcome:
        """La primera escritura para una clave gana; una carrera con otra
        escritura concurrente para la MISMA clave devuelve el resultado ya
        anotado en vez del que acaba de calcular esta llamada."""
        self._connection.execute(
            "UPDATE write_receipts SET outcome_json = ?, pending = ? "
            "WHERE idempotency_key = ? AND outcome_json IS NULL",
            (
                json.dumps(_outcome_to_json(outcome)),
                int(outcome.outcome in {"FAILED", "UNKNOWN"}),
                idempotency_key,
            ),
        )
        try:
            self._connection.execute(
                "INSERT INTO idempotent_writes (idempotency_key, outcome_json) VALUES (?, ?)",
                (idempotency_key, json.dumps(_outcome_to_json(outcome))),
            )
        except sqlite3.IntegrityError:
            return self.get_outcome(idempotency_key) or outcome
        return outcome

    def snapshot_today(self, account: LedgerScope, today: date) -> DailyChangesSnapshot:
        self._assert_legacy_unambiguous(account)
        row = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(delta_minor_units), 0) FROM applied_changes "
            "WHERE scope_key = ? AND change_date = ?",
            (account.key, today.isoformat()),
        ).fetchone()
        return DailyChangesSnapshot(changes_count=row[0], applied_delta_minor_units=row[1])

    def month_to_date_delta(self, account: LedgerScope, today: date) -> int:
        self._assert_legacy_unambiguous(account)
        month_prefix = today.isoformat()[:_MONTH_PREFIX_LENGTH]
        row = self._connection.execute(
            "SELECT COALESCE(SUM(delta_minor_units), 0) FROM applied_changes "
            "WHERE scope_key = ? AND substr(change_date, 1, 7) = ?",
            (account.key, month_prefix),
        ).fetchone()
        return int(row[0])

    def record_applied_change(
        self,
        account: LedgerScope,
        today: date,
        idempotency_key: str,
        delta_minor_units: int,
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO applied_changes "
            "(platform_account_id, change_date, delta_minor_units, idempotency_key, scope_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                account.external_account_id,
                today.isoformat(),
                delta_minor_units,
                idempotency_key,
                account.key,
            ),
        )

    def finalization_scope(self, key: str, intent: WriteIntent, remote_account: str) -> LedgerScope:
        """Bind the result to its durable reservation, without renewing credentials.

        Revocation after dispatch must not prevent recording the original effect.
        A different payload/business/account cannot settle or move this receipt.
        """
        row = self._connection.execute(
            "SELECT business_id, entity_ref, authorization_id, platform_account_id, scope_key "
            "FROM write_receipts WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is not None and row[4] is None:
            raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved")
        if row is None or row[3] != remote_account:
            raise LedgerScopeError("receipt_scope_mismatch")
        if not self.receipt_matches(key, intent, row[2]):
            raise LedgerScopeError("receipt_payload_mismatch")
        scope = LedgerScope(UUID(row[0]), EntityRef.parse(row[1]).platform, row[3])
        if scope.key != row[4]:
            raise LedgerScopeError("receipt_scope_mismatch")
        return scope

    def _assert_legacy_unambiguous(self, scope: LedgerScope) -> None:
        legacy = self._connection.execute(
            "SELECT platform_account_id FROM applied_changes WHERE scope_key IS NULL "
            "UNION SELECT platform_account_id FROM write_receipts WHERE scope_key IS NULL"
        ).fetchall()
        for (account,) in legacy:
            # Numeric identity is the only account format understood by these
            # two SDK adapters. Unknown legacy formats cannot prove separation.
            if not isinstance(account, str) or not re.fullmatch(r"(?:act_)?[0-9]+", account):
                raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved")
            if account.removeprefix("act_") == scope.external_account_id.removeprefix("act_"):
                raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved")
        orphaned = self._connection.execute(
            "SELECT outcome_json FROM idempotent_writes i "
            "WHERE NOT EXISTS (SELECT 1 FROM write_receipts r "
            "WHERE r.idempotency_key=i.idempotency_key) "
            "AND NOT EXISTS (SELECT 1 FROM applied_changes a "
            "WHERE a.idempotency_key=i.idempotency_key)"
        ).fetchall()
        for (raw,) in orphaned:
            try:
                outcome = json.loads(raw)["outcome"]
            except (ValueError, KeyError, TypeError) as exc:
                raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved") from exc
            if outcome not in {"DENIED", "BLOCKED_HARD_CAP", "SKIPPED_DRIFT"}:
                raise LegacyLedgerScopeError("legacy_ledger_scope_unresolved")

    def close(self) -> None:
        self._connection.close()


def _outcome_to_json(outcome: WriteOutcome) -> dict[str, Any]:
    return {
        "outcome": outcome.outcome,
        "applied_value": outcome.applied_value,
        "state_hash_after": outcome.state_hash_after,
        "error_code": outcome.error_code,
        "platform_request_id": outcome.platform_request_id,
    }


def _fingerprint(intent: WriteIntent, authorization_id: str) -> str:
    payload = {
        **(
            {"managed_binding": intent.managed_binding.as_claims()}
            if intent.managed_binding
            else {}
        ),
        "business_id": intent.business_id,
        "entity_ref": str(intent.entity_ref),
        "operation": intent.operation.value,
        "parameter": intent.parametro,
        "before": intent.valor_actual,
        "after": intent.valor_propuesto,
        "diff_hash": intent.diff_hash,
        "expected_state_hash": intent.expected_state_hash,
        **_receipt_identity(intent, authorization_id),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _receipt_identity(intent: WriteIntent, authorization_id: str) -> dict[str, object]:
    """`003-paquete-de-campana` data-model.md Revision 2 §R2.6 (BL-5): para
    `kind == package_step` la huella del recibo se construye SIN
    `authorization_id` -- cada reanudacion re-acuña esa autorizacion
    (`chokepoint_step_executor.sign_authorization`, `derived_from_
    authorization_id` distinto cada vez), y exigir que coincida dejaria el
    paso atascado para siempre (D-T2). En su lugar, `package_hash` +
    `publication_id` + `step_index`: estables mientras el sobre humano siga
    vivo sobre el MISMO contenido, y **distintos** en cuanto el contenido
    cambia (R3 ya lo exige aparte). Para cualquier otro `kind`, sin cambio."""
    binding = intent.package_binding
    if binding is None:
        return {"authorization_id": authorization_id}
    return {
        "package_hash": binding.get("package_hash"),
        "publication_id": binding.get("publication_id"),
        "step_index": binding.get("step_index"),
    }


def _outcome_from_json(raw: dict[str, Any]) -> WriteOutcome:
    return WriteOutcome(
        outcome=raw["outcome"],
        applied_value=raw["applied_value"],
        state_hash_after=raw["state_hash_after"],
        error_code=raw["error_code"],
        platform_request_id=raw["platform_request_id"],
    )


__all__ = ["DailyChangesSnapshot", "WriteLedgerStore"]
