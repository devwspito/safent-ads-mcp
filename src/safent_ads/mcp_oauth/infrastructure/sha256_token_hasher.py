"""`Sha256TokenHasher` (tasks.md T007, threat-model.md C-44): unico hasheo
de tokens opacos del AS. Sin argon2 a proposito -- el token ya tiene 256
bits de entropia propios (RFC 6749 §10.10), no hay diccionario que atacar,
y argon2 en cada llamada a `/mcp` seria un DoS propio."""

from __future__ import annotations

import hashlib

from safent_ads.mcp_oauth.domain.grant import TokenHash


class Sha256TokenHasher:
    def hash(self, raw_token: str) -> TokenHash:
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        return TokenHash(digest)
