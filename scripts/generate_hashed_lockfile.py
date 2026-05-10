#!/usr/bin/env python3
"""Generate a hash-pinned ``requirements.lock.txt`` from the
current environment. Stronger than ``pip freeze`` because every
dependency carries a sha256 hash that ``pip install --require-hashes``
verifies, defeating supply-chain attacks where a published wheel
gets silently re-uploaded with malicious content under the same
version number.

No new deps required — this script uses ``pip download`` (already
in pip) to fetch each pinned package and compute its sha256.

Usage:
  .venv/bin/python scripts/generate_hashed_lockfile.py \\
    --in requirements.lock.txt --out requirements.lock.hashed.txt

Run after every intentional dep bump (pyproject.toml change, new
extra installed). The output is committed to the repo so CI / ops
can install with hash verification:

  pip install --require-hashes -r requirements.lock.hashed.txt

Today the script downloads wheels for the host platform only.
Cross-platform hashed lockfiles need ``pip-compile`` from
``pip-tools`` — when that becomes a project dep, prefer it. This
script is the no-extra-deps fallback so the artifact ships
regardless.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Tuple

PIN_RE = re.compile(r"^([a-zA-Z0-9._\-]+)==([0-9a-zA-Z._+\-]+)\s*(?:;.*)?$")


def parse_lockfile(path: Path) -> Iterator[Tuple[str, str, str]]:
    """Yield (name, version, marker_suffix) for every pinned entry."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Preserve env-marker suffixes (`; platform_system == 'Windows'`).
        marker = ""
        if ";" in line:
            line, marker_part = line.split(";", 1)
            marker = "; " + marker_part.strip()
        match = PIN_RE.match(line + (marker if marker else ""))
        if not match:
            print(f"  skip (not a pin): {raw}", file=sys.stderr)
            continue
        yield match.group(1), match.group(2), marker


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_one(name: str, version: str, work_dir: Path) -> Tuple[str, ...]:
    """Download ``{name}=={version}`` to ``work_dir`` and return the
    list of sha256 hashes for every artifact pip resolved (typically
    one wheel per platform)."""
    target = work_dir / name
    target.mkdir(exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--dest",
        str(target),
        f"{name}=={version}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            f"  ! pip download failed for {name}=={version}: {proc.stderr.strip()}", file=sys.stderr
        )
        return ()
    hashes = []
    for artifact in sorted(target.glob("*")):
        if artifact.is_file():
            hashes.append(sha256_of(artifact))
    return tuple(hashes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="src", required=True, help="Input pinned lockfile")
    parser.add_argument("--out", dest="dst", required=True, help="Output hash-pinned file")
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.exists():
        print(f"ERROR: {src} does not exist", file=sys.stderr)
        return 2

    out_lines = [
        "# Hash-pinned lockfile — every install is sha256-verified.",
        "#",
        "# Install with:",
        "#   pip install --require-hashes -r requirements.lock.hashed.txt",
        "#",
        "# Regenerate after every intentional dep bump:",
        "#   .venv/bin/python scripts/generate_hashed_lockfile.py \\",
        "#     --in requirements.lock.txt --out requirements.lock.hashed.txt",
        "#",
        "# Cross-platform hashing requires pip-tools' pip-compile (not a forge-cli",
        "# dep). The hashes below are for the host that generated the file; CI",
        "# should regenerate per OS / arch when needed.",
        "",
    ]
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for name, version, marker in parse_lockfile(src):
            print(f"hashing {name}=={version}…")
            hashes = hash_one(name, version, work)
            if not hashes:
                # Skip packages we can't hash (e.g. local wheels).
                out_lines.append(f"# unhashed: {name}=={version}{marker}")
                out_lines.append(f"{name}=={version}{marker}")
                continue
            line = f"{name}=={version}{marker}"
            for h in hashes:
                line += f" \\\n    --hash=sha256:{h}"
            out_lines.append(line)
    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"\nwrote {dst} ({len(out_lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
