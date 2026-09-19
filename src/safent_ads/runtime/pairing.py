"""Owner-authorized installation: no public writes, redirects or plaintext token escrow."""

import base64
import hashlib
from typing import Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from safent_ads.runtime.connections import RuntimeConnections
from safent_ads.runtime.store import digest, runtime_error

KEY_BITS = 2048


def public_key(encoded: str) -> rsa.RSAPublicKey:
    try:
        key = serialization.load_der_public_key(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid installation public key") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != KEY_BITS:
        raise ValueError("Installation requires a 2048-bit RSA key")
    return key


def oaep() -> padding.OAEP:
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class PairingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: str = Field(pattern=r"^[a-f0-9]{64}$")
    public_key: str = Field(min_length=300, max_length=600)
    runtime: Literal["codex", "claude"]
    label: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        public_key(value)
        return value

    def fingerprint(self) -> str:
        return hashlib.sha256((self.challenge + self.public_key).encode()).hexdigest()[:12].upper()


class PairingPoll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verifier: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuntimePairings:
    def __init__(self, connections: RuntimeConnections):
        self.connections = connections

    async def approve(self, business: str, request: PairingRequest) -> dict[str, object]:
        request_hash = digest(business + request.model_dump_json())
        async with self.connections.sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:challenge))"),
                {"challenge": request.challenge},
            )
            previous = (
                await session.execute(
                    text(
                        "SELECT request_hash,connection_id,expires_at>now() AS live "
                        "FROM runtime_pairings WHERE challenge=:challenge"
                    ),
                    {"challenge": request.challenge},
                )
            ).first()
            if previous:
                if previous.request_hash != request_hash or not previous.live:
                    raise runtime_error("RUNTIME_PAIRING_USED_OR_EXPIRED")
                return {"connection_id": str(previous.connection_id), "approved": True}
            created = await self.connections.create_in_session(
                session, business, request.label, request.runtime
            )
            sealed = public_key(request.public_key).encrypt(created["token"].encode(), oaep())
            await session.execute(
                text("""INSERT INTO runtime_pairings
                (challenge,request_hash,connection_id,sealed_token)
                VALUES (:challenge,:request_hash,CAST(:connection AS uuid),:sealed)"""),
                {
                    "challenge": request.challenge,
                    "request_hash": request_hash,
                    "connection": created["id"],
                    "sealed": base64.b64encode(sealed).decode(),
                },
            )
            # Only authenticated consent creates/cleans pairing records.
            await session.execute(text("DELETE FROM runtime_pairings WHERE expires_at<now()"))
        return {"connection_id": created["id"], "approved": True}

    async def poll(self, verifier: str) -> dict[str, object]:
        async with self.connections.sessions() as session:
            row = (
                await session.execute(
                    text("""SELECT p.sealed_token,p.connection_id,c.business_id,c.runtime
                    FROM runtime_pairings p JOIN runtime_connections c ON c.id=p.connection_id
                    WHERE p.challenge=:challenge AND p.expires_at>now()
                    AND c.revoked_at IS NULL AND c.expires_at>now()"""),
                    {"challenge": digest(verifier)},
                )
            ).first()
        if not row:
            return {"ready": False}
        return {
            "ready": True,
            "sealed_token": row.sealed_token,
            "connection_id": str(row.connection_id),
            "business_id": str(row.business_id),
            "runtime": row.runtime,
        }
