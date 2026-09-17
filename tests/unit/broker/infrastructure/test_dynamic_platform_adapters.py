"""`DynamicPlatformAdapterRegistry`: resuelve `GoogleAdsAdapter`/
`MetaAdsAdapter` desde `AppCredentialsStorePort` en cada acceso -- el
almacen gana sobre el respaldo de entorno, un cambio real de credenciales
fuerza una reconstruccion y sin ninguna de las dos fuentes la lectura/
escritura falla cerrado con `AppCredentialsNotConfiguredError` (tipado,
nunca un `KeyError` generico ni una respuesta inventada). Una lista blanca
de salida denegada deniega con `UnknownPlatformError`, sin llegar siquiera
a mirar las credenciales de la otra plataforma."""

from __future__ import annotations

import pytest
import structlog.testing

from safent_ads.broker.application.errors import AppCredentialsNotConfiguredError
from safent_ads.broker.application.ports import GoogleAppCredentials, MetaAppCredentials
from safent_ads.broker.infrastructure.adapter_registry import UnknownPlatformError
from safent_ads.broker.infrastructure.dynamic_platform_adapters import (
    DynamicPlatformAdapterRegistry,
    GoogleAppSecrets,
    MetaAppSecrets,
)
from safent_ads.shared.ids import PlatformCode

_NOW = "2026-09-10T00:00:00+00:00"


class _FakeStore:
    """Doble en memoria de `AppCredentialsStorePort`: `set_google`/
    `delete_google` simulan `set_platform_app_credentials`/
    `delete_platform_app_credentials` sobre el socket real."""

    def __init__(self) -> None:
        self._google: GoogleAppCredentials | None = None
        self._meta: MetaAppCredentials | None = None

    def set_google(self, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "login_customer_id": None,
            "updated_at": _NOW,
        }
        defaults.update(overrides)
        self._google = GoogleAppCredentials(**defaults)  # type: ignore[arg-type]

    def delete_google(self) -> None:
        self._google = None

    def set_meta(self, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "app_id": "app-id",
            "app_secret": "app-secret",
            "updated_at": _NOW,
        }
        defaults.update(overrides)
        self._meta = MetaAppCredentials(**defaults)  # type: ignore[arg-type]

    def delete_meta(self) -> None:
        self._meta = None

    def get_google_app_credentials(self) -> GoogleAppCredentials | None:
        return self._google

    def get_meta_app_credentials(self) -> MetaAppCredentials | None:
        return self._meta


class _StubAdapter:
    """Doble del SDK real (contract: "SDK mocked") -- un marcador de
    identidad para probar cacheado/reconstruccion, no un `AdsPlatformPort`
    completo (estos tests nunca llaman a sus metodos de lectura/escritura)."""


def _google_factory_counting(calls: list[GoogleAppSecrets]):
    def factory(secrets: GoogleAppSecrets) -> _StubAdapter:
        calls.append(secrets)
        return _StubAdapter()

    return factory


def _meta_factory_counting(calls: list[MetaAppSecrets]):
    def factory(secrets: MetaAppSecrets) -> _StubAdapter:
        calls.append(secrets)
        return _StubAdapter()

    return factory


def _registry(
    store: _FakeStore,
    *,
    google_calls: list[GoogleAppSecrets] | None = None,
    meta_calls: list[MetaAppSecrets] | None = None,
    google_fallback: GoogleAppSecrets | None = None,
    meta_fallback: MetaAppSecrets | None = None,
    google_egress_allowed: bool = True,
    meta_egress_allowed: bool = True,
) -> DynamicPlatformAdapterRegistry:
    return DynamicPlatformAdapterRegistry(
        store=store,  # type: ignore[arg-type]
        google_factory=_google_factory_counting(google_calls if google_calls is not None else []),
        meta_factory=_meta_factory_counting(meta_calls if meta_calls is not None else []),
        google_fallback=google_fallback,
        meta_fallback=meta_fallback,
        google_egress_allowed=google_egress_allowed,
        meta_egress_allowed=meta_egress_allowed,
    )


