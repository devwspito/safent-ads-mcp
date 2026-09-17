"""`SqlPackageStepRepository`: implementa `PackageStepRepository` sobre
`campaign_package_steps` (0042/0045). `RunPackagePublication` la usa para
leer el recibo confirmado de un paso ya `done` (parent, o creativo para
`{creative_of:X}`) y para persistir el desenlace de cada paso ejecutado.

`upsert` NUNCA usa `INSERT ... ON CONFLICT DO UPDATE`: Postgres dispara los
triggers `BEFORE INSERT` para la fila propuesta ANTES de comprobar si hay
conflicto (documentado, no un bug de esta base) -- con eso, la fila
`pending -> done` de una segunda llamada disparaba el guardia de
`campaign_package_steps_guard` como si fuera un nacimiento (`NEW.state <>
'pending'`) y lo rechazaba SIEMPRE, aunque la fila ya existiera. Por eso
aqui se prueba `UPDATE` primero y solo se cae a `INSERT` si no habia fila
-- el `UPDATE` SI dispara el guardia por la rama correcta (`TG_OP =
'UPDATE'`), que es quien de verdad impide una transicion de estado
invalida o sobrescribir un `created_entity_ref` ya confirmado."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.packages.application.ports import PackageStepRecord

__all__ = ["SqlPackageStepRepository"]

_COLUMNS: Final = """
    publication_id, step_index, kind, local_ref, parent_local_ref, state,
    created_entity_ref, proposal_id, execution_id, outcome_code, confirmed_state_hash
"""

_GET_TEMPLATE: Final = """
    SELECT {columns} FROM campaign_package_steps
     WHERE publication_id = :publication_id AND step_index = :step_index
"""
_GET: Final = _GET_TEMPLATE.format(columns=_COLUMNS)

_INSERT_TEMPLATE: Final = """
    INSERT INTO campaign_package_steps ({columns})
    VALUES (:publication_id, :step_index, :kind, :local_ref, :parent_local_ref, :state,
            :created_entity_ref, CAST(:proposal_id AS UUID), CAST(:execution_id AS UUID),
            :outcome_code, :confirmed_state_hash)
"""
_INSERT: Final = _INSERT_TEMPLATE.format(columns=_COLUMNS)

_UPDATE: Final = """
    UPDATE campaign_package_steps SET
        state                = :state,
        created_entity_ref   = :created_entity_ref,
        proposal_id          = CAST(:proposal_id AS UUID),
        execution_id         = CAST(:execution_id AS UUID),
        outcome_code         = :outcome_code,
        confirmed_state_hash = :confirmed_state_hash
     WHERE publication_id = :publication_id AND step_index = :step_index
    RETURNING publication_id
"""


class SqlPackageStepRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, publication_id: str, step_index: int) -> PackageStepRecord | None:
        result = await self._session.execute(
            text(_GET), {"publication_id": publication_id, "step_index": step_index}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_record(row)

    async def upsert(self, record: PackageStepRecord) -> None:
        params = _params(record)
        result = await self._session.execute(text(_UPDATE), params)
        # `RETURNING publication_id`: sin fila devuelta, la fila no existia
        # todavia -- mas portable que `.rowcount` (mypy: `CursorResult`
        # generico no lo tipa, `crm/infrastructure/sql_repositories.py`
        # sigue el mismo criterio).
        if result.mappings().one_or_none() is None:
            await self._session.execute(text(_INSERT), params)


def _params(record: PackageStepRecord) -> dict[str, Any]:
    return {
        "publication_id": record.publication_id,
        "step_index": record.step_index,
        "kind": record.kind,
        "local_ref": record.local_ref,
        "parent_local_ref": record.parent_local_ref,
        "state": record.state,
        "created_entity_ref": record.created_entity_ref,
        "proposal_id": record.proposal_id,
        "execution_id": record.execution_id,
        "outcome_code": record.outcome_code,
        "confirmed_state_hash": record.confirmed_state_hash,
    }


def _to_record(row: RowMapping) -> PackageStepRecord:
    return PackageStepRecord(
        publication_id=str(row["publication_id"]),
        step_index=int(row["step_index"]),
        kind=str(row["kind"]),
        local_ref=str(row["local_ref"]),
        parent_local_ref=_optional_str(row["parent_local_ref"]),
        state=str(row["state"]),
        created_entity_ref=_optional_str(row["created_entity_ref"]),
        proposal_id=_optional_str(row["proposal_id"]),
        execution_id=_optional_str(row["execution_id"]),
        outcome_code=_optional_str(row["outcome_code"]),
        confirmed_state_hash=_optional_str(row["confirmed_state_hash"]),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
