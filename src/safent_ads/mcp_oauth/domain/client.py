"""`OAuthClient` + `RedirectUri` (data-model.md): el programa agente que
pide acceso delegado. No es de confianza hasta el primer consentimiento
(REGISTERED -> TRUSTED); un REGISTERED sin concesion pasadas 24h es
podable (`UNCONSENTED_CLIENT_TTL`, `application/policy.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import SplitResult, urlsplit

from safent_ads.mcp_oauth.domain.errors import (
    ConfidentialClientRequiresSecretError,
    InvalidClientNameError,
    InvalidRedirectUriError,
    PublicClientCannotHaveSecretError,
    TooManyRedirectUrisError,
)
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.net.loopback import LOOPBACK_HOSTS as _LOOPBACK_HOSTS

_MAX_REDIRECT_URIS = 5
# threat-model.md C-42 y 0035_mcp_oauth (`oauth_clients_client_name_check`):
# el dominio no puede permitir un nombre que la BD despues rechaza con un
# `IntegrityError` opaco -- el mismo tope, una sola vez.
_MAX_CLIENT_NAME_LENGTH = 100
# D-11 (threat-model.md C-70 pieza 4, decision del dueno): el conjunto
# CERRADO de esquemas admitidos. `https` solo sobrevive porque un bucle
# local con TLS propio sigue siendo la misma maquina; un host remoto ya
# no entra por ninguno de los dos.
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MIN_PORT, _MAX_PORT = 1, 65535
# Revision de seguridad (17-sep): `urlsplit` BORRA en silencio CR, LF y TAB
# (WHATWG URL), asi que una URI con ellos pasaria la validacion y `value`
# los conservaria para quien luego la pinte o la escriba en una cabecera.
# Se rechazan sobre la cadena CRUDA, antes de parsear nada.
_CONTROL_CHARACTERS = frozenset(chr(code) for code in (*range(0x20), 0x7F))
# threat-model.md C-70 pieza 3: el `client_name` se PINTA entero en la
# pantalla de consentimiento. Los de anulacion/aislamiento bidireccional
# pueden dar la vuelta al texto ("Claude Code" leyendose sobre un nombre
# distinto) y los de anchura cero pueden partir una palabra sin que se vea.
#
# La lista es deliberadamente ESTRECHA en el bloque `U+200x`: `U+200C`
# (ZWNJ) y `U+200D` (ZWJ) NO entran. Son ortografia obligatoria en persa,
# hindi o malayalam y unen las secuencias de emoji; prohibirlos serviria
# de poco contra el enganyo y dejaria fuera nombres legitimos de medio
# mundo. Lo que si se prohibe es lo que solo sirve para mentir sobre el
# orden o la longitud del texto.
_FORBIDDEN_CLIENT_NAME_RANGES: tuple[tuple[int, int], ...] = (
    (0x00, 0x1F),  # C0
    (0x7F, 0x9F),  # DEL y C1
    (0x200B, 0x200B),  # espacio de anchura cero
    (0x200E, 0x200F),  # marcas de direccion (LRM/RLM)
    (0x202A, 0x202E),  # anulacion bidireccional
    (0x2066, 0x2069),  # aislamiento bidireccional
    (0xFEFF, 0xFEFF),  # BOM / espacio de anchura cero sin salto
)


class TokenEndpointAuthMethod(StrEnum):
    NONE = "none"
    CLIENT_SECRET_POST = "client_secret_post"  # noqa: S105 - nombre de metodo RFC 7591, no un secreto
    CLIENT_SECRET_BASIC = "client_secret_basic"  # noqa: S105 - idem


class OAuthClientState(StrEnum):
    REGISTERED = "REGISTERED"
    TRUSTED = "TRUSTED"


@dataclass(frozen=True, slots=True)
class RedirectUri:
    """Bucle local EXCLUSIVAMENTE (D-11, threat-model.md C-70 pieza 4):
    `http` o `https` a `127.0.0.1`, `[::1]` o `localhost`, con el puerto
    libre y la ruta que quiera el cliente. Sin userinfo, sin fragmento; un
    esquema custom, un host remoto o un host que solo *parezca* de bucle
    local (`127.0.0.1.evil.com`, `localhost.evil`, `127.0.0.2`, `127.1`,
    `[0:0:0:0:0:0:0:1]`) nunca se aceptan. Tampoco los caracteres de
    control, una autoridad que no parsea ni un puerto fuera de 1-65535.

    Comparacion: igualdad exacta, salvo bucle local por `http` (RFC 8252
    §7.3), donde el puerto es libre porque el agente nativo toma uno
    efimero en cada lanzamiento. Un `https` de bucle local exige igualdad
    exacta, puerto incluido: quien monta TLS en su maquina fija el puerto."""

    value: str

    def __post_init__(self) -> None:
        self._reject_control_characters()
        parsed = self._split_or_reject()
        self._reject_userinfo_and_fragment(parsed)
        self._require_allowed_scheme_and_host(parsed)
        self._require_a_port_in_range(parsed)

    def _reject_control_characters(self) -> None:
        # El valor NO entra en el mensaje: lleva justo los caracteres que
        # se estan rechazando (CR/LF acabarian en el registro o en una
        # cabecera de quien reflejara el error).
        if not _CONTROL_CHARACTERS.isdisjoint(self.value):
            raise InvalidRedirectUriError("redirect_uri con caracteres de control")

    def _split_or_reject(self) -> SplitResult:
        """`urlsplit` levanta `ValueError` con una autoridad rota
        (`http://[::1`, corchete sin cerrar) y `hostname`/`port` tambien,
        porque parsean la autoridad de forma PEREZOSA. Traducirlo aqui es
        lo que impide que un `ValueError` cruce la frontera del dominio y
        acabe en un 500 de `/authorize` o en un arranque fallido
        (revision de seguridad, 17-sep)."""
        try:
            parsed = urlsplit(self.value)
            _host, _port = parsed.hostname, parsed.port
        except ValueError as exc:
            raise InvalidRedirectUriError("redirect_uri malformada") from exc
        return parsed

    def _reject_userinfo_and_fragment(self, parsed: SplitResult) -> None:
        if parsed.fragment or "@" in parsed.netloc:
            raise InvalidRedirectUriError(f"redirect_uri con fragmento o userinfo: {self.value!r}")

    def _require_a_port_in_range(self, parsed: SplitResult) -> None:
        """`_split_or_reject` ya descarto los puertos que ni siquiera
        parsean (`:99999`, `:-1`, `:abc`); aqui queda el `0`, que parsea
        pero no es un puerto donde nadie pueda escuchar."""
        if parsed.port is not None and not (_MIN_PORT <= parsed.port <= _MAX_PORT):
            raise InvalidRedirectUriError(f"redirect_uri con puerto fuera de rango: {parsed.port}")

    def _require_allowed_scheme_and_host(self, parsed: SplitResult) -> None:
        # `parsed.hostname` normaliza a minusculas y desnuda los corchetes
        # de IPv6 (`[::1]` -> `::1`), asi que la pertenencia al conjunto
        # cerrado es la comprobacion COMPLETA: cualquier host que no sea
        # literalmente uno de los tres cae en el `raise`.
        if parsed.scheme in _ALLOWED_SCHEMES and parsed.hostname in _LOOPBACK_HOSTS:
            return
        raise InvalidRedirectUriError(
            f"redirect_uri debe ser de bucle local (http o https a 127.0.0.1, "
            f"[::1] o localhost): {self.value!r}"
        )

    def matches(self, candidate: str) -> bool:
        if self.value == candidate:
            return True
        return self._matches_ignoring_loopback_port(candidate)

    def _matches_ignoring_loopback_port(self, candidate: str) -> bool:
        try:
            own, other = urlsplit(self.value), urlsplit(candidate)
            _own_host, _other_host = own.hostname, other.hostname
        except ValueError:
            # `candidate` es una cadena CRUDA (L10): una autoridad rota no
            # "no coincide" con un error, simplemente no coincide.
            return False
        if own.scheme != "http" or other.scheme != "http":
            return False
        if own.hostname not in _LOOPBACK_HOSTS or own.hostname != other.hostname:
            return False
        # L10 de la revision de seguridad (16-sep): `self.value` nunca
        # lleva fragmento (`_reject_userinfo_and_fragment` ya lo rechazo
        # al construir este `RedirectUri`), pero `candidate` es una cadena
        # cruda que un llamante podria no haber validado -- comparar el
        # fragmento explicitamente (en vez de ignorarlo) cierra esa via
        # sin depender de que TODO llamante pase primero por
        # `RedirectUri(candidate)`.
        return (
            own.path == other.path
            and own.query == other.query
            and own.fragment == other.fragment
        )

    def __str__(self) -> str:
        return self.value


def _is_forbidden_in_a_client_name(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _FORBIDDEN_CLIENT_NAME_RANGES)


class OAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_name: str,
        redirect_uris: tuple[RedirectUri, ...],
        token_endpoint_auth_method: TokenEndpointAuthMethod,
        client_secret_hash: str | None,
        grant_types: tuple[str, ...],
        requested_scope: ScopeSet,
        created_at: datetime,
        last_seen_at: datetime | None = None,
        state: OAuthClientState = OAuthClientState.REGISTERED,
    ) -> None:
        self._validate_redirect_uris(redirect_uris)
        self._validate_client_name(client_name)
        self._validate_secret_consistency(token_endpoint_auth_method, client_secret_hash)
        self.id = client_id
        self.client_name = client_name
        self.redirect_uris = redirect_uris
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self.client_secret_hash = client_secret_hash
        self.grant_types = grant_types
        self.requested_scope = requested_scope
        self.created_at = created_at
        self.last_seen_at = last_seen_at
        self.state = state

    @staticmethod
    def _validate_redirect_uris(redirect_uris: tuple[RedirectUri, ...]) -> None:
        if not redirect_uris:
            raise InvalidRedirectUriError("se requiere al menos una redirect_uri")
        if len(redirect_uris) > _MAX_REDIRECT_URIS:
            raise TooManyRedirectUrisError(f"maximo {_MAX_REDIRECT_URIS} redirect_uris")

    @staticmethod
    def _validate_client_name(client_name: str) -> None:
        if not client_name.strip():
            raise InvalidClientNameError("client_name vacio")
        if len(client_name) > _MAX_CLIENT_NAME_LENGTH:
            raise InvalidClientNameError(f"client_name supera {_MAX_CLIENT_NAME_LENGTH} caracteres")
        # C-70 pieza 3: el nombre no entra en el mensaje, por lo mismo que
        # en `_reject_control_characters`.
        if any(_is_forbidden_in_a_client_name(char) for char in client_name):
            raise InvalidClientNameError(
                "client_name con caracteres de control, bidireccionales o de anchura cero"
            )

    @staticmethod
    def _validate_secret_consistency(
        method: TokenEndpointAuthMethod, secret_hash: str | None
    ) -> None:
        is_public = method is TokenEndpointAuthMethod.NONE
        if is_public and secret_hash is not None:
            raise PublicClientCannotHaveSecretError("cliente publico no puede tener secreto")
        if not is_public and secret_hash is None:
            raise ConfidentialClientRequiresSecretError(
                "cliente confidencial requiere un secreto hasheado"
            )

    def find_matching_redirect_uri(self, candidate: str) -> RedirectUri | None:
        for redirect_uri in self.redirect_uris:
            if redirect_uri.matches(candidate):
                return redirect_uri
        return None

    def mark_trusted(self, now: datetime) -> None:
        self.state = OAuthClientState.TRUSTED
        self.last_seen_at = now

    def touch(self, now: datetime) -> None:
        self.last_seen_at = now

    def is_prunable(self, now: datetime, ttl: timedelta) -> bool:
        if self.state is OAuthClientState.TRUSTED:
            return False
        return now - self.created_at > ttl
