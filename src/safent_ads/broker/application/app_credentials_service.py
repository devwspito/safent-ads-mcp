"""`AppCredentialsService`: alta/borrado/estado de las credenciales de
VENDOR (Google OAuth client, Meta app), tecleadas por el
propietario desde el panel (owner decision: "cada usuario puede tener
datos diferentes", nunca desde `vendor.env`). Detras de los tres `op`
nuevos del socket -- `set_platform_app_credentials`/`get_platform_app_status`/
`delete_platform_app_credentials`, solo alcanzables por el uid de `ads-api`
(`ADS_BROKER_ALLOWED_UIDS`, `broker/presentation/socket_server.py`).

`status()` nunca devuelve el secreto: solo los ultimos 4 caracteres de
`client_id`/`login_customer_id`, la misma politica de enmascarado que
`GET /platform-apps` expone al panel (contracts/rest-api.md)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from safent_ads.broker.application.errors import AppCredentialsIncompleteError
from safent_ads.broker.application.ports import (
    AppCredentialsStorePort,
    GoogleAppCredentials,
    MetaAppCredentials,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode

_MASK_VISIBLE_CHARS = 4
_MASK_PREFIX = "****"


@dataclass(frozen=True, slots=True, kw_only=True)
class AppCredentialsStatus:
    platform: PlatformCode
    configured: bool
    client_id_masked: str | None
    login_customer_id_masked: str | None
    updated_at: datetime | None
    client_type: Literal["web", "desktop"] | None = None


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return f"{_MASK_PREFIX}{value[-_MASK_VISIBLE_CHARS:]}"


def _google_status(credentials: GoogleAppCredentials | None) -> AppCredentialsStatus:
    if credentials is None:
        return AppCredentialsStatus(
            platform=PlatformCode.GOOGLE,
            configured=False,
            client_id_masked=None,
            login_customer_id_masked=None,
            updated_at=None,
        )
    return AppCredentialsStatus(
        platform=PlatformCode.GOOGLE,
        configured=True,
        client_id_masked=_mask(credentials.client_id),
        login_customer_id_masked=_mask(credentials.login_customer_id),
        updated_at=credentials.updated_at,
        client_type=credentials.client_type,
    )


def _meta_status(credentials: MetaAppCredentials | None) -> AppCredentialsStatus:
    if credentials is None:
        return AppCredentialsStatus(
            platform=PlatformCode.META,
            configured=False,
            client_id_masked=None,
            login_customer_id_masked=None,
            updated_at=None,
        )
    return AppCredentialsStatus(
        platform=PlatformCode.META,
        configured=True,
        client_id_masked=_mask(credentials.app_id),
        login_customer_id_masked=None,
        updated_at=credentials.updated_at,
    )


class AppCredentialsService:
    def __init__(
        self,
        store: AppCredentialsStorePort,
        clock: Clock,
        *,
        managed_platforms: frozenset[PlatformCode]
        | Callable[[], frozenset[PlatformCode]] = frozenset(),
        managed_required: bool = False,
    ) -> None:
        self._store = store
        self._clock = clock
        self._managed_platforms = managed_platforms
        self._managed_required = managed_required

    def set_google(
        self,
        *,
        client_id: str,
        client_secret: str = "",
        login_customer_id: str | None,
        client_type: Literal["web", "desktop"] = "web",
    ) -> AppCredentialsStatus:
        if (
            not client_id
            or client_type not in {"web", "desktop"}
            or (client_type == "web" and not client_secret)
            or (client_type == "desktop" and bool(client_secret))
        ):
            raise AppCredentialsIncompleteError("google")
        self._store.save_google_app_credentials(
            GoogleAppCredentials(
                client_id=client_id,
                client_secret=client_secret,
                login_customer_id=login_customer_id,
                updated_at=self._clock.now(),
                client_type=client_type,
            )
        )
        return self.status(PlatformCode.GOOGLE)

    def set_meta(self, *, app_id: str, app_secret: str) -> AppCredentialsStatus:
        if not (app_id and app_secret):
            raise AppCredentialsIncompleteError("meta")
        self._store.save_meta_app_credentials(
            MetaAppCredentials(app_id=app_id, app_secret=app_secret, updated_at=self._clock.now())
        )
        return self.status(PlatformCode.META)

    def delete(self, platform: PlatformCode) -> None:
        self._store.delete_app_credentials(platform)

    def status(self, platform: PlatformCode) -> AppCredentialsStatus:
        managed = (
            self._managed_platforms()
            if callable(self._managed_platforms)
            else self._managed_platforms
        )
        if platform in managed or self._managed_required:
            return AppCredentialsStatus(
                platform=platform,
                configured=platform in managed,
                client_id_masked=None,
                login_customer_id_masked=None,
                updated_at=None,
            )
        if platform == PlatformCode.GOOGLE:
            return _google_status(self._store.get_google_app_credentials())
        return _meta_status(self._store.get_meta_app_credentials())
