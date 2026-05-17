# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Provider-agnostic OpenTofu runner — wraps the ``tofu`` CLI via subprocess.

Consumes OpenTofu's machine-readable ``-json`` output (a stable,
versioned protocol) rather than scraping human text. The runner shells
``tofu`` only — never ``terraform`` — so the execution path stays MPL.

Credentials are passed by the caller via the ``env`` mapping (a full
environment, e.g. ``os.environ | provider_env``); the runner never
embeds them in argv or in the ``.tf.json``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


class TofuError(RuntimeError):
    """The ``tofu`` binary is unavailable on PATH."""


@dataclass
class TofuResult:
    """Outcome of a single ``tofu`` invocation."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def tofu_path() -> Optional[str]:
    """Return the path to the ``tofu`` binary, or ``None`` if not installed."""
    return shutil.which("tofu")


def _require_tofu() -> str:
    path = tofu_path()
    if path is None:
        raise TofuError(
            "the `tofu` (OpenTofu) binary was not found on PATH — install it "
            "from https://opentofu.org/docs/intro/install/"
        )
    return path


def _parse_json_events(stdout: str) -> List[Dict[str, Any]]:
    """Parse OpenTofu's newline-delimited JSON log stream."""
    events: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a non-JSON line slipped through; ignore it
    return events


def _run(
    args: List[str],
    *,
    workdir: str,
    env: Optional[Mapping[str, str]],
    command: str,
) -> TofuResult:
    tofu = _require_tofu()
    proc = subprocess.run(
        [tofu, *args],
        cwd=workdir,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
    )
    events = _parse_json_events(proc.stdout) if "-json" in args else []
    return TofuResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        events=events,
    )


def tofu_init(workdir: str, *, backend: bool = True, env: Optional[Mapping[str, str]] = None):
    """``tofu init`` — install providers (and initialise the backend)."""
    args = ["init", "-input=false", "-no-color"]
    if not backend:
        args.append("-backend=false")
    return _run(args, workdir=workdir, env=env, command="init")


def tofu_validate(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> TofuResult:
    """``tofu validate`` — check config syntax + provider-schema correctness."""
    return _run(["validate", "-no-color"], workdir=workdir, env=env, command="validate")


def tofu_plan(
    workdir: str, *, out_file: str = "tfplan", env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu plan`` — write a binary plan to ``out_file`` for later apply."""
    return _run(
        ["plan", "-input=false", "-no-color", "-json", f"-out={out_file}"],
        workdir=workdir,
        env=env,
        command="plan",
    )


def tofu_apply(
    workdir: str, *, plan_file: str = "tfplan", env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu apply`` — apply a previously-saved plan file (no re-prompt)."""
    return _run(
        ["apply", "-input=false", "-no-color", "-json", plan_file],
        workdir=workdir,
        env=env,
        command="apply",
    )


def tofu_destroy(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> TofuResult:
    """``tofu destroy`` — tear down everything in state (used by rollback)."""
    return _run(
        ["destroy", "-input=false", "-no-color", "-json", "-auto-approve"],
        workdir=workdir,
        env=env,
        command="destroy",
    )


def change_summary(result: TofuResult) -> Dict[str, int]:
    """Extract the ``{add, change, remove}`` counts from a plan/apply result."""
    for event in result.events:
        if event.get("type") == "change_summary":
            changes = event.get("changes") or {}
            return {key: int(changes.get(key, 0)) for key in ("add", "change", "remove")}
    return {"add": 0, "change": 0, "remove": 0}
