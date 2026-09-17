"""`canonical_platform_account_id`: la UNICA funcion de canonicalizacion de
un id de cuenta de plataforma en el broker (spec 008 `data-model.md`
§`AccountHardCap` "Clave canonica"). La comparten `set_account_caps`,
`delete_account_caps`, `resolve_account_caps` y la ruta que aplica una
escritura: si el panel guardara bajo otra forma del mismo id, la UI
mostraria un tope que nadie consulta -- fail-closed, si, pero invisible,
que es peor que un rechazo.

Pura y total: no toca disco, no lanza para un id razonable, y es
idempotente (`canonical(canonical(x)) == canonical(x)`), propiedad que un
test comprueba porque de ella depende que la clave guardada y la clave
consultada coincidan siempre.

Solo normaliza las dos formas que el sistema ya trata como canonicas en
otros sitios:
- Google Ads: `customer_id` de 10 digitos sin guiones, la forma que
  devuelve `platform_account_id_from_google_resource_name`
  (`customers/{customer_id}/...`) en la ruta de aplicacion.
- Meta Ads: `act_<id>`, la forma de `_with_account_prefix`
  (`broker/platforms/meta_ads_adapter.py`), a diferencia del `account_id`
  crudo del Graph API.

Cualquier otra forma se devuelve tal cual (recortada): el fichero de topes
es del operador y puede usar el identificador que quiera. Lo que NO se hace
nunca es adivinar: una forma desconocida no se reescribe, se respeta."""

from __future__ import annotations

import re
from typing import Final

_GOOGLE_CUSTOMER_ID: Final = re.compile(r"[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}")
_META_ACCOUNT_PREFIX: Final = "act_"
_MAX_LENGTH: Final = 64

__all__ = [
    "MAX_PLATFORM_ACCOUNT_ID_LENGTH",
    "InvalidPlatformAccountIdError",
    "canonical_platform_account_id",
]

MAX_PLATFORM_ACCOUNT_ID_LENGTH: Final = _MAX_LENGTH


class InvalidPlatformAccountIdError(ValueError):
    """El id de cuenta esta vacio, es demasiado largo o lleva caracteres que
    ningun id de plataforma real contiene (control, separadores de ruta)."""


def canonical_platform_account_id(raw: str) -> str:
    """Forma canonica del id de cuenta DE LA PLATAFORMA, nunca de un
    identificador interno del despliegue."""
    value = raw.strip()
    _assert_shape(value)
    if value[: len(_META_ACCOUNT_PREFIX)].lower() == _META_ACCOUNT_PREFIX:
        return _META_ACCOUNT_PREFIX + value[len(_META_ACCOUNT_PREFIX) :]
    if _GOOGLE_CUSTOMER_ID.fullmatch(value):
        return value.replace("-", "")
    return value


def _assert_shape(value: str) -> None:
    if not value:
        raise InvalidPlatformAccountIdError("platform_account_id_empty")
    if len(value) > _MAX_LENGTH:
        raise InvalidPlatformAccountIdError("platform_account_id_too_long")
    if any(character < " " or character == "\x7f" for character in value):
        raise InvalidPlatformAccountIdError("platform_account_id_control_character")
    if "/" in value or "\\" in value:
        raise InvalidPlatformAccountIdError("platform_account_id_path_separator")
