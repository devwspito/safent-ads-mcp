"""`SqlPackageAuthorizationRepository`: implementa `PackageAuthorizationRepository`
sobre `approvals` (migracion 0044: `subject_kind`/`subject_id`,
`proposal_id` NULL-able). Adaptador PROPIO -- no reutiliza
`proposals.infrastructure.sql_authorization_repository.SqlAuthorizationRepository`,
que asume `proposal_id` obligatorio y no conoce `subject` todavia (fuera
del alcance de esta lane); insertar directamente aqui evita tocar esa
infraestructura ajena para soportar un sujeto que no es una `Proposal`."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.proposals.domain.authorization import Authorization
from safent_ads.shared.managed_ads import binding_to_json

__all__ = ["SqlPackageAuthorizationRepository"]

_INSERT_SQL = text("""
    INSERT INTO approvals
        (id, proposal_id, kind, decision, diff_hash, guardrail_verdict_hash, issued_by,
         channel, signature, comment, decided_at, expires_at, managed_binding,
         subject_kind, subject_id)
    VALUES
        (:id, NULL, :kind, :decision, :diff_hash, :guardrail_verdict_hash, :issued_by,
         :channel, :signature, :comment, :decided_at, :expires_at,
         CAST(:managed_binding AS JSONB), :subject_kind, :subject_id)
""")


class SqlPackageAuthorizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, authorization: Authorization) -> None:
        subject = authorization.subject
        if subject is None:
            raise ValueError("SqlPackageAuthorizationRepository solo persiste subject de paquete")
        await self._session.execute(
            _INSERT_SQL,
            {
                "id": str(authorization.authorization_id),
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
                "managed_binding": (
                    None
                    if authorization.managed_binding is None
                    else binding_to_json(authorization.managed_binding)
                ),
                "subject_kind": subject.kind.value,
                "subject_id": subject.id,
            },
        )
