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

"""Cloud detection + the ``--provider``/binding cross-check.

Two commands take a ``--provider`` override — ``fluid generate iac`` and
``fluid apply`` — and both resolve it through the same
``generate_iac._resolve_provider``. Before this module existed, an explicit
override was honoured verbatim: ``--provider gcp`` on an AWS-bound contract
emitted a module against the GCP plugin, which found no GCP bindings and
wrote a resource-free ``main.tf.json`` with exit 0. Worse, an expose whose
binding is shape-compatible across clouds (an object-store location, say)
emitted the *wrong* cloud's resources — a ``google_storage_bucket`` named
after an S3 bucket, carrying ``location: us-east-1``, which is not a valid
GCS location.

``--provider`` was never a retargeting switch. It exists to disambiguate:
``_resolve_provider`` raises ``generate_iac_ambiguous_provider`` when a
contract spans clouds and tells the operator to pass it, and
``generate_iac_no_provider`` when nothing is detectable at all. Retargeting
is done by editing ``binding`` — see ``examples/sovereignty-platform-swap``,
which swaps one product across AWS / GCP / Snowflake with three contract
files and never once passes ``--provider``.

So the rule is: *the requested provider must be among the clouds the
contract declares*. Both documented uses survive it (an ambiguous contract
declares every cloud you could pick; an undetectable one declares none, and
a mismatch needs something to mismatch against), while the contradiction is
rejected before a byte is emitted.

The detection table lives here, not in the caller, deliberately. PR #475's
post-mortem found a user-blocking bug caused by a validator hand-rolling a
second copy of the emitter's storage-scheme table and letting the two
desync; the fix was to give both sides one importable resolver. Same shape
here: ``generate_iac`` re-exports these helpers rather than owning them, so
a new alias cannot land on one side of the gate only.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Tuple

# Cloud aliases → canonical IaC plugin name. The contract surface uses a few
# interchangeable spellings (``binding.provider: aws`` vs ``binding.platform:
# aws``; ``google``/``bigquery`` for GCP; ``duckdb`` for the in-process local
# runner). Normalising here means a contract authored with any documented
# spelling auto-detects, instead of only the single ``exposes[].binding.platform``
# token the pre-2026-06 resolver inspected.
PROVIDER_ALIASES = {
    "aws": "aws",
    "s3": "aws",
    "glue": "aws",
    "athena": "aws",
    "redshift": "aws",
    "gcp": "gcp",
    "google": "gcp",
    "gcs": "gcp",
    "bigquery": "gcp",
    "snowflake": "snowflake",
    # Confluent Cloud Tableflow (managed Kafka->Iceberg) — ``binding.platform:
    # confluent`` auto-detects to the OpenTofu confluent plugin, matching its
    # entry in OPENTOFU_DEFAULT_PROVIDERS.
    "confluent": "confluent",
    # ``local`` (and its DuckDB engine) is a recognised target but has no
    # OpenTofu plugin — it runs in-process. Detected so we can emit an
    # actionable error rather than the misleading "no supported cloud".
    "local": "local",
    "duckdb": "local",
}

# An unambiguous AWS region (``us-east-1``, ``eu-west-2`` …) is a last-resort
# detection hint — GCP regions (``us-central1``) and the dash-suffixed AWS form
# are distinguishable by the trailing ``-<n>``.
AWS_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")


class ProviderBindingMismatch(Exception):
    """``--provider X`` contradicts every cloud the contract declares.

    Carries the structured facts so each caller can render them in its own
    idiom (``CLIError`` payload for the CLI, log fields for the audit
    trail) without re-deriving them from the message string.
    """

    def __init__(self, requested: str, detected: List[str], source: Optional[str] = None):
        self.requested = requested
        self.detected = list(detected)
        self.source = source
        where = f" ({source})" if source else ""
        super().__init__(
            f"--provider {requested} contradicts the contract's binding.\n"
            f"  requested:         {requested}\n"
            f"  contract declares: {', '.join(self.detected)}{where}\n\n"
            "The emitted module would not match the contract — it would be "
            "empty, or describe the wrong cloud. Either change the contract's "
            "`binding` to target "
            f"{requested}, or drop --provider and let it auto-detect."
        )


def canonical_cloud(token: object) -> str:
    """Map a raw contract platform/provider token to a canonical cloud name.

    Returns the canonical name (``aws``/``gcp``/``snowflake``/``confluent``/
    ``local``) or ``""`` when the token is empty or unrecognised.
    """
    if not isinstance(token, str):
        return ""
    return PROVIDER_ALIASES.get(token.strip().lower().replace("-", "_"), "")


def candidate_regions(contract: Mapping[str, Any]) -> List[str]:
    """Region strings declared on the top-level / expose bindings."""
    regions: List[str] = []

    def _collect(binding: object) -> None:
        if not isinstance(binding, dict):
            return
        for value in (binding.get("region"), (binding.get("location") or {}).get("region")):
            if isinstance(value, str) and value:
                regions.append(value)

    _collect(contract.get("binding"))
    for exposure in contract.get("exposes") or []:
        _collect((exposure or {}).get("binding") or {})
    return regions


def detect_cloud_declarations(contract: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """Every canonical cloud the contract declares, with where it said so.

    Inspects, in the order the spec documents them:

    * ``exposes[].binding.provider`` / ``exposes[].binding.platform``
    * top-level ``binding.provider`` / ``binding.platform`` (Snowflake- and
      single-binding contracts that declare the cloud once at the root)
    * ``builds[].provider`` and ``builds[].execution.runtime.platform``

    Falls back to an unambiguous AWS region (``binding.region`` /
    ``binding.location.region``) only when no platform/provider token was
    found, so a region never overrides an explicit binding.

    Returns ``(cloud, source_path)`` pairs, order-preserving and de-duplicated
    on the cloud — the first declaration of a cloud is the one reported, since
    that is the one an operator reading the error will find first. The
    source path is dotted contract notation (``exposes[0].binding.platform``)
    so the mismatch error can point at a line rather than at the file.
    """
    found: List[Tuple[str, str]] = []
    seen = set()

    def _add(token: object, source: str) -> None:
        cloud = canonical_cloud(token)
        if cloud and cloud not in seen:
            seen.add(cloud)
            found.append((cloud, source))

    # exposes[] data-plane bindings (most specific).
    for i, exposure in enumerate(contract.get("exposes") or []):
        binding = (exposure or {}).get("binding") or {}
        if isinstance(binding, dict):
            _add(binding.get("platform"), f"exposes[{i}].binding.platform")
            _add(binding.get("provider"), f"exposes[{i}].binding.provider")

    # Top-level binding — Snowflake-style + contracts that declare the cloud
    # once at the root via either ``platform`` or ``provider``.
    top_binding = contract.get("binding")
    if isinstance(top_binding, dict):
        _add(top_binding.get("platform"), "binding.platform")
        _add(top_binding.get("provider"), "binding.provider")

    # builds[].provider and builds[].execution.runtime.platform.
    for i, build in enumerate(contract.get("builds") or []):
        if not isinstance(build, dict):
            continue
        _add(build.get("provider"), f"builds[{i}].provider")
        runtime = (build.get("execution") or {}).get("runtime") or {}
        if isinstance(runtime, dict):
            _add(runtime.get("platform"), f"builds[{i}].execution.runtime.platform")

    # Region fallback — only consulted when nothing stronger was declared.
    if not found:
        for region in candidate_regions(contract):
            if AWS_REGION_RE.match(region.strip().lower()):
                found.append(("aws", f"region {region}"))
                break

    return found


def detect_clouds(contract: Mapping[str, Any]) -> List[str]:
    """Canonical cloud names declared by the contract, order-preserving."""
    return [cloud for cloud, _source in detect_cloud_declarations(contract)]


def check_provider_matches_contract(contract: Mapping[str, Any], requested: str) -> None:
    """Raise ``ProviderBindingMismatch`` if ``requested`` contradicts the contract.

    A no-op when ``requested`` is falsy or ``"auto"`` (nothing was
    overridden), and when the contract declares no cloud at all — the latter
    is the documented ``generate_iac_no_provider`` escape hatch, where
    ``--provider`` is the *only* way to name a target and there is nothing
    for it to contradict.
    """
    if not requested or requested == "auto":
        return
    declarations = detect_cloud_declarations(contract)
    if not declarations:
        return
    if any(cloud == requested for cloud, _ in declarations):
        return
    raise ProviderBindingMismatch(
        requested=requested,
        detected=[cloud for cloud, _ in declarations],
        source=declarations[0][1],
    )
