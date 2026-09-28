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
from typing import AbstractSet, Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

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
def severity_for(mode: "EnforcementMode") -> str:
    """Map an enforcement mode onto the severity a blocking-class violation carries.

    The schema defines the modes in one sentence — "strict = block deployment,
    advisory = warn, audit = log only" — and this is the only place that sentence
    is turned into behaviour.

    It has to live in one place because severity is not merely a label here: it
    is what actually decides the outcome. ``fluid validate`` routes messages by
    their rendered PREFIX (❌ / ⚠️ / ℹ️, see validate_sovereignty below and
    cli/validate.py), so a hardcoded severity silently overrides the mode no
    matter what the returned boolean says. That is how this diverged in both
    directions at once: check 1 hardcoded "error", so `advisory` failed builds it
    was documented to merely warn about, and check 3 hardcoded "warning", so
    `strict` could not block a jurisdiction mismatch — the single thing the
    `jurisdiction` field exists to catch.
    """
    if mode == EnforcementMode.STRICT:
        return "error"
    if mode == EnforcementMode.ADVISORY:
        return "warning"
    return "info"


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
    table.update(SovereigntyValidator.VENDORED_CORRECTIONS)
    table.update(_gcp_locations_not_vendored())
    table.update(_MULTI_REGION_JURISDICTIONS)
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

    #: Gap-fills for rows the upstream dataset ships incomplete.
    #:
    #: Kept HERE rather than patched into the vendored csv: that file is a
    #: pristine copy of an ODbL dataset and has to stay refreshable from
    #: upstream, so corrections live in our own source with a reason each.
    #: botocore still wins where it has an answer — it agrees with all three.
    #: Only reachable on installs without boto3, which is exactly where a
    #: silent ``Unknown`` would be least noticed.
    VENDORED_CORRECTIONS = {
        # Upstream row reads literally: eu-south-2,"EU () - ???",,,,
        # AWS calls it "Europe (Spain)".
        "eu-south-2": "EU",
        # Upstream leaves country_tld empty for both GovCloud regions.
        "us-gov-east-1": "US-GOV",
        "us-gov-west-1": "US-GOV",
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
        # Extract sovereignty config (optional in 0.7.1)
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            return True, []  # No sovereignty constraints = always valid
        return self.check_placements(
            sovereignty,
            contract_placements(contract),
            region_placed=region_placed_exposes(contract),
        )

    def check_placements(
        self,
        sovereignty: Mapping[str, Any],
        placements: Sequence[Tuple[str, Optional[str]]],
        *,
        region_placed: AbstractSet[str] = frozenset(),
    ) -> Tuple[bool, List[SovereigntyViolation]]:
        """Evaluate ``sovereignty`` against each ``(where, region)`` placement.

        :meth:`validate` passes the contract's own bindings
        (:func:`contract_placements`); a provider hook passes the places its
        emitted resources actually land (the GCP plugin passes every resource
        ``location``), so a region the provider filled in by default is
        checked where it is used. A placement whose region is ``None`` is a
        binding on a region-placed platform that names none.

        ``region_placed`` names the placements (their ``where``) that sit on a
        :data:`REGION_PLACED_PLATFORMS` cloud. For those, a strict
        jurisdiction refuses a region whose jurisdiction cannot be resolved
        (check 3); elsewhere that stays a warning.
        """
        violations = []

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

        # Validate each place the contract puts data
        for expose_id, region in placements:
            # Check 0: a region-placed binding that names no region. It used
            # to be skipped ("no region, nothing to check"), which failed OPEN:
            # validate and ``plan --check-sovereignty`` printed PASS while the
            # platform picked the region itself (BigQuery: the US multi-region,
            # measured on an EU-only contract). The mode decides, like check 2:
            # strict refuses, advisory warns, audit logs.
            if not region:
                violations.append(
                    SovereigntyViolation(
                        severity=severity_for(enforcement_mode),
                        message=(
                            "Binding declares no region, so where its data lives cannot "
                            "be checked against the sovereignty policy (the platform "
                            "would choose)"
                        ),
                        expose_id=expose_id,
                        region_expected=allowed_regions or None,
                        suggestion=(
                            "Set binding.location.region"
                            + (
                                f" to one of: {', '.join(allowed_regions)}"
                                if allowed_regions
                                else ""
                            )
                        ),
                    )
                )
                continue

            # Check 1: Denied regions — deliberately an error in EVERY mode.
            #
            # This is the one carve-out from severity_for(), and it is a
            # considered decision rather than an oversight: an entry in
            # deniedRegions is an operator naming a specific prohibition, which
            # outranks a mode default in the same way an explicit denylist
            # outranks absence from an allowlist in the agentPolicy CHECK_ORDER.
            # tests/cli/test_plan_sovereignty_gate.py pins it, and pins WHY:
            # `fluid validate` exits 1 on an explicit deny, so `plan` must block
            # too or the two stages disagree about the same contract.
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
                severity = severity_for(enforcement_mode)
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
            if jurisdiction and jurisdiction not in UNCONSTRAINED_JURISDICTIONS:
                region_jurisdiction = region_jurisdiction_map().get(region, "Unknown")
                if region_jurisdiction != jurisdiction and region_jurisdiction != "Global":
                    # "Unknown" is an inability to evaluate, not a violation: a
                    # region the table does not carry says nothing about where
                    # it is. On a cloud region (aws / gcp / azure) under
                    # strict, though, a jurisdiction the policy cannot show is
                    # a place it cannot allow: the GCP table lagged Google by
                    # nine regions and the ASIA multi-region, and each went
                    # through validate and `generate iac` with a warning
                    # (me-central2 on an EU-only contract, measured). So
                    # strict refuses it there, unless the operator vouched for
                    # the region by naming it in allowedRegions. Advisory and
                    # audit, and every other platform, keep the warning.
                    # Check 4 still refuses to let one Unknown agree with
                    # another.
                    unresolvable = region_jurisdiction == "Unknown"
                    fail_closed = (
                        unresolvable
                        and enforcement_mode is EnforcementMode.STRICT
                        and expose_id in region_placed
                        and region not in allowed_regions
                    )
                    violations.append(
                        SovereigntyViolation(
                            severity=(
                                "warning"
                                if unresolvable and not fail_closed
                                else severity_for(enforcement_mode)
                            ),
                            message=f"Region '{region}' (jurisdiction: {region_jurisdiction}) "
                            f"does not match required jurisdiction: {jurisdiction}",
                            expose_id=expose_id,
                            suggestion=(
                                f"Use a region in the {jurisdiction} jurisdiction; if "
                                f"'{region}' is one, name it in sovereignty.allowedRegions"
                                if fail_closed
                                else f"Consider using regions in {jurisdiction} jurisdiction"
                            ),
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
            for exp_id, exp_region in placements:
                if not exp_region:
                    continue
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
                            severity=severity_for(enforcement_mode),
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

        # One rule, in every mode: an error-severity violation blocks and nothing
        # else does. The mode is already expressed in the severities themselves
        # (see severity_for), so re-applying it here is what previously let
        # is_valid=True be returned alongside an error-severity violation.
        #
        # Both existing callers work around that: cli/validate.py re-scans for
        # ❌ messages and cli/plan.py computes its own has_error_finding with a
        # comment explaining why the boolean could not be trusted. Those
        # workarounds are now redundant rather than load-bearing, and a third
        # caller branching on is_valid alone no longer silently under-enforces.
        has_errors = any(v.severity == "error" for v in violations)
        is_valid = not has_errors

        return is_valid, violations


#: Platforms whose bindings put data in a cloud region: a binding on one of
#: these with a sovereignty block and no region is a finding (check 0), not a
#: skip. Other platforms (``local`` above all, and those whose region lives
#: outside the binding) keep the old behaviour: no region, nothing checked.
REGION_PLACED_PLATFORMS = frozenset({"aws", "gcp", "azure"})

#: Multi-region locations the vendored region table does not carry. BigQuery
#: and Cloud Storage both name their multi-regions ``US`` (data centres in the
#: United States) and ``EU`` (data centres in EU member states); left unmapped
#: they resolved "Unknown", so the ``US`` a GCP binding with no region used to
#: land in could not fail a ``jurisdiction: EU`` check.
_MULTI_REGION_JURISDICTIONS = {"US": "US", "EU": "EU", "us": "US", "eu": "EU"}


#: GCP locations the vendored dataset (dgl/cloud-regions) does not carry, by
#: the countries their data centres are in, from Google's own location lists
#: (docs.cloud.google.com/bigquery/docs/locations and
#: /storage/docs/locations, read 2026-09-28). Kept here, not patched into the
#: ODbL csv, for the reason ``VENDORED_CORRECTIONS`` gives. A location
#: resolves only when every country it spans is in one jurisdiction: the
#: dual-regions EUR5 (Belgium + London), EUR7 (London + Frankfurt) and EUR8
#: (Frankfurt + Zürich) span two and stay Unknown, as does the ASIA
#: multi-region ("data centres in Asia", several countries), which a strict
#: jurisdiction then refuses on a cloud region (check 3).
_GCP_LOCATION_COUNTRIES: Dict[str, Tuple[str, ...]] = {
    "africa-south1": ("za",),  # Johannesburg
    "asia-southeast3": ("th",),  # Bangkok
    "europe-north2": ("se",),  # Stockholm
    "europe-west10": ("de",),  # Berlin
    "europe-west12": ("it",),  # Turin
    "me-central1": ("qa",),  # Doha
    "me-central2": ("sa",),  # Dammam
    "me-west1": ("il",),  # Tel Aviv
    "northamerica-south1": ("mx",),  # Mexico
    # Cloud Storage predefined dual-regions (either case is accepted).
    "asia1": ("jp",),  # Tokyo + Osaka
    "eur4": ("fi", "nl"),  # Finland + Netherlands
    "eur5": ("be", "uk"),  # Belgium + London
    "eur7": ("uk", "de"),  # London + Frankfurt
    "eur8": ("de", "ch"),  # Frankfurt + Zürich
    "nam4": ("us",),  # Iowa + South Carolina
}


def _gcp_locations_not_vendored() -> Dict[str, str]:
    """``location -> jurisdiction`` for :data:`_GCP_LOCATION_COUNTRIES`."""
    resolved: Dict[str, str] = {}
    for location, countries in _GCP_LOCATION_COUNTRIES.items():
        found = {SovereigntyValidator.COUNTRY_JURISDICTIONS.get(c) for c in countries}
        jurisdiction = found.pop() if len(found) == 1 else None
        if jurisdiction is None:
            continue
        resolved[location] = jurisdiction
        if location.isalnum():  # a dual-region code: EUR4 and eur4 alike
            resolved[location.upper()] = jurisdiction
    return resolved


def region_placed_exposes(contract: Mapping[str, Any]) -> FrozenSet[str]:
    """``exposeId`` of every expose bound to a :data:`REGION_PLACED_PLATFORMS` cloud."""
    out: Set[str] = set()
    for expose in iter_exposes(dict(contract)):
        binding = expose.get("binding") or {}
        if isinstance(binding, Mapping):
            if str(binding.get("platform") or "").lower() in REGION_PLACED_PLATFORMS:
                out.add(str(expose.get("exposeId", "unknown")))
    return frozenset(out)


def binding_region(binding: Mapping[str, Any]) -> Optional[str]:
    """The region a binding places its data in, read where its emitter reads it.

    ``location.region``, and for GCP also ``location.location``: the GCP
    emitter falls back to it (``iac/providers/gcp.py``), so a check that read
    ``region`` alone never saw a BigQuery dataset placed through ``location``.
    """
    location = binding.get("location") if isinstance(binding, Mapping) else None
    if not isinstance(location, Mapping):
        return None
    region = location.get("region")
    if not region and str(binding.get("platform") or "").lower() == "gcp":
        region = location.get("location")
    return str(region) if region else None


def contract_placements(contract: Mapping[str, Any]) -> List[Tuple[str, Optional[str]]]:
    """``(exposeId, region)`` for each expose the sovereignty checks evaluate.

    An expose with a region is always listed. One without is listed with
    ``None`` only on a :data:`REGION_PLACED_PLATFORMS` platform, where the
    region is the platform's to pick; any other binding with no region is
    left out, as before.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for expose in iter_exposes(dict(contract)):
        binding = expose.get("binding") or {}
        if not isinstance(binding, Mapping):
            continue
        region = binding_region(binding)
        platform = str(binding.get("platform") or "").lower()
        if region or platform in REGION_PLACED_PLATFORMS:
            out.append((str(expose.get("exposeId", "unknown")), region))
    return out


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


#: Jurisdictions that assert no constraint on where a caller may be.
#:
#: "Global" is already special-cased by the provision-time checks. "Multi-Region"
#: is added here because an equality predicate would otherwise refuse EVERY
#: caller on a Multi-Region contract — the value means "several jurisdictions",
#: not a jurisdiction named "Multi-Region", so nothing can ever equal it.
UNCONSTRAINED_JURISDICTIONS = frozenset({"Global", "Multi-Region"})


def derive_caller_jurisdictions(contract: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    """Which caller jurisdictions a contract's own sovereignty block permits.

    Returns ``None`` — meaning "no constraint" — unless the contract actually
    says something that constrains a reader. Nothing new is invented here: a
    contract declaring ``jurisdiction: EU`` with ``crossBorderTransfer`` false
    already MEANS "this data does not leave the EU", and serving it to a caller
    elsewhere is the border crossing it forbids. Enforcing that is honouring
    what is written rather than adding a rule.

    The constraint is dropped in three cases, each for a different reason:

    * **No jurisdiction declared.** There is nothing to enforce.
    * **Global / Multi-Region.** The contract is explicitly not pinned to one
      jurisdiction, so no caller can be outside it.
    * **crossBorderTransfer is true.** The contract permits the transfer this
      gate exists to prevent, so the gate has no business refusing it.

    ``crossBorderTransfer`` defaults to ``False`` (DEFAULT_CROSS_BORDER_TRANSFER),
    matching the schema, so a contract that pins a jurisdiction and stays silent
    about transfers is treated as forbidding them — which is what the schema's
    own default asserts.
    """
    sovereignty = contract.get("sovereignty")
    if not isinstance(sovereignty, dict):
        return None

    jurisdiction = sovereignty.get("jurisdiction")
    if not jurisdiction or jurisdiction in UNCONSTRAINED_JURISDICTIONS:
        return None

    cross_border = sovereignty.get("crossBorderTransfer", DEFAULT_CROSS_BORDER_TRANSFER)
    if cross_border:
        return None

    return (str(jurisdiction),)


def get_region_jurisdiction(region: str) -> str:
    """
    Get jurisdiction for a region.

    Args:
        region: Cloud region identifier

    Returns:
        Jurisdiction code (EU, US, etc.) or "Unknown"
    """
    return region_jurisdiction_map().get(region, "Unknown")
