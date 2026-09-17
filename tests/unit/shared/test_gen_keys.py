"""`tools/gen_keys.py` (quickstart.md §1): escribe las dos claves en
ficheros separados con permisos 0600."""

from __future__ import annotations

import base64
import stat
from pathlib import Path

from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.tools.gen_keys import main


def test_gen_keys_writes_files_with_owner_only_permissions(tmp_path: Path) -> None:
    out_dir = tmp_path / "secrets"

    main(["--out-dir", str(out_dir)])

    private_path = out_dir / "approval_signing_key.b64"
    public_path = out_dir / "approval_public_key.b64"
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o600


def test_gen_keys_produces_a_usable_key_pair(tmp_path: Path) -> None:
    out_dir = tmp_path / "secrets"

    main(["--out-dir", str(out_dir)])

    seed_b64 = (out_dir / "approval_signing_key.b64").read_text().strip()
    public_b64 = (out_dir / "approval_public_key.b64").read_text().strip()
    base64.b64decode(seed_b64, validate=True)

    signer = ApprovalSigner.from_seed_b64(seed_b64)
    verifier = ApprovalVerifier.from_public_key_b64(public_b64)
    signature = signer.sign({"ok": True})

    assert verifier.verify({"ok": True}, signature) is True
