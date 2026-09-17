"""`ResourceIndicator` (data-model.md, RFC 8707): la URL canonica de `/mcp`
a la que un token queda atado.

Revision de seguridad (PR 44, T049): el agregado valida su propio
invariante -- EXACTAMENTE el mismo criterio que
`oauth_authorization_requests_resource_check`/`oauth_grants_resource_check`
(`0054_mcp_oauth_loopback_resource`, agreement probado en
`tests/integration/migrations/test_0054_mcp_oauth_loopback_resource.py`):
`https` con autoridad, o `http` de bucle local (`shared/net/loopback.py`,
la MISMA fuente que `redirect_uri`) con un puerto 1-65535 -- nunca query ni
fragmento. `ApiSettings` construye este VO EN EL ARRANQUE
(`composition/settings.py::_require_a_valid_oauth_resource`): si algun
`ADS_PUBLIC_BASE_URL` que la app admite llegase a producir un `resource`
que el CHECK de Postgres rechaza, esto revienta ahi, con un mensaje que
nombra `ADS_PUBLIC_BASE_URL`, en vez de en el primer `/authorize` real
contra un `IntegrityError`/`invalid_request` opaco."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from safent_ads.mcp_oauth.domain.errors import InvalidResourceError
from safent_ads.shared.net.loopback import is_loopback_http_origin

_MIN_PORT, _MAX_PORT = 1, 65535


@dataclass(frozen=True, slots=True)
class ResourceIndicator:
    value: str

    def __post_init__(self) -> None:
        parsed = self._split_or_reject()
        if parsed.query or parsed.fragment:
            raise InvalidResourceError(f"resource no admite query/fragment: {self.value!r}")
        if parsed.scheme == "https" and parsed.netloc:
            return
        if is_loopback_http_origin(self.value) and self._has_a_valid_port(parsed):
            return
        raise InvalidResourceError(
            "resource debe ser https, o http de bucle local "
            f"(127.0.0.1/localhost/[::1]): {self.value!r}"
        )

    def _split_or_reject(self) -> SplitResult:
        # Mismo motivo que `mcp_oauth/domain/client.py::RedirectUri.
        # _split_or_reject`: `urlsplit`/`.port` parsean la autoridad de
        # forma PEREZOSA y levantan `ValueError` con una rota (`http://
        # [::1`) -- traducirlo aqui evita que cruce la frontera del
        # dominio como una excepcion sin tipo.
        try:
            parsed = urlsplit(self.value)
            _ = parsed.hostname
        except ValueError as exc:
            raise InvalidResourceError(f"resource malformado: {self.value!r}") from exc
        return parsed

    @staticmethod
    def _has_a_valid_port(parsed: SplitResult) -> bool:
        try:
            port = parsed.port
        except ValueError:
            return False
        return port is None or _MIN_PORT <= port <= _MAX_PORT

    @classmethod
    def canonical(cls, public_base_url: str) -> ResourceIndicator:
        return cls(f"{public_base_url}/mcp")

    def __str__(self) -> str:
        return self.value
