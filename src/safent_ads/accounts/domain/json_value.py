"""Alias de tipo `JsonValue`: forma canonica de un valor JSON, usado por
`PlatformStateHash` (canonicalizacion) y por `WriteIntent`/`WriteOutcome`
(contracts/platform-port.md)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

JsonValue = None | bool | int | float | str | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
