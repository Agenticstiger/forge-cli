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

"""``$ref`` confinement scenarios that need a real ``fluid`` process.

Catalog D-17, D-18 and D-23, and two past the catalog that hang: an overlay
that is a FIFO (DX-02) and a contract that contains itself (DX-08). A device,
a FIFO, a self-containing document or a project ``.env`` can block, exhaust or
change the process that reads it, so each case runs ``python -m fluid_build.cli`` against
this checkout with an explicit environment, a working directory under
``tmp_path``, a wall-clock timeout and a memory cap (``RLIMIT_AS`` where the
kernel enforces it, and a resident-size watchdog where ``psutil`` is present).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
import yaml

from fluid_build import loader

CHECKOUT = Path(__file__).resolve().parents[2]
SECRET = "SECRET-1"  # pragma: allowlist secret
IS_LINUX = platform.system() == "Linux"
MEMORY_CAP = 2 * 1024 * 1024 * 1024

pytestmark = pytest.mark.skipif(os.name == "nt", reason="FIFOs, devices and fork are POSIX")

MIN_TEXT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: refs.text
name: Text refs
description:
  purpose: Text refs.
  usage: Use it.
domain: ops
metadata:
  layer: Silver
  owner: {team: platform-ops, email: ops@example.com}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: SELECT 1 AS id
exposes:
  - exposeId: out
    kind: table
    version: "1.0.0"
    binding:
      platform: local
      format: csv
      location: {path: runtime/out/out.csv}
    contract:
      schema:
        - {name: id, type: integer}
      quality:
        - {rule: id_present, expression: id IS NOT NULL, severity: error}
"""


@pytest.fixture(autouse=True)
def _fresh_loader_state() -> Iterator[None]:
    """The loader's warn-once registries empty (the children start fresh anyway)."""
    registries = [
        value
        for name, value in vars(loader).items()
        if name.startswith("_NOTED_") and isinstance(value, set)
    ]
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()


class MemoryCapExceeded(AssertionError):
    """The child grew past :data:`MEMORY_CAP` and was killed."""


def _cap_address_space() -> None:  # pragma: no cover - runs in the child
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_CAP * 2, MEMORY_CAP * 2))
    except (ImportError, ValueError, OSError):
        pass  # macOS does not enforce it; the parent's watchdog covers it


def _rss(pid: int) -> int:
    try:
        import psutil
    except ImportError:  # pragma: no cover - the timeout still bounds the child
        return 0
    try:
        return int(psutil.Process(pid).memory_info().rss)
    except Exception:  # noqa: BLE001 - the child is gone
        return 0


def _fluid(
    args: List[Any],
    *,
    cwd: Path,
    env_extra: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    stdin: Any = subprocess.DEVNULL,
    memory_cap: int = MEMORY_CAP,
) -> subprocess.CompletedProcess:
    """``python -m fluid_build.cli <args>`` against this checkout, bounded in time and memory."""
    return _run(
        ["-m", "fluid_build.cli", *args],
        cwd=cwd,
        env_extra=env_extra,
        timeout=timeout,
        stdin=stdin,
        memory_cap=memory_cap,
    )


