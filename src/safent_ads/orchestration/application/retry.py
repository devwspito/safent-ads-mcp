"""Reintento con espera creciente (plan.md §7: "retries con backoff") para
un unico paso de ciclo. `sleep` es inyectable para que los tests no esperen
tiempo real."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_SECONDS = 1.0

Sleep = Callable[[float], Awaitable[None]]


async def run_with_retry(
    operation: Callable[[], Awaitable[None]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
    sleep: Sleep = asyncio.sleep,
) -> int:
    """Devuelve el numero de intentos realizados. Relanza la ultima
    excepcion si se agotan los reintentos: el llamador decide como
    registrar el fallo (plan.md: "nunca exito parcial silencioso")."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await operation()
        except Exception as exc:  # noqa: BLE001 - limite del ciclo: cualquier fallo se reintenta
            last_error = exc
            if attempt < max_attempts:
                await sleep(base_delay_seconds * attempt)
            continue
        else:
            return attempt
    if last_error is None:  # pragma: no cover - inalcanzable: el bucle siempre asigna o retorna
        raise RuntimeError("run_with_retry: estado inalcanzable")
    raise last_error
