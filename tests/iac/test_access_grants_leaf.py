# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The shared grant definitions live in one stdlib leaf; the old modules re-export them."""

from __future__ import annotations

import subprocess
import sys

from fluid_build.iac import access, principals
from fluid_build.iac.providers import gcp_governance
from fluid_build.providers import _access_grants as leaf


def test_old_modules_export_the_leaf_definitions():
    assert access.GrantScope is leaf.GrantScope
    assert access.parse_grant_scope is leaf.parse_grant_scope
    assert principals.logical_key is leaf.logical_key
    assert gcp_governance.READ_VERBS is leaf.READ_VERBS
    assert {"GrantScope", "parse_grant_scope"} <= set(access.__all__)
    assert "logical_key" in principals.__all__


def test_logical_key_spells_the_prefix_one_way():
    assert leaf.logical_key(" serviceaccount:loader@library.test ") == (
        "serviceAccount:loader@library.test"
    )
    assert leaf.logical_key("Group:Readers@library.test") == "group:Readers@library.test"
    assert leaf.logical_key("readers@library.test") == "readers@library.test"
    assert leaf.logical_key(None) == ""


def test_grant_scope_reads_the_resources_it_can_place():
    assert leaf.parse_grant_scope(()).covers("orders")
    assert leaf.parse_grant_scope(["$.exposes[*]"]).covers("orders")
    one = leaf.parse_grant_scope(["$.exposes[?(@.exposeId=='orders')]"])
    assert one.covers("orders") and not one.covers("returns")
    assert not leaf.parse_grant_scope(["$.exposes[0]"]).covers("orders")


def test_the_leaf_imports_only_the_standard_library():
    code = (
        "import sys; import fluid_build.providers._access_grants; "
        "print('fluid_build.iac' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
