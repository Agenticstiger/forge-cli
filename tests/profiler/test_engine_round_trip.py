# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A format 1-4-0 profile, round trip through the engine's ``fluxsim``.

``fluxsim init`` writes the profile request from a contract, ``fluid profile`` answers it twice
(an exact profile, and a private release that follows the child rows and holds rank
correlations), and ``fluxsim init`` and ``fluxsim run --verify`` take each profile to a bundle
and a checked run. The engine reads every profile with its own reader, so a document this
profiler writes that the engine refuses fails here.

The test needs the engine's executable: it is skipped unless ``FLUID_FLUXSIM`` names one.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import main
from tests.profiler.dp._privacy import need

FLUXSIM = os.environ.get("FLUID_FLUXSIM")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not FLUXSIM, reason="FLUID_FLUXSIM names no fluxsim executable"),
]

CONTRACT = Path(__file__).parent / "fixtures" / "round_trip" / "billing.fluid.yaml"
ACCOUNTS = 200


def _write_records(directory: Path) -> None:
    """200 accounts and about 390 invoices, the same every time."""
    pick = random.Random(7)
    with (directory / "account.csv").open("w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["id", "segment", "age"])
        for key in range(1, ACCOUNTS + 1):
            out.writerow(
                [key, pick.choice(["business", "personal", "public"]), pick.randint(18, 90)]
            )
    start = datetime.datetime(2024, 1, 1)
    with (directory / "invoice.csv").open("w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["invoice_id", "account_id", "status", "qty", "units", "placed_at"])
        number = 0
        for account in range(1, ACCOUNTS + 1):
            for _ in range(pick.randint(1, 3)):
                number += 1
                qty = pick.randint(0, 10)
                units = min(20, qty * 2 + pick.randint(0, 3))
                placed = start + datetime.timedelta(
                    days=pick.randint(0, 364), seconds=pick.randint(0, 86399)
                )
                out.writerow(
                    [
                        number,
                        account,
                        pick.choice(["open", "paid", "void"]),
                        qty,
                        units,
                        placed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ]
                )


def _fluxsim(*args: str) -> Dict[str, Any]:
    """Run ``fluxsim`` with ``--json`` and return the document it prints."""
    assert FLUXSIM is not None
    done = subprocess.run(
        [FLUXSIM, *args, "-j"], capture_output=True, text=True, check=False, timeout=600
    )
    assert done.returncode == 0, f"fluxsim {' '.join(args)}: {done.stdout}{done.stderr}"
    return json.loads(done.stdout)


def _grade(path: Path) -> str:
    """The engine's own grade of a profile, from the interpreter that holds the engine."""
    assert FLUXSIM is not None
    python = Path(FLUXSIM).parent / "python"
    code = (
        "import json, sys\n"
        "from flux_engine.evidence.grades import grade_profile\n"
        "print(grade_profile(json.load(open(sys.argv[1])), pinned=True).pattern.value)\n"
    )
    done = subprocess.run(
        [str(python), "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return done.stdout.strip()


def _profile(*more: str) -> int:
    return main(
        [
            "profile",
            "billing.fluid.yaml",
            "--request",
            "billing.profile-request.yml",
            "--source",
            "account=account.csv",
            "--source",
            "invoice=invoice.csv",
            *more,
        ]
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copy(CONTRACT, tmp_path / "billing.fluid.yaml")
    _write_records(tmp_path)
    return tmp_path


def _ledger(init: Dict[str, Any], *needles: str) -> List[Dict[str, Any]]:
    return [
        each
        for each in init["artifacts"]["ledger"]["statistics"]
        if any(
            needle in each["pointer"] or needle in each.get("definition", "") for needle in needles
        )
    ]


def test_a_1_4_0_profile_goes_through_init_and_run_verify(workspace, capsys):
    need("opendp")
    need("mbi")

    # the engine asks for the 1-2-0 statistics
    _fluxsim("init", "ask", "--contract", "billing.fluid.yaml")
    request = (workspace / "ask.profile-request.yml").read_text(encoding="utf-8")
    assert "profile-request/1-2-0/" in request
    for asked in ("rankCorrelations", "cliqueColumns", "groupBy", "linked"):
        assert asked in request

    # (i) an exact profile, and (ii) a private release that follows its child rows
    (workspace / "billing.profile-request.yml").write_text(request, encoding="utf-8")
    assert _profile("--out", "exact.profile.json") == 0
    assert (
        _profile(
            "--privacy-unit",
            "account",
            "--follow",
            "account.id=invoice.account_id:3",
            "--rho",
            "1",
            "--row-floor",
            "200",
            "--release-record",
            "release.json",
            "--out",
            "private.profile.json",
        )
        == 0
    )
    capsys.readouterr()
    exact = json.loads((workspace / "exact.profile.json").read_text(encoding="utf-8"))
    private = json.loads((workspace / "private.profile.json").read_text(encoding="utf-8"))
    assert exact["_schemaURL"].endswith("/profile/1-4-0/FluxProfile.json")
    assert private["_schemaURL"].endswith("/profile/1-4-0/FluxProfile.json")
    assert private["privacy"]["privacyUnit"]["follows"][0]["maxChildren"] == 3
    pairs = private["tables"]["invoice"]["columnPairs"]
    assert len(pairs) == 3
    assert all(each["rowFloor"] == 200 for pair in pairs for each in pair["statistics"])

    # each profile becomes a bundle, and each bundle runs and verifies
    ran: Dict[str, Dict[str, Any]] = {}
    inits: Dict[str, Dict[str, Any]] = {}
    for name in ("exact", "private"):
        inits[name] = _fluxsim(
            "init",
            f"bundle-{name}",
            "--contract",
            "billing.fluid.yaml",
            "--profile",
            f"{name}.profile.json",
        )
        ran[name] = _fluxsim(
            "run",
            f"bundle-{name}",
            "--verify",
            "--profile",
            f"{name}.profile.json",
            "--state-dir",
            f"state-{name}",
        )
        assert ran[name]["ok"] is True
        assert ran[name]["artifacts"]["verify"]["fidelity"]["invoice"]["status"] == "passed"

    # the ledger names what the rank correlations are used for: the exact ones' pairs are read
    # and left out of a copula with a reason; the private release's children counts are the
    # bundle's counts entry
    for name in inits:
        assert _ledger(inits[name], "kendallTau"), name
    counted = [
        each for each in _ledger(inits["private"], "/relationships/") if each.get("state") == "used"
    ]
    assert counted and all(each["noise"]["mechanism"] == "discreteGaussian" for each in counted)

    # the grade: the release is P3, and the exact profile is not
    assert inits["private"]["artifacts"]["grade"]["grade"] == "P3"
    assert inits["exact"]["artifacts"]["grade"]["grade"] != "P3"
    assert ran["private"]["artifacts"]["plan"]["plan_document"]["privacyGrade"] == "P3"
    assert ran["exact"]["artifacts"]["plan"]["plan_document"]["privacyGrade"] != "P3"
    assert _grade(workspace / "private.profile.json") == "P3"
    assert _grade(workspace / "exact.profile.json") != "P3"
