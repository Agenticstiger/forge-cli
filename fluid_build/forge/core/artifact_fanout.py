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

"""Fanout orchestrator for ``fluid generate artifacts`` — pipeline stage 3.

Takes a Phase-2 bundle (.tgz) and emits a directory of catalog-ready
artifacts plus a unified MANIFEST.json hashed over every output file.
Dispatches to existing emitters (``generate standard``, ``policy-compile``,
``generate schedule``) without duplicating their logic.

Three data-product standards, kept unambiguous (see below)::

    OPDS        LF/ODPI Open Data Product SPECIFICATION v4.1   → opds/*.opds.json
    ODPS-Bitol  Bitol Open Data Product STANDARD v1.0.0        → odps-bitol/*.odps.yaml
    ODCS        Bitol Open Data CONTRACT Standard v3.1.0       → odcs/*.odcs.yaml

Output layout (full default emit set)::

    <out>/
    ├── MANIFEST.json                       # SHA-256 per file + merkle root
    ├── odcs/product.odcs.<exposeId>.yaml   # ODCS v3.1.0 (bitol-io) — one per exposed port
    ├── odps-bitol/<product>.odps.yaml      # ODPS-Bitol v1.0.0 (bitol-io)
    ├── opds/<product>.opds.json            # OPDS v4.1 (LF/ODPI) — schema-validated
    ├── schedule/
    │   ├── dags/<product>_dag.py           # Airflow (Path A)
    │   └── flows/<product>_flow.py         # Prefect (Path A)
    └── policy/bindings.json                # compiled IAM/GRANT bindings

Note on terminology: **OPDS** is fluid's name for the Linux Foundation / ODPI
Open Data Product *Specification* v4.1. Upstream abbreviates it *ODPS*, but that
collides with Bitol's Open Data Product *Standard* (emitted here as
``odps-bitol``); fluid uses **OPDS** (subdir ``opds/``, emit key ``opds``,
files ``*.opds.json``) so the two never blur. The bare ``odps`` emit key is a
deprecated alias of ``opds`` — it warns and resolves to ``opds`` — kept only so
pre-rename ``--emit odps`` callers keep working. The OPDS emitter now produces
conformant ``{schema, version, product}`` v4.1 documents that validate against
the vendored ``providers/opds/opds-schema-v4.1.0.json`` (stage-4
``validate artifacts`` runs the check), so ``opds`` is back in the default set.

dbt is NOT emitted here. Per plan decision D4, dbt project files are
execution artifacts, not catalog artifacts — they stay in the product's
own repo. ``--emit dbt`` is an explicit error.

The build *pattern* (``builds[].pattern``, e.g. ``hybrid-reference``)
governs only HOW the transformation logic runs — it does NOT gate any
emit key here. ODCS/ODPS describe the output *schema*, ``policy``
describes *access control*, and ``schedule`` describes *orchestration*;
all three are independent of where the transformation code lives (B6).
``schedule`` is gated on the genuine signal — the presence of
``orchestration.engine`` — and ``policies`` emits an (empty, warned)
bindings file when the contract declares no access policy.

Upstream specs:

- ODCS v3.1.0:               bitol-io/open-data-contract-standard
- ODPS-Bitol v1.0.0:         bitol-io/open-data-product-standard
- OPDS v4.1 (LF/ODPI):       Open-Data-Product-Initiative/v4.1 (schema-validated)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

from fluid_build.forge.core.bundle import _slug, build_manifest, validate_manifest
from fluid_build.util.safe_yaml import load_yaml_safe

LOG = logging.getLogger("fluid.forge.core.artifact_fanout")

# Canonical emit keys. ``dbt`` is deliberately absent — it's an execution
# artifact per plan D4, owned by the product's code repo, not emitted by
# generate-artifacts. Users asking for ``--emit dbt`` get a clear error
# steering them to ``fluid generate speed-transformation``.
# Canonical emit keys. The bare ``odps`` key is intentionally ABSENT — it is a
# deprecated alias of ``opds`` (handled in ``parse_emit_set``), not a distinct
# emitter. Keeping it out of ``EMIT_KEYS`` removes the old ``odps``-vs-``opds``
# duplication and the cross-surface ambiguity with ``odps-bitol``.
EMIT_KEYS: Tuple[str, ...] = (
    "odps-bitol",
    "odcs",
    "opds",
    "schedule",
    "policies",
)

# Default emit set. Every schema-pinned emitter validates against its vendored
# upstream schema; the schema-free emitters (schedule/policies) are structural.
#
# - odcs         → bitol-io/open-data-contract-standard v3.1.0 — conformant ✅
# - odps-bitol   → bitol-io/open-data-product-standard v1.0.0 — conformant ✅
# - opds         → Open-Data-Product-Initiative/v4.1 (LF/ODPI OPDS v4.1). The
#                  emitter produces the conformant ``{schema, version, product}``
#                  v4.1 shape and validates against the vendored
#                  ``providers/opds/opds-schema-v4.1.0.json`` — restored to the
#                  default set once that validation went green.
# - schedule     → DAG/flow files; shape is scheduler-specific not schema-pinned
# - policies     → compiled IAM bindings; shape is internal
#
# All five emit keys are on by default (the historical card's target set:
# odcs + odps-bitol + opds + schedule + policies). Order matches ``EMIT_KEYS``
# because ``parse_emit_set`` canonicalises every resolved set into ``EMIT_KEYS``
# order for a stable MANIFEST hash — so ``parse_emit_set(None) == DEFAULT_EMIT``.
DEFAULT_EMIT: Tuple[str, ...] = (
    "odps-bitol",
    "odcs",
    "opds",
    "schedule",
    "policies",
)

# Emit keys auto-skipped purely on the basis of the build *pattern*.
#
# B6: this set is intentionally EMPTY. A reference-only build pattern
# (hybrid-reference / reference / external-reference) means the
# transformation *logic* is owned externally — it says nothing about the
# product's catalog schema, access policy, or orchestration. ``schedule``
# is gated separately and correctly on ``orchestration.engine``;
# ``policies`` emits a warned, empty bindings file when no access policy
# is declared. Previously this set was ``("schedule", "policies")``,
# which dropped both even when the contract explicitly requested them and
# genuinely declared an orchestration engine / access policy.
#
# Kept as a (now-empty) public name so callers / tests that import it
# still resolve, and so a future genuinely-pattern-dependent emit key has
# an obvious home.
REFERENCE_ONLY_SKIP: Tuple[str, ...] = ()


class FanoutError(Exception):
    """Raised when an emit step fails. ``key`` carries the responsible emit key."""

    def __init__(self, message: str, *, key: Optional[str] = None):
        super().__init__(message)
        self.key = key


# ---------------------------------------------------------------------------
# Bundle input handling
# ---------------------------------------------------------------------------


def _is_tgz_input(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tgz") or name.endswith(".tar.gz")


def _safe_tar_members(tar: tarfile.TarFile, dest: Path):
    """Filter tar members to prevent path-traversal via ``../`` entries
    or absolute paths, even though ``validate_manifest`` has already
    attested the content.

    Bandit B202 flags unconditional ``tar.extractall`` on the grounds
    that tar entries can escape the destination via ``../`` or
    absolute paths. We trust bundle bytes post-``validate_manifest``
    (SHA-256 merkle root match), but defence-in-depth is cheap:
    reject any member whose resolved path is outside ``dest``.

    Yields the subset of ``tar.getmembers()`` safe to extract.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        # Reject absolute paths + parent refs before resolution —
        # ``Path.resolve()`` itself won't catch symlink-based traversal.
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise FanoutError(
                f"bundle tar entry escapes destination: {member.name!r} "
                f"(absolute path or ``..`` parent reference rejected)",
                key=None,
            )
        # Reject symlink / hardlink / device members outright. A link whose
        # ``linkname`` escapes ``dest`` is a traversal primitive the
        # name-based checks above do not catch. PEP 706's ``data`` filter
        # rejects these too; we do it explicitly so the guarantee holds on
        # every supported Python regardless of the tarfile filter default.
        if member.issym() or member.islnk():
            raise FanoutError(
                f"bundle tar entry is a link, which is not permitted: "
                f"{member.name!r} -> {member.linkname!r}",
                key=None,
            )
        if member.ischr() or member.isblk() or member.isfifo():
            raise FanoutError(
                f"bundle tar entry is a special device file, not permitted: {member.name!r}",
                key=None,
            )
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved) + os.sep) and target != dest_resolved:
            raise FanoutError(
                f"bundle tar entry resolves outside destination: {member.name!r} → {target}",
                key=None,
            )
        yield member


