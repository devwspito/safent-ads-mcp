"""`AppCredentialsService`: alta/borrado/estado de credenciales de VENDOR
(owner decision, app-credentials-ui) -- enmascarado, nunca el secreto,
y validacion minima de campos obligatorios por plataforma."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.errors import AppCredentialsIncompleteError
from safent_ads.broker.application.ports import GoogleAppCredentials, MetaAppCredentials
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import PlatformCode

_NOW = datetime(2026, 9, 10, tzinfo=UTC)


class _FakeStore:
    def __init__(self) -> None:
        self.google: GoogleAppCredentials | None = None
        self.meta: MetaAppCredentials | None = None

    def save_google_app_credentials(self, credentials: GoogleAppCredentials) -> None:
        self.google = credentials

    def get_google_app_credentials(self) -> GoogleAppCredentials | None:
        return self.google

    def save_meta_app_credentials(self, credentials: MetaAppCredentials) -> None:
        self.meta = credentials

    def get_meta_app_credentials(self) -> MetaAppCredentials | None:
        return self.meta

    def delete_app_credentials(self, platform: PlatformCode) -> None:
        if platform == PlatformCode.GOOGLE:
            self.google = None
        else:
            self.meta = None


def _service() -> AppCredentialsService:
    return AppCredentialsService(_FakeStore(), FixedClock(_NOW))


def test_status_of_an_unconfigured_platform_is_not_configured() -> None:
    service = _service()

    status = service.status(PlatformCode.GOOGLE)

    assert status.configured is False
    assert status.client_id_masked is None
    assert status.login_customer_id_masked is None
    assert status.updated_at is None


def test_set_google_masks_client_id_and_login_customer_id() -> None:
    service = _service()

    status = service.set_google(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="secret",
        login_customer_id="1234567890",
    )

    assert status.configured is True
    assert status.client_id_masked == "****.com"
    assert status.login_customer_id_masked == "****7890"
    assert status.updated_at == _NOW


def test_set_google_never_exposes_client_secret() -> None:
    service = _service()

    status = service.set_google(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="super-secret-client-secret",
        login_customer_id=None,
    )

    rendered = str(status)
    assert "super-secret-client-secret" not in rendered
    assert status.login_customer_id_masked is None


def test_set_google_without_login_customer_id_is_allowed() -> None:
    service = _service()

    status = service.set_google(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="secret",
        login_customer_id=None,
    )

    assert status.configured is True
    assert status.login_customer_id_masked is None


@pytest.mark.parametrize(
    "field",
    ["client_id", "client_secret"],
)
def test_set_google_rejects_missing_required_fields(field: str) -> None:
    service = _service()
    fields = {
        "client_id": "abc123.apps.googleusercontent.com",
        "client_secret": "secret",
        "login_customer_id": None,
    }
    fields[field] = ""

    with pytest.raises(AppCredentialsIncompleteError):
        service.set_google(**fields)  # type: ignore[arg-type]


def test_set_meta_masks_app_id_and_has_no_login_customer_id() -> None:
    service = _service()

    status = service.set_meta(app_id="9876543210", app_secret="meta-secret")

    assert status.configured is True
    assert status.client_id_masked == "****3210"
    assert status.login_customer_id_masked is None
    assert status.updated_at == _NOW


def test_set_meta_rejects_missing_app_secret() -> None:
    service = _service()

    with pytest.raises(AppCredentialsIncompleteError):
        service.set_meta(app_id="9876543210", app_secret="")


def test_delete_clears_the_configured_status() -> None:
    service = _service()
    service.set_meta(app_id="9876543210", app_secret="meta-secret")

    service.delete(PlatformCode.META)

    assert service.status(PlatformCode.META).configured is False


def test_delete_is_scoped_to_its_own_platform() -> None:
    service = _service()
    service.set_google(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="secret",
        login_customer_id=None,
    )
    service.set_meta(app_id="9876543210", app_secret="meta-secret")

    service.delete(PlatformCode.GOOGLE)

    assert service.status(PlatformCode.GOOGLE).configured is False
    assert service.status(PlatformCode.META).configured is True
