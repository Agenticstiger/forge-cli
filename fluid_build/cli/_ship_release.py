# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR``: the release a run publishes once every gate
it ran has passed.

The run stages into a sibling ``DIR.partial-<run-id>-*`` directory. When every
selected stage passed, it copies the bundle (with a provenance file that names its
source contract from ``DIR``), the artifacts its MANIFEST lists, the plan and the
reports there, writes ``release.json`` last and renames the directory to ``DIR``. A run that fails, or is interrupted, publishes nothing: its
reports stay in ``runtime/``, and a copy in the staging directory, for a reader
to inspect. ``DIR`` must not exist, so a release is never changed in place.

``release.json`` keeps what identifies the release apart from what describes the
run, as SLSA provenance keeps a build's definition apart from its run details,
and Reproducible Builds keeps the clock out of what should repeat: two clean
runs of the same contract with the same Forge give the same ``identity`` (and
``identity_digest``); timings, the CI run and report times are in
``provenance``.

With ``--artifacts LOCK``, the external files the lock lists (``_release_lock``) are
copied to ``external/<name>/<file>`` and named, by digest and source, in ``identity``.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
from pathlib import Path
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence

from . import _ship_report as report
from . import _ship_stages as st

LOG = logging.getLogger(__name__)

RELEASE_SCHEMA = "fluid-release/1"
RELEASE_INDEX = "release.json"
#: The stages whose output a release holds: they run in the invocation that builds it.
REQUIRED_STAGES = (1, 2, 3, 4)

#: The report each stage writes, copied into the release when the stage passed in
#: this run. A release run removes them all before stage 1 (:func:`forget_stage_reports`),
#: so one that exists afterwards is this run's, never a file the checkout held.
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


#: What each CI system names as the revision, repository and ref it built, in the order
#: ``_attestation._collect_run_metadata`` tries them (the first that is running wins):
#: ``(variables that say it runs, revision, repository, ref)``. Environment variables only.
_SOURCE_VARS = (
    (("GITHUB_RUN_ID",), "GITHUB_SHA", "GITHUB_REPOSITORY", "GITHUB_REF"),
    (("CI_JOB_ID",), "CI_COMMIT_SHA", "CI_PROJECT_URL", "CI_COMMIT_REF_NAME"),
    (
        ("CIRCLE_WORKFLOW_ID", "CIRCLE_BUILD_NUM"),
        "CIRCLE_SHA1",
        "CIRCLE_REPOSITORY_URL",
        "CIRCLE_BRANCH",
    ),
    (("BUILD_BUILDID",), "BUILD_SOURCEVERSION", "BUILD_REPOSITORY_URI", "BUILD_SOURCEBRANCH"),
    (
        ("BITBUCKET_BUILD_NUMBER",),
        "BITBUCKET_COMMIT",
        "BITBUCKET_GIT_HTTP_ORIGIN",
        "BITBUCKET_BRANCH",
    ),
    (("BUILD_ID",), "GIT_COMMIT", "GIT_URL", "GIT_BRANCH"),
)
_REVISION = re.compile(r"[0-9a-f]{7,64}")
_REF = re.compile(r"[A-Za-z0-9._/@+#-]{1,200}")
#: ``user@host:org/repo.git`` (the host may be a bare name such as ``gitserver``). The ``user@``
#: and a path of at least two segments that does not start at a root make it one: ``user:token``
#: (a CI variable set to a credential), a Windows drive (``C:/work``) and ``host:/etc/passwd``
#: are none of that.
_SCP_URL = re.compile(
    r"[A-Za-z0-9._-]+@"
    r"([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*)"
    r":([A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)"
)
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _repository(value: Optional[str]) -> Optional[str]:
    """A repository URL with no user, password, query or fragment (a CI's clone URL can carry a
    token), or ``None`` for anything that is not an ``https``, ``http``, ``ssh`` or ``git`` URL
    or a ``user@host:org/repo`` one, or is not printable ASCII: a path on the runner's disk is no
    part of a release record."""
    from urllib.parse import urlsplit

    text = (value or "").strip()
    if not text or len(text) > 256 or not all(32 < ord(c) < 127 for c in text):
        return None
    if "://" in text:
        try:
            parts = urlsplit(text)
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            return None
        host = parts.hostname
        if parts.scheme not in ("https", "http", "ssh", "git") or not host:
            return None
        if ":" in host:  # an IPv6 literal: urlsplit took its brackets off
            host = f"[{host}]"
        return f"{parts.scheme}://{host}{port}{parts.path}"
    scp = _SCP_URL.fullmatch(text)
    if scp is None or ".." in scp.group(2).split("/"):
        return None
    return f"{scp.group(1)}:{scp.group(2)}"