def _extract_bundle(tgz_path: Path, dest: Path) -> Path:
    """Extract ``contract.resolved.yaml`` + any ``sources/`` from a Phase-2
    bundle into ``dest``. Re-verifies the MANIFEST first so stage 3 can't
    be fed a tampered bundle, AND filters tar members to reject any
    path that escapes ``dest`` (defence-in-depth — bundle contents are
    already content-attested by ``validate_manifest``, but rejecting
    ``../`` / absolute-path members costs ~5 lines and eliminates
    Bandit B202 as a standing HIGH finding).

    Returns the path to ``contract.resolved.yaml`` within ``dest``.
    """
    validate_manifest(tgz_path)  # tamper gate — raises on mismatch
    with tarfile.open(tgz_path, "r:gz") as tar:
        # ``_safe_tar_members`` filters out any member that would
        # land outside ``dest`` after resolution. Combined with the
        # MANIFEST SHA-256 attestation, this covers both tampered-
        # bytes and legitimate-bytes-with-malicious-paths threats.
        tar.extractall(dest, members=_safe_tar_members(tar, dest))
    contract_path = dest / "contract.resolved.yaml"
    if not contract_path.exists():
        raise FanoutError(
            f"bundle missing contract.resolved.yaml: {tgz_path}",
            key=None,
        )
    return contract_path


