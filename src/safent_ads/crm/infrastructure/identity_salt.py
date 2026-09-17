"""`HkdfIdentitySalt`: `crm.application.ports.IdentitySaltProvider` sobre
`shared/crypto/hkdf.py` (mismo patron que `composition/app.py::
_CREATIVE_PREVIEW_SIGNING_KEY_INFO` -- un `info` fijo y distinto por uso
deriva una subclave de `ADS_SESSION_SECRET`, sin pedirle al propietario un
secreto nuevo por cada uno, security-review-f4.md B-1). El `business_id` se
mezcla en el `info`: la sal nunca es la misma entre dos negocios, aunque
ambos deriven del mismo maestro."""

from __future__ import annotations

from safent_ads.shared.crypto.hkdf import derive_key
from safent_ads.shared.ids import BusinessId

_LEAD_IDENTITY_SALT_INFO_PREFIX = b"safent-ads/lead-identity-salt/v1:"


class HkdfIdentitySalt:
    def __init__(self, master_secret: bytes) -> None:
        self._master_secret = master_secret

    def for_business(self, business_id: BusinessId) -> str:
        info = _LEAD_IDENTITY_SALT_INFO_PREFIX + str(business_id.value).encode()
        return derive_key(self._master_secret, info).hex()
