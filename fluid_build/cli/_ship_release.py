# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR``: the release a run publishes once every gate
it ran has passed.

The run stages into a sibling ``DIR.partial-<run-id>-*`` directory. When every
selected stage passed, it copies the bundle, the artifacts its MANIFEST lists,
the plan and the reports there, writes ``release.json`` last and renames the
directory to ``DIR``. A run that fails, or is interrupted, publishes nothing: its
reports stay in ``runtime/``, and a copy in the staging directory, for a reader
to inspect. ``DIR`` must not exist, so a release is never changed in place.

``release.json`` keeps what identifies the release apart from what describes the
run, as SLSA provenance keeps a build's definition apart from its run details,
and Reproducible Builds keeps the clock out of what should repeat: two clean
runs of the same contract with the same Forge give the same ``identity`` (and
``identity_digest``); timings, the CI run and report times are in
``provenance``.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence

from . import _ship_report as report
from . import _ship_stages as st

RELEASE_SCHEMA = "fluid-release/1"
RELEASE_INDEX = "release.json"
#: The stages whose output a release holds: they run in the invocation that builds it.
REQUIRED_STAGES = (1, 2, 3, 4)

#: The report each stage writes, copied into the release when the stage passed in
#: this run (an older file from another run is never copied).
_STAGE_REPORTS: Dict[int, Sequence[str]] = {
    2: (st.VALIDATE_REPORT,),
    4: (st.VALIDATE_ARTIFACTS_REPORT,),
    5: ("runtime/diff-report.json",),
    6: (st.PLAN_HTML_PATH,),
    7: ("runtime/apply-report.html",),
    9: ("runtime/verify-report.json",),
    11: (st.SCHEDULE_SYNC_REPORT,),
}


class ReleaseError(Exception):
    """The release cannot be built or published; nothing is published."""


def refusal(
    release_dir: Optional[Path],
    selected: Collection[int],
    not_selected: Mapping[int, str],
    opts: st.ShipOptions,
) -> Optional[str]:
    """Why a run cannot build a release in ``release_dir``, or ``None``."""
    if release_dir is None:
        return None
    if os.path.lexists(release_dir):
        return (
            f"--release-dir {release_dir} already exists: a release goes into a new "
            "directory, never one an earlier run wrote"
        )
    for num in REQUIRED_STAGES:
        if num not in selected:
            why = not_selected.get(num, "not selected")
        elif num == 3 and not opts.generate_artifacts:
            why = "--no-generate-artifacts"
        else:
            continue
        return (
            "--release-dir needs stages 1-4 (bundle to validate-artifacts) in this "
            f"invocation, and stage {num} {st.STAGE_NAMES[num]} does not run ({why})"
        )
    return None


def open_staging(release_dir: Path, run_id: str) -> Path:
    """A new, empty staging directory beside ``release_dir`` (same filesystem, so
    the final rename is atomic)."""
    parent = release_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{release_dir.name}.partial-{run_id}-", dir=parent))


def _sha256(path: Path) -> str:
    from fluid_build.cli._attestation import _sha256_file

    return "sha256:" + _sha256_file(path)


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _regular_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{path} is not a regular file")
    return path


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(_regular_file(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ReleaseError(f"{path} is not a JSON object")
    return data


def _bundle_manifest(root: Path) -> Dict[str, Any]:
    """The bundle's MANIFEST, after the bundle's own tamper check."""
    from fluid_build.forge.core.bundle import read_tar_member_bounded, validate_manifest

    bundle = _regular_file(root / st.BUNDLE_PATH)
    try:
        validate_manifest(bundle)
        with tarfile.open(bundle, mode="r:gz") as tar:
            manifest = json.loads(read_tar_member_bounded(tar, "MANIFEST.json").decode("utf-8"))
    except (OSError, ValueError, KeyError, tarfile.TarError, RecursionError) as exc:
        raise ReleaseError(f"{st.BUNDLE_PATH} does not verify: {exc}") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("digest"), str):
        raise ReleaseError(f"{st.BUNDLE_PATH} has no MANIFEST digest")
    return manifest


def _emit_set(opts: st.ShipOptions) -> List[str]:
    """The emit keys stage 3 resolved, in ``generate artifacts``' canonical order."""
    import logging

    from fluid_build.forge.core.artifact_fanout import FanoutError, parse_emit_set

    with st._quiet_probe():
        try:
            return parse_emit_set(
                opts.emit or None, reference_only=False, logger=logging.getLogger()
            )
        except FanoutError as exc:
            raise ReleaseError(f"--emit {opts.emit}: {exc}") from None


def _plan_digests(root: Path) -> Dict[str, str]:
    plan = _read_json(root / st.PLAN_PATH)
    digests = {"bundle_digest": plan.get("bundleDigest"), "plan_digest": plan.get("planDigest")}
    if not all(isinstance(v, str) and v for v in digests.values()):
        raise ReleaseError(f"{st.PLAN_PATH} carries no bundleDigest / planDigest")
    return {k: str(v) for k, v in digests.items()}


def build_identity(opts: st.ShipOptions, root: Path, *, with_plan: bool) -> Dict[str, Any]:
    """What identifies the release: the same for two clean runs of the same
    contract, environment, emit set and Forge version. Reads only local files."""
    import fluid_build

    manifest = _bundle_manifest(root)
    try:
        contract = st.bundled_contract(root)
    except ValueError as exc:
        raise ReleaseError(str(exc)) from None
    artifacts = _read_json(root / st.ARTIFACTS_MANIFEST)
    if not isinstance(artifacts.get("digest"), str):
        raise ReleaseError(f"{st.ARTIFACTS_MANIFEST} has no digest")
    identity: Dict[str, Any] = {
        "product_id": contract.get("id"),
        "environment": opts.env,
        "fluid_version": contract.get("fluidVersion"),
        "forge_version": fluid_build.__version__,
        "emit": _emit_set(opts),
        "required_artifacts": list(opts.require_artifacts),
        "bundle": {
            "manifest_digest": manifest["digest"],
            "sha256": _sha256(root / st.BUNDLE_PATH),
        },
        "artifacts": {"manifest_digest": artifacts["digest"]},
    }
    if with_plan:
        plan = _plan_digests(root)
        if plan["bundle_digest"] != manifest["digest"]:
            raise ReleaseError(
                f"{st.PLAN_PATH} was planned against another bundle "
                f"({plan['bundle_digest']}, this run bundled {manifest['digest']})"
            )
        identity["plan"] = plan
    return identity


def identity_digest(identity: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(identity)).hexdigest()


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_regular_file(src), dest)