# --- fail closed, sin credenciales ---
def test_no_credentials_denies_google_with_a_typed_error() -> None:
    registry = _registry(_FakeStore())

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry[PlatformCode.GOOGLE]


def test_no_credentials_denies_meta_with_a_typed_error() -> None:
    registry = _registry(_FakeStore())

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry[PlatformCode.META]


def test_unconfigured_platform_is_absent_from_iteration() -> None:
    registry = _registry(_FakeStore())

    assert set(registry) == set()
    assert len(registry) == 0


# --- resolucion perezosa: el factory no corre hasta el primer acceso ---
def test_factory_never_runs_before_the_first_access() -> None:
    store = _FakeStore()
    store.set_google()
    calls: list[GoogleAppSecrets] = []
    _registry(store, google_calls=calls)

    assert calls == []


# --- almacen gana sobre respaldo de entorno ---
def test_store_credentials_win_over_env_fallback() -> None:
    store = _FakeStore()
    store.set_google(client_id="stored-client-id")
    calls: list[GoogleAppSecrets] = []
    fallback = GoogleAppSecrets(
        client_id="env-client-id", client_secret="env-secret"
    )

    registry = _registry(store, google_calls=calls, google_fallback=fallback)
    registry[PlatformCode.GOOGLE]

    assert calls[0].client_id == "stored-client-id"


def test_env_fallback_used_only_when_store_is_empty() -> None:
    store = _FakeStore()
    calls: list[GoogleAppSecrets] = []
    fallback = GoogleAppSecrets(
        client_id="env-client-id", client_secret="env-secret"
    )

    registry = _registry(store, google_calls=calls, google_fallback=fallback)
    adapter = registry[PlatformCode.GOOGLE]

    assert isinstance(adapter, _StubAdapter)
    assert calls[0].client_id == "env-client-id"


# --- cacheado: sin reconstruccion mientras la credencial no cambie ---
def test_repeated_access_reuses_the_same_adapter_instance() -> None:
    store = _FakeStore()
    store.set_google()
    calls: list[GoogleAppSecrets] = []
    registry = _registry(store, google_calls=calls)

    first = registry[PlatformCode.GOOGLE]
    second = registry[PlatformCode.GOOGLE]

    assert first is second
    assert len(calls) == 1


def test_google_and_meta_caches_are_independent() -> None:
    store = _FakeStore()
    store.set_google()
    store.set_meta()
    google_calls: list[GoogleAppSecrets] = []
    meta_calls: list[MetaAppSecrets] = []
    registry = _registry(store, google_calls=google_calls, meta_calls=meta_calls)

    registry[PlatformCode.GOOGLE]
    registry[PlatformCode.META]
    registry[PlatformCode.GOOGLE]
    registry[PlatformCode.META]

    assert len(google_calls) == 1
    assert len(meta_calls) == 1


# --- invalidacion: un cambio real de credenciales reconstruye ---
def test_credential_rotation_rebuilds_the_adapter() -> None:
    """Simula `set_platform_app_credentials` (rotacion desde el panel,
    misma llamada al socket que dispara `AppCredentialsService.set_google`):
    la SIGUIENTE lectura -- el proximo tick de `IngestionCycle` -- recoge
    la credencial nueva sin reiniciar el broker."""
    store = _FakeStore()
    store.set_google(client_id="old-client-id")
    calls: list[GoogleAppSecrets] = []
    registry = _registry(store, google_calls=calls)

    first = registry[PlatformCode.GOOGLE]
    store.set_google(client_id="new-client-id")
    second = registry[PlatformCode.GOOGLE]

    assert first is not second
    assert [c.client_id for c in calls] == ["old-client-id", "new-client-id"]


def test_deleting_credentials_denies_the_next_access() -> None:
    """Simula `delete_platform_app_credentials`: la siguiente lectura
    deniega tipado en vez de servir con el adaptador cacheado a medias."""
    store = _FakeStore()
    store.set_google()
    registry = _registry(store)
    registry[PlatformCode.GOOGLE]

    store.delete_google()

    with pytest.raises(AppCredentialsNotConfiguredError):
        registry[PlatformCode.GOOGLE]


