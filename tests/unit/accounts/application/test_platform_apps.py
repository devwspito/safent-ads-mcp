"""Casos de uso de `/platform-apps` (owner decision, app-credentials-ui):
reenvian al bróker sin lógica propia, y propagan `BrokerRequestDeniedError`
tal cual -- el router es quien la traduce a HTTP."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.application.platform_apps import (
    DeletePlatformAppCredentials,
    GetPlatformAppStatus,
    SetGooglePlatformAppCredentials,
    SetMetaPlatformAppCredentials,
)
from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppStatus,
)
from safent_ads.shared.ids import PlatformCode
from tests.unit.accounts.application.conftest import FakePlatformAppsBrokerPort

_NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _status(platform: PlatformCode, *, configured: bool = True) -> PlatformAppStatus:
    return PlatformAppStatus(
        platform=platform,
        configured=configured,
        client_id_masked="****.com" if configured else None,
        login_customer_id_masked=None,
        updated_at=_NOW if configured else None,
    )


async def test_get_platform_app_status_forwards_to_the_broker() -> None:
    broker = FakePlatformAppsBrokerPort(status_result=_status(PlatformCode.GOOGLE))

    result = await GetPlatformAppStatus(broker).execute(PlatformCode.GOOGLE)

    assert result == _status(PlatformCode.GOOGLE)


async def test_set_google_credentials_forwards_the_input_and_returns_the_status() -> None:
    broker = FakePlatformAppsBrokerPort(status_result=_status(PlatformCode.GOOGLE))
    credentials = GoogleAppCredentialsInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="secret",
        login_customer_id="1234567890",
    )

    result = await SetGooglePlatformAppCredentials(broker).execute(credentials)

    assert broker.received_google == [credentials]
    assert result == _status(PlatformCode.GOOGLE)


async def test_set_meta_credentials_forwards_the_input_and_returns_the_status() -> None:
    broker = FakePlatformAppsBrokerPort(status_result=_status(PlatformCode.META))
    credentials = MetaAppCredentialsInput(app_id="9876543210", app_secret="secret")

    result = await SetMetaPlatformAppCredentials(broker).execute(credentials)

    assert broker.received_meta == [credentials]
    assert result == _status(PlatformCode.META)


async def test_delete_forwards_the_platform() -> None:
    broker = FakePlatformAppsBrokerPort()

    await DeletePlatformAppCredentials(broker).execute(PlatformCode.META)

    assert broker.deleted == [PlatformCode.META]


async def test_set_google_propagates_broker_denial() -> None:
    deny = BrokerRequestDeniedError("APP_CREDENTIALS_INCOMPLETE")
    broker = FakePlatformAppsBrokerPort(deny=deny)
    credentials = GoogleAppCredentialsInput(
        client_id="abc.apps.googleusercontent.com",
        client_secret="",
        login_customer_id=None,
    )

    with pytest.raises(BrokerRequestDeniedError):
        await SetGooglePlatformAppCredentials(broker).execute(credentials)
