# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``PLATFORM_CONTAINER_KINDS`` must not drift from the tables it describes.

Three modules carry the same platform ↔ container-kind fact in three shapes:

* ``iac/packaging.py::PLATFORM_CONTAINER_KINDS`` — platform → kinds. The one
  a *reporter* reads to decide what it may claim this contract owns.
* ``iac/transition.py::CONTAINER_RESOURCE_TYPES`` — OpenTofu resource type →
  kind, grouped by provider.
* ``iac/plan_packaging.py::CONTAINER_CREATION_OPS`` — native planner op →
  kind, grouped by provider.

Hand-mirrored tables are an explicit repo anti-pattern; there is no codegen
source to derive these from, so the next best thing is a test that fails the
moment one grows an entry the others do not. The op/resource-type prefixes
are the join key (``snowflake_*`` / ``sf.*`` → snowflake, and so on).

``cluster`` is intentionally absent from the latter two: no provider emits a
resource or a planner op for it, which is exactly why owning one is a v2
feature (RFC-packaging-modes.md file 6) and why a ``cluster`` declaration is
vacuous on every platform this build actually ships.
"""

from __future__ import annotations

import pytest

from fluid_build.iac.packaging import CONTAINER_KINDS, PLATFORM_CONTAINER_KINDS
from fluid_build.iac.plan_packaging import CONTAINER_CREATION_OPS
from fluid_build.iac.transition import CONTAINER_RESOURCE_TYPES

pytestmark = pytest.mark.unit

# resource-type prefix (transition.py) → platform
_RESOURCE_PREFIX_PLATFORM = {
    "aws_": "aws",
    "google_": "gcp",
    "snowflake_": "snowflake",
}

# planner-op prefix (plan_packaging.py) → platform
_OP_PREFIX_PLATFORM = {
    "s3.": "aws",
    "glue.": "aws",
    "gcs.": "gcp",
    "bq.": "gcp",
    "sf.": "snowflake",
}


def _kinds_by_platform(table, prefixes):
    found: dict = {}
    for key, kind in table.items():
        platform = next(
            (p for prefix, p in prefixes.items() if key.startswith(prefix)),
            None,
        )
        assert platform is not None, f"{key!r} matches no known platform prefix"
        found.setdefault(platform, set()).add(kind)
    return found


class TestNoDrift:
    def test_every_declared_kind_is_a_real_container_kind(self):
        for platform, kinds in PLATFORM_CONTAINER_KINDS.items():
            unknown = kinds - set(CONTAINER_KINDS)
            assert not unknown, f"{platform} declares unknown kind(s) {sorted(unknown)}"

    def test_resource_types_agree(self):
        """Every kind a provider emits a container *resource* for is declared."""
        assert _kinds_by_platform(CONTAINER_RESOURCE_TYPES, _RESOURCE_PREFIX_PLATFORM) == {
            "aws": {"bucket", "database"},
            "gcp": {"bucket", "dataset"},
            "snowflake": {"database", "schema", "warehouse"},
        }
        for platform, kinds in _kinds_by_platform(
            CONTAINER_RESOURCE_TYPES, _RESOURCE_PREFIX_PLATFORM
        ).items():
            assert kinds <= set(PLATFORM_CONTAINER_KINDS[platform]), (
                f"{platform} emits a container resource for "
                f"{sorted(kinds - set(PLATFORM_CONTAINER_KINDS[platform]))} but "
                "PLATFORM_CONTAINER_KINDS does not list it — a reporter would "
                "hide a container this product really owns"
            )

    def test_creation_ops_agree(self):
        """Every kind a native planner creates a container for is declared."""
        for platform, kinds in _kinds_by_platform(
            CONTAINER_CREATION_OPS, _OP_PREFIX_PLATFORM
        ).items():
            assert kinds <= set(PLATFORM_CONTAINER_KINDS[platform]), (
                f"{platform} plans a container-creation op for "
                f"{sorted(kinds - set(PLATFORM_CONTAINER_KINDS[platform]))} but "
                "PLATFORM_CONTAINER_KINDS does not list it"
            )

    def test_cluster_is_provisioned_by_nothing(self):
        """The v1 reality behind ``cluster-isolated-unsupported``."""
        assert "cluster" not in set(CONTAINER_RESOURCE_TYPES.values())
        assert "cluster" not in set(CONTAINER_CREATION_OPS.values())