def test_deleting_credentials_falls_back_to_env_when_available() -> None:
    store = _FakeStore()
    store.set_google(client_id="panel-client-id")
    fallback = GoogleAppSecrets(
        client_id="env-client-id", client_secret="env-secret"
    )
    calls: list[GoogleAppSecrets] = []
    registry = _registry(store, google_calls=calls, google_fallback=fallback)
    registry[PlatformCode.GOOGLE]

    store.delete_google()
    registry[PlatformCode.GOOGLE]

    assert [c.client_id for c in calls] == ["panel-client-id", "env-client-id"]


# --- lista blanca de salida ---
def test_egress_denied_raises_unknown_platform_without_calling_the_factory() -> None:
    store = _FakeStore()
    store.set_google()
    calls: list[GoogleAppSecrets] = []
    registry = _registry(store, google_calls=calls, google_egress_allowed=False)

    with pytest.raises(UnknownPlatformError):
        registry[PlatformCode.GOOGLE]

    assert calls == []


def test_egress_denied_platform_absent_from_iteration_even_with_credentials() -> None:
    store = _FakeStore()
    store.set_google()
    registry = _registry(store, google_egress_allowed=False)

    assert set(registry) == set()


# --- fingerprint: los secretos son value objects, no identidad ---
def test_google_app_secrets_equality_is_by_value() -> None:
    a = GoogleAppSecrets(client_id="c", client_secret="s")
    b = GoogleAppSecrets(client_id="c", client_secret="s")
    c = GoogleAppSecrets(client_id="c", client_secret="different")

    assert a == b
    assert a != c


def test_meta_app_secrets_equality_is_by_value() -> None:
    a = MetaAppSecrets(app_id="a", app_secret="s")
    b = MetaAppSecrets(app_id="a", app_secret="s")
    c = MetaAppSecrets(app_id="a", app_secret="different")

    assert a == b
    assert a != c


# --- membership refleja el estado en vivo del almacen ---
def test_iteration_reflects_the_live_store_state() -> None:
    store = _FakeStore()
    registry = _registry(store)
    assert set(registry) == set()

    store.set_google()
    assert set(registry) == {PlatformCode.GOOGLE}

    store.set_meta()
    assert set(registry) == {PlatformCode.GOOGLE, PlatformCode.META}

    store.delete_google()
    assert set(registry) == {PlatformCode.META}


# --- nunca un secreto en un log ---
def test_resolving_configured_credentials_never_logs_the_secret() -> None:
    """`DynamicPlatformAdapterRegistry` no emite ningun log propio -- esta
    prueba es una guarda de regresion: si algun dia se anade observabilidad
    aqui (p.ej. un log al reconstruir el adaptador), el secreto real de
    `client_secret`/`app_secret` no puede colarse en el
    evento."""
    store = _FakeStore()
    store.set_google(
        client_id="client-id",
        client_secret="super-secret-client-secret",
    )
    store.set_meta(app_id="app-id", app_secret="super-secret-app-secret")
    registry = _registry(store)

    with structlog.testing.capture_logs() as logs:
        registry[PlatformCode.GOOGLE]
        registry[PlatformCode.META]

    serialized_logs = repr(logs)
    assert "super-secret-client-secret" not in serialized_logs
    assert "super-secret-app-secret" not in serialized_logs


def test_denying_unconfigured_credentials_never_logs_the_secret() -> None:
    """Un respaldo de entorno presente pero incompleto tampoco debe
    filtrar el fragmento de secreto que si llego a configurarse."""
    store = _FakeStore()
    fallback = GoogleAppSecrets(
        client_id="c", client_secret="fallback-secret"
    )
    registry = _registry(store, google_fallback=fallback, google_egress_allowed=False)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(UnknownPlatformError):
            registry[PlatformCode.GOOGLE]

    serialized_logs = repr(logs)
    assert "fallback-secret" not in serialized_logs
