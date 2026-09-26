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
    ├── schedule/<product-id>/             # one directory per product, so
    │   └── <build-id>_dag.py               #   schedule-sync never deletes
    │                                       #   another product's DAGs (Path A)
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
``schedule`` is gated on the genuine signal: an ``orchestration.engine``
(other than ``none``), or a build that declares
``execution.trigger.schedule``, which defaults to Airflow and renders a DAG
per build that runs ``fluid apply --mode amend-and-build --build-id <id>``
(:mod:`fluid_build.schedulers.airflow.fluid_apply`). ``policies`` emits an
(empty, warned) bindings file when the contract declares no access policy.

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
# is gated separately (``orchestration.engine`` or a build trigger);
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


def _load_schedule_contract(
    contract_path: Path, overlay_env: Optional[str], logger: logging.Logger
) -> Optional[Dict[str, Any]]:
    """The contract as ``fluid generate schedule`` will see it, or ``None``.

    Loaded through the same loader (overlay, ``$ref`` resolution and alias
    normalisation included) so the gate below and the renderer agree on
    which builds carry a trigger. ``None`` only when the contract file itself
    cannot be read, which skips ``schedule`` exactly as it always has (the
    other emitters then report the unreadable file). A contract that reads
    but will not load, a malformed overlay above all, raises: skipping would
    drop the schedule while stage 3 reported success, and ``fluid apply
    --env`` fails on the same overlay.
    """
    from fluid_build._contract_loader import load_contract_with_overlay

    try:
        with open(contract_path, "r", encoding="utf-8") as fh:
            load_yaml_safe(fh)
    except (OSError, yaml.YAMLError):
        return None
    try:
        contract = load_contract_with_overlay(str(contract_path), overlay_env, logger)
    except Exception as exc:  # noqa: BLE001 - any loader failure fails the schedule emit
        with_env = f" with --env {overlay_env}" if overlay_env else ""
        raise FanoutError(
            f"cannot load the contract for schedule artifacts{with_env}: {exc}",
            key="schedule",
        ) from exc
    return contract if isinstance(contract, dict) else None


def _orchestration_engine(contract: Dict[str, Any]) -> str:
    orchestration = contract.get("orchestration")
    if not isinstance(orchestration, dict):
        return ""
    engine = orchestration.get("engine")
    return str(engine).strip() if engine else ""


_SCHEDULE_SKIP_HINTS = {
    "generate_artifacts_skip_schedule_engine_none": "orchestration.engine is none",
    "generate_artifacts_skip_schedule_no_engine": (
        "contract has no orchestration.engine and no build declares "
        "execution.trigger.schedule; add either to emit DAG/flow artifacts"
    ),
    "generate_artifacts_skip_schedule_unreadable": "the contract file could not be read",
}


def _declares_overlays(contract_path: Path) -> bool:
    """True when overlay files sit where the loader looks for them without
    knowing the env: ``overlays/`` beside the contract, or
    ``<contract stem>.<env>.yaml`` siblings."""
    suffixes = (".yaml", ".yml", ".json")
    overlays = contract_path.parent / "overlays"
    try:
        if overlays.is_dir() and any(p.suffix in suffixes for p in overlays.iterdir()):
            return True
        prefix = contract_path.stem + "."
        return any(
            p.name.startswith(prefix) and p.suffix in suffixes and p.name != contract_path.name
            for p in contract_path.parent.iterdir()
        )
    except OSError:
        return False


def _schedule_skip_reason(contract: Optional[Dict[str, Any]]) -> Optional[str]:
    """``None`` when the contract gets schedule artifacts, else the skip event.

    The rule: an ``orchestration.engine`` emits (``none`` opts out), and so
    does a contract with no engine whose builds declare
    ``execution.trigger.schedule``; Airflow renders those. Anything else has
    nothing to schedule, and hard-failing on it would block stage 3 for the
    hello-world / local-dev majority of products.
    """
    if contract is None:
        return "generate_artifacts_skip_schedule_unreadable"
    engine = _orchestration_engine(contract)
    if engine == "none":
        return "generate_artifacts_skip_schedule_engine_none"
    if engine:
        return None
    from fluid_build.schedulers.airflow.fluid_apply import has_scheduled_builds

    if has_scheduled_builds(contract):
        return None
    return "generate_artifacts_skip_schedule_no_engine"


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
    gated separately inside ``run_fanout`` (``_schedule_skip_reason``).
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

    # Filenames must be derived through the SAME helper the provider writes
    # with, or a sanitised stem (from a hostile or foreign id) makes these
    # paths miss the real files: the .exists() guards would then silently
    # drop them from the manifest and skip the integrity gate.
    from fluid_build.providers._path_safety import safe_filename_stem

    written: List[Path] = []
    product = bundle.get("product") or {}
    product_stem = safe_filename_stem(product.get("id") or product.get("name"), "product")
    product_path = out_dir / f"{product_stem}.odps.yaml"
    if product_path.exists():
        written.append(product_path)
    for contract_id in bundle.get("contracts") or {}:
        sibling = out_dir / f"{safe_filename_stem(contract_id, 'contract')}.odcs.yaml"
        if sibling.exists():
            written.append(sibling)
    return written


