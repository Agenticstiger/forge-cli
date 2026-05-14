#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 SBOM from the current Python env.

Walks ``pip list --format=json``, queries PyPI for license + project
URL on each package, and writes a CycloneDX-conformant JSON document
operators can ship to enterprise security teams or import into
tools like Dependency-Track.

Cost: zero extra deps. Uses ``httpx`` (already a forge-cli dep) for
PyPI metadata fetches; CycloneDX JSON is hand-emitted because the
spec is small enough that adding ``cyclonedx-bom`` for it would be
the kind of borrow-vs-build tradeoff our /borrow-before-build skill
deliberately rejects when the schema fits in 50 lines.

Usage:
  .venv/bin/python scripts/generate_sbom.py --out sbom.cyclonedx.json

The output validates against
https://cyclonedx.org/docs/1.5/json/.

Limits (call out, don't paper over):
- License field comes from PyPI metadata. If a package's classifier
  is ``UNKNOWN``, we record ``NOASSERTION`` per SPDX convention.
- Component hashes are NOT included in the SBOM today — they live
  in ``requirements.lock.hashed.txt``, generated via
  ``uv pip compile pyproject.toml --generate-hashes -o
  requirements.lock.hashed.txt``.
- Vulnerabilities are NOT cross-referenced; pair the SBOM with a
  scanner like ``pip-audit`` for that signal.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run inside the project venv.", file=sys.stderr)
    sys.exit(2)


PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"


def list_packages() -> List[Dict[str, str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def fetch_pypi_metadata(client: httpx.Client, name: str, version: str) -> Dict[str, Any]:
    try:
        resp = client.get(PYPI_URL.format(name=name, version=version), timeout=10.0)
        if resp.status_code != 200:
            return {}
        return resp.json().get("info") or {}
    except (httpx.HTTPError, json.JSONDecodeError):
        return {}


def license_field(info: Dict[str, Any]) -> str:
    """Best-effort license string — PyPI is not consistent here."""
    license_str = info.get("license") or ""
    if license_str and license_str.upper() != "UNKNOWN":
        return license_str.strip()
    # Fall back to classifiers like "License :: OSI Approved :: MIT License".
    for classifier in info.get("classifiers") or []:
        if classifier.startswith("License ::") and "OSI Approved" in classifier:
            tail = classifier.rsplit("::", 1)[-1].strip()
            if tail and tail != "OSI Approved":
                return tail
    return "NOASSERTION"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output CycloneDX JSON path")
    parser.add_argument(
        "--component-name",
        default="data-product-forge",
        help="Top-level component name in the SBOM",
    )
    parser.add_argument("--component-version", default="0.7.4", help="Top-level component version")
    args = parser.parse_args()

    packages = list_packages()
    print(f"resolved {len(packages)} packages from `pip list`")

    components: List[Dict[str, Any]] = []
    with httpx.Client() as client:
        for pkg in packages:
            name = pkg["name"]
            version = pkg["version"]
            info = fetch_pypi_metadata(client, name, version)
            licenses = []
            lic = license_field(info)
            if lic and lic != "NOASSERTION":
                licenses.append({"license": {"name": lic}})
            else:
                licenses.append({"license": {"name": "NOASSERTION"}})
            external_refs = []
            for kind, url in [
                ("homepage", info.get("home_page")),
                ("documentation", (info.get("project_urls") or {}).get("Documentation")),
                ("vcs", (info.get("project_urls") or {}).get("Source")),
            ]:
                if url:
                    external_refs.append({"type": kind, "url": url})
            component = {
                "type": "library",
                "bom-ref": f"pkg:pypi/{name}@{version}",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name}@{version}",
                "licenses": licenses,
            }
            if external_refs:
                component["externalReferences"] = external_refs
            components.append(component)
            print(f"  {name}=={version}  ({lic})")

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [
                {
                    "vendor": "Agentics Transformation Ltd",
                    "name": "scripts/generate_sbom.py",
                    "version": "0.1.0",
                }
            ],
            "component": {
                "type": "application",
                "bom-ref": f"pkg:pypi/{args.component_name}@{args.component_version}",
                "name": args.component_name,
                "version": args.component_version,
            },
        },
        "components": components,
    }
    Path(args.out).write_text(json.dumps(bom, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out} ({len(components)} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