# ---------------------------------------------------------------------------
# Reference-only detection (honors Phase 0's _contract_is_reference_only)
# ---------------------------------------------------------------------------


_REFERENCE_PATTERNS: Set[str] = {"hybrid-reference", "reference", "external-reference"}


def _contract_is_reference_only(contract_path: Path) -> bool:
    """Parse the contract; True if any ``builds[].pattern`` is a reference
    variant. Matches the detection in ``cli/generate_ci.py`` so stage 3 and
    the CI generator agree on which products skip schedule/policy emission.
    """
    try:
        with open(contract_path, "r", encoding="utf-8") as fh:
            contract = load_yaml_safe(fh) or {}
    except (OSError, yaml.YAMLError):
        return False
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return False
    for build in builds:
        if isinstance(build, dict) and build.get("pattern") in _REFERENCE_PATTERNS:
            return True
    return False


def _contract_has_orchestration_engine(contract_path: Path) -> bool:
    """True when the contract declares ``orchestration.engine`` — the gate
    for whether ``fluid generate schedule`` can actually emit something.

    Without this check, default ``--emit schedule`` hard-fails on any
    contract that doesn't use a scheduler (which is most local-dev /
    hello-world products). Auto-skip mirrors the reference-only pattern:
    emit only what the contract is actually configured for.
    """
    try:
        with open(contract_path, "r", encoding="utf-8") as fh:
            contract = load_yaml_safe(fh) or {}
    except (OSError, yaml.YAMLError):
        return False
    orchestration = contract.get("orchestration")
    if not isinstance(orchestration, dict):
        return False
    engine = orchestration.get("engine")
    return bool(engine and str(engine).strip())


