# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Coverage for the deterministic DV2 hash-key + hash-diff derivation.

Closes plan-gap A2 — until now ``forge_datamodel/dv2/hash_keys.py`` was
shipped without a single direct test, so any change to canonicalisation
(NULL token, delimiter, casing, sort order) could silently flip the
hex output and break every downstream pipeline that pinned a value to a
satellite key. The pins below lock the wire-format behaviour the
plan's prescription — ``sort → null-token → delimiter → uppercase →
md5/sha256`` — promises.

The two functions under test serve two different roles:

* :func:`compute_hash_key` — **order-sensitive.** A ``(party, product)``
  business key is not the same as ``(product, party)``; reordering must
  flip the digest.
* :func:`compute_hash_diff` — **order-insensitive.** Re-ordering columns
  in a satellite must not invalidate prior change-detection — the
  attribute set is canonicalised by sorting names before serialising.

The tests assert *both* directions: that order matters where it should,
and that it doesn't where it shouldn't. Anything that tightens or
relaxes that contract should fail one of these cases loudly.
"""

from __future__ import annotations

import hashlib

import pytest

from fluid_build.copilot.schemas.data_model import HashKeyStrategy
from fluid_build.forge_datamodel.dv2.hash_keys import (
    compute_hash_diff,
    compute_hash_key,
)

# ----------------------------------------------------------------------
# Strategy defaults — pin them so silent default drift fails fast
# ----------------------------------------------------------------------


class TestHashKeyStrategyDefaults:
    def test_default_strategy_uses_md5_with_known_separators(self):
        s = HashKeyStrategy()
        assert s.algorithm == "md5"
        assert s.delimiter == "||"
        assert s.null_token == "__NULL__"
        assert s.upper_case is True

    def test_string_coercion_yields_named_algorithm(self):
        """``HashKeyStrategy("sha256")`` must be equivalent to
        ``HashKeyStrategy(algorithm="sha256")`` so the modeler can emit
        a strategy as either shape."""
        s = HashKeyStrategy.model_validate("sha256")
        assert s.algorithm == "sha256"
        assert s.delimiter == "||"


# ----------------------------------------------------------------------
# compute_hash_key — order-sensitive business keys
# ----------------------------------------------------------------------


class TestComputeHashKey:
    def test_default_strategy_pinned_md5(self):
        """Lock the wire format for the default strategy.

        ``ACME||CUSTOMER_42`` is the canonical payload after upper-case +
        delimiter join; the md5 of that is the value we ship downstream
        as the hub hash key."""
        s = HashKeyStrategy()
        digest = compute_hash_key(["acme", "customer_42"], s)
        expected = hashlib.md5("ACME||CUSTOMER_42".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_sha256_strategy_pinned(self):
        s = HashKeyStrategy(algorithm="sha256")
        digest = compute_hash_key(["acme", "customer_42"], s)
        expected = hashlib.sha256("ACME||CUSTOMER_42".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_order_is_significant(self):
        """Different column orders must produce different digests —
        otherwise ``(party, product)`` and ``(product, party)`` would
        collide and DV2 link semantics would break."""
        s = HashKeyStrategy()
        a = compute_hash_key(["alpha", "beta"], s)
        b = compute_hash_key(["beta", "alpha"], s)
        assert a != b

    def test_null_and_empty_collapse_to_null_token(self):
        """``None`` and ``""`` must both render as ``null_token`` — the
        hash must not distinguish "absent" from "empty" because
        downstream pipelines treat them as the same load-time signal."""
        s = HashKeyStrategy()
        d_none = compute_hash_key(["x", None], s)
        d_empty = compute_hash_key(["x", ""], s)
        d_whitespace = compute_hash_key(["x", "   "], s)
        assert d_none == d_empty == d_whitespace

    def test_whitespace_is_trimmed_before_hashing(self):
        """Leading/trailing whitespace would otherwise be a frequent
        source of false key drift between source extracts."""
        s = HashKeyStrategy()
        a = compute_hash_key(["acme", "customer_42"], s)
        b = compute_hash_key(["  acme ", "customer_42  "], s)
        assert a == b

    def test_upper_case_toggle(self):
        """Disabling ``upper_case`` must preserve original casing."""
        s = HashKeyStrategy(upper_case=False)
        digest = compute_hash_key(["AcMe", "CuStOmEr"], s)
        expected = hashlib.md5("AcMe||CuStOmEr".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_custom_delimiter_and_null_token(self):
        """Operators with downstream tooling that already speaks a
        different delimiter convention must be able to swap it without
        forking the helper."""
        s = HashKeyStrategy(delimiter=":", null_token="<NULL>")
        digest = compute_hash_key(["a", None, "b"], s)
        expected = hashlib.md5("A:<NULL>:B".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_numeric_and_boolean_values_coerce_via_str(self):
        """Business keys are not always strings; ``42`` and ``True``
        must hash identically to ``"42"`` and ``"True"``."""
        s = HashKeyStrategy()
        a = compute_hash_key(["acme", 42], s)
        b = compute_hash_key(["acme", "42"], s)
        c = compute_hash_key(["acme", True], s)
        d = compute_hash_key(["acme", "True"], s)
        assert a == b
        assert c == d

    def test_empty_business_keys_produce_empty_payload_hash(self):
        """No keys → hash of the empty string. Keeps the function
        total — never raises on an empty input — so downstream
        validators report the issue rather than crashing."""
        s = HashKeyStrategy()
        digest = compute_hash_key([], s)
        assert digest == hashlib.md5(b"").hexdigest()

    def test_unsupported_algorithm_raises(self):
        """The ``algorithm`` field is a Pydantic ``Literal``, so an
        invalid value never reaches the helper through normal use.
        We still pin the defensive ``ValueError`` for callers that
        construct a strategy via ``model_construct`` (skip-validation)
        and ship a malformed value."""
        s = HashKeyStrategy.model_construct(
            algorithm="blake2b", delimiter="||", null_token="__NULL__", upper_case=True
        )
        with pytest.raises(ValueError, match="Unsupported hash algorithm"):
            compute_hash_key(["a"], s)

    def test_determinism_across_repeated_calls(self):
        """The same input must produce the same digest forever —
        otherwise change-data-capture against a satellite would emit
        spurious updates on every run."""
        s = HashKeyStrategy()
        v = compute_hash_key(["acme", "customer_42"], s)
        assert all(compute_hash_key(["acme", "customer_42"], s) == v for _ in range(20))


# ----------------------------------------------------------------------
# compute_hash_diff — order-insensitive attribute set
# ----------------------------------------------------------------------


class TestComputeHashDiff:
    def test_attribute_order_is_insignificant(self):
        """Two satellites with the same attributes in different
        column order must report the same hash_diff — re-ordering
        a CREATE TABLE must not retroactively invalidate every
        existing row."""
        s = HashKeyStrategy()
        a = compute_hash_diff({"name": "Alice", "age": 30}, s)
        b = compute_hash_diff({"age": 30, "name": "Alice"}, s)
        c = compute_hash_diff([("name", "Alice"), ("age", 30)], s)
        d = compute_hash_diff([("age", 30), ("name", "Alice")], s)
        assert a == b == c == d

    def test_value_change_changes_digest(self):
        """Any single value flip must produce a fresh digest — that's
        the change-detection contract this function exists for."""
        s = HashKeyStrategy()
        before = compute_hash_diff({"name": "Alice", "age": 30}, s)
        after = compute_hash_diff({"name": "Alice", "age": 31}, s)
        assert before != after

    def test_default_strategy_pinned(self):
        """Lock the wire format for the canonical case."""
        s = HashKeyStrategy()
        digest = compute_hash_diff({"name": "Alice", "age": 30}, s)
        expected = hashlib.md5("AGE=30||NAME=ALICE".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_null_attribute_collapses_to_null_token(self):
        s = HashKeyStrategy()
        a = compute_hash_diff({"name": "Alice", "extra": None}, s)
        b = compute_hash_diff({"name": "Alice", "extra": ""}, s)
        assert a == b

    def test_lower_case_strategy_preserves_names_and_values(self):
        s = HashKeyStrategy(upper_case=False)
        digest = compute_hash_diff({"Name": "alice"}, s)
        expected = hashlib.md5("Name=alice".encode("utf-8")).hexdigest()
        assert digest == expected

    def test_iterable_pairs_and_mapping_yield_same_digest(self):
        """The accepted shape is ``Mapping`` *or* ``Iterable[tuple]`` —
        both must produce the same digest so the modeler can emit
        whichever is convenient."""
        s = HashKeyStrategy()
        a = compute_hash_diff({"k1": 1, "k2": 2}, s)
        b = compute_hash_diff([("k1", 1), ("k2", 2)], s)
        assert a == b

    def test_empty_attributes_produce_empty_payload_hash(self):
        s = HashKeyStrategy()
        assert compute_hash_diff({}, s) == hashlib.md5(b"").hexdigest()

    def test_determinism_across_repeated_calls(self):
        s = HashKeyStrategy()
        v = compute_hash_diff({"name": "Alice", "age": 30}, s)
        assert all(compute_hash_diff({"name": "Alice", "age": 30}, s) == v for _ in range(20))

    def test_sha256_strategy_differs_from_md5(self):
        md5 = compute_hash_diff({"name": "Alice"}, HashKeyStrategy())
        sha = compute_hash_diff({"name": "Alice"}, HashKeyStrategy(algorithm="sha256"))
        assert md5 != sha


# ----------------------------------------------------------------------
# Cross-function — keys and diffs occupy disjoint spaces
# ----------------------------------------------------------------------


class TestKeyAndDiffDistinct:
    def test_same_input_yields_different_digests_under_each_function(self):
        """A list of column values and a dict of name→value with the
        same payload must NOT collide — they encode different schemas
        (positional vs named) and a downstream consumer must be able
        to tell them apart."""
        s = HashKeyStrategy()
        key = compute_hash_key(["alice", 30], s)
        diff = compute_hash_diff({"name": "alice", "age": 30}, s)
        assert key != diff
