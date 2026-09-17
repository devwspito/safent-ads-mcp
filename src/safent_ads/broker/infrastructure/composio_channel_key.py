"""One domain-separated recipient key derivation for broker and provisioning."""

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from safent_ads.broker.infrastructure.credential_store import _decode_master_key


def derive_channel_private_key(master_key_b64: str) -> X25519PrivateKey:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"safent-ads-broker:composio-channel-key:v1",
    ).derive(_decode_master_key(master_key_b64))
    return X25519PrivateKey.from_private_bytes(raw)


def channel_public_key(master_key_b64: str) -> str:
    public = (
        derive_channel_private_key(master_key_b64)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.b64encode(public).decode("ascii")
