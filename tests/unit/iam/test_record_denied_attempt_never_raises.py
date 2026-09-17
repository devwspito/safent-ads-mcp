"""`_record_denied_attempt` (iam/presentation/federated_router.py, M-1
revision de seguridad 17-sep): se llama DENTRO del `except` de
`_handle_callback` -- sin su propio `try`, un fallo del registro del
intento (Postgres caido a medias) escaparia por encima de ese `except` y
aterrizaria en el manejador generico (500), la ultima grieta de "nunca un
500" en el tramo federado (threat-model.md C-75). El registro es
auditoria de mejor esfuerzo: si falla, se loguea y se sigue."""

from __future__ import annotations

from typing import Any

import structlog.testing
from starlette.requests import Request

from safent_ads.iam.application.errors import (
    FederatedDenialReason,
    FederatedIdentityNotAuthorizedError,
)
from safent_ads.iam.domain.email import Email
from safent_ads.iam.presentation.federated_router import _record_denied_attempt


class _BrokenSessionFactory:
    """Simula `container.session_factory()` con Postgres caido a medias:
    el `async with` nunca llega a producir una sesion."""

    def __call__(self) -> _BrokenSessionFactory:
        return self

    async def __aenter__(self) -> None:
        raise RuntimeError("Postgres no responde")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _request(*, peer_ip: str) -> Request:
    scope = {"type": "http", "headers": [], "client": (peer_ip, 1)}
    return Request(scope)


async def test_a_broken_session_factory_never_escapes_as_an_exception() -> None:
    container: Any = type("FakeContainer", (), {"session_factory": _BrokenSessionFactory()})()
    request = _request(peer_ip="203.0.113.9")
    exc = FederatedIdentityNotAuthorizedError(
        FederatedDenialReason.EMAIL_NOT_ALLOWED, Email("intruso@example.com")
    )

    # No debe lanzar -- si lo hace, el test falla con la excepcion sin
    # capturar, exactamente el escape que este arreglo cierra.
    await _record_denied_attempt(container, request, exc, trusted_proxy_hops=0)


async def test_the_failure_is_logged_without_the_claimed_email_or_the_message() -> None:
    container: Any = type("FakeContainer", (), {"session_factory": _BrokenSessionFactory()})()
    request = _request(peer_ip="203.0.113.9")
    claimed_email = "intruso@example.com"
    exc = FederatedIdentityNotAuthorizedError(
        FederatedDenialReason.EMAIL_NOT_ALLOWED, Email(claimed_email)
    )

    with structlog.testing.capture_logs() as logs:
        await _record_denied_attempt(container, request, exc, trusted_proxy_hops=0)

    (entry,) = [log for log in logs if log["event"] == "federated_denied_attempt_not_recorded"]
    assert entry["log_level"] == "warning"
    assert entry["error_type"] == "RuntimeError"
    assert claimed_email not in repr(entry)
    assert "Postgres no responde" not in repr(entry)
