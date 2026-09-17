"""Cablea `ApprovalSigner`/`ApprovalVerifier` (`shared/crypto/ed25519.py`) a
los puertos locales que `proposals.domain.authorization` declara
(`SignerPort`/`VerifierPort`, `sign(bytes) -> bytes` / `verify(bytes, bytes)
-> bool`). Los dos módulos usan la MISMA canonicalización JSON (claves
ordenadas, sin espacios, UTF-8) pero con formas de entrada distintas: el
dominio ya entrega bytes canónicos (`Authorization.signing_payload()`,
`proposals.domain.diff_hash.canonical_json_bytes`), mientras que
`ApprovalSigner`/`ApprovalVerifier` esperan el `Mapping` y canonicalizan
ellos mismos. Decodificar y dejar que canonicalicen de nuevo produce
exactamente los mismos bytes -- no hay pérdida ni segunda fuente de verdad.

Solo `ads-api` firma (plan.md §3.1: "único proceso con la clave privada").
`ads-worker` la necesita igualmente para acuñar `rule_authorization` desde
`RuleCycle` (plan.md §7) -- `compose.yaml` ya comparte `secrets/api.env`
entre ambos servicios (mismo usuario del SO `adsapi`, distinto de
`adsbroker`), así que esto no abre superficie nueva: hace explícito en
`WorkerSettings` un acceso que el despliegue ya concede. Pendiente de
confirmación en la revisión de seguridad ya agendada (`tasks.md` T075,
threat-model.md C-3)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from safent_ads.proposals.domain.authorization import SignerPort, VerifierPort
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier

__all__ = ["ApprovalKeyPair", "build_approval_key_pair"]


class _SignerPortAdapter:
    """`SignerPort` sobre `ApprovalSigner`."""

    def __init__(self, signer: ApprovalSigner) -> None:
        self._signer = signer

    def sign(self, payload: bytes) -> bytes:
        return self._signer.sign(json.loads(payload))


class _VerifierPortAdapter:
    """`VerifierPort` sobre `ApprovalVerifier`."""

    def __init__(self, verifier: ApprovalVerifier) -> None:
        self._verifier = verifier

    def verify(self, payload: bytes, signature: bytes) -> bool:
        return self._verifier.verify(json.loads(payload), signature)


@dataclass(frozen=True, slots=True)
class ApprovalKeyPair:
    """El signer real más un verifier derivado de su propia clave pública:
    el chokepoint re-verifica en `ads-api`/`ads-worker` la autorización que
    el mismo proceso acuñó (defensa en profundidad antes de hablar con el
    bróker, que hace su propia verificación independiente con la clave
    pública que sólo él tiene por `BrokerSettings`)."""

    signer: SignerPort
    verifier: VerifierPort


def build_approval_key_pair(signing_key_b64: str) -> ApprovalKeyPair:
    approval_signer = ApprovalSigner.from_seed_b64(signing_key_b64)
    approval_verifier = ApprovalVerifier.from_public_key_b64(approval_signer.public_key_b64())
    return ApprovalKeyPair(
        signer=_SignerPortAdapter(approval_signer),
        verifier=_VerifierPortAdapter(approval_verifier),
    )