def _run(
    args: List[Any],
    *,
    cwd: Path,
    env_extra: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    stdin: Any = subprocess.DEVNULL,
    memory_cap: int = MEMORY_CAP,
) -> subprocess.CompletedProcess:
    """``python <args>`` against this checkout, bounded in time and memory.

    Raises ``subprocess.TimeoutExpired`` when the child runs past ``timeout``
    (it is killed first) and :class:`MemoryCapExceeded` when it grows past the cap.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    env["PYTHONPATH"] = str(CHECKOUT)
    env.update(env_extra or {})
    cmd = [sys.executable, *[str(a) for a in args]]
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        preexec_fn=_cap_address_space,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.2)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if _rss(proc.pid) > memory_cap:
                proc.kill()
                proc.communicate()
                raise MemoryCapExceeded(f"{cmd} grew past {memory_cap} bytes")
            if time.monotonic() > deadline:
                proc.kill()
                out, err = proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout, out, err)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contract(product: Path, doc: Any) -> Path:
    return _write(product / "contract.fluid.yaml", yaml.safe_dump(doc, sort_keys=False))


def _with(path: List[Any], node: Any) -> Dict[str, Any]:
    doc = yaml.safe_load(MIN_TEXT)
    parent = doc
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = node
    return doc


def _flat(text: str) -> str:
    return " ".join(text.split())


def _mkfifo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkfifo(path)
    except (AttributeError, OSError):
        pytest.skip("FIFOs unavailable here")
    return path


def test_the_subprocess_runs_this_checkout(tmp_path: Path) -> None:
    probe = subprocess.run(
        [sys.executable, "-c", "import fluid_build; print(fluid_build.__file__)"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(CHECKOUT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert Path(probe.stdout.strip()).resolve().is_relative_to(CHECKOUT)
    assert Path(loader.__file__).resolve().is_relative_to(CHECKOUT)


# ── D-17: devices under FLUID_REF_ROOT=/ are refused, never read ───────


@pytest.mark.parametrize(
    "device",
    [
        "dev/zero",
        "dev/stdin",
        "dev/random",
        "dev/urandom",
        "dev/fd/0",
        pytest.param("proc/self/environ", marks=pytest.mark.skipif(not IS_LINUX, reason="Linux")),
        pytest.param("sys/kernel/x", marks=pytest.mark.skipif(not IS_LINUX, reason="Linux")),
    ],
)
def test_d17_a_device_under_the_widest_root_is_refused_by_bundle(
    tmp_path: Path, device: str
) -> None:
    product = tmp_path / "p"
    product.mkdir()
    climb = "../" * (len(product.resolve().parts) - 1)
    path = _contract(product, _with(["description", "usage"], {"$ref": climb + device}))
    out = tmp_path / "bundle.yaml"

    # stdin is a pipe nobody writes to: reading /dev/stdin would block.
    result = _fluid(
        ["bundle", path, "--out", out],
        cwd=tmp_path,
        env_extra={"FLUID_REF_ROOT": "/"},
        timeout=20,
        stdin=subprocess.PIPE,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "blocked system path" in _flat(result.stderr)
    assert not out.exists()


# ── D-18: a project .env cannot widen the root ──────────────────────────


@pytest.mark.parametrize("command", ["validate", "bundle"])
def test_d18_a_project_env_file_cannot_set_fluid_ref_root(tmp_path: Path, command: str) -> None:
    _write(tmp_path / "outside.yaml", f"team: {SECRET}\nemail: secret@example.com\n")
    product = tmp_path / "p"
    path = _contract(product, _with(["metadata", "owner"], {"$ref": "../outside.yaml"}))
    _write(product / ".env", "FLUID_REF_ROOT=/\n")
    args: List[Any] = [command, path.name] + (["--out", "-"] if command == "bundle" else [])

    result = _fluid(args, cwd=product, timeout=60)  # FLUID_* stripped from the real env

    output = result.stdout + result.stderr
    assert result.returncode == (1 if command == "validate" else 2), output
    assert "escapes the ref root" in _flat(output)
    assert SECRET not in output


# ── D-23: a FIFO target is refused, never waited on ─────────────────────


@pytest.mark.parametrize("name", ["q.sql", "x.yaml", "x.json", "notes"])
def test_d23_a_fifo_target_is_refused_without_a_hang(tmp_path: Path, name: str) -> None:
    product = tmp_path / "p"
    _mkfifo(product / name)
    path = _contract(product, _with(["description", "usage"], {"$ref": f"./{name}"}))

    result = _fluid(["validate", path], cwd=tmp_path, timeout=10)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a regular file" in _flat(result.stdout + result.stderr)


def test_d23_a_workspace_file_that_is_a_fifo_is_not_waited_on(tmp_path: Path) -> None:
    """Negative control: ``find_workspace_root`` takes a regular file only, so a
    FIFO named ``fluid.workspace.yaml`` declares nothing and blocks nothing."""
    ws = tmp_path / "ws"
    _mkfifo(ws / "fluid.workspace.yaml")
    _write(ws / "shared" / "team.yaml", "team: shared\nemail: shared@example.com\n")
    path = _contract(
        ws / "products" / "orders",
        _with(["metadata", "owner"], {"$ref": "../../shared/team.yaml"}),
    )

    result = _fluid(["validate", path], cwd=tmp_path, timeout=20)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "escapes the ref root" in _flat(result.stdout + result.stderr)


# ── DX-02: the overlay file is not held to the same rule ────────────────


@pytest.mark.slow
@pytest.mark.parametrize("command", ["validate", "bundle"])
def test_dx02_sg10_an_overlay_that_is_a_fifo_is_refused_without_a_hang(
    tmp_path: Path, command: str
) -> None:
    """SG-10, decided: an overlay is held to the rule of a ``$ref`` target, a regular
    file, checked before it is read and again on the file opened without blocking.
    A FIFO at ``overlays/<env>.yaml`` used to block ``--env <env>`` forever; it is
    refused, naming the overlay."""
    product = tmp_path / "p"
    path = _contract(product, yaml.safe_load(MIN_TEXT))
    _mkfifo(product / "overlays" / "prod.yaml")
    out = tmp_path / "bundle.yaml"
    args: List[Any] = [command, path, "--env", "prod"]
    if command == "bundle":
        args += ["--out", out]

    result = _fluid(args, cwd=tmp_path, timeout=20)

    output = _flat(result.stdout + result.stderr)
    assert result.returncode == 1, output
    assert "not a regular file" in output and "overlays/prod.yaml" in output
    assert not out.exists()


# ── DX-08: a contract that contains itself (YAML alias) ─────────────────

#: A list that holds itself: the walk spins at 100% CPU in constant memory.
_LIST_LOOP = "extensions:\n  loop: &loop\n  - *loop\n"
#: A mapping that holds itself beside a sibling: the walk's stack keeps every
#: sibling with an ever longer path, so memory grows quadratically (2 GiB in
#: about 8 s on the machine these tests were written on).
_MAPPING_LOOP = "extensions:\n  loop: &l {k: 1, self: [*l]}\n"
#: What a hang may cost before the child is killed.
_HANG_MEMORY_CAP = 512 * 1024 * 1024

_API_LOAD = """\
import sys
from fluid_build.api import ContractLoadError, load_contract
try:
    load_contract(sys.argv[1])
