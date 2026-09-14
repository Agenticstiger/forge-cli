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

"""Deriving caller-jurisdiction rules from a contract's own sovereignty block.

Nothing is invented here. A contract declaring ``jurisdiction: EU`` with
``crossBorderTransfer`` false already means "this data does not leave the EU",
and serving it to a reader elsewhere is the border crossing it forbids.

The derivation is deliberately conservative: it returns "no constraint" unless
the contract says something that genuinely constrains a reader, because the
alternative — a rule that fires on contracts nobody intended to restrict — is
how a governance control becomes something operators route around.

NOT ACTIVATED. ``_build_policy`` does not pass the root contract, so this is
reachable only by an embedder. See the transport note in
``fluid_build/output_ports/mcp/policy.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.policy.sovereignty import (
    DEFAULT_CROSS_BORDER_TRANSFER,
    UNCONSTRAINED_JURISDICTIONS,
    derive_caller_jurisdictions,
)


def contract(**sovereignty: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"fluidVersion": "0.7.5", "kind": "DataProduct", "id": "p"}
    if sovereignty:
        doc["sovereignty"] = sovereignty
    return doc


# ---------------------------------------------------------------------------
# constrains
# ---------------------------------------------------------------------------


def test_pinned_jurisdiction_constrains_callers() -> None:
    assert derive_caller_jurisdictions(contract(jurisdiction="EU")) == ("EU",)


def test_explicit_no_cross_border_constrains() -> None:
    assert derive_caller_jurisdictions(contract(jurisdiction="UK", crossBorderTransfer=False)) == (
        "UK",
    )


@pytest.mark.parametrize("j", ["EU", "US", "UK", "CA", "AU", "JP", "CN", "IN", "BR"])
def test_every_pinnable_jurisdiction_constrains(j: str) -> None:
    """All nine real jurisdictions behave alike; only the two catch-alls differ."""
    assert derive_caller_jurisdictions(contract(jurisdiction=j)) == (j,)


# ---------------------------------------------------------------------------
# does not constrain — each for a different reason
# ---------------------------------------------------------------------------


def test_no_sovereignty_block_is_inert() -> None:
    """The overwhelming majority of contracts. They must not start refusing."""
    assert derive_caller_jurisdictions(contract()) is None


def test_sovereignty_without_a_jurisdiction_is_inert() -> None:
    assert derive_caller_jurisdictions(contract(dataResidency=True)) is None


@pytest.mark.parametrize("j", sorted(UNCONSTRAINED_JURISDICTIONS))
def test_catch_all_jurisdictions_are_inert(j: str) -> None:
    """Global and Multi-Region assert no single jurisdiction.

    Multi-Region matters most: an equality predicate would refuse EVERY caller,
    since no caller is ever in a jurisdiction literally named "Multi-Region".
    The provision-time jurisdiction check reads this same constant, so both
    paths agree on what a catch-all means.
    """
    assert derive_caller_jurisdictions(contract(jurisdiction=j)) is None


def test_permitted_cross_border_transfer_is_inert() -> None:
    """The contract permits the very transfer this gate exists to prevent."""
    assert (
        derive_caller_jurisdictions(contract(jurisdiction="EU", crossBorderTransfer=True)) is None
    )


# ---------------------------------------------------------------------------
# the default, and malformed input
# ---------------------------------------------------------------------------


def test_silence_about_transfers_follows_the_schema_default() -> None:
    """crossBorderTransfer defaults to False, so silence means "do not transfer"."""
    assert DEFAULT_CROSS_BORDER_TRANSFER is False
    assert derive_caller_jurisdictions(contract(jurisdiction="EU")) == ("EU",)


@pytest.mark.parametrize("bad", [None, "EU", [], 0])
def test_a_malformed_sovereignty_block_is_inert_not_an_error(bad: Any) -> None:
    """Schema validation owns malformed input; this must not raise on the way.

    A governance helper that throws on a shape the schema already rejects turns
    one clear error into a stack trace.
    """
    assert derive_caller_jurisdictions({"sovereignty": bad}) is None


def test_empty_jurisdiction_string_is_inert() -> None:
    assert derive_caller_jurisdictions(contract(jurisdiction="")) is None
