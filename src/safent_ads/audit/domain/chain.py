"""`ChainVerifier` (threat-model.md C-19/C-20): recomputa la cadena de hash
exactamente como el trigger `decision_log_chain()` de `0002_audit_chain.py`:

    canonical := NEW.seq::text || '|' || previous_hash || '|' || NEW.payload::text
    NEW.entry_hash := encode(digest(canonical, 'sha256'), 'hex')

Byte a byte significa no reserializar el JSON en Python: la representacion
textual de un `jsonb` de Postgres no es `json.dumps` (el orden de claves de
`jsonb::text` lo decide el motor, no la insercion). Por eso este verificador
opera sobre `ChainedRow.payload_text`, que la infraestructura obtiene con
`payload::text` en SQL -- el mismo valor que vio el trigger -- en vez de
reconstruir el JSON aqui."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChainedRow:
    """Una fila cruda de `decision_log`, con el texto exacto que Postgres
    produce para su columna `payload` (`payload::text`)."""

    seq: int
    prev_hash: str
    entry_hash: str
    payload_text: str


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    chain_ok: bool
    verified_through_seq: int | None
    broken_at_seq: int | None


class ChainVerifier:
    """Sin estado, sin I/O: recibe filas ya leidas y responde si la cadena
    cuadra. Quien la llama (application/infrastructure) decide de donde
    vienen esas filas."""

    def verify(self, rows: Iterable[ChainedRow]) -> ChainVerificationResult:
        previous_hash = ""
        verified_through_seq: int | None = None

        for row in rows:
            if row.prev_hash != previous_hash:
                return ChainVerificationResult(
                    chain_ok=False,
                    verified_through_seq=verified_through_seq,
                    broken_at_seq=row.seq,
                )
            if row.entry_hash != self._expected_hash(row.seq, previous_hash, row.payload_text):
                return ChainVerificationResult(
                    chain_ok=False,
                    verified_through_seq=verified_through_seq,
                    broken_at_seq=row.seq,
                )
            previous_hash = row.entry_hash
            verified_through_seq = row.seq

        return ChainVerificationResult(
            chain_ok=True, verified_through_seq=verified_through_seq, broken_at_seq=None
        )

    @staticmethod
    def _expected_hash(seq: int, prev_hash: str, payload_text: str) -> str:
        canonical = f"{seq}|{prev_hash}|{payload_text}"
        return hashlib.sha256(canonical.encode()).hexdigest()
