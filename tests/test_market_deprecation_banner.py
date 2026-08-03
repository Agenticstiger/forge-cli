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

"""The 'fluid marketplace is deprecated' banner must fire ONLY for a direct
(hidden) `fluid marketplace` invocation — not for the new `fluid market
--blueprints` delegation, where the user is already on the recommended command
(showing it there nags users about a command they didn't run).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        capture_output=True,
        text=True,
    )


def test_market_blueprints_human_path_has_no_deprecation_banner() -> None:
    r = _run("market", "--blueprints")
    assert r.returncode == 0, r.stderr
    assert (
        "deprecated" not in (r.stdout + r.stderr).lower()
    ), "the new `fluid market --blueprints` command must not nag about deprecation"


def test_direct_marketplace_still_shows_deprecation_banner() -> None:
    # The hidden, deprecated `fluid marketplace` command SHOULD still nudge the
    # user to migrate — gating the banner must not remove it from the direct path.
    r = _run("marketplace", "search")
    assert (
        "deprecated" in (r.stdout + r.stderr).lower()
    ), "the direct `fluid marketplace` command should still show the deprecation banner"
