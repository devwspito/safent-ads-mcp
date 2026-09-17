"""Read the running broker's startup digest; never read caps.yaml here.

Fixed companion socket and operation, no arguments or environment settings.
Stdout contains only a validated digest; every failure has a static code.
"""

import asyncio
import json
import re
import sys

from safent_ads.broker.presentation.wire_protocol import FrameClient

_SOCKET = "/run/ads-broker/broker.sock"
_TIMEOUT_SECONDS = 5.0


# spec 008 T030 (decision D14): `panel_state_digest` y `panel_accounts_count`
# son campos NUEVOS y aditivos. Se admiten por su nombre exacto y se validan
# igual de estrictos que los otros dos -- no se ignoran "porque son nuevos",
# que seria la forma de colar cualquier cosa en la respuesta. `caps_digest`
# conserva su significado: los bytes de `config/caps.yaml`, y nada mas.
_EXPECTED_STATUS_FIELDS = frozenset(
    {"caps_digest", "accounts_count", "panel_state_digest", "panel_accounts_count"}
)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _digest(raw: bytes) -> str:
    body = json.loads(raw)
    if not isinstance(body, dict) or set(body) != {"ok", "result"} or body["ok"] is not True:
        raise ValueError("caps_status_invalid")
    result = body["result"]
    if (
        not isinstance(result, dict)
        or not set(result) <= _EXPECTED_STATUS_FIELDS
        or not _is_sha256(result.get("caps_digest"))
        or not _is_count(result.get("accounts_count"))
        or not _is_optional_sha256(result.get("panel_state_digest"))
        or not _is_optional_count(result.get("panel_accounts_count"))
    ):
        raise ValueError("caps_status_invalid")
    digest: str = result["caps_digest"]
    return digest


def _is_optional_sha256(value: object) -> bool:
    """`None` es el valor legitimo de "no se pudo leer el estado del panel"
    y tambien el de "este despliegue no lo tiene"; ausente, el de un broker
    anterior a spec 008."""
    return value is None or _is_sha256(value)


def _is_optional_count(value: object) -> bool:
    return value is None or _is_count(value)


async def _read_status() -> str:
    async with asyncio.timeout(_TIMEOUT_SECONDS):
        reader, writer = await asyncio.open_unix_connection(_SOCKET)
        client = FrameClient(reader, writer, max_frame_bytes=1024)
        try:
            await client.write_frame(b'{"op":"get_hard_caps_status"}')
            return _digest(await client.read_frame())
        finally:
            client.close()
            await client.wait_closed()


def main() -> None:
    if len(sys.argv) != 1:
        print("caps_status_invalid_arguments", file=sys.stderr)
        raise SystemExit(2)
    try:
        digest = asyncio.run(_read_status())
    except Exception:
        print("caps_status_unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({"caps_digest": digest}, separators=(",", ":")))


if __name__ == "__main__":
    main()
