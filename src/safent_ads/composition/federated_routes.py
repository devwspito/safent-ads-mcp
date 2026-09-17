"""Composition helper for the federated login surface (spec 002b tasks.md
T032/T036). Kept apart from `composition/api.py`/`composition/app.py` --
the busiest files of the lane/003 merge (plan.md "Riesgos de integracion")
-- so it can be unit-tested before the one-line wiring those two files
still need (tasks.md T033, left to the parent of this lane).

Registration-level gating (research.md Decision F): with the switch off,
`build_federated_router` is never even constructed. The three routes
answer 404 by routing, never by a branch inside a handler. With the
switch on but the configuration incomplete, the service still starts
(FR-105) and only a diagnostic event -- never a value -- says why the
routes stayed off."""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI

from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.application.errors import (
    ConsentTransactionNotFoundError,
    ConsentTransactionNotOpenError,
)
from safent_ads.iam.application.ports import AuthorizedEmailList
from safent_ads.iam.infrastructure.google_oidc_provider import (
    GoogleOidcConfig,
    GoogleOidcProvider,
)
from safent_ads.iam.presentation.federated_router import build_federated_router
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequestState
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)

logger = structlog.get_logger(__name__)

_CALLBACK_PATH = "/api/v1/auth/federated/callback"


class _SqlOpenConsentTransactions:
    """T085 (plan.md "mcp_oauth -> iam, nunca al reves"): implementacion
    REAL de `iam.application.ports.OpenConsentTransactions`. Vive aqui, no
    en `iam/infrastructure/`, porque `composition/` es el UNICO punto que
    puede conocer tanto `iam` como `mcp_oauth` a la vez -- la fila que lee
    es una `AuthorizationRequest` de `mcp_oauth`, y `iam` no debe saberlo."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def require_open(self, txn_id: uuid.UUID) -> None:
        async with self._container.session_factory() as db_session:
            repo = SqlAuthorizationRequestRepository(db_session, clock=self._container.clock)
            authorization_request = await repo.get_by_id(txn_id)
            if authorization_request is None:
                raise ConsentTransactionNotFoundError(f"solicitud {txn_id} no encontrada")
            now = self._container.clock.now()
            if (
                authorization_request.state is not AuthorizationRequestState.PENDING
                or authorization_request.is_expired(now)
            ):
                raise ConsentTransactionNotOpenError(f"solicitud {txn_id} ya no esta abierta")


def register_federated_login_routes(
    app: FastAPI, settings: ApiSettings, container: Container
) -> None:
    """Additive: mounts `/api/v1/auth/federated/*` only when the switch is
    on AND the rest of the configuration is complete."""
    if not settings.federated_login_enabled:
        return
    if not settings.federated_login_active:
        logger.info("federated_login_disabled_incomplete_config")
        return
    redirect_uri = f"{settings.public_base_url}{_CALLBACK_PATH}"
    provider = _build_provider(settings, container)
    if provider is None:
        logger.info("federated_login_disabled_incomplete_config")
        return
    allowed_emails = AuthorizedEmailList.from_raw(settings.federated_allowed_emails)
    app.include_router(
        build_federated_router(
            provider=provider,
            redirect_uri=redirect_uri,
            allowed_emails=allowed_emails,
            open_consent_transactions=_SqlOpenConsentTransactions(container),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    )
    logger.info("federated_login_ready", redirect_uri=redirect_uri)


def _build_provider(settings: ApiSettings, container: Container) -> GoogleOidcProvider | None:
    """`settings.federated_login_active` already guarantees both fields are
    set and non-empty; this re-checks them so mypy narrows the optional
    types instead of asserting past them -- if the invariant ever drifted,
    this fails closed (routes stay unregistered) instead of constructing a
    provider with an empty secret."""
    client_id = settings.google_oidc_client_id
    client_secret = settings.google_oidc_client_secret
    if not client_id or client_secret is None:
        return None
    return GoogleOidcProvider(
        GoogleOidcConfig(client_id=client_id, client_secret=client_secret.get_secret_value()),
        container.clock,
    )
