"""Validated Google Tag Manager changes carried by a signed native write.

The MCP deliberately exposes a small resource-oriented surface instead of an
arbitrary HTTP proxy.  A proposal may create/update one workspace entity,
create a version, or publish an existing version.  The provider body travels
as canonical JSON text so the existing native-write envelope remains flat and
bounded; it is parsed and validated again inside the broker.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "GTM_CHANGE_OPERATION",
    "GoogleTagManagerChangeError",
    "parse_google_tag_manager_change",
    "validate_google_tag_manager_path",
]

GTM_CHANGE_OPERATION: Final = "gtm_change"

_ACTIONS = frozenset(
    {
        "create_workspace",
        "create_tag",
        "update_tag",
        "create_trigger",
        "update_trigger",
        "create_variable",
        "update_variable",
        "create_version",
        "publish_version",
    }
)
_PATH_PATTERN = re.compile(
    r"^accounts/[A-Za-z0-9_-]+"
    r"(?:/containers/[A-Za-z0-9_-]+"
    r"(?:/workspaces/[A-Za-z0-9_-]+(?:/(?:tags|triggers|variables)/[A-Za-z0-9_-]+)?"
    r"|/versions/[A-Za-z0-9_-]+)?"
    r")?$"
)
_MAX_BODY_BYTES = 5 * 1024
_MAX_DEPTH = 8
_MAX_ITEMS = 200
_MAX_FINGERPRINT_LENGTH = 256
_MAX_KEY_LENGTH = 100
_CONTAINER_PATH_SEGMENTS = 4
_WORKSPACE_OR_VERSION_PATH_SEGMENTS = 6
_WORKSPACE_ENTITY_PATH_SEGMENTS = 8
_SERVER_MANAGED_FIELDS = frozenset(
    {
        "accountId",
        "containerId",
        "workspaceId",
        "tagId",
        "triggerId",
        "variableId",
        "path",
        "fingerprint",
    }
)
_TOKEN_LIKE = re.compile(r"(?:access|refresh|oauth)?_?token|client_?secret|password", re.I)


class GoogleTagManagerChangeError(ValueError):
    """The proposed GTM mutation is outside the closed, auditable surface."""


def parse_google_tag_manager_change(payload: Mapping[str, object]) -> dict[str, Any]:
    if set(payload) - {"action", "parent_path", "resource_path", "body_encoded", "fingerprint"}:
        raise GoogleTagManagerChangeError("campos GTM no admitidos")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise GoogleTagManagerChangeError("accion GTM no admitida")

    parent = _optional_path(payload.get("parent_path"), "parent_path")
    resource = _optional_path(payload.get("resource_path"), "resource_path")
    fingerprint = payload.get("fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or len(fingerprint) > _MAX_FINGERPRINT_LENGTH
    ):
        raise GoogleTagManagerChangeError("fingerprint GTM invalido")

    body = _parse_body(payload.get("body_encoded"))
    _validate_change_shape(action, parent=parent, resource=resource, body=body)

    return {
        "action": action,
        "parent_path": parent,
        "resource_path": resource,
        "body": body,
        "fingerprint": fingerprint,
    }


def _validate_change_shape(
    action: str,
    *,
    parent: str | None,
    resource: str | None,
    body: dict[str, Any] | None,
) -> None:
    if action.startswith("create_") and action != "create_version":
        if parent is None or resource is not None or body is None:
            raise GoogleTagManagerChangeError("crear requiere parent_path y body_encoded")
        expected_segments = (
            _CONTAINER_PATH_SEGMENTS
            if action == "create_workspace"
            else _WORKSPACE_OR_VERSION_PATH_SEGMENTS
        )
        if len(parent.split("/")) != expected_segments:
            raise GoogleTagManagerChangeError("parent_path no corresponde a la accion GTM")
        return
    if action.startswith("update_"):
        if resource is None or parent is not None or body is None:
            raise GoogleTagManagerChangeError("actualizar requiere resource_path y body_encoded")
        expected_collection = f"{action.removeprefix('update_')}s"
        segments = resource.split("/")
        if len(segments) != _WORKSPACE_ENTITY_PATH_SEGMENTS or segments[-2] != expected_collection:
            raise GoogleTagManagerChangeError("resource_path no corresponde a la accion GTM")
        return
    if action == "create_version":
        if resource is None or parent is not None or body is None:
            raise GoogleTagManagerChangeError(
                "crear version requiere el resource_path del workspace y body_encoded"
            )
        if (
            len(resource.split("/")) != _WORKSPACE_OR_VERSION_PATH_SEGMENTS
            or "/workspaces/" not in resource
        ):
            raise GoogleTagManagerChangeError("create_version requiere una ruta de workspace")
        return
    if action == "publish_version":
        if resource is None or parent is not None or body is not None:
            raise GoogleTagManagerChangeError(
                "publicar requiere el resource_path de la version y no admite body_encoded"
            )
        if (
            len(resource.split("/")) != _WORKSPACE_OR_VERSION_PATH_SEGMENTS
            or "/versions/" not in resource
        ):
            raise GoogleTagManagerChangeError("publish_version requiere una ruta de version")


def _optional_path(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _PATH_PATTERN.fullmatch(value):
        raise GoogleTagManagerChangeError(f"{field} no es una ruta GTM valida")
    return value


def validate_google_tag_manager_path(value: str) -> str:
    if not _PATH_PATTERN.fullmatch(value):
        raise GoogleTagManagerChangeError("ruta GTM invalida")
    return value


def _parse_body(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > (_MAX_BODY_BYTES * 4 // 3) + 4:
        raise GoogleTagManagerChangeError("body_encoded GTM invalido o demasiado grande")
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) > _MAX_BODY_BYTES:
            raise GoogleTagManagerChangeError("body_encoded GTM demasiado grande")
        parsed = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise GoogleTagManagerChangeError("body_encoded GTM no contiene JSON valido") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise GoogleTagManagerChangeError("body_encoded GTM debe contener un objeto no vacio")
    _validate_json(parsed, depth=1)
    if _SERVER_MANAGED_FIELDS.intersection(parsed):
        raise GoogleTagManagerChangeError("el cuerpo contiene campos gestionados por Google")
    return parsed


def _validate_json(value: object, *, depth: int) -> int:
    if depth > _MAX_DEPTH:
        raise GoogleTagManagerChangeError("el cuerpo GTM supera la profundidad permitida")
    if value is None or isinstance(value, bool | int | float | str):
        return 1
    if isinstance(value, list):
        count = 1
        for item in value:
            count += _validate_json(item, depth=depth + 1)
            if count > _MAX_ITEMS:
                raise GoogleTagManagerChangeError("el cuerpo GTM contiene demasiados elementos")
        return count
    if isinstance(value, dict):
        count = 1
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > _MAX_KEY_LENGTH or _TOKEN_LIKE.search(key):
                raise GoogleTagManagerChangeError("el cuerpo GTM contiene una clave no permitida")
            count += _validate_json(item, depth=depth + 1)
            if count > _MAX_ITEMS:
                raise GoogleTagManagerChangeError("el cuerpo GTM contiene demasiados elementos")
        return count
    raise GoogleTagManagerChangeError("el cuerpo GTM contiene un tipo no permitido")
