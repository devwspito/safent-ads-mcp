"""`StorageUri`: solo claves de almacen, nunca URLs libres
(threat-model.md C-11/C-28)."""

from __future__ import annotations

import pytest

from safent_ads.creative.domain.storage import StorageUri, StorageUriFormatError


def test_valid_key() -> None:
    uri = StorageUri("images/2026/01/abc.png")

    assert str(uri) == "images/2026/01/abc.png"


@pytest.mark.parametrize(
    "bad_key",
    ["", "../../etc/passwd", "/etc/passwd", "images/../secret", "http://evil.example/x"],
)
def test_rejects_traversal_and_absolute_paths(bad_key: str) -> None:
    with pytest.raises(StorageUriFormatError):
        StorageUri(bad_key)
