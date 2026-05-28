#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 SBOM from the current Python env. (EXPERIMENTAL)

EXPERIMENTAL — this is a developer / supply-chain convenience, not yet
part of the supported release surface or CI. It's kept behind this
notice until a real operator workflow consumes the SBOM; until then it
carries no stability guarantee and may change or be removed.


Walks ``pip list --format=json``, queries PyPI for license + project
URL on each package, and writes a CycloneDX-conformant JSON document
operators can ship to enterprise security teams or import into
tools like Dependency-Track.

Borrow-before-build: the CycloneDX document is built with the official
`cyclonedx-python-lib <https://github.com/CycloneDX/cyclonedx-python-lib>`_
(``Bom`` / ``Component`` model + ``make_outputter``) rather than
hand-emitting the JSON. An earlier version hand-rolled the JSON "because
the schema fits in 50 lines"; that rationale doesn't hold — the library
is already present in the venv (transitive), gives **schema-validated**
output, and tracks future CycloneDX spec versions for free, so the
hand-roll was a maintenance liability with no upside. Package metadata
fetches still use ``httpx`` (a core forge-cli dep).

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
from pathlib import Path
from typing import Any, Dict, List

try:
    import httpx
    from cyclonedx.model import ExternalReference, ExternalReferenceType, XsUri
    from cyclonedx.model.bom import Bom
    from cyclonedx.model.component import Component, ComponentType
    from cyclonedx.model.license import DisjunctiveLicense
    from cyclonedx.output import make_outputter
    from cyclonedx.schema import OutputFormat, SchemaVersion
    from packageurl import PackageURL
except ImportError as exc:
    print(
        f"ERROR: missing dependency ({exc.name}). Run inside the project venv "
        "with the dev extra: pip install -e '.[dev]'",
        file=sys.stderr,
    )
    sys.exit(2)


PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"

# Map our internal external-reference kinds → CycloneDX enum members.
_EXT_REF_TYPES = {
    "homepage": ExternalReferenceType.WEBSITE,
    "documentation": ExternalReferenceType.DOCUMENTATION,
    "vcs": ExternalReferenceType.VCS,
}


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


def _build_component(name: str, version: str, info: Dict[str, Any]) -> "tuple[Component, str]":
    """Build a CycloneDX library Component from PyPI metadata.

    Returns ``(component, resolved_license_string)`` — the license
    string is returned alongside for the progress log line."""
    lic = license_field(info)
    component = Component(
        name=name,
        version=version,
        type=ComponentType.LIBRARY,
        purl=PackageURL(type="pypi", name=name, version=version),
        bom_ref=f"pkg:pypi/{name}@{version}",
        # ``DisjunctiveLicense(name=...)`` is the named-license path
        # (not SPDX-id-validated), safe for PyPI's inconsistent strings
        # incl. the ``NOASSERTION`` sentinel.
        licenses=[DisjunctiveLicense(name=lic)],
    )
    for kind, enum_type in _EXT_REF_TYPES.items():
        url = (
            info.get("home_page")
            if kind == "homepage"
            else (info.get("project_urls") or {}).get(
                "Documentation" if kind == "documentation" else "Source"
            )
        )
        if url:
            component.external_references.add(ExternalReference(type=enum_type, url=XsUri(url)))
    return component, lic


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

    print(
        "[EXPERIMENTAL] generate_sbom.py is a developer convenience and is NOT "
        "yet a supported, CI-gated artifact."
    )
    packages = list_packages()
    print(f"resolved {len(packages)} packages from `pip list`")

    # ``Bom()`` auto-assigns the serialNumber (urn:uuid) + metadata
    # timestamp; we only set the described component + the component list.
    bom = Bom()
    bom.metadata.component = Component(
        name=args.component_name,
        version=args.component_version,
        type=ComponentType.APPLICATION,
        bom_ref=f"pkg:pypi/{args.component_name}@{args.component_version}",
    )

    with httpx.Client() as client:
        for pkg in packages:
            name = pkg["name"]
            version = pkg["version"]
            info = fetch_pypi_metadata(client, name, version)
            component, lic = _build_component(name, version, info)
            bom.components.add(component)
            print(f"  {name}=={version}  ({lic})")

    outputter = make_outputter(bom, OutputFormat.JSON, SchemaVersion.V1_5)
    Path(args.out).write_text(outputter.output_as_string(indent=2), encoding="utf-8")
    print(f"\nwrote {args.out} ({len(bom.components)} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
