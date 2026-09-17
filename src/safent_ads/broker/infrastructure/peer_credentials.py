"""`SO_PEERCRED`: identifica el uid real del proceso al otro lado del socket
Unix (contracts/platform-port.md punto 1: "el uid del llamante debe estar en
`ADS_BROKER_ALLOWED_UIDS`. Si no, `DENIED`"). No se fia del modo/propietario
del fichero de socket, solo de las credenciales del kernel."""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Final

_PEERCRED_STRUCT: Final = "3i"  # pid_t, uid_t, gid_t — ABI ucred de Linux


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock: socket.socket | None = writer.get_extra_info("socket")
    if sock is None:
        return None
    raw_creds = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_PEERCRED_STRUCT)
    )
    _pid, uid, _gid = struct.unpack(_PEERCRED_STRUCT, raw_creds)
    return int(uid)


def is_allowed_peer(writer: asyncio.StreamWriter, allowed_uids: frozenset[int]) -> bool:
    uid = peer_uid(writer)
    return uid is not None and uid in allowed_uids
