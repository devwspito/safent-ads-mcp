"""Repositorios SQL del flujo OAuth "Conectar" sobre `credential_refs`
(columnas de salud, migracion 0013) y `oauth_connect_sessions` (migracion
0013). Mismo patron que `sql_repositories.py`: SQL de mano, agregados
entran y salen, nunca una fila de SQLAlchemy.

`credential_refs` no tiene columna `business_id` (0001_bootstrap: es
account-agnostic, resuelta desde `platform_accounts.credential_ref_id`) --
`SqlCredentialRepository.get` recibe `business_id` del llamante, que ya lo
conoce por el `PlatformAccount` propietario, en vez de intentar un JOIN
que solo funcionaria despues de que la cuenta exista."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.domain.platform_credential import CredentialStatus, PlatformCredential
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import BusinessId, PlatformCode

__all__ = ["SqlCredentialRepository", "SqlOAuthConnectSessionRepository"]

_SELECT_CREDENTIAL: Final = """
    SELECT id, platform, alias, scopes, status, obtained_at, expires_at,
           last_validated_at, revoked_at, checked_at, last_error_code
      FROM credential_refs
     WHERE id = :id
"""

_UPSERT_CREDENTIAL: Final = """
    INSERT INTO credential_refs (id, platform, alias, scopes, status, obtained_at, expires_at,
                                 last_validated_at, revoked_at, checked_at, last_error_code)
    VALUES (:id, :platform, :alias, :scopes, :status, :obtained_at, :expires_at,
            :last_validated_at, :revoked_at, :checked_at, :last_error_code)
    ON CONFLICT (id) DO UPDATE
        SET status            = EXCLUDED.status,
            scopes             = EXCLUDED.scopes,
            obtained_at        = EXCLUDED.obtained_at,
            expires_at         = EXCLUDED.expires_at,
            last_validated_at  = EXCLUDED.last_validated_at,
            revoked_at         = EXCLUDED.revoked_at,
            checked_at         = EXCLUDED.checked_at,
            last_error_code    = EXCLUDED.last_error_code,
            rotated_at         = now()
"""

_SELECT_SESSION_BY_ID: Final = """
    SELECT id, business_id, owner_id, provider, state_hash, status, error_code,
           expires_at, completed_at, connection_id
      FROM oauth_connect_sessions
     WHERE id = :id
"""

_SELECT_SESSION_BY_STATE_HASH: Final = """
    SELECT id, business_id, owner_id, provider, state_hash, status, error_code,
           expires_at, completed_at, connection_id
      FROM oauth_connect_sessions
     WHERE state_hash = :state_hash
"""

_UPSERT_SESSION: Final = """
    INSERT INTO oauth_connect_sessions (id, business_id, owner_id, provider, state_hash, status,
                                        error_code, expires_at, completed_at, connection_id)
    VALUES (:id, :business_id, :owner_id, :provider, :state_hash, :status, :error_code,
            :expires_at, :completed_at, :connection_id)
    ON CONFLICT (id) DO UPDATE
        SET status       = EXCLUDED.status,
            error_code    = EXCLUDED.error_code,
            completed_at  = EXCLUDED.completed_at
"""


class SqlCredentialRepository:
    """`CredentialRepository` (connect_ports.py) sobre `credential_refs`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, credential_ref_id: CredentialRefId, *, business_id: BusinessId
    ) -> PlatformCredential | None:
        result = await self._session.execute(
            text(_SELECT_CREDENTIAL), {"id": credential_ref_id.value}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_credential(row, business_id)

    async def save(self, credential: PlatformCredential) -> None:
        await self._session.execute(text(_UPSERT_CREDENTIAL), _credential_params(credential))
        await self._session.flush()


class SqlOAuthConnectSessionRepository:
    """`OAuthConnectSessionRepository` (connect_ports.py) sobre
    `oauth_connect_sessions`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, session: OAuthConnectSession) -> None:
        if session.connection_id is not None:
            await self._session.execute(
                text("""
                INSERT INTO platform_connections(id,business_id,owner_id,platform)
                VALUES(:id,:business,:owner,:platform) ON CONFLICT(id) DO NOTHING
            """),
                {
                    "id": session.connection_id,
                    "business": session.business_id.value,
                    "owner": session.owner_id,
                    "platform": session.provider.value,
                },
            )
        await self._session.execute(text(_UPSERT_SESSION), _session_params(session))
        await self._session.flush()

    async def get_by_id(self, session_id: Any) -> OAuthConnectSession | None:  # noqa: ANN401 - uuid.UUID
        result = await self._session.execute(text(_SELECT_SESSION_BY_ID), {"id": session_id})
        row = result.mappings().one_or_none()
        return None if row is None else _to_session(row)

    async def get_by_state_hash(self, state_hash: str) -> OAuthConnectSession | None:
        result = await self._session.execute(
            text(_SELECT_SESSION_BY_STATE_HASH + " FOR UPDATE"), {"state_hash": state_hash}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_session(row)


def _credential_params(credential: PlatformCredential) -> dict[str, Any]:
    return {
        "id": credential.credential_ref_id.value,
        "platform": credential.platform.value,
        "alias": credential.alias,
        "scopes": list(credential.scopes),
        "status": credential.status.value.upper(),
        "obtained_at": credential.obtained_at,
        "expires_at": credential.expires_at,
        "last_validated_at": credential.last_validated_at,
        "revoked_at": credential.revoked_at,
        "checked_at": credential.checked_at,
        "last_error_code": credential.last_error_code,
    }


def _to_credential(row: Any, business_id: BusinessId) -> PlatformCredential:  # noqa: ANN401 - RowMapping
    return PlatformCredential(
        credential_ref_id=CredentialRefId(row["id"]),
        business_id=business_id,
        platform=PlatformCode(row["platform"]),
        alias=row["alias"],
        scopes=frozenset(row["scopes"]),
        status=CredentialStatus(row["status"].lower()),
        obtained_at=row["obtained_at"],
        expires_at=row["expires_at"],
        last_validated_at=row["last_validated_at"],
        revoked_at=row["revoked_at"],
        checked_at=row["checked_at"],
        last_error_code=row["last_error_code"],
    )


def _session_params(session: OAuthConnectSession) -> dict[str, Any]:
    return {
        "connection_id": session.connection_id,
        "id": session.session_id,
        "business_id": session.business_id.value,
        "owner_id": session.owner_id,
        "provider": session.provider.value,
        "state_hash": session.state_hash,
        "status": session.status.value,
        "error_code": session.error_code,
        "expires_at": session.expires_at,
        "completed_at": session.completed_at,
    }


def _to_session(row: Any) -> OAuthConnectSession:  # noqa: ANN401 - RowMapping
    return OAuthConnectSession(
        connection_id=row["connection_id"],
        session_id=row["id"],
        business_id=BusinessId(row["business_id"]),
        owner_id=row["owner_id"],
        provider=PlatformCode(row["provider"]),
        state_hash=row["state_hash"],
        status=OAuthSessionStatus(row["status"]),
        error_code=row["error_code"],
        expires_at=row["expires_at"],
        completed_at=row["completed_at"],
    )
