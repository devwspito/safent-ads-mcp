"""`SecretsOpaqueTokenFactory` (tasks.md T007, threat-model.md C-44): 256
bits de entropia por token (RFC 6749 §10.10) -- mismo criterio que
`iam/application/session_issuance.py::issue_session`."""

from __future__ import annotations

import secrets

_TOKEN_BYTES = 32  # 256 bits


class SecretsOpaqueTokenFactory:
    def new_token(self) -> str:
        return secrets.token_urlsafe(_TOKEN_BYTES)
