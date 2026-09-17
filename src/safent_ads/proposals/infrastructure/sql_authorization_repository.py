"""Adaptador SQL de `AuthorizationRepository` sobre `approvals`
(0008_proposals).

`approvals` es SOLO-ANEXABLE por trigger: no admite UPDATE, DELETE ni
TRUNCATE. Revocar es anexar una decision `revoked`, nunca tocar la anterior
(data-model.md; threat-model.md C-19). Este adaptador solo inserta y lee.

Sin el, el chokepoint no puede verificar ninguna autorizacion contra la base
y toda ejecucion acaba en `authorization_not_found`: es la pieza que faltaba
para que la ruta de escritura corra sobre SQL en vez de sobre dobles.

Traducciones de frontera (el dominio nunca las ve):
- `approvals.rule_id` es el UUID de la regla y el esquema lo exige para toda
  `rule_authorization` (`approvals_rule_kind_check`). `Authorization` no
  lleva ese campo: la regla viaja en `issued_by` como CODIGO (`M05`), que es
  lo que firma `AuthorizeRuleAction`. Se resuelve por codigo, igual que hace
  `SqlProposalRepository` con la causa de la propuesta.
- La firma es `bytes` en el dominio y texto en la columna: viaja en
  hexadecimal, como el resto de digests del sistema."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.proposals.domain.authorization import (
    Authorization,
    AuthorizationChannel,
    AuthorizationDecision,
    AuthorizationKind,
)
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.infrastructure.errors import (
    AuthorizationProposalNotFoundError,
    CorruptAuthorizationError,
    DuplicateAuthorizationError,
    UnknownRuleCodeError,
)
from safent_ads.shared.managed_ads import binding_from_json, binding_to_json

__all__ = ["SqlAuthorizationRepository"]

_COLUMNS: Final = """
    id, proposal_id, kind, decision, diff_hash, guardrail_verdict_hash, issued_by, channel,
    signature, comment, decided_at, expires_at, managed_binding
"""

_GET_TEMPLATE: Final = "SELECT {columns} FROM approvals WHERE id = :id"
_GET: Final = _GET_TEMPLATE.format(columns=_COLUMNS)

# Ultima decision `approved` de la propuesta. Una revocacion posterior es
# otra fila; quien la consulte decide que hacer con ella, igual que en el
# doble en memoria.
_ACTIVE_TEMPLATE: Final = """
    SELECT {columns} FROM approvals
     WHERE proposal_id = :proposal_id AND decision = 'approved'
     ORDER BY decided_at DESC, id DESC
     LIMIT 1
"""
_ACTIVE: Final = _ACTIVE_TEMPLATE.format(columns=_COLUMNS)

# `rules.code` no es unico globalmente (lo es por ambito): gana la regla mas
# general que lo lleve, que es la que siembra `0007`.
_INSERT: Final = """
    INSERT INTO approvals (id, proposal_id, kind, decision, diff_hash, guardrail_verdict_hash,
                           issued_by, rule_id, channel, signature, comment, decided_at,
                           expires_at, managed_binding)
    SELECT :id, :proposal_id, :kind, :decision, :diff_hash, :guardrail_verdict_hash,
           :issued_by,
           CASE WHEN :kind = 'rule_authorization'
                THEN (SELECT rule.id FROM rules AS rule
                       WHERE rule.code = :issued_by
                       ORDER BY CASE rule.scope WHEN 'global' THEN 0 ELSE 1 END
                       LIMIT 1)
                ELSE NULL END,
           :channel, :signature, :comment, :decided_at, :expires_at, CAST(:managed_binding AS JSONB)
    RETURNING id
"""


class SqlAuthorizationRepository:
    """Implementa `proposals.application.ports.AuthorizationRepository`.

    Como `SqlProposalRepository`, vive dentro de la transaccion del
    `AsyncSession` que le pasan y no hace `commit()`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, authorization_id: AuthorizationId) -> Authorization | None:
        result = await self._session.execute(text(_GET), {"id": str(authorization_id)})
        row = result.mappings().one_or_none()
        return None if row is None else _to_authorization(row)

    async def get_active_for_proposal(self, proposal_id: ProposalId) -> Authorization | None:
        result = await self._session.execute(text(_ACTIVE), {"proposal_id": str(proposal_id)})
        row = result.mappings().one_or_none()
        return None if row is None else _to_authorization(row)

    async def save(self, authorization: Authorization) -> None:
        try:
            result = await self._session.execute(text(_INSERT), _params(authorization))
        except IntegrityError as exc:
            raise _translate(exc, authorization) from exc
        if result.first() is None:  # pragma: no cover - INSERT ... SELECT sin filas
            raise UnknownRuleCodeError(
                f"no se pudo anexar la autorizacion {authorization.authorization_id}"
            )


def _params(authorization: Authorization) -> dict[str, Any]:
    return {
        "managed_binding": binding_to_json(authorization.managed_binding),
        "id": str(authorization.authorization_id),
        "proposal_id": str(authorization.proposal_id),
        "kind": authorization.kind.value,
        "decision": authorization.decision.value,
        "diff_hash": authorization.diff_hash,
        "guardrail_verdict_hash": authorization.guardrail_verdict_hash,
        "issued_by": authorization.issued_by,
        "channel": authorization.channel.value,
        "signature": authorization.signature.hex(),
        "comment": authorization.comment,
        "decided_at": authorization.decided_at,
        "expires_at": authorization.expires_at,
    }


def _translate(exc: IntegrityError, authorization: Authorization) -> Exception:
    message = str(exc.orig)
    if "approvals_rule_kind_check" in message or "null value" in message:
        return UnknownRuleCodeError(
            f"{authorization.issued_by!r} no es una regla del catalogo: no puede autorizar"
        )
    # BUG corregido: una violacion de FOREIGN KEY (la propuesta referenciada
    # no existe todavia -- p. ej. `UndoExecution` guardando la autorizacion
    # antes que su propuesta compensatoria) no es lo mismo que un duplicado;
    # antes de este fix ambas caian en `DuplicateAuthorizationError`, un
    # diagnostico enganoso que apuntaba a un indice unico en vez del orden
    # de guardado real.
    if "foreign key" in message.lower() or "approvals_proposal_id_fkey" in message:
        return AuthorizationProposalNotFoundError(
            f"{authorization.proposal_id} no existe todavia: no se puede autorizar"
        )
    return DuplicateAuthorizationError(
        f"ya hay una autorizacion viva para {authorization.proposal_id} con ese diff"
    )


def _to_authorization(row: RowMapping) -> Authorization:
    return Authorization(
        managed_binding=binding_from_json(row["managed_binding"]),
        authorization_id=AuthorizationId(uuid.UUID(str(row["id"]))),
        proposal_id=ProposalId(uuid.UUID(str(row["proposal_id"]))),
        kind=AuthorizationKind(str(row["kind"])),
        decision=AuthorizationDecision(str(row["decision"])),
        diff_hash=str(row["diff_hash"]),
        guardrail_verdict_hash=str(row["guardrail_verdict_hash"]),
        issued_by=str(row["issued_by"]),
        channel=AuthorizationChannel(str(row["channel"])),
        signature=_signature(str(row["signature"])),
        decided_at=row["decided_at"],
        expires_at=row["expires_at"],
        comment=None if row["comment"] is None else str(row["comment"]),
    )


def _signature(raw: str) -> bytes:
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise CorruptAuthorizationError(
            "la firma almacenada no es hexadecimal: no se puede verificar"
        ) from exc