# ---------------------------------------------------------------------------
# Emit-set parsing
# ---------------------------------------------------------------------------


def parse_emit_set(
    raw: Optional[str],
    *,
    reference_only: bool,
    logger: logging.Logger,
) -> List[str]:
    """Parse ``--emit`` csv and validate keys.

    Returns the resolved emit list in canonical order.

    B6: the build pattern (``reference_only``) no longer strips any emit
    key — ``REFERENCE_ONLY_SKIP`` is empty. ODCS/ODPS (schema),
    ``policy`` (access control) and ``schedule`` (orchestration) are all
    independent of where the transformation logic lives. ``schedule`` is
    gated separately on ``orchestration.engine`` inside ``run_fanout``.
    The ``reference_only`` parameter is retained for API stability and to
    give a future genuinely-pattern-dependent emit key a place to hook.
    """
    if raw is None or raw.strip() == "":
        requested: List[str] = list(DEFAULT_EMIT)
    else:
        requested = [part.strip() for part in raw.split(",") if part.strip()]

    # ``odps`` is a DEPRECATED letter-swap alias of the canonical ``opds`` key —
    # both target the LF/ODPI Open Data Product Specification v4.1. Rewrite it to
    # ``opds`` (with a one-time warning) BEFORE the unknown-key check so pre-rename
    # ``--emit odps`` callers keep working and never emit to a separate subdir.
    # ``odps-bitol`` is a distinct key (Bitol's Open Data Product Standard) and is
    # deliberately left untouched.
    if "odps" in requested:
        logger.warning(
            "deprecated_emit_key",
            extra={
                "alias": "odps",
                "canonical": "opds",
                "note": (
                    "--emit odps is a deprecated alias of --emit opds (LF/ODPI "
                    "Open Data Product Specification v4.1); resolving to opds. "
                    "Bitol's Open Data Product Standard is --emit odps-bitol."
                ),
            },
        )
        requested = ["opds" if k == "odps" else k for k in requested]

    # dbt check — fail loud with actionable fix.
    if "dbt" in requested:
        raise FanoutError(
            "--emit dbt is not a catalog artifact: dbt projects are execution "
            "artifacts and stay in the product's own repo. Use "
            "`fluid generate speed-transformation` to emit a dbt project into "
            "your code repo, then reference it from the contract via "
            "transformation.dbt.project_dir.",
            key="dbt",
        )

    # Unknown-key check.
    unknown = [k for k in requested if k not in EMIT_KEYS]
    if unknown:
        raise FanoutError(
            f"unknown --emit keys: {sorted(unknown)}. Valid: {sorted(EMIT_KEYS)}",
            key=unknown[0],
        )

    # Auto-skip for reference-only.
    if reference_only:
        dropped = [k for k in requested if k in REFERENCE_ONLY_SKIP]
        if dropped:
            logger.info(
                "generate_artifacts_skip_reference_only",
                extra={"dropped": dropped},
            )
            requested = [k for k in requested if k not in REFERENCE_ONLY_SKIP]

    # De-dup while preserving canonical order (EMIT_KEYS).
    requested_set = set(requested)
    return [k for k in EMIT_KEYS if k in requested_set]


# ---------------------------------------------------------------------------
# Per-emit-key helpers — all delegate to existing emitters
# ---------------------------------------------------------------------------


def _slug_from_contract(contract: Dict[str, Any]) -> str:
    """Derive a filesystem-safe product slug from the contract for file names.

    Matches the Phase-2 bundle's default-filename convention so artifact
    files line up with the bundle they came from.
    """
    raw = contract.get("id") or contract.get("name") or "contract"
    return _slug(str(raw))


def _load_contract(contract_path: Path) -> Dict[str, Any]:
    with open(contract_path, "r", encoding="utf-8") as fh:
        doc = load_yaml_safe(fh)
    if not isinstance(doc, dict):
        raise FanoutError(
            f"contract at {contract_path} did not parse as a mapping",
            key=None,
        )
    return doc