def _emit_odcs(contract_path: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """ODCS is per-port — one file per ``exposes[]`` entry. Use the provider
    directly rather than the CLI shim so we control output paths exactly."""
    from fluid_build.providers.odcs.odcs import OdcsProvider

    contract = _load_contract(contract_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same shared-helper derivation as the writer, so a sanitised exposeId
    # still yields the real path rather than a phantom one the manifest
    # would later fail to hash.
    from fluid_build.providers._path_safety import safe_filename_stem

    provider = OdcsProvider()
    results = provider.render_all_ports(contract, out_dir=out_dir, fmt="yaml")
    return [
        out_dir / f"{safe_filename_stem(f'product.odcs.{eid}', 'product.odcs')}.yaml"
        for eid, _odcs in results
    ]


def _emit_schedule(
    contract_path: Path,
    out_dir: Path,
    logger: logging.Logger,
    *,
    contract: Optional[Dict[str, Any]] = None,
    overlay_env: Optional[str] = None,
    env: Optional[str] = None,
    dag_contract_path: Optional[str] = None,
    warn_no_env: bool = False,
) -> List[Path]:
    """DAG/flow emission via ``generate schedule`` into ``<out>/schedule/<product-id>/``.

    ``env`` is the ``--env`` a ``fluid apply`` DAG passes on every run.
    ``overlay_env`` is the overlay applied while rendering: the same env for a
    raw contract, ``None`` for a bundle, which stage 1 already overlaid.
    ``dag_contract_path`` is the contract's path relative to
    ``$FLUID_PROJECT_DIR`` on the worker. ``contract`` is the already-loaded
    contract (:func:`_load_schedule_contract`), when the caller has it.
    ``warn_no_env`` asks for a warning when ``fluid apply`` DAGs are rendered
    with no ``--env`` although the input may have been meant for one.
    """
    from fluid_build.cli import generate_schedule
    from fluid_build.schedulers.airflow import fluid_apply

    if contract is None:
        contract = _load_schedule_contract(contract_path, overlay_env, logger)
    if contract is None:
        raise FanoutError(f"cannot read the contract at {contract_path}", key="schedule")
    raw_id: Any = contract.get("id")
    try:
        product_id = fluid_apply.validate_id(raw_id, kind="contract.id")
    except fluid_apply.ScheduleRenderError as exc:
        raise FanoutError(f"cannot scope schedule artifacts: {exc}", key="schedule") from exc

    apply_dags = fluid_apply.uses_fluid_apply_dags(contract, engine=_orchestration_engine(contract))
    if apply_dags and warn_no_env and env is None:
        logger.warning(
            "generate_artifacts_schedule_env_defaulted",
            extra={
                "hint": (
                    "no --env and no FLUID_ENV: every scheduled run applies the base "
                    "contract with no overlay. Pass --env (or set FLUID_ENV) to the env "
                    "the pipeline applies with, or --env '' to confirm none"
                ),
            },
        )
    if dag_contract_path is None:
        if apply_dags:
            logger.warning(
                "generate_artifacts_schedule_contract_path_defaulted",
                extra={
                    "contract_path": fluid_apply.DEFAULT_CONTRACT_PATH,
                    "hint": (
                        "the input does not say where the contract lives in the project; "
                        "pass --contract-path with its path relative to the project directory"
                    ),
                },
            )
        dag_contract_path = fluid_apply.DEFAULT_CONTRACT_PATH

    # One directory per product: stage 11 (``schedule-sync``, default
    # ``--delete-scope product``) mirrors it into the same-named directory of
    # the scheduler's DAG root, so deleting stale DAGs never reaches another
    # product's files there.
    scope_dir = out_dir / product_id
    scope_dir.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        contract=str(contract_path),
        env=overlay_env,
        dag_env=env,
        dag_contract_path=dag_contract_path,
        scheduler=None,
        output=str(scope_dir),
        overwrite=True,
        list_schedulers=False,
        verbose=False,
    )
    rc = generate_schedule.run(args, logger)
    if rc != 0:
        raise FanoutError(
            f"generate schedule failed (exit {rc})",
            key="schedule",
        )
    files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    if not files:
        # Nothing to schedule after all: leave no empty product directory
        # for stage 11 to mirror (and so empty) at the scheduler.
        shutil.rmtree(out_dir)
    return files


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


def project_relative_path(path: Path) -> Optional[str]:
    """``path`` relative to the current directory, as POSIX; ``None`` outside it.

    The default ``--contract-path`` of the schedule DAGs for a raw contract.
    """
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return None


def run_fanout(
    bundle_or_contract: Path,
    out_dir: Path,
    *,
    emit_raw: Optional[str],
    manifest_path: Optional[Path],
    logger: logging.Logger,
    env: Optional[str] = None,
    contract_path: Optional[str] = None,
    source_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Top-level orchestrator called from the ``generate-artifacts`` CLI.

    Accepts either a bundle (.tgz) or a raw resolved contract (.yaml/.yml).
    Bundle input is extracted to a tmp dir + MANIFEST re-verified; raw-
    contract input is read directly (useful for iterating without
    re-bundling, but not the CI path).

    ``env`` and ``contract_path`` only shape the schedule emitter: the
    ``--env`` and the project-relative contract path each scheduled
    ``fluid apply`` run uses. ``None`` or ``""`` means no ``--env``; for
    ``None`` ("not given" rather than "none")
    ``generate_artifacts_schedule_env_defaulted`` is logged when the input is
    a bundle or the contract has overlays. ``contract_path`` defaults to the
    input's path relative to the current directory for a raw contract, and to
    ``contract.fluid.yaml`` (with a warning) for a bundle, which does not
    record where its contract lives. For a raw contract the schedule is also
    rendered with the ``env`` overlay applied, as ``fluid apply --env`` will
    see it; a bundle was overlaid by stage 1, so build it with the same env.

    ``source_path`` is the raw contract a ``.tgz`` input was made from
    (``generate artifacts --env`` on a raw contract fans out through a
    temporary bundle). It only supplies the default ``contract_path``, the
    way a raw-contract input does. A path derived like that is not held to
    the explicit ``contract_path`` check up front: only a schedule DAG
    carries it, and rendering one checks it.

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

    # Both values end up in every schedule DAG (and ``env`` names an overlay
    # file), so refuse a bad one before anything is removed or written.
    from fluid_build.schedulers.airflow import fluid_apply

    try:
        if env:
            fluid_apply.validate_env_name(env)
        if contract_path is not None:
            contract_path = fluid_apply.validate_contract_path(contract_path)
    except fluid_apply.ScheduleRenderError as exc:
        raise FanoutError(str(exc), key="schedule") from exc

    # Clean slate — blow away pre-existing outputs so stale files don't
    # survive into the MANIFEST. The caller owns out_dir; we only remove
    # subdirs we generate into.
    for sub in ("odps", "odps-bitol", "odcs", "opds", "schedule", "policy"):
        target = out_dir / sub
        if target.exists():
            shutil.rmtree(target)

    dag_contract_path = contract_path
    if dag_contract_path is None:
        # A raw contract locates itself, as does the one a temporary --env
        # bundle came from; a bundle records no location. Outside the
        # project: None, defaulted below with a warning.
        source = source_path
        if source is None and not _is_tgz_input(bundle_or_contract):
            source = bundle_or_contract
        if source is not None:
            dag_contract_path = project_relative_path(source)
    overlay_env = None if _is_tgz_input(bundle_or_contract) else env

    # Extract bundle if applicable. ``resolved_contract`` is the file every
    # emitter reads (the extracted ``contract.resolved.yaml`` for a bundle).
    with tempfile.TemporaryDirectory(prefix="fluid-artifacts-") as tmpdir:
        if _is_tgz_input(bundle_or_contract):
            resolved_contract = _extract_bundle(bundle_or_contract, Path(tmpdir))
        else:
            resolved_contract = bundle_or_contract

        reference_only = _contract_is_reference_only(resolved_contract)
        emits = parse_emit_set(emit_raw, reference_only=reference_only, logger=logger)

        schedule_contract: Optional[Dict[str, Any]] = None
        schedule_skip: Optional[str] = None
        if "schedule" in emits:
            schedule_contract = _load_schedule_contract(resolved_contract, overlay_env, logger)
            schedule_skip = _schedule_skip_reason(schedule_contract)
        if "schedule" in emits and schedule_skip is not None:
            logger.info(schedule_skip, extra={"hint": _SCHEDULE_SKIP_HINTS[schedule_skip]})
            emits = [k for k in emits if k != "schedule"]

        out_dir.mkdir(parents=True, exist_ok=True)
        written: List[Path] = []
        for key in emits:
            subdir_name, fn = _DISPATCH[key]
            subdir = out_dir / subdir_name
            if key == "schedule":
                files = _emit_schedule(
                    resolved_contract,
                    subdir,
                    logger,
                    contract=schedule_contract,
                    overlay_env=overlay_env,
                    env=env,
                    dag_contract_path=dag_contract_path,
                    # A bundle does not say whether stage 1 used an overlay.
                    warn_no_env=_is_tgz_input(bundle_or_contract)
                    or _declares_overlays(bundle_or_contract),
                )
            else:
                files = fn(resolved_contract, subdir, logger)
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
