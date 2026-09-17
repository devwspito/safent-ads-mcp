"""`LoopbackAwareClientInformation` (contracts/oauth.md §4, threat-model.md
C-37, tasks.md T008): la unica forma en que `/authorize` y `/token` toleran
el puerto de bucle local variable de RFC 8252 §7.3.

El SDK valida `redirect_uri` con igualdad exacta contra
`OAuthClientInformationFull.redirect_uris`
(`.venv/.../mcp/shared/auth.py::validate_redirect_uri`) -- eso rechazaria
un segundo lanzamiento de Claude Code/Codex que reutiliza el mismo
`redirect_uri` registrado pero con OTRO puerto libre. Esta subclase delega
la comparacion tolerante en `domain.client.RedirectUri.matches()` (la
UNICA implementacion de esa regla, C-9: el dominio ya la ejercita en
`test_redirect_uri.py`) y devuelve la URI **pedida**, no la registrada --
`sdk:handlers/token.py:166-183` compara ese valor, tal cual, contra lo que
el cliente mande en `/token`.

L10 de la revision de seguridad (16-sep): el candidato tambien pasa por
`RedirectUri()` -- no solo las registradas -- para que las mismas reglas
de forma (sin userinfo, sin fragmento, esquema/host permitidos) se
apliquen a lo que el cliente PRESENTA, no solo a lo que quedo registrado.
Un candidato que no las cumple nunca puede "coincidir" con nada: se
rechaza aqui mismo, antes de comparar."""

from __future__ import annotations

from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl

from safent_ads.mcp_oauth.domain.client import RedirectUri
from safent_ads.mcp_oauth.domain.errors import (
    InvalidRedirectUriError as DomainInvalidRedirectUriError,
)


class LoopbackAwareClientInformation(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            return super().validate_redirect_uri(redirect_uri)
        if self._matches_a_registered_uri(str(redirect_uri)):
            return redirect_uri
        raise InvalidRedirectUriError(f"redirect_uri no registrada: {redirect_uri!r}")

    def _matches_a_registered_uri(self, candidate: str) -> bool:
        try:
            RedirectUri(candidate)
        except DomainInvalidRedirectUriError:
            return False
        registered = self.redirect_uris or []
        return any(RedirectUri(str(uri)).matches(candidate) for uri in registered)
