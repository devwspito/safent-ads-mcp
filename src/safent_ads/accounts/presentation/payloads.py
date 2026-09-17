"""DTOs de `/api/v1/platform-accounts/*` (contracts/rest-api.md
§Conexiones). Sin logica: solo forma y validacion de entrada. Ninguno de
estos modelos tiene un campo capaz de llevar un token -- ni de entrada
(`MetaSystemUserTokenRequest.token` se valida contra el proveedor y se
descarta, nunca se guarda ni se devuelve) ni de salida."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from safent_ads.accounts.application.begin_oauth_connect import BeginOAuthConnectResult
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.shared.ids import PlatformCode

_MAX_PASTED_TOKEN_LENGTH = 4096


class StrictModel(BaseModel):
    """Base comun: rechaza campos no declarados (threat-model.md C-11)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BeginConnectResponse(StrictModel):
    session_id: uuid.UUID
    authorize_url: str
    expires_at: datetime

    @classmethod
    def from_result(cls, result: BeginOAuthConnectResult) -> BeginConnectResponse:
        return cls(
            session_id=result.session_id,
            authorize_url=result.authorize_url,
            expires_at=result.expires_at,
        )


class BeginConnectRequest(StrictModel):
    google_customer_id: str | None = Field(default=None, max_length=32)

    @field_validator("google_customer_id")
    @classmethod
    def _normalize_customer_id(cls, value: str | None) -> str | None:
        # The route supplies the provider and rejects this field for Meta.
        return normalize_google_customer_id(value, provider=PlatformCode.GOOGLE)


class ConnectStatusResponse(StrictModel):
    state: str
    error_code: str | None
    message: str | None

    @classmethod
    def from_session(
        cls, session: OAuthConnectSession, *, message: str | None
    ) -> ConnectStatusResponse:
        return cls(state=session.status.value, error_code=session.error_code, message=message)


class ConnectedAccountSummary(StrictModel):
    platform: PlatformCode
    external_account_id: str
    label: str
    currency: str
    timezone: str
    api_tier: ApiTier

    @classmethod
    def from_platform_account(cls, account: PlatformAccount) -> ConnectedAccountSummary:
        # `platform_accounts.label` no existe todavia (list_platform_accounts.py:
        # "hasta entonces, la referencia legible es el propio AccountRef").
        return cls(
            platform=account.account_ref.platform,
            external_account_id=account.account_ref.external_account_id,
            label=str(account.account_ref),
            currency=account.currency,
            timezone=account.timezone,
            api_tier=account.api_tier,
        )


class ConnectedAccountsResponse(StrictModel):
    accounts: list[ConnectedAccountSummary]


class MetaSystemUserTokenRequest(StrictModel):
    token: str = Field(min_length=1, max_length=_MAX_PASTED_TOKEN_LENGTH)
