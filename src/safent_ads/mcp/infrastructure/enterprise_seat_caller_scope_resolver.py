"""`EnterpriseSeatCallerScopeResolver` (004 tasks.md A4): implementa
`CallerScopeResolverPort` sobre `SeatAuthorityPort`. Sustituye a
`StaticCallerScopeResolver` (borrado en A1): sin el nunca hay alcance por
defecto -- una admision sin negocio produce alcance VACIO, nunca total.

004 tasks-2.md Q2 (contracts/mcp.md §6): 120 introspecciones/min por
persona, contadas AQUI, antes de la llamada HTTP a Enterprise -- superado
el cupo, `SeatAuthorityPort.resolve` ni se llama. La clave del contador es
el digest sha256 de la credencial, nunca la credencial ni el `user_id`
(que todavia no se conoce en este punto)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from safent_ads.mcp.application.caller_scope import CallerScope, IntrospectionQuotaExceededError
from safent_ads.mcp.application.seat_authority import SeatAuthorityPort
from safent_ads.shared.clock import Clock, SystemClock

_PERSON_CALLER_PREFIX = "person:"
_INTROSPECTION_WINDOW_SECONDS = 60
_DEFAULT_INTROSPECTION_LIMIT_PER_MINUTE = 120


@dataclass(slots=True)
class _IntrospectionWindow:
    started_at: float
    count: int


class EnterpriseSeatCallerScopeResolver:
    def __init__(
        self,
        seat_authority: SeatAuthorityPort,
        *,
        clock: Clock | None = None,
        introspection_limit_per_minute: int = _DEFAULT_INTROSPECTION_LIMIT_PER_MINUTE,
    ) -> None:
        self._seat_authority = seat_authority
        self._clock = clock or SystemClock()
        self._introspection_limit = introspection_limit_per_minute
        self._introspection_windows: dict[str, _IntrospectionWindow] = {}

    async def aclose(self) -> None:
        aclose = getattr(self._seat_authority, "aclose", None)
        if aclose is not None:
            await aclose()

    async def resolve(self, bearer_token: str) -> CallerScope:
        self._consume_introspection_quota(bearer_token)
        admission = await self._seat_authority.resolve(bearer_token)
        return CallerScope(
            caller_id=f"{_PERSON_CALLER_PREFIX}{admission.user_id}",
            allowed_business_ids=frozenset({admission.business_id})
            if admission.business_id
            else frozenset(),
            permission=admission.permission,
            person_label=admission.person_label,
        )

    def _consume_introspection_quota(self, bearer_token: str) -> None:
        key = hashlib.sha256(bearer_token.encode()).hexdigest()
        now = self._clock.now().timestamp()
        window = self._introspection_windows.get(key)
        if window is None or now - window.started_at >= _INTROSPECTION_WINDOW_SECONDS:
            self._introspection_windows[key] = _IntrospectionWindow(started_at=now, count=1)
            return
        if window.count >= self._introspection_limit:
            elapsed = now - window.started_at
            retry_after = max(1, round(_INTROSPECTION_WINDOW_SECONDS - elapsed))
            raise IntrospectionQuotaExceededError(retry_after)
        window.count += 1
