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

"""
FLUID 0.7.1 Sovereignty Validator

Validates data sovereignty constraints against infrastructure bindings.
Prevents deployment of contracts that violate jurisdiction requirements.
"""

import csv
import functools
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._common import iter_exposes


class EnforcementMode(Enum):
    """Sovereignty enforcement modes."""

    STRICT = "strict"  # Block deployment on violation
    ADVISORY = "advisory"  # Warn only, allow deployment
    AUDIT = "audit"  # Log for compliance tracking


# Single source of truth for the sovereignty-block defaults, mirroring the
# ``default`` keys the JSON schema declares for ``$defs.sovereignty``. Anything
# that needs to display or apply a default reads these, so the value used to
# decide and the value shown to the operator cannot drift apart.
# ``tests/test_sovereignty.py`` pins them against the bundled schema.
DEFAULT_ENFORCEMENT_MODE = "strict"
DEFAULT_DATA_RESIDENCY = True
DEFAULT_CROSS_BORDER_TRANSFER = False

# Distinct from ``None``, which the jurisdiction map also returns for an
# unmapped region. Check 4 needs "no baseline yet" and "jurisdiction unknown"
# to be different states — see the comment there.
_UNSET = object()


@dataclass
class SovereigntyViolation:
    """Represents a sovereignty constraint violation."""

    severity: str  # "error", "warning", "info"
    message: str
    expose_id: Optional[str] = None
    region_found: Optional[str] = None
    region_expected: Optional[List[str]] = None
    suggestion: Optional[str] = None


_VENDORED_REGION_DATA = Path(__file__).parent / "data" / "cloud_regions"


def _parse_place(description: str) -> List[str]:
    """Candidate place names from a botocore region description.

    ``"Europe (London)"`` -> ``["London", "Europe"]`` — the parenthetical first,
    since "Europe" alone does not name a country and "London" does.
    """
    match = re.match(r"^(.*?)\s*\((.*)\)\s*$", description or "")
    if not match:
        return [description.strip()] if description else []
    return [match.group(2).strip(), match.group(1).strip()]


def _jurisdiction_for_description(description: str) -> Optional[str]:
    """Resolve a botocore description through the two hand tables."""
    places = _parse_place(description)
    # Specials first across ALL candidates. "AWS GovCloud (US-East)" resolves
    # correctly today only because the parenthetical is "US-East" (hyphen)
    # while PLACE_COUNTRIES holds "US East" (space) — if AWS ever normalises
    # that string, a per-place loop would silently downgrade GovCloud to plain
    # US. Ordering the lookups this way removes the coupling.
    for place in places:
        special = SovereigntyValidator.SPECIAL_PLACE_JURISDICTIONS.get(place)
        if special:
            return special
    for place in places:
        country = SovereigntyValidator.PLACE_COUNTRIES.get(place)
        if country:
            return SovereigntyValidator.COUNTRY_JURISDICTIONS.get(country)
    return None


def _load_vendored(provider: str) -> Dict[str, str]:
    """``region -> jurisdiction`` from a vendored dgl/cloud-regions csv."""
    path = _VENDORED_REGION_DATA / f"{provider}.csv"
    if not path.exists():  # pragma: no cover - packaging guard
        return {}
    resolved: Dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            region = (row.get("region") or "").strip().strip('"')
            country = (row.get("country_tld") or "").strip().lower()
            jurisdiction = SovereigntyValidator.COUNTRY_JURISDICTIONS.get(country)
            if region and jurisdiction:
                resolved[region] = jurisdiction
    return resolved


def _load_botocore_aws() -> Dict[str, str]:
    """``region -> jurisdiction`` from botocore's shipped ``endpoints.json``.

    Returns ``{}`` when botocore is absent — the light CLI does not require
    boto3, and a missing optional dependency must degrade to the vendored csv
    rather than break a governance check.
    """
    try:
        import botocore  # noqa: F401 — presence check, path taken from the module
    except ImportError:
        return {}
    try:
        data_path = Path(botocore.__file__).parent / "data" / "endpoints.json"
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):  # pragma: no cover - corrupt/renamed SDK data
        return {}

    resolved: Dict[str, str] = {}
    for partition in payload.get("partitions") or []:
        for region, meta in (partition.get("regions") or {}).items():
            jurisdiction = _jurisdiction_for_description(meta.get("description", ""))
            if jurisdiction:
                resolved[region] = jurisdiction
    return resolved


