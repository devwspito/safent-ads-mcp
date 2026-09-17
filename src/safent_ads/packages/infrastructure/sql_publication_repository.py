"""`SqlPackagePublicationRepository`: implementa `PackagePublicationRepository`
sobre `campaign_package_publications` (migracion 0042/0044). `RunPackagePublication`
(T023, siguiente entrega) es quien avanza `cursor`/`state`; esta entrega solo
crea la fila `pending` al aprobar y la relee para el modelo de lectura."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.packages.application.ports import PackagePublicationRecord
from safent_ads.packages.domain.approval_envelope import (
    PackageApprovalEnvelope,
    StepKind,
    StepTemplate,
)
from safent_ads.packages.domain.identifiers import PackageId
from safent_ads.packages.infrastructure.package_codec import dump_json
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode

__all__ = ["SqlPackagePublicationRepository"]

_GET_BY_ID_SQL = text("""
    SELECT id, package_id, authorization_id, state, cursor,
           approval_envelope::text AS approval_envelope_text, approval_signature,
           envelope_hash, approval_expires_at, approved_plan::text AS approved_plan_text,
           started_at, halt_reason, failed_step_index, finished_at
      FROM campaign_package_publications
     WHERE id = :publication_id
       FOR UPDATE SKIP LOCKED
""")

_ADVANCE_SQL = text("""
    UPDATE campaign_package_publications
       SET cursor = :cursor, state = :state, halt_reason = :halt_reason,
           failed_step_index = :failed_step_index, finished_at = :finished_at
     WHERE id = :publication_id
""")

_LIST_OPEN_SQL = text("""
    SELECT id FROM campaign_package_publications
     WHERE state IN ('pending', 'running')
     ORDER BY started_at
""")

_INSERT_SQL = text("""
    INSERT INTO campaign_package_publications
        (id, package_id, authorization_id, state, cursor, approval_envelope,
         approval_signature, envelope_hash, approval_expires_at, approved_plan, started_at)
    VALUES
        (:id, :package_id, CAST(:authorization_id AS UUID), :state, :cursor,
         CAST(:approval_envelope AS JSONB), :approval_signature, :envelope_hash,
         :approval_expires_at, CAST(:approved_plan AS JSONB), :started_at)
""")

_GET_BY_PACKAGE_SQL = text("""
    SELECT id, package_id, authorization_id, state, cursor,
           approval_envelope::text AS approval_envelope_text, approval_signature,
           envelope_hash, approval_expires_at, approved_plan::text AS approved_plan_text,
           started_at, halt_reason, failed_step_index, finished_at
      FROM campaign_package_publications
     WHERE package_id = :package_id
""")


class SqlPackagePublicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: PackagePublicationRecord) -> None:
        await self._session.execute(
            _INSERT_SQL,
            {
                "id": record.publication_id,
                "package_id": str(record.package_id),
                "authorization_id": record.authorization_id,
                "state": record.state,
                "cursor": record.cursor,
                "approval_envelope": dump_json(record.envelope.to_canonical()),
                "approval_signature": record.approval_signature,
                "envelope_hash": record.envelope_hash,
                "approval_expires_at": record.approval_expires_at,
                "approved_plan": dump_json(record.approved_plan),
                "started_at": record.started_at,
            },
        )

    async def get_by_package_id(self, package_id: PackageId) -> PackagePublicationRecord | None:
        result = await self._session.execute(_GET_BY_PACKAGE_SQL, {"package_id": str(package_id)})
        row = result.mappings().one_or_none()
        return None if row is None else _row_to_record(row)

    async def get_by_id(self, publication_id: str) -> PackagePublicationRecord | None:
        result = await self._session.execute(
            _GET_BY_ID_SQL, {"publication_id": publication_id}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _row_to_record(row)

    async def advance(
        self,
        publication_id: str,
        *,
        cursor: int,
        state: str,
        halt_reason: str | None,
        failed_step_index: int | None,
        finished_at: datetime | None,
    ) -> None:
        await self._session.execute(
            _ADVANCE_SQL,
            {
                "publication_id": publication_id,
                "cursor": cursor,
                "state": state,
                "halt_reason": halt_reason,
                "failed_step_index": failed_step_index,
                "finished_at": finished_at,
            },
        )

    async def list_open(self) -> tuple[str, ...]:
        result = await self._session.execute(_LIST_OPEN_SQL)
        return tuple(str(row[0]) for row in result.all())


def _row_to_record(row: RowMapping) -> PackagePublicationRecord:
    return PackagePublicationRecord(
        publication_id=str(row["id"]),
        package_id=PackageId.parse(str(row["package_id"])),
        authorization_id=str(row["authorization_id"]),
        state=row["state"],
        cursor=row["cursor"],
        envelope=_decode_envelope(json.loads(row["approval_envelope_text"])),
        envelope_hash=row["envelope_hash"],
        approval_signature=bytes(row["approval_signature"]),
        approval_expires_at=row["approval_expires_at"],
        approved_plan=json.loads(row["approved_plan_text"]),
        started_at=row["started_at"],
        halt_reason=row["halt_reason"],
        failed_step_index=row["failed_step_index"],
        finished_at=row["finished_at"],
    )


def _decode_envelope(data: dict[str, Any]) -> PackageApprovalEnvelope:
    account_ref = EntityRef.parse(str(data["account_ref"]))
    step_plan = tuple(_decode_step_template(item) for item in data["step_plan"])
    return PackageApprovalEnvelope(
        package_id=PackageId.parse(str(data["package_id"])),
        package_hash=str(data["package_hash"]),
        business_id=BusinessId.parse(str(data["business_id"])),
        platform=PlatformCode(str(data["platform"])),
        account_ref=account_ref,
        publication_id=str(data["publication_id"]),
        approved_by=str(data["approved_by"]),
        approved_at=datetime.fromisoformat(str(data["approved_at"])),
        approval_expires_at=datetime.fromisoformat(str(data["approval_expires_at"])),
        step_plan=step_plan,
    )


def _decode_step_template(data: dict[str, Any]) -> StepTemplate:
    expected_done_steps = data["expected_done_steps"]
    return StepTemplate(
        step_index=int(data["step_index"]),
        step_kind=StepKind(data["step_kind"]),
        local_ref=str(data["local_ref"]),
        parent_local_ref=(
            str(data["parent_local_ref"]) if data["parent_local_ref"] is not None else None
        ),
        payload_template_hash=str(data["payload_template_hash"]),
        expected_done_steps=int(expected_done_steps) if expected_done_steps is not None else None,
        depends_on=tuple(data.get("depends_on", ())),
    )
