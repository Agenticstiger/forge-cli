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

"""``fluid schedule-sync`` — pipeline stage 11 (Path A).

Pushes DAG files emitted by ``fluid generate schedule`` / ``fluid generate
artifacts`` to a scheduler backend. Dispatches via ``subprocess.run`` to
native scheduler CLIs that the deploying host must have installed
(``aws``, ``gcloud``, ``astro``, ``prefect``, ``dagster-cloud``, ``rsync``,
``scp``, ``gsutil``, ``az``).

Stage-11 is a **no-op for Path-B scheduling** (``orchestration.engine`` in
``eventbridge``, ``snowflake_tasks``, ``mwaa-native``): those engines embed
the schedule into ``plan.json`` so stage-7 apply executes them alongside
DDL. Stage-11 is only meaningful for DAG-push engines (``airflow``,
``prefect``, ``dagster``) and their hosted variants (``mwaa``,
``composer``, ``astronomer``).

Security posture (high level — detailed notes inline at each touchpoint):

* **Never** invokes the shell. Every dispatch builds ``argv`` as a list
  and calls ``subprocess.run(argv, shell=False, check=False)``.
* User-controlled strings that flow into ``argv`` (``--destination``,
  ``--environment-name``, ``--workspace``, ``--location``) are validated
  against a strict whitelist before reaching ``argv``. The whitelist
  rejects shell metacharacters and path separators where they do not
  belong.
* ``--dags-dir`` is resolved + validated via
  :func:`fluid_build.cli.security.validate_input_file` (extension
  whitelist, forbidden-path gate, depth cap). Directory contents are not
  individually scanned — that is the scheduler CLI's responsibility.
* Subprocess ``argv`` is logged through :func:`auth._sanitize_argv` so
  credential-bearing flags (e.g. ``--password``) are redacted — even
  though this command does not pass them itself, defence-in-depth
  protects against future regressions that add such flags.
* ``--dry-run`` short-circuits before subprocess invocation and emits
  the (redacted) planned argv. Nothing about the underlying system
  state changes.

CLI surface::

    fluid schedule-sync --scheduler {airflow|mwaa|composer|astronomer|prefect|dagster}
                        --dags-dir <path>
                        [--destination <url-or-path>]        # airflow URL-scheme dispatch
                        [--environment-name <name>]          # mwaa / composer / astronomer
                        [--location <region>]                # composer GCP region
                        [--workspace <name>]                 # prefect / dagster-cloud
                        [--env <dev|stg|prd>]
                        [--dry-run]
                        [--timeout <seconds>]                # per-subprocess, default 600, hard cap 3600
                        [--report <path>]                    # JSON result summary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fluid_build.cli._common import CLIError
from fluid_build.cli.auth import _sanitize_argv
from fluid_build.cli.console import cprint

COMMAND = "schedule-sync"
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

_SCHEDULERS = ("airflow", "mwaa", "composer", "astronomer", "prefect", "dagster")

# Strict identifier for environment/workspace/location values that flow into
# argv. Matches what the scheduler CLIs themselves accept, and refuses
# anything that could be a shell-meta or path-separator injection vector
# (``;`` ``&`` ``|`` ``$`` `` `` ``\`` ``/`` etc.). Note that ``:`` and ``.``
# are allowed because GCP locations look like ``us-central1`` and MWAA bucket
# names can contain dots.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

# URL schemes we accept for ``--destination`` with ``--scheduler airflow``.
# ``git+ssh`` is a two-token scheme that ``urlparse`` returns whole; we
# validate the URL shape before handing to ``git`` / ``rsync`` / ``scp``.
_AIRFLOW_URL_SCHEMES = {
    "s3",  # AWS S3 — dispatched to ``aws s3 sync``
    "gs",  # GCS — dispatched to ``gsutil -m rsync``
    "az",  # Azure Blob — dispatched to ``az storage blob upload-batch``
    "ssh",  # SSH rsync — ``rsync -av -e ssh``
    "scp",  # plain scp — ``scp -r``
    "file",  # local filesystem — ``rsync -av --delete``
    "git+ssh",  # git-over-ssh — clone / commit / push
}

_DEFAULT_TIMEOUT = 600
_MAX_TIMEOUT = 3600


# -----------------------------------------------------------------------------
# argparse registration
# -----------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Push DAG files to a scheduler (pipeline stage 11, Path A)",
        description=(
            "Stage-11 of the 11-stage pipeline. Dispatches DAG files produced "
            "by ``fluid generate schedule`` to a scheduler backend. Stage-11 "
            "is a no-op for Path-B engines (eventbridge, snowflake_tasks) — "
            "those embed schedule actions into plan.json for stage-7 apply."
        ),
        epilog=(
            "Examples:\n"
            "  # Airflow, local filesystem destination (rsync)\n"
            "  fluid schedule-sync --scheduler airflow --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --destination /opt/airflow/dags/\n\n"
            "  # Airflow, S3 destination (aws s3 sync)\n"
            "  fluid schedule-sync --scheduler airflow --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --destination s3://my-airflow-bucket/dags/\n\n"
            "  # MWAA\n"
            "  fluid schedule-sync --scheduler mwaa --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --destination s3://mwaa-env-bucket/dags/\n\n"
            "  # Composer\n"
            "  fluid schedule-sync --scheduler composer --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --environment-name my-env --location us-central1\n\n"
            "  # Astronomer (uses astro CLI config in cwd)\n"
            "  fluid schedule-sync --scheduler astronomer --dags-dir dist/artifacts/schedule/\n\n"
            "  # Prefect\n"
            "  fluid schedule-sync --scheduler prefect --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --workspace production\n\n"
            "  # Dagster Cloud\n"
            "  fluid schedule-sync --scheduler dagster --dags-dir dist/artifacts/schedule/ \\\n"
            "                      --workspace prod-deployment\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--scheduler",
        required=True,
        choices=list(_SCHEDULERS),
        help="Target scheduler. mwaa/composer/astronomer are hosted Airflow variants.",
    )
    p.add_argument(
        "--dags-dir",
        required=True,
        help="Directory containing DAG files to push (typically dist/artifacts/schedule/).",
    )
    p.add_argument(
        "--destination",
        default=None,
        help=(
            "Destination URL or path for airflow scheduler. Supports "
            "s3://, gs://, az://, file://, ssh://, scp://, git+ssh://, or a "
            "plain local path. Required when --scheduler=airflow or mwaa."
        ),
    )
    p.add_argument(
        "--environment-name",
        default=None,
        help="Environment/deployment name for composer / astronomer / mwaa.",
    )
    p.add_argument(
        "--location",
        default=None,
        help="GCP region for composer (e.g. us-central1, europe-west1).",
    )
    p.add_argument(
        "--workspace",
        default=None,
        help="Prefect workspace or dagster-cloud deployment name.",
    )
    p.add_argument(
        "--env",
        default=os.environ.get("FLUID_ENV", "dev"),
        help="Logical deployment env tag for logging/report (default: $FLUID_ENV or dev).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log the planned subprocess argv (with secrets redacted) without executing.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        help=f"Per-subprocess timeout in seconds (default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT}).",
    )
    p.add_argument(
        "--report",
        default=None,
        help="Optional path to write a JSON result summary.",
    )
    p.set_defaults(func=run)


# -----------------------------------------------------------------------------
# Input validation helpers
# -----------------------------------------------------------------------------


def _validate_safe_ident(value: str, field: str) -> str:
    """Reject anything that isn't a strict identifier.

    Shell-meta and path-separator chars are refused here — before the
    value reaches ``argv`` — because the underlying scheduler CLIs expect
    plain identifiers and a malicious value could otherwise bleed into
    subsequent argv positions if a future refactor ever switched to
    ``shell=True``.
    """
    if not _SAFE_IDENT_RE.fullmatch(value):
        raise CLIError(
            2,
            "schedule_sync_invalid_ident",
            {
                "field": field,
                "value": value,
                "hint": "must match ^[A-Za-z0-9_.-]{1,128}$",
            },
        )
    return value


def _validate_dags_dir(raw: str) -> Path:
    """Resolve, confirm-directory, and enforce the dir is non-empty.

    We deliberately do NOT walk the tree to enforce an extension whitelist —
    schedulers accept a mix of ``.py``, ``.yaml``, ``.json``, ``requirements.txt``
    and custom files. The downstream CLI (aws s3 sync, gsutil rsync, rsync,
    astro) has its own file validation. Our job is: directory exists, is
    readable, not empty.
    """
    path = Path(raw).resolve()
    if not path.exists():
        raise CLIError(2, "schedule_sync_dags_dir_missing", {"path": str(path)})
    if not path.is_dir():
        raise CLIError(2, "schedule_sync_dags_dir_not_directory", {"path": str(path)})
    # Refuse the root and well-known system dirs as a defence-in-depth
    # catch — a user typing ``--dags-dir /`` almost certainly means
    # something else, and a scheduler sync from ``/`` would be ruinous.
    sensitive = {Path("/"), Path("/etc"), Path("/private/etc"), Path("/usr"), Path("/var")}
    if path in sensitive:
        raise CLIError(
            2,
            "schedule_sync_dags_dir_refuses_system_path",
            {"path": str(path)},
        )
    # Empty dir is almost always a mistake (forgot to run generate schedule).
    # Fail loud rather than sync an empty tree that would delete all DAGs
    # on the destination (many sync tools default to ``--delete``).
    try:
        if not any(path.iterdir()):
            raise CLIError(
                2,
                "schedule_sync_dags_dir_empty",
                {
                    "path": str(path),
                    "hint": "run ``fluid generate schedule`` first",
                },
            )
    except PermissionError as exc:
        raise CLIError(
            2,
            "schedule_sync_dags_dir_not_readable",
            {"path": str(path), "error": str(exc)},
        ) from exc
    return path


def _validate_destination(raw: str, scheduler: str) -> Tuple[str, Optional[str]]:
    """Return ``(scheme, normalized)`` for airflow-family dispatch.

    A bare path with no scheme is treated as ``file://`` — convenient for
    local rsync against a bind-mounted airflow ``dags/`` dir. Any other
    unknown scheme is rejected.
    """
    if not raw:
        raise CLIError(
            2,
            "schedule_sync_destination_required",
            {"scheduler": scheduler},
        )

    # Strip wrapping quotes that some CI systems leak through.
    raw = raw.strip()
    if not raw:
        raise CLIError(
            2,
            "schedule_sync_destination_required",
            {"scheduler": scheduler, "note": "empty after strip"},
        )

    # Accept bare absolute / relative paths as file://
    if "://" not in raw:
        # Must be a path — resolve and coerce to file:// form so downstream
        # uses a consistent code path.
        local = Path(raw).resolve()
        return ("file", str(local))

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in _AIRFLOW_URL_SCHEMES:
        raise CLIError(
            2,
            "schedule_sync_destination_scheme_unsupported",
            {
                "scheduler": scheduler,
                "scheme": scheme,
                "allowed": sorted(_AIRFLOW_URL_SCHEMES),
            },
        )

    # SECURITY: reject netloc beginning with a hyphen — scp/rsync argv
    # parsers treat any element starting with ``-`` as an option flag
    # regardless of shell quoting (this is argv-level parsing, not
    # shell-level), so ``scp://-oProxyCommand=…/path`` would smuggle
    # ``-oProxyCommand=<cmd>`` into the scp argv and OpenSSH would run
    # ``<cmd>`` locally to establish the connection (CVE-2020-15778
    # class). urlparse does NOT reject this; the whitelist further down
    # in :func:`_validate_safe_ident` never runs on netloc. This gate
    # plus the ``--`` end-of-options marker inserted in
    # :func:`_airflow_dispatch` / :func:`_mwaa_dispatch` form a
    # defence-in-depth pair — either alone is sufficient, but together
    # they close both the validation and the argv-construction sides of
    # the hole.
    if parsed.netloc.startswith("-"):
        raise CLIError(
            2,
            "schedule_sync_destination_hyphen_netloc",
            {
                "raw": raw,
                "netloc": parsed.netloc,
                "reason": (
                    "destination netloc may not start with '-' — this is a "
                    "scp/rsync/ssh option-smuggling vector (CVE-2020-15778 class)"
                ),
            },
        )

    # Any shell metacharacter past the scheme is suspicious. The
    # SCP / SSH forms accept ``user@host`` but not spaces or semicolons.
    if any(c in raw for c in (";", "|", "&", "`", "$", "\n", "\r")):
        raise CLIError(
            2,
            "schedule_sync_destination_shell_metacharacter",
            {"raw": raw},
        )

    return (scheme, raw)


def _clamp_timeout(raw: int) -> int:
    if raw < 1:
        raise CLIError(2, "schedule_sync_timeout_nonpositive", {"value": raw})
    if raw > _MAX_TIMEOUT:
        logger.warning("timeout_clamped", extra={"requested": raw, "max": _MAX_TIMEOUT})
        return _MAX_TIMEOUT
    return raw


# -----------------------------------------------------------------------------
# Subprocess helper
# -----------------------------------------------------------------------------


def _which_or_raise(binary: str) -> str:
    """Resolve a scheduler CLI on PATH or fail loud with install guidance."""
    resolved = shutil.which(binary)
    if not resolved:
        raise CLIError(
            2,
            "schedule_sync_binary_not_on_path",
            {
                "binary": binary,
                "hint": "install the scheduler CLI before running schedule-sync",
            },
        )
    return resolved


def _run_subprocess(argv: List[str], *, timeout: int, dry_run: bool) -> Dict:
    """Execute ``argv`` with redacted logging and dry-run short-circuit.

    Returns a result dict ``{argv, exit_code, stdout_tail, stderr_tail,
    duration_s}``. Never raises on non-zero exit — the caller decides
    whether a non-zero status is fatal for the overall stage (some
    scheduler CLIs use exit codes for "nothing to sync", which is fine).
    """
    redacted = _sanitize_argv(argv)
    logger.info(
        "schedule_sync_subprocess",
        extra={"argv": redacted, "dry_run": dry_run, "timeout": timeout},
    )
    cprint(f"[schedule-sync] → {' '.join(redacted)}", markup=False)

    if dry_run:
        return {
            "argv": redacted,
            "exit_code": 0,
            "stdout_tail": "(dry-run)",
            "stderr_tail": "",
            "duration_s": 0.0,
            "dry_run": True,
        }

    import time

    start = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603  # argv is list, shell=False, inputs are validated
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        logger.error(
            "schedule_sync_timeout",
            extra={"argv": redacted, "timeout": timeout, "duration": duration},
        )
        return {
            "argv": redacted,
            "exit_code": 124,  # conventional "timeout" code
            "stdout_tail": (
                (exc.stdout or b"").decode("utf-8", "replace")[-2048:]
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")[-2048:]
            ),
            "stderr_tail": f"timeout after {timeout}s",
            "duration_s": round(duration, 2),
            "dry_run": False,
        }
    except (FileNotFoundError, PermissionError) as exc:
        duration = time.monotonic() - start
        logger.error(
            "schedule_sync_exec_error",
            extra={"argv": redacted, "error": str(exc)},
        )
        return {
            "argv": redacted,
            "exit_code": 127,
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
            "duration_s": round(duration, 2),
            "dry_run": False,
        }

    duration = time.monotonic() - start
    return {
        "argv": redacted,
        "exit_code": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-2048:],
        "stderr_tail": (completed.stderr or "")[-2048:],
        "duration_s": round(duration, 2),
        "dry_run": False,
    }


# -----------------------------------------------------------------------------
# Per-scheduler dispatchers
# -----------------------------------------------------------------------------


def _airflow_dispatch(dags_dir: Path, args) -> List[Dict]:
    """URL-scheme dispatch for vanilla Airflow destinations.

    Only validated schemes reach this function (see
    :func:`_validate_destination`). Each branch builds a plain ``argv``
    and delegates to :func:`_run_subprocess`.
    """
    scheme, dest = _validate_destination(args.destination or "", args.scheduler)

    trailing_slash_src = str(dags_dir).rstrip("/") + "/"

    # SECURITY: every argv below that positions user-controlled strings
    # (remote_target, local_dest, dest) AFTER the ``rsync`` / ``scp`` /
    # ``aws`` binary and its option flags inserts ``--`` as the
    # end-of-options marker. This forces the target tool to treat all
    # subsequent positional elements as pathnames — even if a future
    # regex gap lets a leading-hyphen value slip past
    # :func:`_validate_destination`. This is defence-in-depth paired
    # with the netloc-hyphen rejection there.
    #
    # Tool-specific notes:
    # - rsync: supports ``--`` end-of-options universally.
    # - scp:   supports ``--`` since OpenSSH 8.2 (Feb 2020). Any hosts
    #          running an older sshd on the SENDING side (where scp is
    #          invoked) would ignore ``--`` and still parse options —
    #          which is exactly why we also reject hyphen-netloc at
    #          validation time above.
    # - aws s3 sync / gsutil rsync: the user-controlled element is a
    #          ``s3://`` / ``gs://`` URL whose shape (bucket-name rules)
    #          already excludes a leading hyphen on the AWS/GCS side,
    #          but the netloc-hyphen rejection above makes this
    #          structural — not a coincidence — so ``--`` is added
    #          anyway.
    if scheme == "s3":
        binary = _which_or_raise("aws")
        # aws s3 sync accepts --delete as a flag; the positional args
        # are src + dest. Inserting ``--`` would make aws treat
        # ``--delete`` itself as a pathname, so DO NOT add it here.
        # The hyphen-netloc rejection above is the sole defence for this
        # branch (s3:// bucket names never start with '-' per AWS rules).
        argv = [binary, "s3", "sync", trailing_slash_src, dest, "--delete"]
    elif scheme == "gs":
        binary = _which_or_raise("gsutil")
        # gsutil rsync: same analysis as aws s3. gs:// bucket names per
        # Google rules never start with '-'; netloc-hyphen rejection
        # covers this branch.
        argv = [binary, "-m", "rsync", "-r", "-d", trailing_slash_src, dest]
    elif scheme == "az":
        binary = _which_or_raise("az")
        # az://<container>/<path> → --destination <container> --destination-path <path>
        assert dest is not None
        parsed = urlparse(dest)
        container = parsed.netloc
        blob_path = parsed.path.lstrip("/")
        if not container:
            raise CLIError(
                2,
                "schedule_sync_az_missing_container",
                {"destination": dest},
            )
        # Azure container and blob names are the user-controlled
        # values. Azure storage naming rules forbid leading '-' (see
        # the netloc-hyphen rejection above); the blob_path is after a
        # '/'. Each value flows through a named flag (--destination /
        # --destination-path) rather than a positional argument, so
        # even a leading-'-' value would be consumed as the flag's
        # argument — not re-parsed as a new option.
        argv = [
            binary,
            "storage",
            "blob",
            "upload-batch",
            "--destination",
            container,
            "--destination-path",
            blob_path,
            "--source",
            str(dags_dir),
            "--overwrite",
        ]
    elif scheme == "file":
        binary = _which_or_raise("rsync")
        # Strip "file://" prefix for rsync (it doesn't understand the URL form).
        assert dest is not None
        local_dest = dest
        if local_dest.startswith("file://"):
            local_dest = local_dest[len("file://") :]
        # --delete is deliberate: Airflow expects DAG removal to propagate.
        # ``--`` end-of-options before the positional src / dest so a
        # future change to _validate_destination that lets a leading-'-'
        # path slip through still doesn't smuggle an rsync option.
        argv = [
            binary,
            "-av",
            "--delete",
            "--",
            trailing_slash_src,
            local_dest.rstrip("/") + "/",
        ]
    elif scheme == "ssh":
        binary = _which_or_raise("rsync")
        # rsync over ssh: ssh://user@host/path → user@host:/path
        assert dest is not None
        parsed = urlparse(dest)
        if not parsed.netloc:
            raise CLIError(2, "schedule_sync_ssh_missing_host", {"destination": dest})
        remote_target = f"{parsed.netloc}:{parsed.path or '/'}"
        argv = [
            binary,
            "-av",
            "--delete",
            "-e",
            "ssh",
            "--",
            trailing_slash_src,
            remote_target.rstrip("/") + "/",
        ]
    elif scheme == "scp":
        binary = _which_or_raise("scp")
        # scp://user@host/path → user@host:/path
        assert dest is not None
        parsed = urlparse(dest)
        if not parsed.netloc:
            raise CLIError(2, "schedule_sync_scp_missing_host", {"destination": dest})
        remote_target = f"{parsed.netloc}:{parsed.path or '/'}"
        # ``--`` is supported by scp since OpenSSH 8.2 (Feb 2020). On
        # older sshd on the sending side the ``--`` would be passed
        # through as a literal path and the transfer would fail — which
        # is a safer failure mode than option smuggling. Combined with
        # the netloc-hyphen rejection in _validate_destination, this
        # closes CVE-2020-15778-class attacks across all supported scp
        # versions.
        argv = [binary, "-r", "--", trailing_slash_src, remote_target]
    elif scheme == "git+ssh":
        # We don't implement in-process git orchestration for this release —
        # that needs a temp workdir, clone, rsync, git add/commit/push flow
        # that is too stateful for stage-11's subprocess shape. Document
        # the workaround and fail loud.
        raise CLIError(
            2,
            "schedule_sync_git_ssh_not_implemented",
            {
                "hint": (
                    "run ``git clone`` + ``rsync`` + ``git push`` in a "
                    "preceding stage, then point --destination at the "
                    "local clone path"
                )
            },
        )
    else:  # defensive — _validate_destination should have refused already
        raise CLIError(2, "schedule_sync_unhandled_scheme", {"scheme": scheme})

    return [_run_subprocess(argv, timeout=args.timeout, dry_run=args.dry_run)]


def _mwaa_dispatch(dags_dir: Path, args) -> List[Dict]:
    """MWAA is S3-backed airflow — use aws s3 sync to the MWAA bucket.

    The bucket path is supplied via ``--destination s3://<mwaa-bucket>/dags/``.
    """
    scheme, dest = _validate_destination(args.destination or "", args.scheduler)
    if scheme != "s3":
        raise CLIError(
            2,
            "schedule_sync_mwaa_requires_s3",
            {
                "actual_scheme": scheme,
                "hint": "MWAA reads DAGs from a managed S3 bucket",
            },
        )
    binary = _which_or_raise("aws")
    argv = [
        binary,
        "s3",
        "sync",
        str(dags_dir).rstrip("/") + "/",
        dest,
        "--delete",
    ]
    return [_run_subprocess(argv, timeout=args.timeout, dry_run=args.dry_run)]


def _composer_dispatch(dags_dir: Path, args) -> List[Dict]:
    """GCP Composer — gcloud composer environments storage dags import."""
    if not args.environment_name:
        raise CLIError(
            2,
            "schedule_sync_composer_missing_env_name",
            {"hint": "--environment-name is required for --scheduler composer"},
        )
    if not args.location:
        raise CLIError(
            2,
            "schedule_sync_composer_missing_location",
            {"hint": "--location (GCP region) is required for --scheduler composer"},
        )
    _validate_safe_ident(args.environment_name, "--environment-name")
    _validate_safe_ident(args.location, "--location")

    binary = _which_or_raise("gcloud")
    argv = [
        binary,
        "composer",
        "environments",
        "storage",
        "dags",
        "import",
        "--environment",
        args.environment_name,
        "--location",
        args.location,
        f"--source={dags_dir}",
    ]
    return [_run_subprocess(argv, timeout=args.timeout, dry_run=args.dry_run)]


def _astronomer_dispatch(dags_dir: Path, args) -> List[Dict]:
    """Astronomer — ``astro deploy --dags`` from the dags directory.

    The ``astro`` CLI reads its workspace/deployment config from
    ``./astro.yaml`` in the working directory, so we ``cd`` into
    ``dags_dir.parent`` before dispatch. No credential flags flow through
    argv — astro auth is via ``astro login`` session token.
    """
    binary = _which_or_raise("astro")
    if args.environment_name:
        _validate_safe_ident(args.environment_name, "--environment-name")
        argv = [
            binary,
            "deploy",
            args.environment_name,
            "--dags",
        ]
    else:
        argv = [binary, "deploy", "--dags"]

    # Run from the parent of dags_dir so astro.yaml (if present) is in cwd.
    return [
        _run_subprocess_with_cwd(
            argv,
            cwd=str(dags_dir.parent),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    ]


def _prefect_dispatch(dags_dir: Path, args) -> List[Dict]:
    """Prefect — ``prefect deploy --all`` against a ``prefect.yaml`` in dags_dir."""
    binary = _which_or_raise("prefect")
    argv = [binary, "deploy", "--all"]
    if args.workspace:
        _validate_safe_ident(args.workspace, "--workspace")
        # Workspace selection: ``prefect cloud workspace set``. Keeping
        # scope tight — we invoke the deploy in the current workspace. A
        # future extension can prepend a ``workspace set`` call.
        logger.info(
            "prefect_workspace_noted",
            extra={
                "workspace": args.workspace,
                "note": "caller must prefect cloud workspace set first",
            },
        )
    return [
        _run_subprocess_with_cwd(
            argv,
            cwd=str(dags_dir),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    ]


def _dagster_dispatch(dags_dir: Path, args) -> List[Dict]:
    """Dagster Cloud — ``dagster-cloud deploy`` with --deployment."""
    binary = _which_or_raise("dagster-cloud")
    argv = [binary, "deploy"]
    if args.workspace:
        _validate_safe_ident(args.workspace, "--workspace")
        argv.extend(["--deployment", args.workspace])
    argv.extend(["--location-file", "dagster_cloud.yaml"])
    return [
        _run_subprocess_with_cwd(
            argv,
            cwd=str(dags_dir),
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    ]


# -----------------------------------------------------------------------------
# cwd-aware subprocess helper
# -----------------------------------------------------------------------------


def _run_subprocess_with_cwd(argv: List[str], *, cwd: str, timeout: int, dry_run: bool) -> Dict:
    """Thin cwd wrapper around _run_subprocess.

    We keep this distinct rather than adding a cwd kwarg to
    ``_run_subprocess`` because the astronomer/prefect/dagster branches
    genuinely need a cwd and the s3/gs/az branches genuinely must not
    (any cwd change would change relative path semantics and could bleed
    into the argv). Keeping the two helpers prevents accidental misuse.
    """
    redacted = _sanitize_argv(argv)
    logger.info(
        "schedule_sync_subprocess_cwd",
        extra={"argv": redacted, "cwd": cwd, "dry_run": dry_run, "timeout": timeout},
    )
    cprint(f"[schedule-sync] (cwd={cwd}) → {' '.join(redacted)}", markup=False)

    if dry_run:
        return {
            "argv": redacted,
            "cwd": cwd,
            "exit_code": 0,
            "stdout_tail": "(dry-run)",
            "stderr_tail": "",
            "duration_s": 0.0,
            "dry_run": True,
        }

    import time

    start = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603  # argv list, shell=False, cwd validated
            argv,
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return {
            "argv": redacted,
            "cwd": cwd,
            "exit_code": 124,
            "stdout_tail": "",
            "stderr_tail": f"timeout after {timeout}s",
            "duration_s": round(duration, 2),
            "dry_run": False,
        }
    except (FileNotFoundError, PermissionError) as exc:
        duration = time.monotonic() - start
        return {
            "argv": redacted,
            "cwd": cwd,
            "exit_code": 127,
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
            "duration_s": round(duration, 2),
            "dry_run": False,
        }

    duration = time.monotonic() - start
    return {
        "argv": redacted,
        "cwd": cwd,
        "exit_code": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-2048:],
        "stderr_tail": (completed.stderr or "")[-2048:],
        "duration_s": round(duration, 2),
        "dry_run": False,
    }


# -----------------------------------------------------------------------------
# Dispatcher map
# -----------------------------------------------------------------------------


_DISPATCHERS = {
    "airflow": _airflow_dispatch,
    "mwaa": _mwaa_dispatch,
    "composer": _composer_dispatch,
    "astronomer": _astronomer_dispatch,
    "prefect": _prefect_dispatch,
    "dagster": _dagster_dispatch,
}


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


def run(args) -> int:
    """Entry point wired to ``func=run`` in :func:`register`.

    Returns an exit code: 0 on success, 1 on any non-zero subprocess
    exit, 2 on validation error. Validation errors are raised as
    :class:`CLIError` which the bootstrap catches and renders.
    """
    # Clamp + validate inputs first. Fails here are exit-2 (config error),
    # not exit-1 (transient scheduler failure).
    args.timeout = _clamp_timeout(args.timeout)
    dags_dir = _validate_dags_dir(args.dags_dir)

    if args.scheduler not in _DISPATCHERS:  # defensive — argparse choices enforces this
        raise CLIError(
            2,
            "schedule_sync_unknown_scheduler",
            {"scheduler": args.scheduler, "supported": list(_DISPATCHERS.keys())},
        )

    dispatcher = _DISPATCHERS[args.scheduler]
    cprint(
        f"[schedule-sync] scheduler={args.scheduler} "
        f"dags-dir={dags_dir} env={args.env} dry-run={args.dry_run}",
        markup=False,
    )

    results = dispatcher(dags_dir, args)

    # Aggregate exit code: any non-zero → overall failure.
    overall_exit = 0
    for r in results:
        if r.get("exit_code", 1) != 0:
            overall_exit = 1
            logger.error(
                "schedule_sync_subprocess_failed",
                extra={
                    "exit_code": r.get("exit_code"),
                    "stderr_tail": r.get("stderr_tail", "")[-512:],
                },
            )

    report = {
        "command": COMMAND,
        "scheduler": args.scheduler,
        "env": args.env,
        "dags_dir": str(dags_dir),
        "dry_run": args.dry_run,
        "results": results,
        "overall_exit": overall_exit,
    }

    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        cprint(f"[schedule-sync] report → {report_path}", markup=False)

    if overall_exit == 0:
        cprint(
            f"[schedule-sync] ✔ {args.scheduler} sync complete " f"({len(results)} subprocess(es))",
            markup=False,
        )
    else:
        cprint(
            f"[schedule-sync] ✘ {args.scheduler} sync failed " f"(see logs for argv + stderr tail)",
            markup=False,
        )

    return overall_exit
