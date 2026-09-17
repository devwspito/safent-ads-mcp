"""`DynamicPlatformAdapterRegistry`: resuelve `GoogleAdsAdapter`/
`MetaAdsAdapter` desde `AppCredentialsStorePort` en cada acceso, en vez de
una unica vez al arrancar `ads-broker` -- mismo patron que
`broker/platforms/dynamic_oauth_adapters.py` ya usa para el flujo OAuth
"Conectar". Asi el propietario puede teclear o borrar la app de Google/Meta
desde el panel y la siguiente lectura/escritura (incluido el proximo tick
de `IngestionCycle`, via el socket) la recoge sin reiniciar el broker
(owner decision, app-credentials-ui) -- el hueco que dejaba
`composition/broker.py::_build_registry` (owner decision, app-credentials-ui,
alcance conocido de esa lane).

`fallback` (`BrokerSettings`/entorno) es solo para desarrollo: si el
almacen no tiene nada guardado, se usa en su lugar; sin ninguno de los dos
la operacion falla cerrado con `AppCredentialsNotConfiguredError` --
`broker/presentation/dispatcher.py` ya la traduce a
`PLATFORM_APP_NOT_CONFIGURED` para lecturas y escrituras por igual (mismo
mapeo que usa el flujo OAuth "Conectar").

El adaptador construido se CACHEA mientras las credenciales resueltas no
cambien: `GoogleAdsAdapter`/`MetaAdsAdapter` guardan estado propio entre
llamadas (`DailyOperationBudget`/`WriteBudgetWindow`, los topes de
`contracts/platform-port.md`) que una reconstruccion en cada lectura
resetearia -- una forma silenciosa de saltarse el tope diario. Solo un
cambio real de `client_id`/`client_secret`
(`app_id`/`app_secret` en Meta) fuerza una reconstruccion.

El chequeo de lista blanca de salida (`composition/broker.py::_egress_allowed`)
sigue evaluandose UNA vez al arrancar, no por credencial: el host de cada
plataforma es fijo (`googleads.googleapis.com`/`graph.facebook.com`) y no
depende de que credenciales tenga el propietario -- igual que antes de este
cableado, una resolucion DNS bloqueada dejaria la plataforma fuera del
registro durante toda la vida del proceso."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping

from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.broker.application.errors import AppCredentialsNotConfiguredError
from safent_ads.broker.application.ports import AppCredentialsStorePort
from safent_ads.broker.infrastructure.adapter_registry import UnknownPlatformError
from safent_ads.shared.ids import PlatformCode


class GoogleAppSecrets:
    """Subconjunto de `GoogleAppCredentials` que de verdad cablea
    `GoogleAdsAdapter`/`LiveGoogleAdsSearchClient` -- sin `login_customer_id`
    (de CLIENTE, resuelto por cuenta contra `CredentialStorePort`, nunca
    aqui)."""

    __slots__ = ("client_id", "client_secret")

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GoogleAppSecrets):
            return NotImplemented
        return self.client_id == other.client_id and self.client_secret == other.client_secret

    def __hash__(self) -> int:
        return hash((self.client_id, self.client_secret))


class MetaAppSecrets:
    """Subconjunto de `MetaAppCredentials` que de verdad cablea
    `MetaAdsAdapter`/`LiveMetaGraphClient`."""

    __slots__ = ("app_id", "app_secret")

    def __init__(self, *, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MetaAppSecrets):
            return NotImplemented
        return self.app_id == other.app_id and self.app_secret == other.app_secret

    def __hash__(self) -> int:
        return hash((self.app_id, self.app_secret))


class DynamicPlatformAdapterRegistry(Mapping[PlatformCode, AdsPlatformPort]):
    """`Mapping` de solo lectura -- se pasa tal cual a
    `PlatformAdapterRegistry.adapters` (mismo contrato que el `dict`
    estatico que usaban los tests existentes, `broker/infrastructure/
    adapter_registry.py`)."""

    def __init__(
        self,
        *,
        store: AppCredentialsStorePort,
        google_factory: Callable[[GoogleAppSecrets | None], AdsPlatformPort],
        meta_factory: Callable[[MetaAppSecrets | None], AdsPlatformPort],
        google_fallback: GoogleAppSecrets | None,
        meta_fallback: MetaAppSecrets | None,
        google_egress_allowed: bool,
        meta_egress_allowed: bool,
        managed_transport_enabled: bool | Callable[[], bool] = False,
    ) -> None:
        self._store = store
        self._google_factory = google_factory
        self._meta_factory = meta_factory
        self._google_fallback = google_fallback
        self._meta_fallback = meta_fallback
        self._google_egress_allowed = google_egress_allowed
        self._meta_egress_allowed = meta_egress_allowed
        self._managed_transport_enabled = managed_transport_enabled
        self._cache: dict[PlatformCode, tuple[object, AdsPlatformPort]] = {}

    def _managed_available(self) -> bool:
        value = self._managed_transport_enabled
        return value() if callable(value) else value

    def __getitem__(self, platform: PlatformCode) -> AdsPlatformPort:
        if platform is PlatformCode.GOOGLE:
            return self._google_adapter()
        if platform is PlatformCode.META:
            return self._meta_adapter()
        raise KeyError(platform)

    def __iter__(self) -> Iterator[PlatformCode]:
        return iter(platform for platform in PlatformCode if self._is_available(platform))

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def _is_available(self, platform: PlatformCode) -> bool:
        if platform is PlatformCode.GOOGLE:
            return self._managed_available() or (
                self._google_egress_allowed and self._google_secrets() is not None
            )
        if platform is PlatformCode.META:
            return self._managed_available() or (
                self._meta_egress_allowed and self._meta_secrets() is not None
            )
        return False

    def _google_adapter(self) -> AdsPlatformPort:
        secrets = self._google_secrets()
        if secrets is None and not self._managed_available():
            raise AppCredentialsNotConfiguredError(PlatformCode.GOOGLE.value)
        if not self._google_egress_allowed:
            if not self._managed_available():
                raise UnknownPlatformError(PlatformCode.GOOGLE.value)
            secrets = None  # Managed egress never authorizes a blocked native SDK.
        cached = self._cache.get(PlatformCode.GOOGLE)
        if cached is not None and cached[0] == secrets:
            return cached[1]
        adapter = self._google_factory(secrets)
        self._cache[PlatformCode.GOOGLE] = (secrets, adapter)
        return adapter

    def _meta_adapter(self) -> AdsPlatformPort:
        secrets = self._meta_secrets()
        if secrets is None and not self._managed_available():
            raise AppCredentialsNotConfiguredError(PlatformCode.META.value)
        if not self._meta_egress_allowed:
            if not self._managed_available():
                raise UnknownPlatformError(PlatformCode.META.value)
            secrets = None
        cached = self._cache.get(PlatformCode.META)
        if cached is not None and cached[0] == secrets:
            return cached[1]
        adapter = self._meta_factory(secrets)
        self._cache[PlatformCode.META] = (secrets, adapter)
        return adapter

    def _google_secrets(self) -> GoogleAppSecrets | None:
        stored = self._store.get_google_app_credentials()
        if stored is None:
            return self._google_fallback
        return GoogleAppSecrets(
            client_id=stored.client_id,
            client_secret=stored.client_secret,
        )

    def _meta_secrets(self) -> MetaAppSecrets | None:
        stored = self._store.get_meta_app_credentials()
        if stored is None:
            return self._meta_fallback
        return MetaAppSecrets(app_id=stored.app_id, app_secret=stored.app_secret)
