"""`HashedIdentity` (data-model.md §LeadAttribution): sal por negocio,
sha256, nunca el dato crudo (threat-model.md C-31, `test_no_pii_in_model_context`).

`compute()` es la unica puerta de entrada: toma el identificador crudo (email,
telefono, gclid...) y la sal de forma transitoria, en memoria, y devuelve solo
el digest. Ni el crudo ni la sal se guardan en el propio value object."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from safent_ads.crm.domain.errors import BlankDigestError, BlankRawIdentifierError, BlankSaltError
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, slots=True)
class HashedIdentity:
    business_id: BusinessId
    digest: str

    @classmethod
    def compute(cls, *, business_id: BusinessId, raw_identifier: str, salt: str) -> HashedIdentity:
        if not raw_identifier.strip():
            raise BlankRawIdentifierError("raw_identifier vacio")
        if not salt.strip():
            raise BlankSaltError("salt vacia")
        normalized = raw_identifier.strip().lower()
        digest = hashlib.sha256(f"{salt}:{normalized}".encode()).hexdigest()
        return cls(business_id=business_id, digest=digest)

    @classmethod
    def from_digest(cls, *, business_id: BusinessId, digest: str) -> HashedIdentity:
        """Reconstruye una identidad ya hasheada rio arriba (p. ej. por el
        CRM externo). Nunca recibe el dato crudo."""
        if not digest.strip():
            raise BlankDigestError("digest vacio")
        return cls(business_id=business_id, digest=digest)

    def __str__(self) -> str:
        return self.digest
