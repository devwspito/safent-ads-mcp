"""`RefreshGrant` (contracts/oauth.md §6, threat-model.md C-43): rota el
par de tokens de una concesion viva. Presentar un refresh ya rotado
revoca la concesion entera (deteccion de reuso); el alcance nunca crece.

L1 de la revision de seguridad (16-sep): el refresco SIEMPRE emite y
reporta el alcance COMPLETO de la concesion -- nunca lo estrecha al
`scope` que el cliente pida en `/token`. RFC 6749 SS6 permite estrechar,
pero es opcional y, sin ningun cliente propio (Claude Code, Codex) que lo
use, es superficie sin beneficio: un `scope` solicitado que NO es
subconjunto de la concesion sigue siendo `invalid_scope`
(`ensure_scope_within_grant`, la misma comprobacion de siempre), pero uno
que si lo es ya no reduce nada -- simplemente se ignora."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.mcp_oauth.application.errors import (
    ClientMismatchError,
    InvalidTargetError,
    UnknownRefreshTokenError,
)
from safent_ads.mcp_oauth.application.ports import GrantRepository, OpaqueTokenFactory, TokenHasher
from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.errors import RefreshTokenReusedError
from safent_ads.mcp_oauth.domain.grant import Grant, TokenHash
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshedTokenPair:
    access_token: str
    refresh_token: str
    scope_set: ScopeSet


class RefreshGrant:
    def __init__(
        self,
        *,
        grants: GrantRepository,
        token_hasher: TokenHasher,
        token_factory: OpaqueTokenFactory,
        clock: Clock,
    ) -> None:
        self._grants = grants
        self._token_hasher = token_hasher
        self._token_factory = token_factory
        self._clock = clock

    async def execute(
        self, *, refresh_token: str, client_id: str, resource: str, requested_scope: str | None
    ) -> RefreshedTokenPair:
        token_hash = self._token_hasher.hash(refresh_token)
        grant = await self._find_grant(token_hash)
        self._validate_binding(grant, client_id, resource)
        self._reject_scope_outside_grant(grant, requested_scope)

        now = self._clock.now()
        access, refresh = mint_access_and_refresh(
            now=now, factory=self._token_factory, hasher=self._token_hasher
        )
        try:
            grant.rotate_refresh_token(
                presented_hash=token_hash,
                new_access=access.issued,
                new_refresh=refresh.issued,
                now=now,
            )
        except RefreshTokenReusedError:
            # Nit 10 (revision de seguridad, 16-sep): `finally: save()`
            # persistia CUALQUIER excepcion, incluida una que nunca mutase
            # `grant` (`GrantRevokedError`/`TokenExpiredError`, ambas antes
            # de tocar `_tokens`) -- una escritura de mas sin motivo. Solo
            # el reuso muta el agregado (`revoke()`) antes de fallar, y ese
            # efecto SI es obligatorio persistirlo aunque la llamada
            # termine en error.
            await self._grants.save(grant)
            raise
        await self._grants.save(grant)

        return RefreshedTokenPair(
            access_token=access.raw_value,
            refresh_token=refresh.raw_value,
            scope_set=grant.scope_set,
        )

    async def _find_grant(self, token_hash: TokenHash) -> Grant:
        # fix/refresh-rotation-race: `get_by_token_hash_for_rotation`, no
        # el `get_by_token_hash` liso -- reserva la fila del refresh
        # ANTES de leerla, para que dos rotaciones concurrentes del mismo
        # token nunca se relean a medio camino la una a la otra.
        grant = await self._grants.get_by_token_hash_for_rotation(token_hash)
        if grant is None:
            raise UnknownRefreshTokenError("refresh token desconocido")
        return grant

    @staticmethod
    def _validate_binding(grant: Grant, client_id: str, resource: str) -> None:
        if grant.client_id != client_id:
            raise ClientMismatchError("client_id no coincide con la concesion")
        if grant.resource.value != resource:
            raise InvalidTargetError(f"resource no coincide con el canonico: {resource!r}")

    @staticmethod
    def _reject_scope_outside_grant(grant: Grant, requested_scope: str | None) -> None:
        if requested_scope is None:
            return
        grant.ensure_scope_within_grant(ScopeSet.parse(requested_scope))
