# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Fail when the base install resolves to more packages than its ceiling.

``pip install data-product-forge`` pulls a transitive tree, and nothing in the
repo noticed it growing. This resolves ``pyproject.toml`` (base dependencies,
no extras) for each supported target and compares the package count to a
committed ceiling. When a count goes over, the failure names the packages
added and removed against a committed baseline list for that target, so the
reviewer sees which dependency brought the rest.

    python scripts/ci/dep_budget.py            # check every target
    python scripts/ci/dep_budget.py --update   # rewrite the baseline lists

Borrowed, not built: resolution is ``uv pip compile`` (astral-sh/uv, MIT).
No off-the-shelf "dependency count budget" gate exists, so the comparison
stays this thin.

Lowering a ceiling is a one-line edit to ``TARGETS`` plus ``--update`` to
refresh the baseline. The check is not run on every PR (see
``.github/workflows/dep-budget.yml``) because an upstream release can raise a
count with no change here.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINES = Path(__file__).resolve().parent / "dep_budget_baselines"

# name -> (uv --python-platform, --python-version, ceiling). Measured with
# uv 0.11.23 on 2026-11-03.
TARGETS: dict[str, tuple[str, str, int]] = {
    "linux-py312": ("linux", "3.12", 88),
    "linux-py310": ("linux", "3.10", 86),
    "windows-py312": ("windows", "3.12", 89),
}

# ``name==version`` or ``name @ url``: uv writes one or the other per package.
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==|@)")


def parse_packages(compiled: str) -> list[str]:
    """Package names (PEP 503 normalised, sorted) from ``uv pip compile`` output.

    A line that is neither a comment nor a requirement fails loudly, so an
    output format this does not understand cannot let a package through
    uncounted.
    """
    names = set()
    for line in compiled.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _REQ.match(line)
        if not m:
            raise ValueError(f"unrecognised line in uv pip compile output: {line!r}")
        names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return sorted(names)


def compile_target(platform: str, python: str) -> list[str]:
    try:
        out = subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                "pyproject.toml",
                "--python-version",
                python,
                "--python-platform",
                platform,
                "--no-header",
                "--no-annotate",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        # A resolver conflict or an index outage: show uv's own reason, which
        # CalledProcessError's message leaves out.
        raise SystemExit(
            f"uv pip compile failed for {platform}/{python} (exit {exc.returncode}):\n"
            f"{(exc.stderr or '').strip()}"
        ) from exc
    return parse_packages(out)


def check(name: str, packages: list[str], ceiling: int, baseline: list[str]) -> str | None:
    """Return a failure message, or None when the count is within the ceiling."""
    if len(packages) <= ceiling:
        return None
    added, removed = sorted(set(packages) - set(baseline)), sorted(set(baseline) - set(packages))
    return (
        f"{name}: base install resolves to {len(packages)} packages (ceiling {ceiling}).\n"
        f"  added vs baseline:   {', '.join(added) or '-'}\n"
        f"  removed vs baseline: {', '.join(removed) or '-'}\n"
        "  Over the ceiling: a new dependency, or an upstream release that grew its own "
        "tree. Drop it, or move it to an extra.\n"
        "  If the growth is intended, raise the ceiling in scripts/ci/dep_budget.py "
        "and run --update."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the baseline lists")
    args = ap.parse_args(argv)

    failures = []
    for name, (platform, python, ceiling) in TARGETS.items():
        packages = compile_target(platform, python)
        path = BASELINES / f"{name}.txt"
        # Compare against the committed baseline before --update rewrites it,
        # so an over-ceiling failure still names what changed.
        baseline = path.read_text().split() if path.exists() else []
        print(f"{name}: {len(packages)} packages (ceiling {ceiling})")
        msg = check(name, packages, ceiling, baseline)
        if msg:
            failures.append(msg)
        elif len(packages) < ceiling:
            # Ratchet: a ceiling with room to spare would let the count creep back.
            print(
                f"::notice::{name} is under its ceiling: lower it to {len(packages)} "
                "in scripts/ci/dep_budget.py and run --update"
            )
        if args.update:
            BASELINES.mkdir(exist_ok=True)
            path.write_text("".join(f"{p}\n" for p in packages))
    if failures:
        print("\n" + "\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
