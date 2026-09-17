"""DTOs de `/api/v1/auth/*` (contracts/rest-api.md). Sin logica: solo forma
y validacion de entrada."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from safent_ads.iam.domain.session import SessionOrigin


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class BusinessSummary(BaseModel):
    business_id: uuid.UUID
    slug: str
    name: str


class SessionSummary(BaseModel):
    """contracts/federated-login.md §2 `GET /auth/me`: `origin` es
    auditoria (nunca autoridad, ver `SessionOrigin`); `fresh_identification_
    until` es el instante DERIVADO (`last_federated_auth_at` + ventana), una
    pista para la interfaz -- el servidor vuelve a comprobarlo en cada
    accion sensible (`require_fresh_identification`)."""

    origin: SessionOrigin
    fresh_identification_until: datetime | None


class MeResponse(BaseModel):
    owner_id: uuid.UUID
    email: str
    businesses: list[BusinessSummary]
    session: SessionSummary
    federated_login_available: bool


class ExchangeRequest(BaseModel):
    assertion: str = Field(min_length=1, max_length=8192)


class ExchangeResponse(BaseModel):
    owner_id: uuid.UUID
    business_id: uuid.UUID | None
    expires_at: datetime
    surface: str = "safent_cockpit"


class FederatedStatusResponse(BaseModel):
    available: bool


class FederatedStartRequest(BaseModel):
    txn_id: uuid.UUID | None = None


class FederatedStartResponse(BaseModel):
    authorization_url: str
    expires_at: datetime
