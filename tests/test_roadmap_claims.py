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

"""``fluid roadmap`` must not announce a release the package has not made."""

import re
from importlib import resources

import pytest

import fluid_build

# "forge-cli v1.0 shipped", "v2.1 shipped": a release claim, not a milestone label.
_RELEASE_CLAIM = re.compile(
    r"\bv(\d+)\.\d+(?:\.\d+)?\s+(?:(?:has\s+been|was|has)\s+)?shipped\b", re.IGNORECASE
)


def _major(version: str) -> int:
    match = re.match(r"\d+", version)
    return int(match.group()) if match else 0


def test_roadmap_does_not_claim_a_release_beyond_the_package_version():
    text = resources.files("fluid_build.copilot").joinpath("roadmap.md").read_text("utf-8")
    package_major = _major(fluid_build.__version__)
    claimed = [int(m.group(1)) for m in _RELEASE_CLAIM.finditer(text)]
    assert all(
        major <= package_major for major in claimed
    ), f"roadmap.md claims a v{max(claimed)} release but the package is {fluid_build.__version__}"


@pytest.mark.parametrize(
    "claim",
    ["forge-cli v1.0 shipped 2026-04-23.", "v1.0 was shipped", "v2.1 has been shipped"],
)
def test_the_guard_catches_the_old_claim(claim):
    assert _RELEASE_CLAIM.search(claim)


def test_no_section_or_sentence_presents_v1_as_a_release():
    text = resources.files("fluid_build.copilot").joinpath("roadmap.md").read_text("utf-8")
    assert "Shipped in v1.0" not in text
    assert "shipped the day after v1.0" not in text