def _emit_opds(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """Emit the LF/ODPI OPDS v4.1 document (``opds/<slug>.opds.json``).

    Routes through ``generate_standard._export_opds`` (the LF/ODPI Open Data
    Product Specification exporter). The on-disk file is the bare
    ``{schema, version, product}`` v4.1 document, which stage-4
    ``validate artifacts`` checks against the vendored OPDS schema.
    """
    from fluid_build.cli.generate_standard import _export_opds

    contract = _load_contract(contract_path)
    slug = _slug_from_contract(contract)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{slug}.opds.json"
    _export_opds(str(contract_path), None, str(out), logger)
    return [out]


def _emit_odps_bitol(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """Emit a complete Bitol ODPS v1.0.0 bundle: 1 product + N sibling ODCS.

    Uses ``BitolOdpsProvider.render(out_dir=...)`` directly so the bundle
    is self-contained inside ``odps-bitol/`` — every ``contractId`` in the
    product file resolves to a sibling ``<contractId>.odcs.yaml`` in the
    same directory. This means ``--emit odps-bitol`` alone produces a
    usable Bitol bundle; you no longer need ``--emit odps-bitol,odcs``
    just to avoid dangling ``contractId`` references.

    ``_emit_odcs`` still emits per-port ODCS to ``odcs/`` for the
    catalog-fanout consumer that prefers one-format-per-subdir layout.
    """
    from fluid_build.providers.odps_standard import BitolOdpsProvider

    contract = _load_contract(contract_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = BitolOdpsProvider()
    bundle = provider.render(contract, out_dir=out_dir, fmt="yaml")

    written: List[Path] = []
    product = bundle.get("product") or {}
    product_id = product.get("id") or product.get("name") or "product"
    product_path = out_dir / f"{product_id}.odps.yaml"
    if product_path.exists():
        written.append(product_path)
    for contract_id in bundle.get("contracts") or {}:
        sibling = out_dir / f"{contract_id}.odcs.yaml"
        if sibling.exists():
            written.append(sibling)
    return written


def _emit_odcs(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """ODCS is per-port — one file per ``exposes[]`` entry. Use the provider
    directly rather than the CLI shim so we control output paths exactly."""
    from fluid_build.providers.odcs.odcs import OdcsProvider

    contract = _load_contract(contract_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = OdcsProvider()
    results = provider.render_all_ports(contract, out_dir=out_dir, fmt="yaml")
    written = [out_dir / f"product.odcs.{eid}.yaml" for eid, _odcs in results]
    return written


def _emit_schedule(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """DAG/flow emission via ``generate schedule``. Invokes the CLI helper
    with a namespace that points at our ``<out>/schedule/`` subdir."""
    from fluid_build.cli import generate_schedule

    out_dir.mkdir(parents=True, exist_ok=True)
    # generate_schedule.run() writes to output_dir; we observe what landed
    # there afterwards to compute the MANIFEST.
    args = argparse.Namespace(
        contract=str(contract_path),
        env=None,
        scheduler=None,
        output_dir=str(out_dir),
        list=False,
    )
    rc = generate_schedule.run(args, logger)
    if rc != 0:
        raise FanoutError(
            f"generate schedule failed (exit {rc})",
            key="schedule",
        )
    # List all files under out_dir (recursive) — matches what the schedule
    # generator actually wrote.
    return sorted(p for p in out_dir.rglob("*") if p.is_file())


def _expose_level_policies(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Harvest per-expose governance policy from the contract (B7).

    ``policy-compile`` only compiles the contract-level ``accessPolicy``
    into provider IAM bindings — it never looks at the *expose-level*
    ``exposes[].policy`` block. As a result the emitted ``bindings.json``
    silently dropped authorization (``authz.readers/writers/
    columnRestrictions``), column-level access control, privacy masking
    (``privacy.masking``) and row-level security (``privacy.rowLevelPolicy``).

    Downstream policy enforcement (catalog publish, ``policy-apply``)
    needs these. This collects them, per expose, into a structure that is
    attached alongside the IAM ``bindings`` array.

    Returns one entry per expose that declares a non-empty ``policy``;
    exposes without a policy block are skipped.
    """
    out: List[Dict[str, Any]] = []
    exposes = contract.get("exposes")
    if not isinstance(exposes, list):
        return out

    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        policy = expose.get("policy")
        if not isinstance(policy, dict) or not policy:
            continue

        expose_id = expose.get("exposeId") or expose.get("id")
        entry: Dict[str, Any] = {"exposeId": expose_id}

        # Physical resource the policy applies to — lets policy-apply bind
        # the masking / row-level rules to a concrete table.
        binding = expose.get("binding")
        if isinstance(binding, dict):
            entry["platform"] = binding.get("platform")
            location = binding.get("location")
            if isinstance(location, dict):
                entry["location"] = location

        if policy.get("classification") is not None:
            entry["classification"] = policy["classification"]

        # Authorization: readers / writers / column restrictions.
        authz = policy.get("authz")
        if isinstance(authz, dict) and authz:
            authz_out: Dict[str, Any] = {}
            for key in ("readers", "writers", "columnRestrictions"):
                val = authz.get(key)
                if val:
                    authz_out[key] = val
            if authz_out:
                entry["authz"] = authz_out

        # Privacy: column masking + row-level security predicate.
        privacy = policy.get("privacy")
        if isinstance(privacy, dict) and privacy:
            privacy_out: Dict[str, Any] = {}
            masking = privacy.get("masking")
            if masking:
                privacy_out["masking"] = masking
            row_level = privacy.get("rowLevelPolicy")
            if row_level:
                privacy_out["rowLevelPolicy"] = row_level
            if privacy_out:
                entry["privacy"] = privacy_out

        # Only emit when something governance-relevant was actually found
        # (an entry with just exposeId + location carries no policy).
        if any(k in entry for k in ("classification", "authz", "privacy")):
            out.append(entry)

    return out


def _emit_policies(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    from fluid_build.cli import policy_compile

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "bindings.json"
    args = argparse.Namespace(
        contract=str(contract_path),
        env=None,
        out=str(out),
    )
    rc = policy_compile.run(args, logger)
    if rc != 0:
        raise FanoutError(
            f"policy-compile failed (exit {rc})",
            key="policies",
        )

    # B7: policy-compile emits only contract-level accessPolicy → IAM
    # bindings. Augment the file in place with the expose-level
    # governance policy (authz / columnRestrictions / masking /
    # rowLevelPolicy) so downstream policy enforcement sees the full
    # picture. Read-modify-write keeps the file a single JSON document
    # the MANIFEST hashes over.
    try:
        with open(out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            payload = {"bindings": [], "warnings": []}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("generate_artifacts_policy_reread_failed: %s", exc)
        payload = {"bindings": [], "warnings": []}

    contract = _load_contract(contract_path)
    expose_policies = _expose_level_policies(contract)
    payload["exposePolicies"] = expose_policies
    logger.info(
        "generate_artifacts_policy_expose_level",
        extra={"count": len(expose_policies)},
    )

    # Re-write deterministically (sorted keys, trailing newline) so two
    # runs produce byte-identical output for the stage-4 SHA-256 gate.
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [out]


_DISPATCH = {
    "odps-bitol": ("odps-bitol", _emit_odps_bitol),
    "odcs": ("odcs", _emit_odcs),
    "opds": ("opds", _emit_opds),
    "schedule": ("schedule", _emit_schedule),
    "policies": ("policy", _emit_policies),
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_fanout(
    bundle_or_contract: Path,
    out_dir: Path,
    *,
    emit_raw: Optional[str],
    manifest_path: Optional[Path],
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Top-level orchestrator called from the ``generate-artifacts`` CLI.

    Accepts either a bundle (.tgz) or a raw resolved contract (.yaml/.yml).
    Bundle input is extracted to a tmp dir + MANIFEST re-verified; raw-
    contract input is read directly (useful for iterating without
    re-bundling, but not the CI path).

    Returns a dict matching the on-disk MANIFEST.json written next to the
    artifacts (same schema Phase-2 bundle MANIFEST uses — callers can
    feed this to ``validate_manifest``-equivalent checks in stage 4).
    """
    bundle_or_contract = Path(bundle_or_contract)
    out_dir = Path(out_dir)
    if not bundle_or_contract.exists():
        raise FanoutError(
            f"input not found: {bundle_or_contract}",
            key=None,
        )

    # Clean slate — blow away pre-existing outputs so stale files don't
    # survive into the MANIFEST. The caller owns out_dir; we only remove
    # subdirs we generate into.
    for sub in ("odps", "odps-bitol", "odcs", "opds", "schedule", "policy"):
        target = out_dir / sub
        if target.exists():
            shutil.rmtree(target)

    # Extract bundle if applicable.
    with tempfile.TemporaryDirectory(prefix="fluid-artifacts-") as tmpdir:
        if _is_tgz_input(bundle_or_contract):
            contract_path = _extract_bundle(bundle_or_contract, Path(tmpdir))
        else:
            contract_path = bundle_or_contract

        reference_only = _contract_is_reference_only(contract_path)
        has_scheduler = _contract_has_orchestration_engine(contract_path)
        emits = parse_emit_set(emit_raw, reference_only=reference_only, logger=logger)

        # Auto-skip schedule when the contract doesn't declare orchestration.engine.
        # Hard-failing on "no scheduler configured" for every non-scheduled product
        # would block `fluid generate artifacts` on the hello-world / local-dev
        # majority of products.
        if "schedule" in emits and not has_scheduler:
            logger.info(
                "generate_artifacts_skip_schedule_no_engine",
                extra={
                    "hint": "contract has no orchestration.engine; set one to emit DAG/flow artifacts"
                },
            )
            emits = [k for k in emits if k != "schedule"]

        out_dir.mkdir(parents=True, exist_ok=True)
        written: List[Path] = []
        for key in emits:
            subdir_name, fn = _DISPATCH[key]
            subdir = out_dir / subdir_name
            files = fn(contract_path, subdir, logger)
            written.extend(files)

    # Build MANIFEST across all emitted files (bytes from disk — matches
    # the bundle's hash-what-you-wrote model).
    manifest_files: Dict[str, bytes] = {}
    for fp in written:
        rel = fp.relative_to(out_dir).as_posix()
        manifest_files[rel] = fp.read_bytes()

    contract_id = ""
    if written:
        # Re-read the resolved contract for contract_id in the manifest header.
        try:
            # Prefer the bundle's own contract.resolved.yaml if we still have it,
            # but cheaper to just read one of the emitted artifacts if available.
            # The simpler route: re-extract the bundle if needed.
            pass
        except Exception:
            pass

    manifest = build_manifest(
        manifest_files,
        contract_id=contract_id,
        generator="fluid generate artifacts",
    )

    # Write MANIFEST.json last. Include MANIFEST.json in the on-disk
    # artifact set but NOT in its own ``files`` map (can't hash itself).
    resolved_manifest_path = manifest_path or (out_dir / "MANIFEST.json")
    resolved_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    resolved_manifest_path.write_bytes(manifest_bytes)

    return manifest


__all__ = [
    "DEFAULT_EMIT",
    "EMIT_KEYS",
    "FanoutError",
    "REFERENCE_ONLY_SKIP",
    "parse_emit_set",
    "run_fanout",
]
