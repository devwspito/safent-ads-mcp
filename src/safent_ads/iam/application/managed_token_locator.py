"""Untrusted token routing hints. Only EE introspection/consume authenticates."""

import base64
import json
from uuid import UUID

from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied
from safent_ads.shared.managed_ads import ManagedAdsBinding

_MAX_BYTES = 8192


def token_claims(token: str, purpose: str) -> dict[str, object]:
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= _MAX_BYTES:
            raise ValueError
        payload, signature = token.split(".")
        if len(signature) != 128:  # noqa: PLR2004 - Ed25519 hex signature length
            raise ValueError
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
        if (
            not isinstance(claims, dict)
            or claims.get("purpose") != purpose
            or claims.get("aud") != "safent-ads-central"
            or json.dumps(claims, sort_keys=True, separators=(",", ":")).encode() != raw
        ):
            raise ValueError
        return claims
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ManagedAdsDenied("managed_identity_required") from None


def grant_locator(token: str) -> ManagedAdsBinding:
    claims = token_claims(token, "ads_account_delegation")
    for key in ("v", "purpose", "aud", "iat", "exp", "jti"):
        claims.pop(key, None)
    try:
        return ManagedAdsBinding.from_claims(claims)
    except ValueError:
        raise ManagedAdsDenied("managed_identity_required") from None


def human_locator(token: str) -> tuple[UUID, str]:
    claims = token_claims(token, "ads_human_approval")
    try:
        proposal, diff_hash = UUID(str(claims["proposal_id"])), claims["diff_hash"]
        if not isinstance(diff_hash, str) or len(diff_hash) != 64:  # noqa: PLR2004 - SHA256 hex length
            raise ValueError
        return proposal, diff_hash
    except (KeyError, ValueError):
        raise ManagedAdsDenied("managed_identity_required") from None
