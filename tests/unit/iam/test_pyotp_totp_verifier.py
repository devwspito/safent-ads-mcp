"""`PyotpTotpVerifier.matched_time_step` (T075/F2-F3 finding 2a): el
contador RFC 6238 que de verdad hizo match, para poder quemarlo despues
-- distinto de `verify_code`, que solo dice si/no."""

from __future__ import annotations

from datetime import UTC, datetime

import pyotp

from safent_ads.iam.infrastructure.pyotp_totp_verifier import PyotpTotpVerifier

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
_SECRET = pyotp.random_base32()


def _totp() -> pyotp.TOTP:
    return pyotp.TOTP(_SECRET)


class TestMatchedTimeStep:
    def test_current_code_matches_current_step(self) -> None:
        verifier = PyotpTotpVerifier()
        code = _totp().at(_NOW)

        step = verifier.matched_time_step(_SECRET, code, at=_NOW)

        assert step == _totp().timecode(_NOW)

    def test_code_one_step_behind_matches_within_tolerance(self) -> None:
        verifier = PyotpTotpVerifier()
        code = _totp().at(_NOW, -1)

        step = verifier.matched_time_step(_SECRET, code, at=_NOW)

        assert step == _totp().timecode(_NOW) - 1

    def test_code_two_steps_behind_is_rejected(self) -> None:
        verifier = PyotpTotpVerifier()
        code = _totp().at(_NOW, -2)

        step = verifier.matched_time_step(_SECRET, code, at=_NOW)

        assert step is None

    def test_wrong_code_is_rejected(self) -> None:
        verifier = PyotpTotpVerifier()
        real_code = _totp().at(_NOW)
        wrong_digit = "1" if real_code[0] != "1" else "2"
        wrong_code = wrong_digit + real_code[1:]

        step = verifier.matched_time_step(_SECRET, wrong_code, at=_NOW)

        assert step is None

    def test_blank_code_is_rejected(self) -> None:
        verifier = PyotpTotpVerifier()

        assert verifier.matched_time_step(_SECRET, "   ", at=_NOW) is None

    def test_distinct_offsets_yield_distinct_steps(self) -> None:
        """El contador devuelto para dos codigos consecutivos (`offset=-1`
        y `offset=0`) difiere en 1 -- es lo que permite que
        `execution_rest._require_reauth` los queme por separado en vez de
        colapsarlos en la misma fila."""
        verifier = PyotpTotpVerifier()
        code_now = _totp().at(_NOW)
        code_before = _totp().at(_NOW, -1)

        step_now = verifier.matched_time_step(_SECRET, code_now, at=_NOW)
        step_before = verifier.matched_time_step(_SECRET, code_before, at=_NOW)

        assert step_now is not None
        assert step_before is not None
        assert step_now - step_before == 1
