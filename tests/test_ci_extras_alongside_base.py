# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The extras that carry litellm, mcp and openlineage now that base dropped them.

0.27.0 added ``[litellm]``, ``[mcp]``, ``[lineage]``, ``[standard]`` and
``[ai]`` next to base; 0.28.0 removed the three packages from base. These
tests keep the extras layout from drifting and keep the packages out of base.
"""

from __future__ import annotations

import pytest

from tests.ci_guards import extras_alongside_base as g

PROJECT = g.load()
BASE = g.base_requirements(PROJECT)
EXTRAS = g.extras(PROJECT)

#: Extras that ``all`` deliberately leaves out, each with its reason. A new
#: extra must either join ``all`` or be added here: that is the point.
NOT_IN_ALL = {
    "all": "itself",
    "sdk": "plugin-author tooling, not a runtime feature",
    "odcs-strict": "opt-in second validation pass",
    "databricks": "reachable through [catalogs]",
    "datahub": "reachable through [catalogs]",
    "keyring": "empty alias; keyring is a base dependency",
    "catalogs": "umbrella of adapters, installed on request",
    "opds": "back-compat alias of [odps]",
    "standard": "alias group, covered through [litellm] and [mcp]",
    "ai": "alias of [litellm]",
    "test-emulators": "CI-only emulators",
    "test-emulators-heavy": "CI-only emulators",
}


def _base_lines(package: str) -> list[str]:
    return [r for r in BASE if g.requirement_name(r) == package]


def _names(extra: str) -> set[str]:
    return {g.requirement_name(r) for r in g.closure(extra, EXTRAS)}


class TestBaseIsLean:
    @pytest.mark.parametrize("package", ["litellm", "mcp", "openlineage-python"])
    def test_base_does_not_list_it(self, package):
        assert _base_lines(package) == []


class TestLitellmMarkerSplit:
    def test_extra_keeps_both_marker_lines_verbatim(self):
        assert sorted(EXTRAS["litellm"]) == sorted(
            [
                "litellm>=1.83.14,!=1.107.0,<2; python_version >= '3.11'",
                "litellm>=1.83.14,!=1.107.0,<1.98; python_version < '3.11'",
            ]
        )

    def test_python_310_keeps_the_cap(self):
        capped = [r for r in EXTRAS["litellm"] if "python_version < '3.11'" in r]
        assert len(capped) == 1
        assert "<1.98" in capped[0]


class TestFilledExtras:
    def test_mcp_pins_the_sdk_and_declares_pyjwt(self):
        assert "mcp>=1.30,!=2.0.*,!=2.1.*,<3.0" in EXTRAS["mcp"]
        assert "pyjwt" in _names("mcp")

    def test_lineage_carries_openlineage(self):
        assert EXTRAS["lineage"] == ["openlineage-python>=1.52", "python-dateutil>=2.8.2"]

    def test_standard_covers_litellm_and_mcp(self):
        assert g.closure("litellm", EXTRAS) <= g.closure("standard", EXTRAS)
        assert g.closure("mcp", EXTRAS) <= g.closure("standard", EXTRAS)

    def test_ai_equals_litellm(self):
        assert g.closure("ai", EXTRAS) == g.closure("litellm", EXTRAS)


class TestUmbrellas:
    @pytest.mark.parametrize("extra", ["litellm", "mcp", "lineage"])
    def test_all_covers_the_new_extras(self, extra):
        assert g.closure(extra, EXTRAS) <= g.closure("all", EXTRAS)

    def test_all_covers_every_user_facing_extra(self):
        covered = g.closure("all", EXTRAS)
        missing = [
            name
            for name in EXTRAS
            if name not in NOT_IN_ALL and not g.closure(name, EXTRAS) <= covered
        ]
        assert not missing, f"extras missing from [all] (or add to NOT_IN_ALL): {missing}"

    def test_the_exclusion_list_names_only_real_extras(self):
        assert set(NOT_IN_ALL) <= set(EXTRAS)

    def test_all_keeps_dev(self):
        assert g.closure("dev", EXTRAS) <= g.closure("all", EXTRAS)

    @pytest.mark.parametrize("extra", ["litellm", "mcp", "lineage"])
    def test_dev_covers_the_new_extras(self, extra):
        assert g.closure(extra, EXTRAS) <= g.closure("dev", EXTRAS)