except ContractLoadError as exc:
    print(exc.event)
    sys.exit(1)
"""


def _self_containing(tmp_path: Path, *, with_ref: bool, loop: str = _LIST_LOOP) -> Path:
    product = tmp_path / "p"
    _write(product / "team.yaml", "team: platform-ops\nemail: ops@example.com\n")
    doc = yaml.safe_load(MIN_TEXT)
    if with_ref:
        doc["metadata"]["owner"] = {"$ref": "./team.yaml"}
    return _write(product / "contract.fluid.yaml", yaml.safe_dump(doc, sort_keys=False) + loop)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("with_ref", "loop"),
    [(False, _LIST_LOOP), (True, _LIST_LOOP), (False, _MAPPING_LOOP)],
    ids=["no-ref-list", "with-ref-list", "no-ref-mapping"],
)
def test_dx08_validate_fails_fast_on_a_contract_that_contains_itself(
    tmp_path: Path, with_ref: bool, loop: str
) -> None:
    path = _self_containing(tmp_path, with_ref=with_ref, loop=loop)

    result = _fluid(["validate", path], cwd=tmp_path, timeout=60, memory_cap=_HANG_MEMORY_CAP)

    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.slow
def test_dx08_validate_fails_fast_on_an_overlay_that_contains_itself(tmp_path: Path) -> None:
    path = _self_containing(tmp_path, with_ref=False, loop="")
    _write(path.parent / "overlays" / "prod.yaml", _LIST_LOOP)

    result = _fluid(
        ["validate", path, "--env", "prod"], cwd=tmp_path, timeout=60, memory_cap=_HANG_MEMORY_CAP
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.slow
def test_dx08_the_api_fails_fast_on_a_contract_that_contains_itself(tmp_path: Path) -> None:
    path = _self_containing(tmp_path, with_ref=False)

    result = _run(["-c", _API_LOAD, path], cwd=tmp_path, timeout=60, memory_cap=_HANG_MEMORY_CAP)

    assert result.returncode == 1
    assert result.stdout.strip() == "contract_load_failed"


@pytest.mark.slow
def test_dx08_bundle_fails_fast_on_a_contract_with_refs_that_contains_itself(
    tmp_path: Path,
) -> None:
    path = _self_containing(tmp_path, with_ref=True)

    result = _fluid(
        ["bundle", path, "--out", tmp_path / "b.yaml"],
        cwd=tmp_path,
        timeout=60,
        memory_cap=_HANG_MEMORY_CAP,
    )

    assert result.returncode in (1, 2)
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.slow
def test_dx08_validate_fragment_fails_fast_on_a_fragment_that_contains_itself(
    tmp_path: Path,
) -> None:
    """The same walk decides a fragment's root when a contract sits above it."""
    path = _self_containing(tmp_path, with_ref=False, loop="")
    fragment = _write(path.parent / "cols.yaml", "- &col {name: id, type: integer, x: [*col]}\n")

    result = _fluid(
        ["validate", "--fragment", "schema", fragment],
        cwd=tmp_path,
        timeout=60,
        memory_cap=_HANG_MEMORY_CAP,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr


def test_dx08_bundle_stops_early_on_a_contract_without_refs_that_contains_itself(
    tmp_path: Path,
) -> None:
    """Characterization: bundle's own recursive ``_has_refs`` walk of the raw
    contract hits the recursion limit first, so without a ``$ref`` (none to stop
    that walk early) it fails fast instead of reaching the composition that hangs."""
    path = _self_containing(tmp_path, with_ref=False)

    result = _fluid(["bundle", path, "--out", tmp_path / "b.yaml"], cwd=tmp_path, timeout=60)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Compilation failed" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr
