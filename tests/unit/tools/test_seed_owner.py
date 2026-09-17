"""`tools.seed_owner` (008-mcp-ads-estandar T017): la contrasena SOLO
llega por stdin -- nunca por `--password` en argv (quedaba en el
historial del shell y en `ps`, NFR-001)."""

from __future__ import annotations

import io

import pytest

from safent_ads.tools.seed_owner import (
    MissingPasswordError,
    _parse_args,
    _read_password_from_stdin,
)


def test_password_argument_no_longer_exists() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--email", "owner@example.com", "--password", "leaked-in-argv"])


def test_password_stdin_is_required() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--email", "owner@example.com"])


def test_parses_email_and_password_stdin_flag() -> None:
    args = _parse_args(["--email", "owner@example.com", "--password-stdin"])

    assert args.email == "owner@example.com"
    assert args.password_stdin is True


def test_reads_password_from_stdin_without_a_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = io.StringIO("s3cr3t")
    monkeypatch.setattr(stdin, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", stdin)

    assert _read_password_from_stdin() == "s3cr3t"


def test_strips_exactly_one_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = io.StringIO("s3cr3t\n")
    monkeypatch.setattr(stdin, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", stdin)

    assert _read_password_from_stdin() == "s3cr3t"


def test_empty_stdin_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = io.StringIO("")
    monkeypatch.setattr(stdin, "isatty", lambda: False)
    monkeypatch.setattr("sys.stdin", stdin)

    with pytest.raises(MissingPasswordError, match="stdin no traia"):
        _read_password_from_stdin()


def test_a_tty_is_rejected_never_typed_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = io.StringIO("s3cr3t")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", stdin)

    with pytest.raises(MissingPasswordError, match="TTY"):
        _read_password_from_stdin()
