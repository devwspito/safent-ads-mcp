"""ETag helper for read-heavy panel endpoints (perf 16-sep, item 5): the
panel polls list endpoints on a fixed interval and most polls see no
change, so hashing the JSON body once and answering `304` on a matching
`If-None-Match` turns most of those polls into an empty body instead of
the full payload. Same intent as `panel/presentation/cockpit_rest.py`'s
own `_compute_etag` (spec 026, which also layers a short TTL memo cache
on top for its own reasons) -- factored out here for read models that
only need the ETag/304 half, not a server-side memo cache."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

__all__ = ["compute_json_etag", "json_response_with_etag"]

# `must-revalidate` (not `no-store`): the panel is allowed to reuse the
# last body ONLY after asking with `If-None-Match` -- never silently, and
# never across owners (`private`).
_CACHE_CONTROL = "private, max-age=0, must-revalidate"


def compute_json_etag(body: dict[str, Any]) -> str:
    """`body` must already be JSON-safe (run it through `jsonable_encoder`
    first) -- this only hashes it, it does not convert domain types."""
    canonical = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def json_response_with_etag(request: Request, body: dict[str, Any]) -> Response:
    etag = compute_json_etag(body)
    headers = {"cache-control": _CACHE_CONTROL, "etag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)