def _repository_value(env: Mapping[str, str], repository_var: str) -> Optional[str]:
    """What the CI says the repository is: a URL, or for GitHub the ``org/repo`` on its server."""
    repository = env.get(repository_var)
    if repository_var != "GITHUB_REPOSITORY":
        return repository
    slug = (repository or "").strip()
    server = (env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    return f"{server}/{slug}" if _GITHUB_REPOSITORY.fullmatch(slug) else None


def _source_of(
    env: Mapping[str, str], revision_var: str, repository_var: str, ref_var: str
) -> Dict[str, str]:
    """``{"revision", "repository", "ref"}`` of one CI, each only when it is what it should be."""
    found: Dict[str, str] = {}
    revision = (env.get(revision_var) or "").strip().lower()
    if _REVISION.fullmatch(revision):
        found["revision"] = revision
    repository = _repository(_repository_value(env, repository_var))
    if repository:
        found["repository"] = repository
    ref = (env.get(ref_var) or "").strip()
    if _REF.fullmatch(ref):
        found["ref"] = ref
    return found


def source_revision(env: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """The code the run built, as its CI names it: ``{"revision", "repository", "ref"}``, each
    only when the CI sets it and it is what it should be (a hex commit, a URL with no
    credential in it, a ref name). Read from environment variables only, as the CI run is."""
    for markers, *names in _SOURCE_VARS:
        if any(env.get(name) for name in markers):
            return _source_of(env, *names) or None
    return None


def refusal(
    release_dir: Optional[Path],
    selected: Collection[int],
    not_selected: Mapping[int, str],
    opts: st.ShipOptions,
) -> Optional[str]:
    """Why a run cannot build a release in ``release_dir``, or ``None``. A lock of external
    files (``--artifacts``) is read here, before any stage, and refused when the release
    could not seal it."""
    if release_dir is None:
        if opts.artifacts:
            return (
                f"--artifacts {opts.artifacts} names files to seal into a release, and this run "
                "publishes none: add --release-dir DIR"
            )
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
            why = st.scaffold_drop_reason(opts) or "--no-generate-artifacts"
        else:
            continue
        return (
            "--release-dir needs stages 1-4 (bundle to validate-artifacts) in this "
            f"invocation, and stage {num} {st.STAGE_NAMES[num]} does not run ({why})"
        )
    if opts.artifacts:
        from . import _release_lock

        try:
            _release_lock.load(opts.artifacts)
        except ReleaseError as exc:
            return str(exc)
    return None


def forget_stage_reports(root: Path) -> None:
    """Remove the reports an earlier run (or the checkout) left in runtime/, before
    stage 1 of a release run: a stage that passes without writing its report then
    leaves nothing to copy, rather than an older file."""
    for paths in _STAGE_REPORTS.values():
        for rel in paths:
            path = root / rel
            if path.is_symlink() or path.is_file():
                path.unlink()


def open_staging(release_dir: Path, run_id: str) -> Path:
    """A new, empty staging directory beside ``release_dir`` (same filesystem, so
    the final rename is atomic)."""
    parent = release_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    # A run id is a name, never a path: anything but [A-Za-z0-9._-] becomes "_".
    name = "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in run_id)
    # os.mkdir, not tempfile.mkdtemp: the release is for others to read, so it
    # takes the umask's mode, not mkdtemp's 0700.
    for _ in range(100):
        path = parent / f"{release_dir.name}.partial-{name[:64]}-{secrets.token_hex(4)}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"no free staging name beside {release_dir}")


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


def build_identity(
    opts: st.ShipOptions,
    root: Path,
    *,
    with_plan: bool,
    applied: bool = False,
    external: Sequence[Any] = (),
) -> Dict[str, Any]:
    """What identifies the release: the same for two clean runs of the same
    contract, environment, emit set and Forge version. Reads only local files.
    ``applied``: stage 7 ran, so its mode (a dry run, or a real apply) is part of it.
    ``external``: the files the ``--artifacts`` lock lists (``_release_lock.ExternalArtifact``),
    recorded by name, digest and source, never by where they sit on this machine."""
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
    versions = {
        str(e.get("exposeId")): e["version"].strip()
        for e in contract.get("exposes") or []
        if isinstance(e, Mapping)
        and e.get("exposeId")
        and isinstance(e.get("version"), str)
        and e["version"].strip()
    }
    if versions:
        identity["expose_versions"] = versions
    if with_plan:
        plan = _plan_digests(root)
        if plan["bundle_digest"] != manifest["digest"]:
            raise ReleaseError(
                f"{st.PLAN_PATH} was planned against another bundle "
                f"({plan['bundle_digest']}, this run bundled {manifest['digest']})"
            )
        identity["plan"] = plan
    if applied:
        identity["apply_mode"] = opts.mode
    if external:
        identity["external"] = [a.entry.identity() for a in external]
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
        if Path(rel).is_absolute() or ".." in Path(rel).parts or "\\" in rel:
            raise ReleaseError(f"{st.ARTIFACTS_MANIFEST} lists {rel!r}, not a path inside it")
        problem = st.listed_file_problem(root, files, rel)
        if problem:
            raise ReleaseError(f"{st.ARTIFACTS_DIR}{rel} {problem}")
        _copy(root / st.ARTIFACTS_DIR / rel, staging / "artifacts" / rel)
    _copy(manifest_path, staging / "artifacts" / "MANIFEST.json")


def _copy_external(staging: Path, identity: Mapping[str, Any], external: Sequence[Any]) -> None:
    """Each external file to ``external/<name>/<file>``, hashed again once copied: the release
    holds the bytes its identity names, or nothing is published."""
    from fluid_build.cli._attestation import _sha256_file

    if list(identity.get("external") or []) != [a.entry.identity() for a in external]:
        raise ReleaseError("the release's identity names other external files than it copies")
    for artifact in external:
        entry = artifact.entry
        dest = staging / entry.release_path
        _copy(artifact.local, dest)
        if _sha256_file(dest) != entry.sha256:
            raise ReleaseError(
                f"{entry.path} ({entry.name}) changed while the run ran: its bytes are no longer "
                f"the sha256 {entry.sha256} the lock names"
            )


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
    """Every file of the release but its index, by path: an external file may be named
    ``release.json`` too (``external/<name>/release.json``), and is listed."""
    return {
        path.relative_to(staging).as_posix(): _sha256(path)
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path != staging / RELEASE_INDEX
    }


def _write_bundle_provenance(
    root: Path, staging: Path, release_dir: Path, bundle_digest: str
) -> None:
    """The provenance file beside the release's ``bundle.tgz``, written for where the
    release will sit.

    The one ``fluid bundle`` wrote beside ``runtime/bundle.tgz`` names the source
    contract relative to ``runtime/``, so a copy would point elsewhere, and a plan made
    from the release's bundle would record no source contract: ``fluid apply`` could
    then not find a dbt build's project in the checkout to check it against the packed
    copy, and refused. The same contract is recorded relative to ``release_dir``,
    resolved as its readers resolve the bundle's directory. Like the original, the file
    is outside every digest and outside ``identity``, as it depends on where the
    release sits; ``release.json`` lists it with the other files. A bundle that names
    no usable source contract gets none, and so does a release on another drive.
    """
    from fluid_build.forge.core import bundle as bundle_mod

    built = root / st.BUNDLE_PATH
    contract, why = bundle_mod.resolve_bundle_source_contract(built)
    if contract is None:
        _say_no_provenance(f"{st.BUNDLE_PATH} {why}")
        return
    recorded = bundle_mod.read_bundle_provenance(built) or {}
    place = Path(release_dir).parent.resolve() / Path(release_dir).name
    try:
        contract_rel = Path(os.path.relpath(contract, place)).as_posix()
    except ValueError:  # another drive on Windows: no relative path exists
        _say_no_provenance(f"{release_dir} is on another drive than its source contract {contract}")
        return
    bundle_mod._write_provenance(
        staging / "bundle.tgz",
        bundle_digest,
        {"contract": contract_rel, "overlay": recorded.get("overlay")},
    )


def _say_no_provenance(why: str) -> None:
    """Warn where the release is made: without the file, an apply refuses it later."""
    LOG.warning(
        "release_provenance_missing: %s. The release holds no bundle.tgz.provenance.json, so "
        "a plan made from its bundle records no source contract, and fluid apply refuses it "
        "for a contract with a dbt build.",
        why,
    )


def publish(
    staging: Path,
    release_dir: Path,
    *,
    root: Path,
    identity: Mapping[str, Any],
    gates: List[Dict[str, Any]],
    provenance: Mapping[str, Any],
    with_plan: bool,
    external: Sequence[Any] = (),
) -> None:
    """Fill ``staging`` (its reports are already there), write the index last and
    rename it to ``release_dir``. On any error nothing is renamed. ``external``: the
    files ``identity`` names under ``external`` (``_release_lock.ExternalArtifact``)."""
    try:
        _copy(root / st.BUNDLE_PATH, staging / "bundle.tgz")
        _write_bundle_provenance(root, staging, release_dir, identity["bundle"]["manifest_digest"])
        _copy_artifacts(root, staging)
        _copy_external(staging, identity, external)
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


def unpublish(staging: Path) -> None:
    """A staging directory that will not be renamed holds no release index."""
    with contextlib.suppress(OSError):
        (staging / RELEASE_INDEX).unlink()


def gates(records: Sequence[report.StageRecord]) -> List[Dict[str, Any]]:
    """Each stage's outcome, as the index records it: no times, so it repeats."""
    return [
        {"number": r.number, "name": r.name, "status": r.status, "reason": r.reason}
        for r in records
    ]


def provenance(
    records: Sequence[report.StageRecord],
    *,
    run_id: str,
    report_written_at: Optional[str],
    journal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """What describes this run and not the release: times, the CI run, report times."""
    from fluid_build.cli._attestation import _collect_run_metadata

    ran = [r for r in records if r.started_at is not None]
    source = source_revision(os.environ)
    return {
        "run_id": run_id,
        "started_at": ran[0].started_at if ran else None,
        "finished_at": ran[-1].finished_at if ran else None,
        **({"source": source} if source else {}),
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
        # The stage journal of the release and target, as it was when the run
        # ended: where it is and its digest (the file keeps growing).
        **({"journal": journal} if journal is not None else {}),
    }