def _copy_artifacts(root: Path, staging: Path) -> None:
    """Exactly the files the artifacts MANIFEST lists, each checked against its
    digest, plus the MANIFEST itself: nothing else under dist/artifacts/."""
    manifest_path = root / st.ARTIFACTS_MANIFEST
    files = _read_json(manifest_path).get("files")
    if not isinstance(files, dict):
        raise ReleaseError(f"{st.ARTIFACTS_MANIFEST} lists no files")
    for rel in sorted(files):
        problem = st.listed_file_problem(root, files, rel)
        if problem:
            raise ReleaseError(f"{st.ARTIFACTS_DIR}{rel} {problem}")
        _copy(root / st.ARTIFACTS_DIR / rel, staging / "artifacts" / rel)
    _copy(manifest_path, staging / "artifacts" / "MANIFEST.json")


def copy_reports(staging: Path, report_dir: Path, root: Path, passed: Collection[int]) -> None:
    """The run's reports, and each passed stage's own report, into ``reports/``."""
    for name in (report.REPORT_JSON, report.REPORT_JUNIT):
        if (report_dir / name).is_file():
            _copy(report_dir / name, staging / "reports" / name)
    for num, paths in _STAGE_REPORTS.items():
        for rel in paths:
            if num in passed and (root / rel).is_file():
                _copy(root / rel, staging / "reports" / Path(rel).name)


def _files(staging: Path) -> Dict[str, str]:
    return {
        path.relative_to(staging).as_posix(): _sha256(path)
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.name != RELEASE_INDEX
    }


def publish(
    staging: Path,
    release_dir: Path,
    *,
    root: Path,
    identity: Mapping[str, Any],
    gates: List[Dict[str, Any]],
    provenance: Mapping[str, Any],
    with_plan: bool,
) -> None:
    """Fill ``staging`` (its reports are already there), write the index last and
    rename it to ``release_dir``. On any error nothing is renamed."""
    try:
        _copy(root / st.BUNDLE_PATH, staging / "bundle.tgz")
        _copy_artifacts(root, staging)
        if with_plan:
            _copy(root / st.PLAN_PATH, staging / "plan.json")
        index = {
            "schema": RELEASE_SCHEMA,
            "identity": identity,
            "identity_digest": identity_digest(identity),
            "gates": gates,
            "files": _files(staging),
            "provenance": provenance,
        }
        (staging / RELEASE_INDEX).write_text(
            json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if os.path.lexists(release_dir):  # appeared while the run ran
            raise ReleaseError(f"{release_dir} appeared while the run ran")
        os.rename(staging, release_dir)
    except OSError as exc:
        raise ReleaseError(f"cannot publish {release_dir}: {exc.strerror or exc}") from None


def gates(records: Sequence[report.StageRecord]) -> List[Dict[str, Any]]:
    """Each stage's outcome, as the index records it: no times, so it repeats."""
    return [
        {"number": r.number, "name": r.name, "status": r.status, "reason": r.reason}
        for r in records
    ]


def provenance(
    records: Sequence[report.StageRecord], *, run_id: str, report_written_at: Optional[str]
) -> Dict[str, Any]:
    """What describes this run and not the release: times, the CI run, report times."""
    from fluid_build.cli._attestation import _collect_run_metadata

    ran = [r for r in records if r.started_at is not None]
    return {
        "run_id": run_id,
        "started_at": ran[0].started_at if ran else None,
        "finished_at": ran[-1].finished_at if ran else None,
        "stages": [
            {
                "number": r.number,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration_seconds": round(r.duration_seconds, 3),
            }
            for r in ran
        ],
        "ci": _collect_run_metadata(),  # environment variables only: no process, no network
        "report_written_at": report_written_at,
    }
