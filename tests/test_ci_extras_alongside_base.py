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

"""The extras that will carry litellm, mcp and openlineage once base drops them.

Slice 2 of the dependency diet adds ``[litellm]``, ``[mcp]``, ``[lineage]``,
``[standard]`` and ``[ai]`` while base still lists all three packages. These
tests keep that layout from drifting before the removal release.
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


class TestLitellmMarkerSplit:
    def test_base_still_has_the_split(self):
        # If this fails the split moved or was collapsed in base; fix the
        # guard and the extra together, never one alone.
        assert len(_base_lines("litellm")) == 2

    def test_extra_copies_both_marker_lines_verbatim(self):
        assert sorted(EXTRAS["litellm"]) == sorted(_base_lines("litellm"))

    def test_python_310_keeps_the_cap(self):
        capped = [r for r in EXTRAS["litellm"] if "python_version < '3.11'" in r]
        assert len(capped) == 1
        assert "<1.98" in capped[0]


class TestFilledExtras:
    def test_mcp_matches_base_and_declares_pyjwt(self):
        assert [r for r in _base_lines("mcp")][0] in EXTRAS["mcp"]
        assert "pyjwt" in _names("mcp")

    def test_lineage_matches_base(self):
        assert EXTRAS["lineage"] == _base_lines("openlineage-python")

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
