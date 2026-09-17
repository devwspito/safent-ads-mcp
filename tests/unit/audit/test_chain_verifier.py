"""`ChainVerifier` puro (threat-model.md C-19): mismo calculo que el trigger
de `0002_audit_chain.py`, pero contra filas ya leidas -- sin tocar la base
de datos. `test_chain_verifies_after_1000_entries` (T010) corre contra
Postgres real en `tests/integration/test_audit_chain_verifier.py`."""

from __future__ import annotations

import hashlib

from safent_ads.audit.domain.chain import ChainedRow, ChainVerifier


def _hash(seq: int, prev_hash: str, payload_text: str) -> str:
    canonical = f"{seq}|{prev_hash}|{payload_text}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def _chain(payloads: list[str]) -> list[ChainedRow]:
    rows: list[ChainedRow] = []
    prev_hash = ""
    for seq, payload_text in enumerate(payloads, start=1):
        entry_hash = _hash(seq, prev_hash, payload_text)
        rows.append(
            ChainedRow(
                seq=seq, prev_hash=prev_hash, entry_hash=entry_hash, payload_text=payload_text
            )
        )
        prev_hash = entry_hash
    return rows


def test_empty_chain_is_trivially_ok() -> None:
    result = ChainVerifier().verify([])

    assert result.chain_ok is True
    assert result.verified_through_seq is None
    assert result.broken_at_seq is None


def test_valid_chain_verifies_through_last_seq() -> None:
    rows = _chain(['{"a": 1}', '{"a": 2}', '{"a": 3}'])

    result = ChainVerifier().verify(rows)

    assert result.chain_ok is True
    assert result.verified_through_seq == 3
    assert result.broken_at_seq is None


def test_tampered_payload_breaks_the_chain_at_that_seq() -> None:
    rows = _chain(['{"a": 1}', '{"a": 2}', '{"a": 3}'])
    tampered = ChainedRow(
        seq=rows[1].seq,
        prev_hash=rows[1].prev_hash,
        entry_hash=rows[1].entry_hash,
        payload_text='{"a": 999}',
    )
    rows[1] = tampered

    result = ChainVerifier().verify(rows)

    assert result.chain_ok is False
    assert result.verified_through_seq == 1
    assert result.broken_at_seq == 2


def test_broken_prev_hash_link_is_detected() -> None:
    rows = _chain(['{"a": 1}', '{"a": 2}'])
    disconnected = ChainedRow(
        seq=rows[1].seq,
        prev_hash="not-the-real-prev-hash",
        entry_hash=rows[1].entry_hash,
        payload_text=rows[1].payload_text,
    )
    rows[1] = disconnected

    result = ChainVerifier().verify(rows)

    assert result.chain_ok is False
    assert result.broken_at_seq == 2


def test_chain_verifies_after_1000_entries() -> None:
    rows = _chain([f'{{"seq": {i}}}' for i in range(1000)])

    result = ChainVerifier().verify(rows)

    assert result.chain_ok is True
    assert result.verified_through_seq == 1000
