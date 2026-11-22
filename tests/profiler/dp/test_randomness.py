# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The seeded randomness source: determinism, the stream's definition, uniformity."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from fluid_build.profiler.dp.randomness import SEED_BYTES, SeededSource, new_seed, priority

pytestmark = pytest.mark.unit

SEED = bytes(range(32))


def _block(seed: bytes, label: str, counter: int) -> bytes:
    message = b"fluid-profile/1\x00" + label.encode("utf-8") + b"\x00" + counter.to_bytes(8, "big")
    return hmac.new(seed, message, hashlib.sha256).digest()


def test_new_seed_is_32_fresh_bytes():
    first, second = new_seed(), new_seed()
    assert SEED_BYTES == 32
    assert len(first) == 32 and len(second) == 32
    assert first != second


def test_same_seed_and_label_give_the_same_draws():
    a, b = SeededSource(SEED, "t"), SeededSource(SEED, "t")
    assert [a.below(1000) for _ in range(50)] == [b.below(1000) for _ in range(50)]


def test_another_label_or_seed_gives_other_draws():
    base = [SeededSource(SEED, "t").bits(64) for _ in range(1)]
    other_label = [SeededSource(SEED, "u").bits(64)]
    other_seed = [SeededSource(bytes(32), "t").bits(64)]
    assert base != other_label
    assert base != other_seed


def test_bits_read_the_hmac_blocks_front_to_back():
    blocks = b"".join(_block(SEED, "test", i) for i in range(3))
    stream = int.from_bytes(blocks, "big")
    total = 768
    source = SeededSource(SEED, "test")
    taken = 0
    for k in (1, 7, 64, 0, 255, 256, 3, 100, 82):  # crosses block edges; sums to 768
        expected = (stream >> (total - taken - k)) & ((1 << k) - 1)
        assert source.bits(k) == expected
        taken += k
    assert taken == total


def test_below_is_rejection_on_the_next_bits():
    stream = int.from_bytes(_block(SEED, "test", 0), "big")
    taken = 0
    expected = []
    while len(expected) < 20:
        value = (stream >> (256 - taken - 3)) & 7  # n = 6 needs 3 bits
        taken += 3
        if value < 6:
            expected.append(value)
    source = SeededSource(SEED, "test")
    assert [source.below(6) for _ in range(20)] == expected


def test_below_one_is_zero_and_consumes_nothing():
    a, b = SeededSource(SEED, "x"), SeededSource(SEED, "x")
    assert [a.below(1) for _ in range(5)] == [0] * 5
    assert a.bits(64) == b.bits(64)


def test_below_frequencies_are_uniform():
    source = SeededSource(SEED, "chi")
    draws = 60_000
    counts = [0] * 6
    for _ in range(draws):
        counts[source.below(6)] += 1
    expected = draws / 6
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    # 5 degrees of freedom: the 0.999 quantile is 20.5; the seed is fixed, so this never flakes.
    assert chi2 < 20.5


def test_priority_is_one_keyed_hash():
    expected = hmac.new(SEED, b"fluid-profile/1\x00rows\x00" + b"row-7", hashlib.sha256).digest()
    assert priority(SEED, "rows", b"row-7") == expected
    assert priority(SEED, "rows", b"row-8") != expected


@pytest.mark.parametrize(
    "call",
    [
        lambda: SeededSource(bytes(31), "t"),
        lambda: SeededSource(bytes(33), "t"),
        lambda: SeededSource(SEED, ""),
        lambda: SeededSource(SEED, "t").below(0),
        lambda: SeededSource(SEED, "t").below(-3),
        lambda: SeededSource(SEED, "t").bits(-1),
        lambda: priority(bytes(31), "t", b""),
        lambda: priority(SEED, "", b""),
        # A zero character ends the label in every keyed message, so it is refused.
        lambda: SeededSource(SEED, "a\x00b"),
        lambda: priority(SEED, "a\x00", b"b"),
    ],
)
def test_refusals_raise_value_error(call):
    with pytest.raises(ValueError):
        call()


@pytest.mark.parametrize("bad", [True, 2.0, "3", None])
def test_below_refuses_a_non_int(bad):
    with pytest.raises(TypeError):
        SeededSource(SEED, "t").below(bad)


def test_known_answer_vector():
    first = SeededSource(SEED, "test")
    assert first.bits(64) == KNOWN_BITS_64
    second = SeededSource(SEED, "test")
    assert [second.below(10) for _ in range(10)] == KNOWN_BELOW_10


KNOWN_BITS_64 = 7602785410567763006
KNOWN_BELOW_10 = [6, 9, 8, 2, 8, 5, 0, 4, 6, 9]
