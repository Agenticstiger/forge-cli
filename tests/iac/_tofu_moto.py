# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Real ``tofu`` against an in-process moto server, for the creds-free Stage 1 lanes.

:func:`moto_endpoint` starts a ``ThreadedMotoServer`` (reset first: moto's records
are process-wide), :func:`moto_tofu_env` drops every ``AWS_*`` variable so nothing
reaches a real account, and :func:`tofu` / :func:`init` run the binary, ``init``
retrying the plugin-cache lock pytest-xdist workers contend for.
"""

from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, Iterator

from fluid_build.iac import runner
from fluid_build.iac.credentials import build_tofu_env


def have_moto_server() -> bool:
    try:
        from moto.server import ThreadedMotoServer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — Flask (the `server` extra) may be absent
        return False


def moto_endpoint() -> Iterator[str]:
    """Yield a fresh moto server's ``http://127.0.0.1:<port>``; for a fixture."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"http://127.0.0.1:{port}"
        reset = urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")
        with urllib.request.urlopen(reset, timeout=30) as answered:  # noqa: S310 — local moto
            assert answered.status == 200
        yield endpoint
    finally:
        server.stop()


def moto_tofu_env(plugin_cache: Path) -> Dict[str, str]:
    """The tofu environment with no way to reach a real account."""
    env = {k: v for k, v in build_tofu_env().items() if not k.startswith("AWS_")}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(plugin_cache))
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def tofu(workdir: Path, env: Dict[str, str], *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(runner.tofu_path()), *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def init(workdir: Path, env: Dict[str, str]) -> None:
    for attempt in range(4):
        done = tofu(workdir, env, "init", "-backend=false", "-input=false", "-no-color")
        if done.returncode == 0 or "unable to acquire file lock" not in done.stdout + done.stderr:
            break
        time.sleep(2 * (attempt + 1))
    assert done.returncode == 0, f"tofu init failed:\n{done.stdout}\n{done.stderr}"
