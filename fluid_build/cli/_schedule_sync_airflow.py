# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Airflow check of ``fluid schedule-sync``: flags, preflight, wait and report.

``schedule_sync.run`` drives it in four steps:

1. :meth:`AirflowCheck.from_args` checks the ``--airflow-*`` flags and ``--airflow-url``
   (exit 2 on a bad combination), reads the DAG ids of the files to push, and returns
   ``None`` when ``--airflow-url`` is not set: the stage then behaves as before.
2. :meth:`AirflowCheck.preflight`, BEFORE anything is pushed: resolve the credential,
   find the Airflow major, sign in and make one authorized read. A failure pushes
   nothing (exit 2, or 1 when Airflow cannot be reached).
3. The push.
4. :meth:`AirflowCheck.wait`: until every pushed file is parsed with no import error
   (``_airflow_sync_check``), then the ``airflow_check`` block of the report.

Imported only when ``--airflow-url`` is set (``httpx`` comes with it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build._console import cprint_literal
from fluid_build._contract_loader import CLIError
from fluid_build._error_catalog import slug_for
from fluid_build.cli import _airflow_sync_check as chk
from fluid_build.cli._airflow_api import (
    CREDENTIAL_REF_ENV,
    TOKEN_REF_ENV,
    AirflowApi,
    AirflowApiError,
    credentials_from_env,
    normalize_url,
)
from fluid_build.util import airflow_pools

#: Schedulers whose Airflow REST API forge can reach with a token or a password. MWAA's
#: API is reached through AWS (a web login token, or InvokeRestApi), which this does not do.
CHECK_SCHEDULERS = ("airflow", "astronomer", "composer")
#: Destinations Airflow reads on its own schedule (a bucket, a repository it pulls).
INDIRECT_SCHEMES = frozenset({"s3", "gs", "az", "git+ssh"})
MAX_WAIT = 3600
_BUNDLE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")
_BUNDLE_VERSION = re.compile(r"^[\x21-\x7e]{1,256}$")
_GIT_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PREFIX = "[schedule-sync] airflow:"


def _say(text: str) -> None:
    cprint_literal(f"{_PREFIX} {text}")


def _flags(message: str) -> CLIError:
    return CLIError(2, "schedule_sync_airflow_flags", {"error": message})


def _as_cli_error(exc: AirflowApiError) -> CLIError:
    context = {"error": exc.message}
    if exc.fix:
        context["fix"] = exc.fix
    return CLIError(1 if exc.transient else 2, exc.event, context)


def pushed_git_head(results: List[Dict[str, Any]]) -> Optional[str]:
    """The commit a ``git+ssh`` push left the branch at (its ``git rev-parse HEAD`` step)."""
    for result in reversed(results):
        argv = result.get("argv") or []
        if list(argv[-2:]) == ["rev-parse", "HEAD"] and result.get("exit_code") == 0:
            head = str(result.get("stdout_tail") or "").strip()
            return head if _GIT_HEAD.fullmatch(head) else None
    return None


@dataclass
class AirflowCheck:
    """One ``--airflow-url`` check, from its flags to its report block."""

    base_url: str
    api: str
    wait_s: int
    bundle: Optional[str]
    bundle_version: Optional[str]
    check_only: bool
    scheme: Optional[str]
    indirect: bool
    files: List[chk.DagFile]
    client: Optional[AirflowApi] = None
    #: ``{name: description}`` of the pools the DAG files declare (``FLUID_TABLE_POOLS``).
    pools: Dict[str, str] = field(default_factory=dict)
    #: The report's ``pools`` block, once :meth:`ensure_pools` has run.
    pools_report: Optional[Dict[str, Any]] = None

    # 1 ── flags ─────────────────────────────────────────────────────────

    @classmethod
    def from_args(
        cls,
        args: Any,
        dags_dir: Path,
        *,
        scheme: Optional[str],
        default_wait: int,
    ) -> Optional["AirflowCheck"]:
        url = getattr(args, "airflow_url", None)
        check_only = bool(getattr(args, "check_only", False))
        bundle = getattr(args, "airflow_bundle", None)
        bundle_version = getattr(args, "airflow_bundle_version", None)
        api = getattr(args, "airflow_api", "auto") or "auto"
        wait_s = getattr(args, "airflow_wait", default_wait)
        if not url:
            given = [
                flag
                for flag, value in (
                    ("--check-only", check_only),
                    ("--airflow-bundle", bundle),
                    ("--airflow-bundle-version", bundle_version),
                    ("--airflow-api", api != "auto"),
                    ("--airflow-wait", wait_s != default_wait),
                )
                if value
            ]
            if given:
                raise _flags(
                    f"{', '.join(given)} only apply to the Airflow check, which needs "
                    "--airflow-url <the Airflow API server URL>."
                )
            return None
        if args.scheduler not in CHECK_SCHEDULERS:
            raise CLIError(
                2,
                "schedule_sync_airflow_unsupported_scheduler",
                {
                    "error": (
                        f"--airflow-url is for Airflow's REST API, which --scheduler "
                        f"{args.scheduler} does not offer forge"
                        + (
                            " (MWAA's API is reached through AWS, not with a token or a "
                            "password)."
                            if args.scheduler == "mwaa"
                            else "."
                        )
                    ),
                    "fix": f"Use it with --scheduler {', '.join(CHECK_SCHEDULERS)}.",
                },
            )
        if not isinstance(wait_s, int) or wait_s <= 0:
            raise _flags("--airflow-wait must be a positive number of seconds.")
        if wait_s > MAX_WAIT:
            _say(f"--airflow-wait {wait_s}s is above the cap; waiting at most {MAX_WAIT}s.")
            wait_s = MAX_WAIT
        if bundle is not None and not _BUNDLE_NAME.fullmatch(bundle):
            raise _flags("--airflow-bundle must be a DAG bundle name (letters, digits, _ . -).")
        if bundle_version is not None and not _BUNDLE_VERSION.fullmatch(bundle_version):
            raise _flags("--airflow-bundle-version must be a version with no space (a commit).")
        try:
            base_url = normalize_url(url)
        except AirflowApiError as exc:
            raise _as_cli_error(exc) from None
        files = chk.dag_files(dags_dir)
        if not files:
            raise CLIError(
                2,
                "schedule_sync_airflow_no_dag_files",
                {
                    "error": (
                        "--dags-dir holds no .py file Airflow would parse, so there is "
                        "nothing to check."
                    )
                },
            )
        if check_only and getattr(args, "destination", None):
            _say("--check-only pushes nothing; --destination is ignored.")
        indirect = args.scheduler in ("astronomer", "composer") or (
            scheme in INDIRECT_SCHEMES and not check_only
        )
        return cls(
            base_url=base_url,
            api=api,
            wait_s=wait_s,
            bundle=bundle,
            bundle_version=bundle_version,
            check_only=check_only,
            scheme=scheme,
            indirect=indirect,
            files=files,
            pools=pools_of(files),
        )

    def plan(self) -> None:
        """``--dry-run``: what the check would do. No request, no secret read."""
        import os

        auth = (
            f"token via {TOKEN_REF_ENV}"
            if os.environ.get(TOKEN_REF_ENV)
            else (
                f"password via {CREDENTIAL_REF_ENV}"
                if os.environ.get(CREDENTIAL_REF_ENV)
                else "no credentials"
            )
        )
        ids = [dag_id for f in self.files for dag_id in f.dag_ids]
        dynamic = [f.path for f in self.files if f.dynamic]
        _say(
            f"would check {self.base_url} (API {self.api}, auth {auth}) and would wait up to "
            f"{self.wait_s}s for {len(self.files)} DAG file(s): {', '.join(ids) or 'no DAG id read'}"
            + (f"; DAG ids read by Airflow only: {', '.join(dynamic)}" if dynamic else "")
        )

    # 2 ── before the push ───────────────────────────────────────────────

    def before_push(self, *, dry_run: bool) -> None:
        """``--dry-run`` plans the check; otherwise sign in and read once, make the pools the
        DAG files use, or raise. The pools come before the push: a DAG that names a pool
        Airflow lacks is never left running."""
        if dry_run:
            self.plan()
            if self.pools and not self.check_only:
                self.plan_pools()
        else:
            self.preflight()
            if self.pools and not self.check_only:
                self.pools_report = self.ensure_pools()

    def preflight(self) -> None:
        try:
            credentials = credentials_from_env()
            client = AirflowApi(self.base_url, credentials)
            client.connect(self.api)
        except AirflowApiError as exc:
            raise _as_cli_error(exc) from None
        if client.dialect == "v1" and (self.bundle or self.bundle_version):
            raise _flags(
                f"This is Airflow {client.version}, and Airflow 2 has no DAG bundles: "
                "--airflow-bundle and --airflow-bundle-version need Airflow 3."
            )
        self.client = client
        _say(
            f"Airflow {client.version} at {self.base_url} (API {client.dialect}, auth "
            f"{client.auth_style})"
        )

    # 4 ── after the push ────────────────────────────────────────────────

    def after_push(
        self, results: List[Dict[str, Any]], *, pushed: bool, dry_run: bool
    ) -> Optional[Dict[str, Any]]:
        """The report's ``airflow_check`` block: ``None`` on a dry run, ``skipped`` when the
        push failed, otherwise the outcome of :meth:`wait`."""
        if dry_run:
            return None
        if not pushed:
            return skipped_report("The push failed, so Airflow was not checked.")
        report = self.wait(results)
        if self.pools_report is not None:
            report["pools"] = self.pools_report
        return report

    def ensure_pools(self) -> Dict[str, Any]:
        """Create each pool the DAG files declare that Airflow does not hold yet.

        A pool that exists is left as it is, whoever made it and however many slots it
        has: this never shrinks or edits one. The block is the report's ``pools``.
        """
        client = self.client
        if client is None:  # preflight() raised, or was never called
            raise RuntimeError("AirflowCheck.ensure_pools() before a successful preflight()")
        created: List[str] = []
        existing: List[Dict[str, Any]] = []
        try:
            held = client.pools()
            for name, description in sorted(self.pools.items()):
                if name not in held and client.create_pool(
                    name, airflow_pools.POOL_SLOTS, description
                ):
                    created.append(name)
                    _say(f"pool {name} created with {airflow_pools.POOL_SLOTS} slot")
                    continue
                slots = held.get(name)
                existing.append({"name": name, "slots": slots})
                said = f"pool {name} exists" + (
                    f" with {slots} slots; left as it is (its writers may overlap)"
                    if slots not in (None, airflow_pools.POOL_SLOTS)
                    else "; left as it is"
                )
                _say(said)
        except AirflowApiError as exc:
            commands = airflow_pools.set_commands(self.pools)
            _say(f"✘ [{slug_for(exc.event)}] {exc.message}")
            for command in commands:
                _say(f"with Airflow's CLI: {command}")
            error = _as_cli_error(exc)
            error.context["error"] = (
                f"The pools could not be made, so nothing was pushed: {exc.message}"
            )
            raise error from None
        return {"result": "passed", "created": created, "existing": existing}

    def plan_pools(self) -> None:
        """``--dry-run``: which pools Airflow holds and which a real run would create. One read
        of ``/pools``, nothing written; a read that cannot be made is said, not guessed."""
        try:
            client = AirflowApi(self.base_url, credentials_from_env())
            try:
                client.connect(self.api)
                held = client.pools()
            finally:
                client.close()
        except AirflowApiError as exc:
            _say(
                f"the pools were not read ({exc.message}); a real run would make each one that "
                f"is missing: {', '.join(sorted(self.pools))}"
            )
            return
        have = sorted(n for n in self.pools if n in held)
        missing = sorted(n for n in self.pools if n not in held)
        _say(
            f"pools: {len(have)} exist ({', '.join(have) or 'none'}), would create "
            f"{len(missing)} with {airflow_pools.POOL_SLOTS} slot ({', '.join(missing) or 'none'})"
        )

    def wait(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Wait for the parse; the report's ``airflow_check`` block (``result`` says pass)."""
        client = self.client
        if client is None:  # preflight() raised, or was never called
            raise RuntimeError("AirflowCheck.wait() before a successful preflight()")
        expected = self.bundle_version
        if expected is None and self.scheme == "git+ssh" and not self.check_only:
            expected = pushed_git_head(results)
        ids = sum(len(f.dag_ids) for f in self.files)
        _say(
            f"waiting up to {self.wait_s}s for {len(self.files)} DAG file(s), {ids} DAG id(s)"
            + (f", at bundle version {expected[:12]}" if expected else "")
        )
        try:
            outcome = chk.wait_for_parse(
                client,
                self.files,
                wait_s=self.wait_s,
                expected_version=expected,
                version_explicit=self.bundle_version is not None,
                bundle=self.bundle,
                indirect=self.indirect,
                on_change=self._file_line,
                on_progress=_say,
            )
        finally:
            client.close()
        if outcome.passed and outcome.proven:
            _say(f"✔ {outcome.message}")
        elif outcome.passed:
            _say(f"✔ [{slug_for(outcome.event or '')}] {outcome.message}")
        else:
            _say(f"✘ [{slug_for(outcome.event or '')}] {outcome.message}")
        for note in outcome.notes:
            _say(f"note: {note}")
        report = outcome.to_report()
        report.update(
            {
                "url": self.base_url,
                "api": client.dialect,
                "airflow_version": client.version,
                "auth": client.auth_style,
                "bundle": self.bundle,
                "expected_bundle_version": expected,
                "wait_s": self.wait_s,
                "check_only": self.check_only,
            }
        )
        return report

    @staticmethod
    def _file_line(result: chk.FileResult) -> None:
        if result.state == "unproven":
            _say(f"✔ {result.path} (not proven): {result.detail}")
            return
        mark = "✔" if result.state == "clean" else "✘"
        _say(f"{mark} {result.path}: {result.detail}")


def pools_of(files: List[chk.DagFile]) -> Dict[str, str]:
    """``{name: description}`` of the pools the DAG files declare."""
    pools: Dict[str, str] = {}
    for file in files:
        if file.source and airflow_pools.CONSTANT in file.source:
            pools.update(airflow_pools.declared_pools(file.source))
    return pools


def skipped_report(reason: str) -> Dict[str, Any]:
    """The ``airflow_check`` block when the push failed, so Airflow was not checked."""
    return {"result": "skipped", "event": None, "message": reason, "files": []}
