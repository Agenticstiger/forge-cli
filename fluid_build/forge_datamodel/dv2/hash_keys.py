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

"""Deterministic DV2 hash-key + hash-diff derivation.

Two public functions; both are pure, side-effect-free, and fully driven
by a :class:`HashKeyStrategy`.  No LLM involvement — the modeler emits
*which columns* go into the hash; this module decides *how* to compute
the hash bytes.

Conventions (matching the plan's prescription
``sort → null-token → delimiter → uppercase → md5/sha256``):

* :func:`compute_hash_key` — business keys are **ordered-sensitive**.
  Order-of-columns is semantic for a hub / link key (a ``(party,
  product)`` key is not the same as ``(product, party)``), so we do
  **not** sort them.  We replace NULL/empty with ``strategy.null_token``,
  join with ``strategy.delimiter``, optionally uppercase, then hash.

* :func:`compute_hash_diff` — attribute sets are **order-insensitive**.
  To keep hash_diff stable across satellite column re-ordering, we sort
  the pairs by column name, then serialise ``name=value`` fragments
  joined by delimiter, optionally uppercase, then hash.

Both return a lowercase hex-digest string.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Mapping, Sequence, Union

from fluid_build.copilot.schemas.data_model import HashKeyStrategy

_Value = Union[str, int, float, bool, None]


def _hasher(strategy: HashKeyStrategy) -> "hashlib._Hash":
    if strategy.algorithm == "md5":
        return hashlib.md5(usedforsecurity=False)
    if strategy.algorithm == "sha256":
        return hashlib.sha256()
    raise ValueError(f"Unsupported hash algorithm: {strategy.algorithm!r}")


def _canonicalise(value: _Value, strategy: HashKeyStrategy) -> str:
    if value is None:
        return strategy.null_token
    text = str(value).strip()
    if not text:
        return strategy.null_token
    return text.upper() if strategy.upper_case else text


def compute_hash_key(
    business_keys: Sequence[_Value],
    strategy: HashKeyStrategy,
) -> str:
    """Deterministic hub / link hash key.

    The order of ``business_keys`` is preserved — business-key column
    order is semantic in DV2.  NULL / empty values become
    ``strategy.null_token``.  Values are delimiter-joined, optionally
    upper-cased, then hashed with ``strategy.algorithm``.
    """
    parts = [_canonicalise(v, strategy) for v in business_keys]
    payload = strategy.delimiter.join(parts)
    h = _hasher(strategy)
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


def compute_hash_diff(
    attributes: Union[Mapping[str, _Value], Iterable[tuple[str, _Value]]],
    strategy: HashKeyStrategy,
) -> str:
    """Deterministic satellite hash-diff.

    Attributes are sorted by column name before joining so that
    re-ordering columns in the source does not trigger false
    change-detections.  The wire format is
    ``NAME=VALUE<delim>NAME=VALUE<delim>...`` (names uppercased when
    ``strategy.upper_case`` is true; values canonicalised identically to
    :func:`compute_hash_key`).
    """
    if isinstance(attributes, Mapping):
        items = list(attributes.items())
    else:
        items = list(attributes)
    items.sort(key=lambda pair: pair[0])
    fragments = []
    for name, value in items:
        canonical_name = name.upper() if strategy.upper_case else name
        fragments.append(f"{canonical_name}={_canonicalise(value, strategy)}")
    payload = strategy.delimiter.join(fragments)
    h = _hasher(strategy)
    h.update(payload.encode("utf-8"))
    return h.hexdigest()
