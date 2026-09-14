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

"""Fail a CI lane that went green while proving nothing.

Emulator-backed tests skip themselves when their emulator is unreachable —
deliberately, so the suite stays runnable on a laptop without Docker and on a
fork PR with no secrets. The cost of that design is a lane that exits 0 having
skipped everything it advertises.

That is not hypothetical. ``integration-emulated-heavy.yml`` selected 54 tests
by marker, provisioned an emulator for 3 of them, and reported success for
fourteen months. This script closes that hole: each ``--require`` names an area
the lane provisioned, and the lane fails unless that area actually ran.

    python scripts/ci/assert_lane_coverage.py report.xml \\
      --require "GCP emulators=tests/iac/test_iac_gcp_emulator_e2e.py" \\
      --require "BigQuery=tests/providers/test_bigquery_emulated_happy_path.py"

A pass is a testcase with no ``skipped``/``failure``/``error`` child. xfail is
recorded as a skip by pytest's JUnit writer, so an area whose only outcome is a
documented xfail counts as not covered — which is the honest reading.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def normalise(value: str) -> str:
    """Reduce a path, a needle or a dotted module to one comparable form.

    pytest's JUnit writer does not always set ``@file``; with only
    ``@classname`` the identifier is the dotted module
    (``tests.providers.test_x``, or ``tests.providers.test_x.TestClass``),
    which carries no ``.py``. Comparing that against a ``--require`` needle
    written as a real path missed every time — the guard reported an area as
    uncovered while its tests were passing. Both sides go through here.
    """
    return value.replace(".py", "").replace(".", "/")


def passed_files(report: Path) -> list[str]:
    """Return a normalised source identifier for every passing testcase."""
    root = ET.parse(report).getroot()
    out: list[str] = []
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("skipped", "failure", "error")):
            continue
        out.append(normalise(case.get("file") or case.get("classname", "")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="pytest --junitxml output")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="LABEL=PATH_SUBSTRING",
        help="an area that must contribute at least one passing test",
    )
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"::error::no JUnit report at {args.report} — did pytest run?")
        return 1

    files = passed_files(args.report)
    print(f"{len(files)} passing tests in {args.report}")

    missing = []
    for requirement in args.require:
        label, _, needle = requirement.partition("=")
        if not needle:
            print(f"::error::malformed --require {requirement!r}; want LABEL=PATH")
            return 2
        hits = sum(1 for f in files if normalise(needle) in f)
        if hits:
            print(f"  OK   {label}: {hits} passing")
        else:
            print(f"  MISS {label}: 0 passing (looked for {needle})")
            missing.append(label)

    if missing:
        # Print what was actually seen — a needle that no longer matches any
        # test is a guard bug, and it must be told apart from a dead emulator.
        print("passing tests came from:")
        for source in sorted(set(files)):
            print(f"    {source}")
        print(
            "::error::this lane reported success without exercising: "
            + ", ".join(missing)
            + ". Its emulator did not start, or its tests skipped themselves."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