@functools.lru_cache(maxsize=1)
def region_jurisdiction_map() -> Mapping[str, str]:
    """The resolved ``region -> jurisdiction`` table.

    Later sources win: vendored csv (all three clouds) first, then botocore for
    AWS, which is the vendor's own data and measurably ahead of the csv.

    Memoised — the tables are static for the life of the process — and lazy, so
    ``fluid --help`` never imports botocore or reads a csv. Call
    ``region_jurisdiction_map.cache_clear()`` in tests that need a rebuild.
    """
    table: Dict[str, str] = {}
    for provider in ("aws", "gcp", "azure"):
        table.update(_load_vendored(provider))
    table.update(_load_botocore_aws())
    # Read-only: the cached object is shared process-wide (and re-exported by
    # providers/aws/util/sovereignty.py), so a stray mutation anywhere would
    # silently rewrite the jurisdiction table for every later check.
    return MappingProxyType(table)


class SovereigntyValidator:
    """Validates sovereignty constraints in FLUID 0.7.1 contracts."""

    # Region → jurisdiction is **derived, not hand-maintained**.
    #
    # A region table in a repo is a losing race: the clouds ship regions faster
    # than anyone edits this file, and a stale entry is a silent governance bug
    # rather than a missing feature. So the volatile half comes from data that
    # updates itself:
    #
    #   AWS          botocore's ``endpoints.json`` — the vendor's own table,
    #                shipped with the SDK and refreshed on every release. Covers
    #                all partitions, GovCloud and the EU Sovereign Cloud included.
    #   GCP / Azure  ``policy/data/cloud_regions/*.csv``, vendored from
    #                dgl/cloud-regions (ODbL-1.0 — see the README next to it).
    #                Neither vendor ships an offline dataset of their own.
    #
    # Measured when this was written: the vendored AWS csv was 14 regions behind
    # botocore (missing ``eusc-de-east-1``, the AWS European Sovereign Cloud,
    # among others) and had no region botocore lacked — hence botocore first for
    # AWS, with the csv as the fallback for installs without boto3.
    #
    # What stays hand-written is the two small, *geopolitically* stable tables
    # below. They change when borders and treaties change, not when a vendor
    # opens a datacentre.
    #
    # Resolution is lazy and memoised (:func:`region_jurisdiction_map`), so the
    # ``fluid --help`` cold path never imports botocore or reads a csv.

    #: Country (ISO-ish, TLD-biased — ``uk`` not ``gb``, matching the vendored
    #: csv) → jurisdiction. **Identity, not adequacy**: the UK and Switzerland
    #: hold GDPR adequacy decisions, so an EU→UK transfer is usually lawful, but
    #: it is still a transfer to a third country and a contract asking for
    #: ``jurisdiction: EU`` has not asked for the UK. Conflating the two is why
    #: London used to resolve to "EU" and an EU-only product deploying there
    #: reported clean. Adequacy belongs in ``transferMechanisms``.
    COUNTRY_JURISDICTIONS = {
        # EU member states
        "at": "EU",
        "be": "EU",
        "bg": "EU",
        "hr": "EU",
        "cy": "EU",
        "cz": "EU",
        "dk": "EU",
        "ee": "EU",
        "fi": "EU",
        "fr": "EU",
        "de": "EU",
        "gr": "EU",
        "hu": "EU",
        "ie": "EU",
        "it": "EU",
        "lv": "EU",
        "lt": "EU",
        "lu": "EU",
        "mt": "EU",
        "nl": "EU",
        "pl": "EU",
        "pt": "EU",
        "ro": "EU",
        "sk": "EU",
        "si": "EU",
        "es": "EU",
        "se": "EU",
        # EEA but not EU — GDPR applies, EU-only residency still excludes them
        "no": "EEA",
        "is": "EEA",
        "li": "EEA",
        # Everyone else, by country
        "uk": "UK",
        "ch": "CH",
        "us": "US",
        "ca": "CA",
        "mx": "MX",
        "br": "BR",
        "cl": "CL",
        "il": "IL",
        "ae": "AE",
        "bh": "BH",
        "sa": "SA",
        "qa": "QA",
        "za": "ZA",
        "ng": "NG",
        "ke": "KE",
        "jp": "JP",
        "kr": "KR",
        "cn": "CN",
        "hk": "HK",
        "tw": "TW",
        "sg": "SG",
        "in": "IN",
        "id": "ID",
        "my": "MY",
        "th": "TH",
        "vn": "VN",
        "ph": "PH",
        "au": "AU",
        "nz": "NZ",
        "tr": "TR",
    }

    #: Place → country, keyed on the names botocore puts in its region
    #: descriptions ("Europe (London)" → ``London``, "Israel (Tel Aviv)" →
    #: ``Israel``). Both the parenthetical and the outer group are tried, so a
    #: description need only match on one. Only needed because botocore
    #: describes regions in prose rather than by country code.
    PLACE_COUNTRIES = {
        "Frankfurt": "de",
        "Ireland": "ie",
        "Paris": "fr",
        "Milan": "it",
        "Spain": "es",
        "Stockholm": "se",
        "Germany": "de",
        "London": "uk",
        "Zurich": "ch",
        "US East": "us",
        "US West": "us",
        "Canada": "ca",
        "Canada West": "ca",
        "Mexico": "mx",
        "Sao Paulo": "br",
        "South America": "br",
        "Israel": "il",
        "Tel Aviv": "il",
        "Bahrain": "bh",
        "UAE": "ae",
        "Cape Town": "za",
        "Hong Kong": "hk",
        "Taipei": "tw",
        "Mumbai": "in",
        "Hyderabad": "in",
        "Tokyo": "jp",
        "Osaka": "jp",
        "Seoul": "kr",
        "Singapore": "sg",
        "Sydney": "au",
        "Melbourne": "au",
        "Jakarta": "id",
        "Malaysia": "my",
        "New Zealand": "nz",
        "Thailand": "th",
        "Beijing": "cn",
        "Ningxia": "cn",
    }

    #: Descriptions that name a jurisdiction directly rather than a place.
    #: Descriptions that name a jurisdiction directly rather than a place.
    #:
    #: NB: the classified partitions ("US ISO East", "US ISOB East (Ohio)",
    #: "EU ISOE West", …) deliberately resolve to Unknown — they carry no
    #: entry here and no place entry below. Do NOT "fix" that by adding
    #: ``"Ohio": "us"`` to PLACE_COUNTRIES: it would silently reclassify an
    #: air-gapped intelligence-community region as ordinary commercial US.
    SPECIAL_PLACE_JURISDICTIONS = {"AWS GovCloud": "US-GOV"}

    def validate(self, contract: Dict[str, Any]) -> Tuple[bool, List[SovereigntyViolation]]:
        """
        Validate sovereignty constraints.

        Returns:
            (is_valid, violations) - is_valid=False means BLOCK deployment in strict mode
        """
        violations = []

        # Extract sovereignty config (optional in 0.7.1)
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            return True, []  # No sovereignty constraints = always valid

        # Defaults MUST mirror the JSON schema's declared ``default`` keys
        # (``$defs.sovereignty`` in fluid-schema-0.7.x.json). They previously
        # did not — every one was the permissive inverse of what the schema
        # advertises — so a contract that declared a policy and relied on the
        # documented defaults was evaluated under the weakest possible
        # settings. A GDPR contract with exposes straddling EU and US printed
        # ``PASS`` because ``dataResidency`` silently became False and Check 4
        # was never entered. Same fail-open class as the empty-hook bug: the
        # control is present, and quietly does nothing.
        enforcement_mode = EnforcementMode(
            sovereignty.get("enforcementMode", DEFAULT_ENFORCEMENT_MODE)
        )
        allowed_regions = sovereignty.get("allowedRegions", [])
        denied_regions = sovereignty.get("deniedRegions", [])
        jurisdiction = sovereignty.get("jurisdiction")
        data_residency = sovereignty.get("dataResidency", DEFAULT_DATA_RESIDENCY)
        cross_border_transfer = sovereignty.get(
            "crossBorderTransfer", DEFAULT_CROSS_BORDER_TRANSFER
        )

        # Validate each expose's binding location
        for expose in iter_exposes(contract):
            binding = expose.get("binding", {})
            location = binding.get("location", {})
            region = location.get("region")

            if not region:
                continue  # No region specified, skip validation

            expose_id = expose.get("exposeId", "unknown")

            # Check 1: Denied regions (always enforced regardless of mode)
            if region in denied_regions:
                violations.append(
                    SovereigntyViolation(
                        severity="error",
                        message=f"Region '{region}' is explicitly denied by sovereignty policy",
                        expose_id=expose_id,
                        region_found=region,
                        region_expected=allowed_regions,
                        suggestion=f"Use one of the allowed regions: {', '.join(allowed_regions) if allowed_regions else 'none specified'}",
                    )
                )

            # Check 2: Allowed regions (if specified)
            if allowed_regions and region not in allowed_regions:
                severity = "error" if enforcement_mode == EnforcementMode.STRICT else "warning"
                violations.append(
                    SovereigntyViolation(
                        severity=severity,
                        message=f"Region '{region}' not in allowed regions list",
                        expose_id=expose_id,
                        region_found=region,
                        region_expected=allowed_regions,
                        suggestion=f"Allowed regions: {', '.join(allowed_regions)}",
                    )
                )

            # Check 3: Jurisdiction match
            if jurisdiction and jurisdiction != "Global":
                region_jurisdiction = region_jurisdiction_map().get(region, "Unknown")
                if region_jurisdiction != jurisdiction and region_jurisdiction != "Global":
                    violations.append(
                        SovereigntyViolation(
                            severity="warning",
                            message=f"Region '{region}' (jurisdiction: {region_jurisdiction}) "
                            f"does not match required jurisdiction: {jurisdiction}",
                            expose_id=expose_id,
                            suggestion=f"Consider using regions in {jurisdiction} jurisdiction",
                        )
                    )

        # Check 4: Data residency and cross-border transfer.
        #
        # Hoisted OUT of the per-expose loop — it is a property of the contract
        # as a whole, and running it per-expose emitted one duplicate error per
        # expose.
        #
        # The subtlety this code exists to get right: ``None`` used to mean two
        # different things — "no baseline yet" and "this region is not in
        # the resolved region table. Conflating them made the check both
        # over- and under-sensitive, and its verdict depended on expose order:
        #   * eu-west-1 + eu-south-1 (both EU, the latter unmapped) -> BLOCKED
        #   * the same two, reversed                                -> passed
        #   * me-central-1 + us-gov-west-1 (both unmapped)          -> passed,
        #     a genuine cross-border transfer reported as clean.
        # The map holds 31 entries and will always lag AWS/GCP/Azure, so
        # "unknown" is a real, common state that needs its own answer rather
        # than being silently folded into a jurisdiction comparison.
        if data_residency and not cross_border_transfer:
            baseline: Any = _UNSET
            for exp in iter_exposes(contract):
                exp_region = exp.get("binding", {}).get("location", {}).get("region")
                if not exp_region:
                    continue
                exp_id = exp.get("exposeId", "unknown")
                exp_jurisdiction = region_jurisdiction_map().get(exp_region, "Unknown")

                if exp_jurisdiction == "Unknown":
                    # Say what we actually know. Warning severity, so an
                    # unmapped-but-legitimate region does not block a
                    # deployment on the strength of a gap in our own table.
                    violations.append(
                        SovereigntyViolation(
                            severity="warning",
                            message=(
                                f"Region '{exp_region}' has no known jurisdiction — "
                                f"cross-border transfer cannot be verified for this expose"
                            ),
                            expose_id=exp_id,
                            region_found=exp_region,
                            suggestion=(
                                "Add this region to the jurisdiction map, or pin residency "
                                "explicitly with sovereignty.allowedRegions"
                            ),
                        )
                    )
                    # Never seeds or trips the baseline — an unknown must not
                    # masquerade as agreement with another unknown.
                    continue

                if baseline is _UNSET:
                    baseline = exp_jurisdiction
                elif exp_jurisdiction != baseline:
                    violations.append(
                        SovereigntyViolation(
                            severity="error",
                            message=(
                                "Cross-border data transfer prohibited but multiple "
                                f"jurisdictions detected ({baseline} and {exp_jurisdiction})"
                            ),
                            expose_id=exp_id,
                            region_found=exp_region,
                            suggestion="Ensure all regions are within the same jurisdiction when crossBorderTransfer=false",
                        )
                    )
                    break

        # Determine final validity based on enforcement mode
        has_errors = any(v.severity == "error" for v in violations)

        if enforcement_mode == EnforcementMode.STRICT:
            is_valid = not has_errors
        elif enforcement_mode == EnforcementMode.ADVISORY:
            is_valid = True  # Warnings only, allow deployment
        else:  # AUDIT
            is_valid = True  # Log only, allow deployment

        return is_valid, violations


def validate_sovereignty(contract: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Convenience function for CLI integration.

    Returns:
        (is_valid, error_messages)
    """
    validator = SovereigntyValidator()
    is_valid, violations = validator.validate(contract)

    messages = []
    for v in violations:
        prefix = "❌" if v.severity == "error" else "⚠️" if v.severity == "warning" else "ℹ️"
        msg = f"{prefix} [{v.expose_id}] {v.message}"
        if v.suggestion:
            msg += f"\n   💡 {v.suggestion}"
        messages.append(msg)

    return is_valid, messages


def get_region_jurisdiction(region: str) -> str:
    """
    Get jurisdiction for a region.

    Args:
        region: Cloud region identifier

    Returns:
        Jurisdiction code (EU, US, etc.) or "Unknown"
    """
    return region_jurisdiction_map().get(region, "Unknown")
