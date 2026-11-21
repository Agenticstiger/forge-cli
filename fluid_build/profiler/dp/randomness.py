# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A seeded randomness source for the noise of a release.

The noise is drawn from a seeded randomness source recorded inside the data
owner's boundary, so the release can be re-derived there. The stream is
HMAC-SHA256 in counter mode (the pseudorandom-function counter construction of
NIST SP 800-108), deterministic on every platform, and uses the standard library
only. Whoever holds the seed and a release can take the noise off it, so the
seed stays where the data is.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SEED_BYTES = 32
"""The length of a seed, in bytes."""

_DOMAIN = b"fluid-profile/1\x00"
_BLOCK_BITS = 256


def new_seed() -> bytes:
    """A fresh seed of :data:`SEED_BYTES` random bytes."""
    return secrets.token_bytes(SEED_BYTES)


def _check_seed(seed: bytes) -> None:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != SEED_BYTES:
        raise ValueError(f"a seed is exactly {SEED_BYTES} bytes")


def _check_label(label: str) -> None:
    # The label ends at a zero byte in every keyed message, so a label that holds one could
    # spell another label's stream: it is refused.
    if not isinstance(label, str) or not label or "\x00" in label:
        raise ValueError("a label is a non-empty string with no zero character")


class SeededSource:
    """A deterministic stream of random bits for one seed and one label.

    The stream is the blocks ``HMAC-SHA256(seed, "fluid-profile/1" 0x00 label
    0x00 counter)`` for the counter 0, 1, 2, ... (8 bytes, big-endian), read as
    one long big-endian bit string from the front.
    """

    def __init__(self, seed: bytes, label: str) -> None:
        _check_seed(seed)
        _check_label(label)
        self._seed = bytes(seed)
        self._prefix = _DOMAIN + label.encode("utf-8") + b"\x00"
        self._counter = 0
        self._acc = 0
        self._held = 0

    def _fill(self, k: int) -> None:
        while self._held < k:
            block = hmac.new(
                self._seed,
                self._prefix + self._counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
            self._counter += 1
            self._acc = (self._acc << _BLOCK_BITS) | int.from_bytes(block, "big")
            self._held += _BLOCK_BITS

    def bits(self, k: int) -> int:
        """A uniform integer in ``[0, 2**k)``, taking the next ``k`` bits."""
        if isinstance(k, bool) or not isinstance(k, int):
            raise TypeError("k must be an int")
        if k < 0:
            raise ValueError("k must not be negative")
        if k == 0:
            return 0
        self._fill(k)
        rest = self._held - k
        value = self._acc >> rest
        self._acc &= (1 << rest) - 1
        self._held = rest
        return value

    def below(self, n: int) -> int:
        """A uniform integer in ``[0, n)``, by rejection on ``(n - 1).bit_length()`` bits."""
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError("n must be an int")
        if n < 1:
            raise ValueError("n must be at least 1")
        k = (n - 1).bit_length()
        while True:
            value = self.bits(k)
            if value < n:
                return value


def priority(seed: bytes, label: str, data: bytes) -> bytes:
    """One keyed hash of *data* under *seed* and *label*, for choosing rows."""
    _check_seed(seed)
    _check_label(label)
    return hmac.new(
        bytes(seed), _DOMAIN + label.encode("utf-8") + b"\x00" + data, hashlib.sha256
    ).digest()
