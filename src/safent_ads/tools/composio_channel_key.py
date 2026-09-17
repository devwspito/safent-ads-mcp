"""Read one base64 master key from stdin; emit only its public recipient key."""

import sys

from safent_ads.broker.infrastructure.composio_channel_key import channel_public_key

_MAX_INPUT_BYTES = 256


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise ValueError("arguments_not_supported")
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if not raw or len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("input_limit")
        public = channel_public_key(raw.decode("ascii").strip())
    except Exception:
        sys.stderr.write("COMPOSIO_CHANNEL_KEY_INVALID_INPUT\n")
        return 2
    sys.stdout.write(public + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
