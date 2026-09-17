"""`composition.broker._build_registry`: un adaptador solo entra al
registro cuando el almacen cifrado (o, sin nada guardado ahi,
`BrokerSettings` como respaldo de desarrollo) trae las credenciales de
VENDOR de esa plataforma Y `assert_egress_allowed` pasa -- sin cualquiera
de las dos, la plataforma deniega tipado
(`AppCredentialsNotConfiguredError` -> `PLATFORM_APP_NOT_CONFIGURED`),
nunca un adaptador a medias ni una respuesta inventada. El registro es
DINAMICO (`DynamicPlatformAdapterRegistry`): si el propietario teclea o
borra la app desde el panel DESPUES de construir el registro, la
SIGUIENTE lectura la recoge sin reconstruir nada a mano -- el mismo
almacen que ya usaba el flujo OAuth "Conectar".

`composition.broker._build_write_pipeline`: `ApprovalVerifier` +
`CapsConfig` + `WriteLedgerStore` se cablean en un `WriteAuthorizationPipeline`
compartido por los dos adaptadores cuando `approval_public_key` es material
Ed25519 valido; una clave invalida deja `write_pipeline=None` en los dos sin
tumbar el registro (fail closed, las lecturas siguen sirviendo).

`composition.broker._build_runtime` (owner decision, app-credentials-ui):
el flujo OAuth "Conectar" resuelve la app de VENDOR del almacen cifrado en
cada llamada, con `BrokerSettings`/entorno solo como respaldo de
desarrollo -- el almacen gana cuando tiene algo guardado, y sin ninguno de
los dos falla cerrado con `AppCredentialsNotConfiguredError` en vez de
construir una `authorization_url` con un `client_id` vacio. `_build_registry`
y `_build_runtime` comparten el MISMO `EncryptedCredentialStore` (`run`) --
estos tests construyen uno propio por caso, igual que `run` construye uno
solo por proceso."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.errors import AppCredentialsNotConfiguredError
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.caps_config import CapsConfig, CapsDefaults
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.egress_guard import EgressDeniedError
from safent_ads.broker.platforms.composio_sdk_clients import ComposioMetaAdLibraryClient
from safent_ads.broker.platforms.google_ads_adapter import GoogleAdsAdapter
from safent_ads.broker.platforms.live_google_ads_client import (
    LiveGoogleAssetUploadClient,
    LiveGoogleKeywordIdeaClient,
)
from safent_ads.broker.platforms.live_meta_ad_library_client import LiveMetaAdLibraryClient
from safent_ads.broker.platforms.meta_ads_adapter import MetaAdsAdapter
from safent_ads.broker.platforms.write_pipeline import WriteAuthorizationPipeline
from safent_ads.composition import broker as broker_composition
from safent_ads.composition.settings import BrokerSettings
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import PlatformCode

_VALID_PUBLIC_KEY_B64 = base64.b64encode(b"k" * 32).decode()
_CREDENTIAL_MASTER_KEY_B64 = base64.b64encode(b"m" * 32).decode()


def _settings(**overrides: Any) -> BrokerSettings:
    base: dict[str, Any] = {
        "broker_socket_path": "/tmp/safent-ads-test/broker.sock",
        "approval_public_key": "test-public-key",
        "allowed_uids": [10001],
        "hard_caps_file": "/tmp/safent-ads-test/caps.yaml",
        # lane oauth-connect: el almacen cifrado de credenciales de cliente es
        # obligatorio (fail closed); clave de 32 bytes en base64 de prueba.
        "credential_master_key": _CREDENTIAL_MASTER_KEY_B64,
        "credential_store_dir": "/tmp/safent-ads-test/credentials",
    }
    base.update(overrides)
    return BrokerSettings(_env_file=None, **base)


def _caps() -> CapsConfig:
    return CapsConfig(
        defaults=CapsDefaults(max_step_pct=100.0, max_changes_per_day=10, autonomy_enabled=True),
        accounts={},
    )


def _store(tmp_path: Path) -> EncryptedCredentialStore:
    return EncryptedCredentialStore(tmp_path / "credentials", _CREDENTIAL_MASTER_KEY_B64)


@pytest.fixture(autouse=True)
def _egress_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(_hostname: str, *, resolver: Any = None) -> None:
        return None

    monkeypatch.setattr(broker_composition, "assert_egress_allowed", _allow)


async def test_no_vendor_credentials_denies_google_with_a_typed_error(tmp_path: Path) -> None:
    registry = await broker_composition._build_registry(_settings(), _caps(), _store(tmp_path))

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry.get(PlatformCode.GOOGLE)


async def test_no_vendor_credentials_means_empty_iteration(tmp_path: Path) -> None:
    registry = await broker_composition._build_registry(_settings(), _caps(), _store(tmp_path))

    assert set(registry.adapters) == set()


async def test_google_env_fallback_registers_a_google_adapter(tmp_path: Path) -> None:
    registry = await broker_composition._build_registry(
        _settings(
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
        ),
        _caps(),
        _store(tmp_path),
    )

    assert set(registry.adapters) == {PlatformCode.GOOGLE}
    assert isinstance(registry.get(PlatformCode.GOOGLE), GoogleAdsAdapter)


async def test_meta_env_fallback_registers_a_meta_adapter(tmp_path: Path) -> None:
    registry = await broker_composition._build_registry(
        _settings(meta_app_id="app-id", meta_app_secret="app-secret"), _caps(), _store(tmp_path)
    )

    assert set(registry.adapters) == {PlatformCode.META}
    assert isinstance(registry.get(PlatformCode.META), MetaAdsAdapter)


async def test_partial_google_env_fallback_keeps_google_denied(tmp_path: Path) -> None:
    """`client_secret` ausente -- ni siquiera el respaldo de entorno esta
    completo, no hay adaptador a medias."""
    registry = await broker_composition._build_registry(
        _settings(google_ads_client_id="client-id"),
        _caps(),
        _store(tmp_path),
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry.get(PlatformCode.GOOGLE)


async def test_stored_credentials_win_over_env_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = _settings(
        google_ads_client_id="env-client-id",
        google_ads_client_secret="env-secret",
    )
    registry = await broker_composition._build_registry(settings, _caps(), store)
    app_credentials = AppCredentialsService(store, SystemClock())
    app_credentials.set_google(
        client_id="panel-client-id",
        client_secret="panel-secret",
        login_customer_id=None,
    )

    adapter = registry.get(PlatformCode.GOOGLE)

    assert adapter._config.client_id == "panel-client-id"  # type: ignore[attr-defined]


async def test_saving_credentials_after_the_registry_is_built_is_picked_up_next_call(
    tmp_path: Path,
) -> None:
    """Regresion del hueco documentado en `composition/broker.py`: guardar
    la app desde el panel (`set_platform_app_credentials`) ya NO exige
    reiniciar `ads-broker` para que la siguiente lectura la use."""
    store = _store(tmp_path)
    registry = await broker_composition._build_registry(_settings(), _caps(), store)
    app_credentials = AppCredentialsService(store, SystemClock())

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry.get(PlatformCode.GOOGLE)

    app_credentials.set_google(
        client_id="client-id",
        client_secret="client-secret",
        login_customer_id=None,
    )

    assert isinstance(registry.get(PlatformCode.GOOGLE), GoogleAdsAdapter)


async def test_deleting_credentials_denies_the_next_call_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    app_credentials = AppCredentialsService(store, SystemClock())
    app_credentials.set_google(
        client_id="client-id",
        client_secret="client-secret",
        login_customer_id=None,
    )
    registry = await broker_composition._build_registry(_settings(), _caps(), store)
    registry.get(PlatformCode.GOOGLE)

    app_credentials.delete(PlatformCode.GOOGLE)

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry.get(PlatformCode.GOOGLE)


async def test_egress_denied_keeps_google_out_even_with_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _deny(_hostname: str, *, resolver: Any = None) -> None:
        raise EgressDeniedError("blocked")

    monkeypatch.setattr(broker_composition, "assert_egress_allowed", _deny)

    registry = await broker_composition._build_registry(
        _settings(
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
        ),
        _caps(),
        _store(tmp_path),
    )

    assert set(registry.adapters) == set()


async def test_a_valid_public_key_wires_the_same_pipeline_into_both_adapters(
    tmp_path: Path,
) -> None:
    registry = await broker_composition._build_registry(
        _settings(
            approval_public_key=_VALID_PUBLIC_KEY_B64,
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
            meta_app_id="app-id",
            meta_app_secret="app-secret",
        ),
        _caps(),
        _store(tmp_path),
    )

    google_pipeline = registry.get(PlatformCode.GOOGLE)._write_pipeline  # type: ignore[attr-defined]
    meta_pipeline = registry.get(PlatformCode.META)._write_pipeline  # type: ignore[attr-defined]
    assert isinstance(google_pipeline, WriteAuthorizationPipeline)
    assert google_pipeline is meta_pipeline


async def test_a_malformed_public_key_leaves_both_adapters_without_a_pipeline(
    tmp_path: Path,
) -> None:
    """`approval_public_key` invalido (no base64 de 32 bytes) no tumba el
    arranque del broker: las lecturas siguen disponibles, y `execute_write`
    deniega por diseno con `write_path_not_wired` en vez de mutar a
    ciegas."""
    registry = await broker_composition._build_registry(
        _settings(
            approval_public_key="not-a-valid-ed25519-public-key",
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
        ),
        _caps(),
        _store(tmp_path),
    )

    google_adapter = registry.get(PlatformCode.GOOGLE)
    assert google_adapter._write_pipeline is None  # type: ignore[attr-defined]


async def test_single_owner_mode_wires_the_keyword_idea_and_ad_library_ports(
    tmp_path: Path,
) -> None:
    """004 tasks-2.md R4/R7: `get_google_keyword_ideas`/`search_competitor_ads`
    used to fail closed with `PlatformCapabilityNotImplementedError` because
    `_build_google_adapter`/`_build_meta_adapter` never wired a client --
    Hermes (`ADS_SINGLE_OWNER_MODE`, no `composio_api_key`) now gets real
    ones, composed over the SAME vendor app credentials the rest of each
    adapter already uses (no new secret)."""
    registry = await broker_composition._build_registry(
        _settings(
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
            meta_app_id="app-id",
            meta_app_secret="app-secret",
        ),
        _caps(),
        _store(tmp_path),
    )

    google_adapter = registry.get(PlatformCode.GOOGLE)
    meta_adapter = registry.get(PlatformCode.META)
    assert isinstance(
        google_adapter._keyword_idea_client,  # type: ignore[attr-defined]
        LiveGoogleKeywordIdeaClient,
    )
    assert isinstance(
        google_adapter._asset_upload_client,  # type: ignore[attr-defined]
        LiveGoogleAssetUploadClient,
    )
    assert isinstance(
        meta_adapter._ad_library_client,  # type: ignore[attr-defined]
        LiveMetaAdLibraryClient,
    )


async def test_hosted_composio_mode_also_wires_the_keyword_idea_and_ad_library_ports(
    tmp_path: Path,
) -> None:
    """Same two ports, but with `composio_api_key` set (hosted mode,
    `ComposioGoogleAdsSearchClient`/`ComposioMetaGraphClient` in place of the
    direct SDK clients): `LiveGoogleKeywordIdeaClient` composes whichever
    concrete search client the factory built (`build_sdk_client` is
    polymorphic) -- neither port is ever `None` in this mode either.
    fix/ad-library-over-composio: unlike the keyword/asset ports (which
    wrap whatever `search_client` the factory built), the Ad Library client
    IS mode-selected here -- `ComposioMetaAdLibraryClient` in hosted mode,
    even though a native app is ALSO configured in this test, same
    precedence as `graph_client`/`search_client` above it in
    `_build_meta_adapter`/`_build_google_adapter`."""
    registry = await broker_composition._build_registry(
        _settings(
            google_ads_client_id="client-id",
            google_ads_client_secret="client-secret",
            meta_app_id="app-id",
            meta_app_secret="app-secret",
            composio_api_key="private-test-key",
            composio_googleads_auth_config_id="ac_bound",
            composio_metaads_auth_config_id="ac_bound",
        ),
        _caps(),
        _store(tmp_path),
    )

    google_adapter = registry.get(PlatformCode.GOOGLE)
    meta_adapter = registry.get(PlatformCode.META)
    assert isinstance(
        google_adapter._keyword_idea_client,  # type: ignore[attr-defined]
        LiveGoogleKeywordIdeaClient,
    )
    assert isinstance(
        google_adapter._asset_upload_client,  # type: ignore[attr-defined]
        LiveGoogleAssetUploadClient,
    )
    assert isinstance(
        meta_adapter._ad_library_client,  # type: ignore[attr-defined]
        ComposioMetaAdLibraryClient,
    )


async def test_hosted_composio_mode_without_a_native_meta_app_still_wires_the_ad_library_port(
    tmp_path: Path,
) -> None:
    """The reported bug (fix/ad-library-over-composio): on the VM (Composio
    transport only, no native Meta app credentials at all), `_ad_library_client`
    used to stay `LiveMetaAdLibraryClient(app_id="", app_secret="")` -- fails
    closed on every single call (`CredentialNotConnectedError
    ("meta_native_app_not_configured")`). It must be `ComposioMetaAdLibraryClient`
    here, exactly like `test_hosted_composio_mode_also_wires_the_keyword_idea_and_
    ad_library_ports` above, even with no `meta_app_id`/`meta_app_secret` set."""
    registry = await broker_composition._build_registry(
        _settings(
            composio_api_key="private-test-key",
            composio_metaads_auth_config_id="ac_bound",
        ),
        _caps(),
        _store(tmp_path),
    )

    meta_adapter = registry.get(PlatformCode.META)
    assert isinstance(
        meta_adapter._ad_library_client,  # type: ignore[attr-defined]
        ComposioMetaAdLibraryClient,
    )


# --- lane: app-credentials-ui ---
async def test_build_runtime_wires_an_app_credentials_service(tmp_path: Path) -> None:
    settings = _settings()

    runtime = broker_composition._build_runtime(
        settings, PlatformAdapterRegistry({}), _store(tmp_path)
    )

    assert isinstance(runtime.app_credentials, AppCredentialsService)
    assert runtime.app_credentials.status(PlatformCode.GOOGLE).configured is False


async def test_build_runtime_oauth_begin_uses_the_env_fallback_when_store_is_empty(
    tmp_path: Path,
) -> None:
    settings = _settings(
        google_ads_client_id="env-client-id.apps.googleusercontent.com",
        google_ads_client_secret="env-client-secret",
    )
    runtime = broker_composition._build_runtime(
        settings, PlatformAdapterRegistry({}), _store(tmp_path)
    )

    result = await runtime.oauth_flow.begin(
        provider=PlatformCode.GOOGLE, business_id="biz-1", redirect_uri="https://x/callback"
    )

    assert "env-client-id.apps.googleusercontent.com" in result.authorization_url


async def test_build_runtime_oauth_begin_fails_closed_without_any_google_credentials(
    tmp_path: Path,
) -> None:
    settings = _settings()
    runtime = broker_composition._build_runtime(
        settings, PlatformAdapterRegistry({}), _store(tmp_path)
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        await runtime.oauth_flow.begin(
            provider=PlatformCode.GOOGLE, business_id="biz-1", redirect_uri="https://x/callback"
        )


async def test_build_runtime_oauth_flow_and_app_credentials_share_the_same_store(
    tmp_path: Path,
) -> None:
    """El propietario teclea la app desde el panel (`app_credentials.set_google`)
    y el flujo "Conectar" (`oauth_flow.begin`) la ve de inmediato -- prueba
    de que las dos piezas comparten el MISMO `EncryptedCredentialStore`, sin
    reiniciar `ads-broker`."""
    settings = _settings()
    runtime = broker_composition._build_runtime(
        settings, PlatformAdapterRegistry({}), _store(tmp_path)
    )

    runtime.app_credentials.set_google(
        client_id="panel-client-id.apps.googleusercontent.com",
        client_secret="panel-secret",
        login_customer_id=None,
    )

    result = await runtime.oauth_flow.begin(
        provider=PlatformCode.GOOGLE, business_id="biz-1", redirect_uri="https://x/callback"
    )

    assert "panel-client-id.apps.googleusercontent.com" in result.authorization_url


# --- end lane: app-credentials-ui ---


def test_run_no_longer_overrides_the_sockets_global_frame_limit() -> None:
    """M-3 (revision de seguridad 0.2.22): `run()` subia el limite GLOBAL
    de lectura del socket a 16 MiB solo para que cupiera la respuesta de
    `render_image` -- eso tambien dejaba pasar una peticion de basura de
    16 MiB bajo cualquier otra `op` antes de rechazarla. El techo grande
    ahora vive por-operacion en `broker/presentation/socket_server.py`
    (`_PER_OP_MAX_RESPONSE_BYTES`, solo para la RESPUESTA); este modulo ya
    no necesita ninguna constante de trama propia."""
    assert not hasattr(broker_composition, "_MAX_FRAME_BYTES")
