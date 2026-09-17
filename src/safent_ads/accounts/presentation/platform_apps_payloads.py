"""DTOs de `/api/v1/platform-apps/*` (contracts/rest-api.md §Conexiones,
owner decision app-credentials-ui). Aquí vive la ÚNICA validación real de
forma/longitud de las credenciales de VENDOR -- el bróker (defensa en
profundidad, `AppCredentialsIncompleteError`) solo comprueba que los
campos obligatorios no lleguen vacíos. Ningún modelo de salida tiene un
campo capaz de llevar el secreto: solo los últimos 4 caracteres que ya
enmascaró el bróker."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from safent_ads.accounts.application.platform_apps_ports import PlatformAppStatus
from safent_ads.accounts.presentation.payloads import StrictModel
from safent_ads.shared.ids import PlatformCode

_MAX_SECRET_LENGTH = 512
_MAX_CUSTOMER_ID_LENGTH = 20
_MAX_APP_ID_LENGTH = 64

# `*.apps.googleusercontent.com`: forma fija que Google Cloud Console emite
# para los clientes OAuth web y de escritorio.
_GOOGLE_CLIENT_ID_PATTERN = re.compile(r"^[\w-]+\.apps\.googleusercontent\.com$")
# `login_customer_id`/`app_id`: el customer id de la MCC de Google es
# siempre numerico (guiones opcionales de formato se aceptan y se ignoran
# aqui -- el propietario copia el id tal cual lo muestra la consola).
_NUMERIC_ID_PATTERN = re.compile(rf"[0-9]{{1,{_MAX_CUSTOMER_ID_LENGTH}}}")
_FORMATTED_CUSTOMER_ID_PATTERN = re.compile(r"[0-9]{3}-[0-9]{3}-[0-9]{4}")


class SetGoogleAppCredentialsRequest(StrictModel):
    client_id: str = Field(min_length=1, max_length=_MAX_SECRET_LENGTH)
    client_secret: str = Field(default="", max_length=_MAX_SECRET_LENGTH)
    client_type: Literal["web", "desktop"] = "web"
    login_customer_id: str | None = Field(default=None, max_length=_MAX_CUSTOMER_ID_LENGTH)

    @model_validator(mode="after")
    def _validate_client_kind(self) -> Self:
        if self.client_type == "web" and not self.client_secret:
            raise ValueError("El cliente web requiere client_secret")
        if self.client_type == "desktop" and self.client_secret:
            raise ValueError("El cliente de escritorio no usa client_secret")
        return self

    @field_validator("client_id")
    @classmethod
    def _validate_client_id_shape(cls, value: str) -> str:
        if not _GOOGLE_CLIENT_ID_PATTERN.match(value):
            raise ValueError("client_id debe terminar en .apps.googleusercontent.com")
        return value

    @field_validator("login_customer_id")
    @classmethod
    def _validate_login_customer_id(cls, value: str | None) -> str | None:
        if value is not None and _FORMATTED_CUSTOMER_ID_PATTERN.fullmatch(value):
            return value.replace("-", "")
        if value is not None and not _NUMERIC_ID_PATTERN.fullmatch(value):
            raise ValueError("login_customer_id debe ser numerico")
        return value


class SetMetaAppCredentialsRequest(StrictModel):
    app_id: str = Field(min_length=1, max_length=_MAX_APP_ID_LENGTH)
    app_secret: str = Field(min_length=1, max_length=_MAX_SECRET_LENGTH)


class PlatformAppStatusResponse(StrictModel):
    platform: PlatformCode
    configured: bool
    client_id_masked: str | None
    login_customer_id_masked: str | None
    redirect_uri: str
    updated_at: datetime | None
    client_type: Literal["web", "desktop"] | None = None

    @classmethod
    def from_status(
        cls, status: PlatformAppStatus, *, redirect_uri: str
    ) -> PlatformAppStatusResponse:
        return cls(
            platform=status.platform,
            configured=status.configured,
            client_id_masked=status.client_id_masked,
            login_customer_id_masked=status.login_customer_id_masked,
            redirect_uri=redirect_uri,
            updated_at=status.updated_at,
            client_type=status.client_type,
        )


class PlatformAppsResponse(StrictModel):
    items: list[PlatformAppStatusResponse]
