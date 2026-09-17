"""Short, session-bound proof of one explicit owner confirmation, never MFA."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

CONFIRMATION_TTL = timedelta(seconds=120)
_MAX_TOKEN_LENGTH = 2048
_PURPOSE = "ads_owner_action_confirmation"


class ConfirmationError(ValueError):
    """Only stable error codes cross the boundary, never token or request data."""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


class ActionConfirmationCodec:
    def __init__(self, secret: str):
        self._key = hmac.digest(secret.encode(), _PURPOSE.encode(), "sha256")

    def binding(
        self, *, session_id: UUID, method: str, path: str, query: str, body: bytes, action: str
    ) -> str:
        # HMAC rather than a plain payload hash: low-entropy credentials must
        # not become offline dictionary targets in the token or database.
        material = json.dumps(
            [str(session_id), method, path, query, hashlib.sha256(body).hexdigest(), action],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hmac.digest(self._key, material, "sha256").hex()

    def issue(self, *, binding: str, now: datetime) -> tuple[str, datetime]:
        expires = (now + CONFIRMATION_TTL).replace(microsecond=0)
        payload = json.dumps(
            {
                "v": 1,
                "purpose": _PURPOSE,
                "nonce": str(uuid4()),
                "binding": binding,
                "iat": int(now.timestamp()),
                "exp": int(expires.timestamp()),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"{_encode(payload)}.{_encode(hmac.digest(self._key, payload, 'sha256'))}", expires

    def verify(self, token: str, *, binding: str, now: datetime) -> tuple[UUID, int]:
        try:
            if len(token) > _MAX_TOKEN_LENGTH:
                raise ValueError
            encoded, signature = token.split(".")
            payload = _decode(encoded)
            if not hmac.compare_digest(
                hmac.digest(self._key, payload, "sha256"), _decode(signature)
            ):
                raise ValueError
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {
                "v",
                "purpose",
                "nonce",
                "binding",
                "iat",
                "exp",
            }:
                raise ValueError
            if type(data["v"]) is not int or data["v"] != 1 or data["purpose"] != _PURPOSE:
                raise ValueError
            if not isinstance(data["binding"], str) or not hmac.compare_digest(
                data["binding"], binding
            ):
                raise ValueError
            if type(data["iat"]) is not int or type(data["exp"]) is not int:
                raise ValueError
            if data["exp"] - data["iat"] != int(CONFIRMATION_TTL.total_seconds()):
                raise ValueError
            nonce = UUID(data["nonce"])
            if now.timestamp() < data["iat"]:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ConfirmationError("CONFIRMATION_INVALID") from exc
        if now.timestamp() >= data["exp"]:
            raise ConfirmationError("CONFIRMATION_EXPIRED")
        return nonce, data["exp"]
