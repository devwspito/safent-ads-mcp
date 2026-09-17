"""Sonda operativa del socket del bróker (`$ADS_BROKER_SOCKET`,
contracts/platform-port.md): corre DENTRO de `ads-api` (mismo uid que
`accounts.infrastructure.broker_client.BrokerSocketClient`, `adsapi` en
`ADS_BROKER_ALLOWED_UIDS`) y comprueba en caliente que el bróker sigue vivo
y sigue denegando cualquier intento de escritura sin autorización -- "No
existe una variante de escritura sin autorización" (contracts/platform-port.md
punto 60). `tests/smoke/f0_smoke.sh` la usa tras cada despliegue.

El framing (uint32 big-endian + JSON) se repite aqui en vez de reutilizar
`BrokerSocketClient` a propósito: esta sonda envía DELIBERADAMENTE una
petición sin el objeto `authorization` -- un comando "sin firmar" en el
sentido más literal, que ni siquiera declara tener una firma -- y el
cliente tipado no permite construir una petición así (`SignedAuthorization`
exige todos sus campos). Duplicar ~20 líneas es más barato que forzar al
cliente real a exponer una vía sin tipar solo para esta sonda (mismo
criterio que `accounts/infrastructure/broker_client.py`).

Uso:
    python -m safent_ads.tools.broker_probe --write

Imprime un único objeto JSON a stdout con la respuesta cruda del bróker.
Para un comando sin firmar, el esquema tipado del bróker
(`broker/presentation/request_schemas.py::ExecuteWriteRequest`) lo rechaza
antes de que ningún adaptador lo vea:

    {"ok": false, "error_code": "DENIED", "reason": "invalid_schema"}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
from typing import Any, Final

from safent_ads.composition.settings import ApiSettings

_LENGTH_PREFIX_FORMAT: Final = ">I"
_LENGTH_PREFIX_SIZE: Final = struct.calcsize(_LENGTH_PREFIX_FORMAT)
_TIMEOUT_SECONDS: Final = 10.0

# Deliberadamente sin "authorization": contracts/platform-port.md dice que
# no existe una vía de escritura sin autorización -- esta petición ni
# siquiera declara tener una. `entity_ref` no necesita resolver a una
# entidad ni a un adaptador real: la validación del esquema ocurre ANTES de
# que `broker/presentation/dispatcher.py` resuelva ningún adaptador.
_UNSIGNED_WRITE_REQUEST: Final[dict[str, Any]] = {
    "op": "execute_write",
    "entity_ref": "google:campaign:customers/000000000/campaigns/1",
    "operation": "PAUSE",
    "parametro": "status",
    "valor_actual": "ACTIVE",
    "valor_propuesto": "PAUSED",
    "diff_hash": "0" * 64,
    "expected_state_hash": "0" * 64,
    "idempotency_key": "broker-probe-unsigned",
}


async def _send(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path), timeout=_TIMEOUT_SECONDS
    )
    try:
        body = json.dumps(payload).encode("utf-8")
        writer.write(struct.pack(_LENGTH_PREFIX_FORMAT, len(body)) + body)
        await writer.drain()
        header = await asyncio.wait_for(
            reader.readexactly(_LENGTH_PREFIX_SIZE), timeout=_TIMEOUT_SECONDS
        )
        (length,) = struct.unpack(_LENGTH_PREFIX_FORMAT, header)
        raw_response = await asyncio.wait_for(
            reader.readexactly(length), timeout=_TIMEOUT_SECONDS
        )
    finally:
        writer.close()
        await writer.wait_closed()
    response: dict[str, Any] = json.loads(raw_response)
    return response


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        required=True,
        help="Envia execute_write sin autorizacion y muestra el veredicto del broker.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    _parse_args(argv)
    settings = ApiSettings()  # type: ignore[call-arg]
    response = asyncio.run(_send(str(settings.broker_socket_path), _UNSIGNED_WRITE_REQUEST))
    print(json.dumps(response))


if __name__ == "__main__":
    main()
