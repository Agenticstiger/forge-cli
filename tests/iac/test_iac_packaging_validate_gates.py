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

"""Validate-time packaging gates (RFC-packaging-modes.md file 9).

``fluid validate`` had NO packaging awareness at all. Two consequences,
both reproduced against live Snowflake before these gates existed:

* ``packaging: {mode: shared}`` with no ``pool`` validated clean and then
  failed at ``fluid plan`` — whose own remediation block says "Run 'fluid
  validate <contract>' first to rule out a contract problem", which
  reported the contract valid. The operator is sent in a circle.
* An ``overlays/prod.yaml`` of ``packaging: {mode: isolated}`` over a base
  of ``{mode: shared, pool: …, containers: {database: shared}}`` validated
  with ZERO warnings, emitted a module carrying no ``snowflake_database``
  (the base's per-kind entry beats ``mode``), produced a GREEN
  ``tofu plan: +2 ~0 -0``, and then died in APPLY on a raw provider error
  — so the plan artifact a human approved was itself misleading.
"""

from __future__ import annotations

import pytest

from fluid_build.iac.packaging import (
    validate_overlay_packaging,
    validate_packaging_block,
)


def _contract(packaging=None, exposes=None):
    contract = {"fluidVersion": "0.7.6", "kind": "DataProduct", "exposes": exposes or []}
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


# ---------------------------------------------------------------------
# validate_packaging_block — the resolver, run at validate time
# ---------------------------------------------------------------------


def test_shared_without_pool_is_an_error_at_validate_time():
    errors, warnings = validate_packaging_block(_contract({"mode": "shared"}))
    assert len(errors) == 1
    assert "pool-required" in errors[0]
    assert "add `pool: <id>`" in errors[0]
    assert warnings == []


def test_shared_with_pool_validates():
    assert validate_packaging_block(_contract({"mode": "shared", "pool": "platform"})) == ([], [])


def test_isolated_validates():
    assert validate_packaging_block(_contract({"mode": "isolated"})) == ([], [])


@pytest.mark.parametrize(
    "block,kind",
    [
        ({"mode": "nonsense"}, "invalid-mode"),
        ({"mode": "isolated", "containers": {"nope": "isolated"}}, "invalid-container-kind"),
        ({"mode": "isolated", "containers": {"schema": "nope"}}, "invalid-container-mode"),
        ({"mode": "isolated", "unknownKey": 1}, "invalid-block"),
    ],
)
def test_every_packaging_error_kind_surfaces_at_validate_time(block, kind):
    errors, _ = validate_packaging_block(_contract(block))
    assert len(errors) == 1
    assert kind in errors[0]


def test_cluster_isolated_surfaces_at_validate_time_when_a_platform_binds_cluster():
    """``cluster-isolated-unsupported`` needs a cluster-binding platform.

    The rejection is gated on :func:`binds_cluster`, which fails **closed** —
    a contract with no bindings (or a platform this build does not recognise)
    is neither owned nor rejected, so asserting the error on a binding-less
    contract would pin the pre-gate behaviour. ``confluent`` is one of the two
    platforms that map the ``cluster`` kind.
    """
    contract = _contract(
        {"mode": "isolated", "containers": {"cluster": "isolated"}},
        exposes=[
            {
                "exposeId": "t",
                "binding": {"platform": "confluent", "format": "iceberg", "location": {}},
            }
        ],
    )
    errors, _ = validate_packaging_block(contract)
    assert len(errors) == 1
    assert "cluster-isolated-unsupported" in errors[0]


def test_cluster_isolated_is_not_rejected_without_a_cluster_platform():
    """The fail-closed half: no binding means no claim, in either direction."""
    assert validate_packaging_block(
        _contract({"mode": "isolated", "containers": {"cluster": "isolated"}})
    ) == ([], [])


def test_a_contract_with_no_packaging_block_is_untouched():
    """The LEGACY path must stay a provable no-op."""
    assert validate_packaging_block(_contract()) == ([], [])
    assert validate_packaging_block({}) == ([], [])


# ---------------------------------------------------------------------
# validate_overlay_packaging — the RFC Example-3 mode-flip warning
# ---------------------------------------------------------------------

_BASE = {
    "packaging": {
        "mode": "shared",
        "pool": "platform-pool",
        "containers": {"database": "shared"},
    }
}


def test_overlay_mode_flip_over_an_inherited_containers_map_warns():
    errors, warnings = validate_overlay_packaging(_BASE, {"packaging": {"mode": "isolated"}})
    assert errors == []
    assert len(warnings) == 1
    assert "'shared' → 'isolated'" in warnings[0]
    assert "database: shared" in warnings[0]


def test_restating_the_affected_kind_in_the_overlay_clears_the_warning():
    """The remediation the warning prescribes: the fixed overlay emits a
    snowflake_database and the apply succeeds."""
    assert validate_overlay_packaging(
        _BASE,
        {"packaging": {"mode": "isolated", "containers": {"database": "isolated"}}},
    ) == ([], [])


def test_the_rfcs_own_restate_wholesale_idiom_still_warns():
    """RFC Example 3 documents ``packaging: {mode: isolated, containers: {}}``
    as the "restate, don't patch" fix. It does not work: the deep merge is
    key-wise, so an EMPTY containers map overwrites nothing and the base's
    ``{database: shared}`` survives — ``fluid generate iac --env prod``
    still emits no ``snowflake_database``. The warning must fire on the
    documented idiom too, and its remediation names the working form
    (restate the affected KINDS) rather than the broken one."""
    errors, warnings = validate_overlay_packaging(
        _BASE, {"packaging": {"mode": "isolated", "containers": {}}}
    )
    assert errors == []
    assert len(warnings) == 1
    assert "database: shared" in warnings[0]


def test_no_warning_when_the_overlay_does_not_change_mode():
    assert validate_overlay_packaging(_BASE, {"packaging": {"pool": "other-pool"}}) == ([], [])
    assert validate_overlay_packaging(_BASE, {"packaging": {"mode": "shared"}}) == ([], [])


def test_no_warning_when_the_base_declares_no_containers_map():
    base = {"packaging": {"mode": "shared", "pool": "platform-pool"}}
    assert validate_overlay_packaging(base, {"packaging": {"mode": "isolated"}}) == ([], [])


def test_no_warning_when_the_inherited_kinds_already_agree_with_the_new_mode():
    """``containers: {database: isolated}`` inherited under a flip TO
    isolated changes nothing — there is nothing to surprise the author."""
    base = {
        "packaging": {
            "mode": "shared",
            "pool": "p",
            "containers": {"database": "isolated"},
        }
    }
    assert validate_overlay_packaging(base, {"packaging": {"mode": "isolated"}}) == ([], [])


def test_overlay_without_a_packaging_block_is_ignored():
    assert validate_overlay_packaging(_BASE, {"metadata": {"layer": "Gold"}}) == ([], [])
    assert validate_overlay_packaging({}, {"packaging": {"mode": "isolated"}}) == ([], [])
