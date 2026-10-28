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

"""Workspace ``refRoot`` (scenario catalog area L, ids L-01 to L-27).

Spec, as decided (catalog section 1, item 12): a committed
``fluid.workspace.yaml`` may widen the ``$ref`` root of the contracts under
it, found by walking up from the contract. A relative value is anchored at the
workspace file, must stay at or below that file's directory and must contain
the contract. A workspace at ``$HOME``, at ``/``, at a temp root, or above the
contract's repository is refused, and an operator off-switch (a protected
environment variable that a project ``.env`` cannot set) turns the setting
off. Precedence: ``ref_root=`` > ``FLUID_REF_ROOT`` > workspace > the
contract's own directory.

Implemented key spellings: ``ref_root`` and ``refRoot`` (top level, or inside
a ``workspace:`` mapping); off-switch ``FLUID_IGNORE_WORKSPACE_REF_ROOT``. A
value or a file that cannot be used is ignored with a WARNING, and every
escape error of a contract under it says it was ignored and why.

Every test builds its files under ``tmp_path``. The broad-root cases patch the
sandbox's ``_home_dirs`` / ``_HOMES_PARENTS`` / ``_SYSTEM_TEMP_DIRS`` /
``tempfile.gettempdir`` and the workspace finder; none sets a fake ``$HOME``
under ``tmp_path`` (that makes ``broad_root_reason`` flag ``/var`` as a home
ancestor on macOS).

A test marked ``xfail(strict=True)`` pins the spec where the code disagrees;
it turns into a failure the day the code is fixed, so the marker gets removed.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Sequence

import pytest
import yaml

import fluid_build
from fluid_build import loader
from fluid_build.loader import (
    RefConfinementError,
    RefResolutionError,
    load_contract,
    load_with_overlay,
)
from fluid_build.providers import _duckdb_sandbox as sandbox

REPO_ROOT = Path(__file__).resolve().parents[2]
OFF_SWITCH = loader.IGNORE_WORKSPACE_REF_ROOT_ENV
WS_IGNORED_EVENT = "workspace_ref_root_ignored"
ENV_IGNORED_EVENT = "ref_root_env_ignored"
TEAM = {"team": "commerce-data", "email": "commerce@example.com"}
SECRET = "SECRET-1"  # pragma: allowlist secret


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _the_checkout_under_test_is_the_one_that_runs() -> None:
    """Prove which ``fluid_build`` the in-process tests exercise (subprocesses get PYTHONPATH)."""
    loaded = Path(fluid_build.__file__).resolve()
    assert loaded.is_relative_to(REPO_ROOT), f"{loaded} is not inside the checkout {REPO_ROOT}"


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient root settings, and the loader's warn-once registries empty."""
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv(OFF_SWITCH, raising=False)
    # ``main()`` switches python-dotenv off for the process; set through
    # monkeypatch so the test run is left as it was found.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    _clear_registries()
    yield
    _clear_registries()


def _clear_registries() -> None:
    for name, value in list(vars(loader).items()):
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture
def loader_log() -> Iterator[List[logging.LogRecord]]:
    """Every record ``fluid.loader`` emits, whatever the root logging is doing."""
    records: List[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("fluid.loader")
    handler = _Collect(level=logging.DEBUG)
    before = (logger.level, logger.disabled)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(before[0])
        logger.disabled = before[1]


class IoSpy(NamedTuple):
    """What a load opened, parsed and asked the workspace finder."""

    opened: List[str]
    parsed: List[str]
    finder_calls: List[Any]
    #: Whether patching ``io.open`` sees ``Path.read_text`` on this Python (3.10 binds it early).
    opens_visible: bool

    def files_under(self, root: Path) -> List[str]:
        prefix = str(root)
        return [p for p in self.opened if str(p).startswith(prefix)]

    def parsed_names(self) -> List[str]:
        return [Path(p).name for p in self.parsed]

    def clear(self) -> None:
        self.opened.clear()
        self.parsed.clear()
        self.finder_calls.clear()


@pytest.fixture
def io_spy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> IoSpy:
    import fluid_build.util.workspace_root as workspace_root

    opened: List[str] = []
    parsed: List[str] = []
    finder_calls: List[Any] = []
    real_open = io.open
    real_parse = loader._parse_file
    real_find = workspace_root.find_workspace_root

    def open_spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def parse_spy(path: Path) -> Any:
        parsed.append(str(path))
        return real_parse(path)

    def find_spy(start: Optional[Path] = None) -> Any:
        finder_calls.append(start)
        return real_find(start)

    # ``Path.read_text`` opens through ``io.open``; ``builtins.open`` is the same function.
    monkeypatch.setattr(io, "open", open_spy)
    monkeypatch.setattr(builtins, "open", open_spy)
    monkeypatch.setattr(loader, "_parse_file", parse_spy)
    monkeypatch.setattr(workspace_root, "find_workspace_root", find_spy)
    canary = tmp_path / "io-spy-canary.txt"
    canary.write_text("x", encoding="utf-8")
    canary.read_text(encoding="utf-8")
    visible = any(Path(p).name == canary.name for p in opened)
    opened.clear()
    return IoSpy(opened, parsed, finder_calls, visible)


# ── builders ────────────────────────────────────────────────────────────


def contract_text(name: str = "orders", owner: str = "{$ref: ../../shared/team.yaml}") -> str:
    """A valid 0.7.6 contract whose owner is ``owner`` (a ref by default)."""
    return (
        'fluidVersion: "0.7.6"\n'
        "kind: DataProduct\n"
        f"id: commerce.{name}\n"
        f"name: {name}\n"
        "domain: commerce\n"
        "metadata:\n"
        "  layer: Silver\n"
        f"  owner: {owner}\n"
        "exposes:\n"
        f"  - exposeId: {name}\n"
        "    kind: table\n"
        f"    binding: {{platform: local, format: csv, location: {{path: out/{name}.csv}}}}\n"
        "    contract:\n"
        "      schema:\n"
        "        - {name: order_id, type: string, required: true}\n"
    )


class Workspace(NamedTuple):
    root: Path
    contract: Path
    contracts: Dict[str, Path]

    @property
    def product(self) -> Path:
        return self.contract.parent

    @property
    def shared(self) -> Path:
        return self.root / "shared" / "team.yaml"


def make_ws(
    tmp_path: Path,
    workspace_yaml: Optional[str] = "name: commerce\nrefRoot: .\n",
    *,
    names: Sequence[str] = ("orders",),
    products_dir: str = "products",
    ref: str = "../../shared/team.yaml",
    dirname: str = "ws",
) -> Workspace:
    """``<dirname>/fluid.workspace.yaml``, ``shared/team.yaml`` and one product per name."""
    root = tmp_path / dirname
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "team.yaml").write_text(yaml.safe_dump(TEAM), encoding="utf-8")
    if workspace_yaml is not None:
        (root / "fluid.workspace.yaml").write_text(workspace_yaml, encoding="utf-8")
    contracts: Dict[str, Path] = {}
    for name in names:
        product = root / products_dir / name
        product.mkdir(parents=True)
        path = product / "contract.fluid.yaml"
        path.write_text(contract_text(name, "{$ref: %s}" % ref), encoding="utf-8")
        contracts[name] = path.resolve()
    return Workspace(root.resolve(), contracts[names[0]], contracts)


def setting(key: str, value: Any) -> str:
    """A workspace file with ``key: value`` (YAML-quoted as needed)."""
    return yaml.safe_dump({"name": "commerce", key: value})


def owner_of(path: Path, **kwargs: Any) -> Any:
    return load_contract(path, **kwargs)["metadata"]["owner"]


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def ignored(records: List[logging.LogRecord], event: str = WS_IGNORED_EVENT) -> List[Any]:
    return [r for r in records if getattr(r, "event", None) == event]


def reason(record: logging.LogRecord) -> str:
    """The problem a ``workspace_ref_root_ignored`` / ``ref_root_env_ignored`` warning gave."""
    return str(record.args[0])  # type: ignore[index]


def run_main(*argv: Any) -> int:
    from fluid_build.cli import main

    return main([str(a) for a in argv])


class Run(NamedTuple):
    returncode: int
    output: str


def fluid(
    args: Sequence[Any], cwd: Path, env_extra: Optional[Dict[str, str]] = None, timeout: int = 180
) -> Run:
    """``python -m fluid_build.cli <args>`` in a clean process (checkout on PYTHONPATH)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("FLUID_") and k != "PYTHON_DOTENV_DISABLED"
    }
    env.update(PYTHONPATH=str(REPO_ROOT), NO_COLOR="1", COLUMNS="400", TERM="dumb")
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *[str(a) for a in args]],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return Run(proc.returncode, " ".join((proc.stdout + proc.stderr).split()))


def has_ref_key(node: Any) -> bool:
    """Whether a mapping KEY ``$ref`` is left anywhere (text that mentions it does not count)."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


# ── L-01 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cwd_name", ["workspace", "product", "unrelated"])
def test_l01_validate_passes_from_any_directory_with_no_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cwd_name: str
) -> None:
    ws = make_ws(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(
        {"workspace": ws.root, "product": ws.product, "unrelated": unrelated}[cwd_name]
    )

    assert run_main("validate", ws.contract) == 0


def test_l01_bundle_plan_and_api_compose_the_owner_from_the_shared_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract

    ws = make_ws(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert run_main("bundle", ws.contract, "--out", elsewhere / "bundle.yaml") == 0
    bundled = yaml.safe_load((elsewhere / "bundle.yaml").read_text(encoding="utf-8"))
    assert bundled["metadata"]["owner"] == TEAM
    assert not has_ref_key(bundled)

    assert run_main("plan", ws.contract, "--out", elsewhere / "plan.json") == 0
    plan = json.loads((elsewhere / "plan.json").read_text(encoding="utf-8"))
    assert plan["contract"]["metadata"]["owner"] == TEAM

    loaded = api_load_contract(ws.contract)
    assert loaded.contract["metadata"]["owner"] == TEAM
    assert ws.shared.resolve() in loaded.files

    # A bundle built from the sources needs no root at all afterwards.
    assert run_main("bundle", ws.contract, "--format", "tgz", "--out", elsewhere / "b.tgz") == 0
    assert run_main("plan", elsewhere / "b.tgz", "--out", elsewhere / "from-bundle.json") == 0
    replanned = json.loads((elsewhere / "from-bundle.json").read_text(encoding="utf-8"))
    assert replanned["contract"]["metadata"]["owner"] == TEAM


@pytest.mark.slow
def test_l01_sub_a_clean_process_validates_from_an_unrelated_directory(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    run = fluid(["validate", ws.contract], cwd=unrelated)

    assert run.returncode == 0, run.output


# ── L-02 ────────────────────────────────────────────────────────────────


def test_l02_the_root_is_anchored_at_the_workspace_file_not_at_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path)
    monkeypatch.chdir(ws.product)

    assert run_main("validate", "contract.fluid.yaml") == 0


def test_l02_contrast_fluid_ref_root_dot_is_relative_to_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same string means two things: the variable's ``.`` is the cwd, the file's is the file."""
    ws = make_ws(tmp_path, "name: commerce\n")  # a workspace without the setting
    monkeypatch.setenv("FLUID_REF_ROOT", ".")

    monkeypatch.chdir(ws.root)
    assert run_main("validate", "products/orders/contract.fluid.yaml") == 0
    monkeypatch.chdir(ws.product)
    assert run_main("validate", "contract.fluid.yaml") == 1


# ── L-03 ────────────────────────────────────────────────────────────────


def test_l03_a_ref_root_above_the_workspace_is_refused_and_the_default_root_is_kept(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", ".."))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert err.value.ref == "../../shared/team.yaml"
    assert Path(err.value.root or "") == ws.product, "the root is the contract's own directory"
    (warning,) = ignored(loader_log)
    assert "outside the workspace directory" in reason(warning)
    assert str(ws.root / "fluid.workspace.yaml") in warning.getMessage()


def test_l03_the_escape_error_says_the_ref_root_was_ignored_and_why(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, setting("refRoot", ".."))

    for _ in range(2):  # the second load logs nothing: its error is all a service's caller sees
        with pytest.raises(RefConfinementError) as err:
            load_contract(ws.contract)

        message = str(err.value)
        assert "ignored" in message.lower(), message
        assert "outside the workspace directory" in message, message
        assert str(ws.root / "fluid.workspace.yaml") in message, "it names the file to fix"
        assert "declare ref_root" not in message, "it asks for the ref_root that was declared"


def test_l03_with_the_off_switch_the_hint_does_not_ask_for_a_workspace_ref_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, io_spy: IoSpy
) -> None:
    ws = make_ws(tmp_path)
    monkeypatch.setenv(OFF_SWITCH, "1")

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    message = str(err.value)
    assert f"{OFF_SWITCH} is set" in message, message
    assert "declare ref_root" not in message, "the hint asks for the setting that is switched off"
    assert io_spy.finder_calls == [], "the switch does not even look for the workspace file"
    assert "fluid.workspace.yaml" not in io_spy.parsed_names()


def test_l03_an_ignored_variable_and_an_ignored_workspace_value_are_both_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", ".."))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FLUID_REF_ROOT", str(elsewhere))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    message = str(err.value)
    assert "FLUID_REF_ROOT is set but was ignored" in message, message
    assert "The workspace's ref_root was ignored" in message, message
    assert "outside the workspace directory" in message, message
    assert err.value.ignored_ref_root_env == str(elsewhere)
    assert Path(err.value.root or "") == ws.product, "neither widened the root"
    assert len(ignored(loader_log)) == 1 and len(ignored(loader_log, ENV_IGNORED_EVENT)) == 1


# ── L-04 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["/", "/Users/x", "~", "~/x", "C:\\x", "\\\\srv\\x", "C:x"],
    ids=["slash", "absolute-path", "tilde", "tilde-path", "drive", "unc-share", "drive-relative"],
)
def test_l04_absolute_home_and_drive_forms_are_refused_without_expanding_them(
    tmp_path: Path, loader_log: List[logging.LogRecord], value: str
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", value))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert Path(err.value.root or "") == ws.product
    (warning,) = ignored(loader_log)
    # Refused for being absolute, before anything is resolved: not for "outside" or "no contract".
    assert "relative" in reason(warning), reason(warning)


# ── L-05 ────────────────────────────────────────────────────────────────


def test_l05_a_symlinked_ref_root_that_resolves_outside_the_workspace_is_refused(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "team.yaml").write_text(f"team: {SECRET}\n", encoding="utf-8")
    ws = make_ws(tmp_path, setting("refRoot", "shared-link"))
    symlink(ws.root / "shared-link", outside)

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert Path(err.value.root or "") == ws.product
    assert SECRET not in str(err.value)
    (warning,) = ignored(loader_log)
    assert "outside the workspace directory" in reason(warning)


def test_l05_a_symlinked_ref_root_that_stays_inside_the_workspace_is_used(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, setting("refRoot", "alias"))
    symlink(ws.root / "alias", ws.root)

    assert owner_of(ws.contract) == TEAM


# ── L-06 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["shared/team.yaml", "no-such-directory"])
def test_l06_a_ref_root_that_is_a_file_or_missing_is_refused_with_a_diagnostic(
    tmp_path: Path, loader_log: List[logging.LogRecord], target: str
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", target))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert Path(err.value.root or "") == ws.product
    (warning,) = ignored(loader_log)
    assert "not a directory" in reason(warning)


# ── L-07 ────────────────────────────────────────────────────────────────


def test_l07_a_ref_root_that_does_not_contain_the_contract_is_refused(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", "shared"))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert Path(err.value.root or "") == ws.product
    (warning,) = ignored(loader_log)
    assert "does not contain the contract" in reason(warning)


# ── L-08 ────────────────────────────────────────────────────────────────


def _refused_as_broad(ws: Workspace, loader_log: List[logging.LogRecord], code: str) -> None:
    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)
    assert Path(err.value.root or "") == ws.product, "the contract keeps its own directory"
    (warning,) = ignored(loader_log)
    assert "too broad" in reason(warning), reason(warning)
    assert f"({code})" in reason(warning), reason(warning)


@pytest.mark.parametrize(
    "code", ["home", "home_ancestor", "temp_root", "user_home", "filesystem_root"]
)
def test_l08_every_broad_root_code_refuses_the_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader_log: List[logging.LogRecord],
    code: str,
) -> None:
    """The mechanism: whatever ``broad_root_reason`` answers, the workspace root is not used."""
    ws = make_ws(tmp_path)
    monkeypatch.setattr(sandbox, "broad_root_reason", lambda path: code)

    _refused_as_broad(ws, loader_log, code)


def _patch_home(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    mp.setattr(sandbox, "_home_dirs", lambda: (str(ws.root),))


def _patch_holds_home(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    mp.setattr(sandbox, "_home_dirs", lambda: (str(ws.product),))


def _patch_is_temp_root(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    mp.setattr(tempfile, "gettempdir", lambda: str(ws.root))


def _patch_holds_temp_root(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    held = str(ws.root / "products")
    mp.setattr(sandbox, "_SYSTEM_TEMP_DIRS", (*sandbox._SYSTEM_TEMP_DIRS, held))


def _patch_homes_parent(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    mp.setattr(sandbox, "_HOMES_PARENTS", (str(ws.root.parent),))


def _patch_filesystem_root(mp: pytest.MonkeyPatch, ws: Workspace) -> None:
    import fluid_build.util.workspace_root as workspace_root

    real_parse = loader._parse_file
    mp.setattr(workspace_root, "find_workspace_root", lambda start=None: Path(os.sep))
    mp.setattr(
        loader,
        "_parse_file",
        lambda path: {"ref_root": "."} if Path(path).parent == Path(os.sep) else real_parse(path),
    )


@pytest.mark.parametrize(
    ("code", "patch"),
    [
        ("home", _patch_home),
        ("home_ancestor", _patch_holds_home),
        ("temp_root", _patch_is_temp_root),
        ("temp_root", _patch_holds_temp_root),
        ("user_home", _patch_homes_parent),
        ("filesystem_root", _patch_filesystem_root),
    ],
    ids=[
        "home",
        "home_ancestor",
        "workspace-is-the-temp-dir",
        "workspace-holds-a-temp-dir",
        "another-users-home",
        "filesystem-root",
    ],
)
def test_l08_the_real_vetting_refuses_a_workspace_that_is_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader_log: List[logging.LogRecord],
    code: str,
    patch: Callable[[pytest.MonkeyPatch, Workspace], None],
) -> None:
    ws = make_ws(tmp_path)
    patch(monkeypatch, ws)

    _refused_as_broad(ws, loader_log, code)


def test_l08_a_project_directory_below_a_temp_root_is_still_a_project(tmp_path: Path) -> None:
    """Negative control: pytest's own tmp_path sits below the system temp dir and is granted."""
    ws = make_ws(tmp_path)

    assert sandbox.broad_root_reason(ws.root) is None
    assert owner_of(ws.contract) == TEAM


# ── L-09, L-10 ──────────────────────────────────────────────────────────


def _vcs_marker(repo: Path, kind: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if kind == "git-dir":
        (repo / ".git").mkdir()
    elif kind == "git-file":  # a worktree or a submodule
        (repo / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
    elif kind == "hg-dir":
        (repo / ".hg").mkdir()
    else:  # a dangling link still marks the directory
        symlink(repo / ".git", repo / "no-such-git-dir")


@pytest.mark.parametrize("kind", ["git-dir", "git-file", "hg-dir", "git-dangling-link"])
def test_l09_a_workspace_above_the_contracts_repository_widens_nothing(
    tmp_path: Path, loader_log: List[logging.LogRecord], kind: str
) -> None:
    ws = make_ws(tmp_path, products_dir="repoA/products", ref="../../../shared/team.yaml")
    _vcs_marker(ws.root / "repoA", kind)

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)

    (warning,) = ignored(loader_log)
    assert "above the repository" in reason(warning), reason(warning)
    assert str(ws.root / "repoA") in reason(warning), "the message names the repository"


def test_l09_a_product_that_is_its_own_repository_under_the_workspace_is_refused(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path)
    _vcs_marker(ws.product, "git-dir")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)
    assert "above the repository" in reason(ignored(loader_log)[0])


def test_l09_a_ref_root_at_the_repository_root_is_allowed(tmp_path: Path) -> None:
    """The marker AT the root is the repository the root is, not one it is above."""
    ws = make_ws(
        tmp_path,
        setting("refRoot", "repoA"),
        products_dir="repoA/products",
        ref="../../shared/team.yaml",
    )
    (ws.root / "repoA" / "shared").mkdir()
    (ws.root / "repoA" / "shared" / "team.yaml").write_text(yaml.safe_dump(TEAM), encoding="utf-8")
    _vcs_marker(ws.root / "repoA", "git-dir")

    assert owner_of(ws.contract) == TEAM


def test_l10_a_monorepo_with_git_at_the_workspace_root_is_granted(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path)
    (ws.root / ".git").mkdir()

    assert owner_of(ws.contract) == TEAM
    assert not ignored(loader_log)


def test_l10_a_workspace_with_no_vcs_marker_between_is_granted(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path)

    assert owner_of(ws.contract) == TEAM
    assert not ignored(loader_log)


# ── L-11 ────────────────────────────────────────────────────────────────

_HYDRATE = """
import json, os, sys
from pathlib import Path
from fluid_build.cli._common import hydrate_dotenv

name = "FLUID_IGNORE_WORKSPACE_REF_ROOT"
before = os.environ.get(name)
hydrate_dotenv(Path(sys.argv[1]))
print(json.dumps({"before": before, "after": os.environ.get(name)}))
"""


@pytest.mark.slow
def test_l11_sub_the_off_switch_in_the_real_environment_ignores_the_workspace_ref_root(
    tmp_path: Path,
) -> None:
    ws = make_ws(tmp_path)

    on = fluid(["validate", ws.contract], cwd=ws.root)
    off = fluid(["validate", ws.contract], cwd=ws.root, env_extra={OFF_SWITCH: "1"})

    assert on.returncode == 0, on.output
    assert off.returncode == 1, off.output
    assert "escapes the ref root" in off.output


@pytest.mark.slow
def test_l11_sub_a_project_dotenv_has_no_effect_on_the_off_switch(tmp_path: Path) -> None:
    pytest.importorskip("dotenv")
    ws = make_ws(tmp_path)
    (ws.product / ".env").write_text(f"{OFF_SWITCH}=1\n", encoding="utf-8")
    (ws.root / ".env").write_text(f"{OFF_SWITCH}=1\n", encoding="utf-8")

    run = fluid(["validate", ws.contract], cwd=ws.product)
    assert run.returncode == 0, "a project .env switched the workspace setting off: " + run.output

    def hydrated(env_extra: Dict[str, str], dotenv: str) -> Dict[str, Optional[str]]:
        (ws.root / ".env").write_text(dotenv, encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-c", _HYDRATE, str(ws.root)],
            cwd=str(ws.root),
            env={
                **{k: v for k, v in os.environ.items() if not k.startswith("FLUID_")},
                "PYTHONPATH": str(REPO_ROOT),
                **env_extra,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])

    # A project .env cannot set it ...
    assert hydrated({}, f"{OFF_SWITCH}=1\n") == {"before": None, "after": None}
    # ... and cannot clear the operator's.
    assert hydrated({OFF_SWITCH: "1"}, f"{OFF_SWITCH}=\n") == {"before": "1", "after": "1"}


def test_l11_the_off_switch_is_a_protected_name_a_project_file_cannot_set() -> None:
    from fluid_build.credentials.protected_env import DOTENV_LOADABLE, is_protected_env_key

    assert OFF_SWITCH == "FLUID_IGNORE_WORKSPACE_REF_ROOT"
    assert is_protected_env_key(OFF_SWITCH)
    assert is_protected_env_key(OFF_SWITCH.lower()), "compared upper-cased"
    assert OFF_SWITCH not in DOTENV_LOADABLE


@pytest.mark.parametrize(
    ("value", "switched_off"),
    [("1", True), ("true", True), ("0", True), ("false", True), ("", False), ("   ", False)],
    ids=repr,
)
def test_l11_any_non_blank_value_turns_the_off_switch_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, switched_off: bool
) -> None:
    """SG-33 (the switch's semantics): not boolean-parsed. ``0`` and ``false`` switch it ON; a blank is unset."""
    ws = make_ws(tmp_path)
    monkeypatch.setenv(OFF_SWITCH, value)

    if switched_off:
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(ws.contract)
    else:
        assert owner_of(ws.contract) == TEAM


# ── L-12, L-13, L-14 ────────────────────────────────────────────────────


def test_l12_the_ref_root_argument_beats_the_workspace_even_when_narrower(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract, ref_root=ws.product)


def test_l12_the_variable_beats_the_workspace_even_when_narrower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path)
    monkeypatch.setenv("FLUID_REF_ROOT", str(ws.product))

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)


def test_l12_with_nothing_set_the_workspace_applies(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)

    assert owner_of(ws.contract) == TEAM


def test_l12_the_argument_beats_the_variable_and_an_unusable_workspace_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", ".."))
    monkeypatch.setenv("FLUID_REF_ROOT", str(ws.product))

    assert owner_of(ws.contract, ref_root=ws.root) == TEAM


def test_l13_an_ignored_variable_falls_back_to_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader_log: List[logging.LogRecord]
) -> None:
    """SG-33 (characterised): the fallback is the root the contract gets without the variable."""
    ws = make_ws(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FLUID_REF_ROOT", str(elsewhere))

    assert owner_of(ws.contract) == TEAM
    assert owner_of(ws.contract) == TEAM  # a second load

    (warning,) = ignored(loader_log, ENV_IGNORED_EVENT)  # once per value
    assert "must contain the contract" in reason(warning)
    assert str(ws.root) in warning.getMessage(), "the warning names the root that applies instead"


def test_l13_an_escape_beyond_the_workspace_root_names_the_ignored_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_ws(tmp_path, ref="../../../elsewhere/x.yaml")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.yaml").write_text("team: other\n", encoding="utf-8")
    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path / "elsewhere"))

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert err.value.ignored_ref_root_env == str(tmp_path / "elsewhere")
    assert "FLUID_REF_ROOT is set but was ignored" in str(err.value)
    assert Path(err.value.root or "") == ws.root, "the workspace's root held, not the product's"


def test_l14_an_inner_workspace_file_without_the_key_shadows_an_outer_one_that_has_it(
    tmp_path: Path,
) -> None:
    """SG-33 (characterised): the nearest file wins, so a nested one without the key widens nothing."""
    ws = make_ws(tmp_path)
    (ws.root / "products" / "fluid.workspace.yaml").write_text("name: inner\n", encoding="utf-8")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)


def test_l14_the_nearest_workspace_file_with_the_key_applies(tmp_path: Path) -> None:
    ws = make_ws(tmp_path, "name: outer\n", ref="../common/team.yaml")
    (ws.root / "products" / "common").mkdir()
    (ws.root / "products" / "common" / "team.yaml").write_text(
        yaml.safe_dump(TEAM), encoding="utf-8"
    )
    (ws.root / "products" / "fluid.workspace.yaml").write_text("ref_root: .\n", encoding="utf-8")

    assert owner_of(ws.contract) == TEAM


# ── L-15 ────────────────────────────────────────────────────────────────

_ALIAS_BOMB = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join([f'*{chr(97 + i)}'] * 9)}]\n" for i in range(9)
)
_MALFORMED_FILES: Dict[str, str] = {
    "list-root": "- not: a mapping\n",
    "invalid-yaml": "refRoot: [unclosed\n",
    "alias-bomb": _ALIAS_BOMB + "refRoot: .\n",
    "over-5-mib": "# " + "x" * (5 * 1024 * 1024 + 64) + "\nrefRoot: .\n",
}


@pytest.mark.parametrize("kind", sorted(_MALFORMED_FILES))
def test_l15_a_malformed_workspace_file_is_no_setting_and_never_a_crash(
    tmp_path: Path, kind: str
) -> None:
    ws = make_ws(tmp_path, _MALFORMED_FILES[kind])
    started = time.monotonic()

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)

    assert time.monotonic() - started < 30, "the malformed file hung the loader"


@pytest.mark.parametrize("kind", sorted(_MALFORMED_FILES))
def test_l15_a_contract_that_needs_no_wider_root_still_validates_beside_a_malformed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ws = make_ws(tmp_path, _MALFORMED_FILES[kind], ref="./team.yaml")
    (ws.product / "team.yaml").write_text(yaml.safe_dump(TEAM), encoding="utf-8")
    monkeypatch.chdir(ws.product)

    assert run_main("validate", ws.contract) == 0


@pytest.mark.parametrize("kind", sorted(_MALFORMED_FILES))
def test_l15_a_malformed_workspace_file_leaves_a_diagnostic(
    tmp_path: Path, loader_log: List[logging.LogRecord], kind: str
) -> None:
    ws = make_ws(tmp_path, _MALFORMED_FILES[kind])
    source = str(ws.root / "fluid.workspace.yaml")

    with pytest.raises(RefConfinementError) as err:
        load_contract(ws.contract)

    mentions = [
        r
        for r in loader_log
        if r.levelno >= logging.WARNING and "fluid.workspace.yaml" in r.getMessage()
    ]
    assert mentions, "nothing was logged about the unusable fluid.workspace.yaml"
    (warning,) = mentions
    assert warning.event == WS_IGNORED_EVENT  # type: ignore[attr-defined]
    assert source in warning.getMessage() and "cannot be parsed" in reason(warning)
    # The escape error says so too: a service's caller never sees the log.
    assert "The workspace's ref_root was ignored" in str(err.value), str(err.value)
    assert source in str(err.value) and "cannot be parsed" in str(err.value)
    assert "declare ref_root" not in str(err.value)


def test_l15_a_yaml_syntax_error_is_placed_not_quoted(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    """The parser's message quotes the lines it failed on; the diagnostic names where they are."""
    ws = make_ws(tmp_path, f"name: commerce\nnote: [{SECRET}, unclosed\nrefRoot: .\n")
    with pytest.raises(Exception, match=SECRET):  # control: the parser's own message quotes it
        yaml.safe_load((ws.root / "fluid.workspace.yaml").read_text(encoding="utf-8"))

    with pytest.raises(RefConfinementError) as err:
        load_contract(ws.contract)

    (warning,) = ignored(loader_log)
    assert "line " in reason(warning), reason(warning)
    for text in (warning.getMessage(), str(err.value)):
        assert SECRET not in text, text


@pytest.mark.parametrize(
    ("value", "warned"),
    [([".", "shared"], True), (42, True), (True, True), ("", True), (None, False)],
    ids=["list", "int", "bool", "empty-string", "null"],
)
def test_l15_a_ref_root_of_the_wrong_type_is_no_setting_with_a_diagnostic(
    tmp_path: Path, loader_log: List[logging.LogRecord], value: Any, warned: bool
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", value))

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)

    if warned:
        (warning,) = ignored(loader_log)
        assert "must be a directory path" in reason(warning)
    else:
        assert not ignored(loader_log), "a null is the setting being absent"


@pytest.mark.xfail(
    strict=True,
    reason="BUG L-15: fluid validate with no argument crashes (cli_unhandled_exception, "
    "\"'list' object has no attribute 'get'\") in a workspace whose fluid.workspace.yaml has a "
    "non-mapping root; the explicit-contract form of the same directory validates (pre-existing, "
    "cli/workspace_config.py load_workspace_config)",
)
def test_l15_validate_with_no_argument_survives_a_workspace_file_with_a_list_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = make_ws(tmp_path, "- not: a mapping\n", ref="./team.yaml")
    (ws.product / "team.yaml").write_text(yaml.safe_dump(TEAM), encoding="utf-8")
    monkeypatch.chdir(ws.root)

    run_main("validate")

    seen = capsys.readouterr()
    assert "cli_unhandled_exception" not in seen.out + seen.err


# ── L-16 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", ["no-refs", "pointer-refs-only"])
@pytest.mark.parametrize("env", [None, "prod"])
def test_l16_a_contract_with_no_external_refs_never_touches_the_workspace(
    tmp_path: Path, io_spy: IoSpy, variant: str, env: Optional[str]
) -> None:
    ws = make_ws(tmp_path)
    owner = "{team: base}"
    text = contract_text("orders", owner)
    if variant == "pointer-refs-only":
        text += 'description: {$ref: "#/metadata/owner"}\n'
    ws.contract.write_text(text, encoding="utf-8")
    overlays = ws.product / "overlays"
    overlays.mkdir()
    (overlays / "prod.yaml").write_text("metadata: {owner: {team: prod}}\n", encoding="utf-8")
    load_with_overlay(ws.contract, env)  # warm the lazy imports before spying
    io_spy.clear()

    load_with_overlay(ws.contract, env)

    expected = sorted(["contract.fluid.yaml"] + (["prod.yaml"] if env else []))
    assert io_spy.finder_calls == [], "the workspace was looked for"
    assert (
        sorted(io_spy.parsed_names()) == expected
    ), "each file is parsed once, none is the workspace"
    if io_spy.opens_visible:
        read = [Path(p).name for p in io_spy.files_under(ws.root)]
        assert "fluid.workspace.yaml" not in read
        assert sorted(read) == expected


def test_l16_control_a_file_ref_makes_the_loader_look_for_the_workspace(
    tmp_path: Path, io_spy: IoSpy
) -> None:
    ws = make_ws(tmp_path)

    assert owner_of(ws.contract) == TEAM

    assert io_spy.finder_calls, "the spy cannot see the workspace lookup, so L-16 proves nothing"
    assert "fluid.workspace.yaml" in io_spy.parsed_names()


# ── L-17 ────────────────────────────────────────────────────────────────


def _overlay_only_ref_ws(tmp_path: Path, workspace_yaml: str) -> Workspace:
    ws = make_ws(tmp_path, workspace_yaml)
    ws.contract.write_text(contract_text("orders", "{team: base}"), encoding="utf-8")
    overlays = ws.product / "overlays"
    overlays.mkdir()
    (overlays / "prod.yaml").write_text(
        "metadata:\n  owner: {$ref: ../../../shared/team.yaml}\n", encoding="utf-8"
    )
    return ws


def test_l17_an_overlay_only_external_ref_honours_the_workspace_ref_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _overlay_only_ref_ws(tmp_path, "refRoot: .\n")
    monkeypatch.chdir(tmp_path)

    assert run_main("validate", ws.contract, "--env", "prod") == 0
    assert load_with_overlay(ws.contract, "prod")["metadata"]["owner"] == TEAM


def test_l17_without_the_setting_the_same_overlay_ref_is_an_escape_naming_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _overlay_only_ref_ws(tmp_path, "name: commerce\n")
    monkeypatch.chdir(tmp_path)

    assert run_main("validate", ws.contract, "--env", "prod") == 1
    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_with_overlay(ws.contract, "prod")
    assert str(ws.product / "overlays" / "prod.yaml") in str(err.value)
    assert err.value.pointer == "/metadata/owner"


# ── L-18 ────────────────────────────────────────────────────────────────


def _climb_to(target: str, product: Path) -> str:
    """A relative ref from ``product`` that climbs out of the filesystem and down to ``target``."""
    return "../" * (len(product.parts) + 2) + target.lstrip("/")


def test_l18_a_ref_that_climbs_out_of_the_workspace_is_an_escape(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ws.contract.write_text(
        contract_text("orders", "{$ref: %s}" % _climb_to("etc/hosts", ws.product)),
        encoding="utf-8",
    )

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(ws.contract)

    assert Path(err.value.root or "") == ws.root, "the workspace's root held"
    assert "workspace's" in str(err.value), "the hint says where the root came from"


@pytest.mark.parametrize("how", ["environment", "argument"])
def test_l18_with_the_root_widened_to_slash_the_deny_list_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    ws = make_ws(tmp_path)
    ws.contract.write_text(
        contract_text("orders", "{$ref: %s}" % _climb_to("etc/hosts", ws.product)),
        encoding="utf-8",
    )
    kwargs: Dict[str, Any] = {}
    if how == "environment":
        monkeypatch.setenv("FLUID_REF_ROOT", os.sep)
    else:
        kwargs["ref_root"] = os.sep

    with pytest.raises(RefResolutionError, match="blocked system path"):
        load_contract(ws.contract, **kwargs)


# ── L-19 ────────────────────────────────────────────────────────────────

_HONOURED_SPELLINGS: Dict[str, str] = {
    "top-level ref_root": "ref_root: .\n",
    "top-level refRoot": "refRoot: .\n",
    "workspace.ref_root": "workspace:\n  name: commerce\n  ref_root: .\n",
    "workspace.refRoot": "workspace:\n  name: commerce\n  refRoot: .\n",
    "ref_root wins over refRoot": "ref_root: .\nrefRoot: ..\n",
}
_IGNORED_SPELLINGS: Dict[str, str] = {
    "refroot": "refroot: .\n",
    "RefRoot": "RefRoot: .\n",
    "ref-root": "ref-root: .\n",
    "REF_ROOT": "REF_ROOT: .\n",
    "reference_root": "reference_root: .\n",
    "ref_root null beats refRoot": "ref_root: null\nrefRoot: .\n",
    "top-level ref_root beside a workspace: mapping": "workspace: {name: commerce}\nref_root: .\n",
    "top-level refRoot beside a workspace: mapping": "workspace: {name: commerce}\nrefRoot: .\n",
}


@pytest.mark.parametrize("spelling", sorted(_HONOURED_SPELLINGS))
def test_l19_the_spellings_that_are_honoured(tmp_path: Path, spelling: str) -> None:
    ws = make_ws(tmp_path, _HONOURED_SPELLINGS[spelling])

    assert owner_of(ws.contract) == TEAM


@pytest.mark.parametrize("spelling", sorted(_IGNORED_SPELLINGS))
def test_l19_the_spellings_that_are_not_honoured_are_ignored_without_a_word(
    tmp_path: Path, loader_log: List[logging.LogRecord], spelling: str
) -> None:
    """SG-33 (characterised): an unread key is no setting, and nothing says so.

    The last two are the shape ``fluid init`` writes: a ``workspace:`` mapping
    with the envelope keys beside it. The ``ref_root`` the docs show at the top
    level is ignored there, with no diagnostic: it only works inside ``workspace:``.
    """
    ws = make_ws(tmp_path, _IGNORED_SPELLINGS[spelling])

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)

    assert not [r for r in loader_log if r.levelno >= logging.WARNING]


# ── L-20 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["./", "shared/..", "./.", ".//", "products/..", " . ", "./shared/../"],
    ids=[
        "dot-slash",
        "via-shared",
        "dot-dot",
        "double-slash",
        "via-products",
        "padded",
        "round-trip",
    ],
)
def test_l20_every_spelling_of_dot_is_the_workspace_directory(tmp_path: Path, value: str) -> None:
    ws = make_ws(tmp_path, setting("refRoot", value))
    assert owner_of(ws.contract) == TEAM

    # ... and it is exactly the workspace directory, not something wider.
    ws.contract.write_text(
        contract_text("orders", "{$ref: ../../../outside.yaml}"), encoding="utf-8"
    )
    with pytest.raises(RefConfinementError) as err:
        load_contract(ws.contract)
    assert Path(err.value.root or "") == ws.root


# ── L-21 ────────────────────────────────────────────────────────────────


def test_l21_a_nul_byte_in_the_ref_root_is_a_clean_refusal(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path, 'refRoot: "a\\0b"\n')

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(ws.contract)

    (warning,) = ignored(loader_log)
    assert reason(warning), "the refusal says why (the wording depends on the Python version)"


def _unstattable_ref_root(tmp_path: Path, shape: str, contract_ref: str) -> Workspace:
    """A workspace whose ``ref_root`` makes ``Path.is_dir()`` raise instead of answering."""
    if shape == "one-name":
        ws = make_ws(tmp_path, setting("refRoot", "a" * 5000), ref=contract_ref)
    elif shape == "many-segments":
        ws = make_ws(tmp_path, setting("refRoot", "/".join(["a"] * 2500)), ref=contract_ref)
    else:  # a directory the process may not enter
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            pytest.skip("permissions are not enforced here")
        ws = make_ws(tmp_path, setting("refRoot", "locked/sub"), ref=contract_ref)
        (ws.root / "locked").mkdir()
    return ws


@contextlib.contextmanager
def _locked(ws: Workspace, shape: str) -> Iterator[None]:
    locked = ws.root / "locked"
    if shape == "untraversable-directory":
        locked.chmod(0)
    try:
        yield
    finally:
        if locked.exists():
            locked.chmod(0o755)


#: Each makes ``Path.is_dir()`` raise up to Python 3.13 (3.14 answers False instead).
_UNSTATTABLE = ["one-name", "many-segments", "untraversable-directory"]


@pytest.mark.parametrize("shape", _UNSTATTABLE)
def test_l21_an_unstattable_ref_root_is_ignored_and_a_local_contract_still_loads(
    tmp_path: Path, loader_log: List[logging.LogRecord], shape: str
) -> None:
    ws = _unstattable_ref_root(tmp_path, shape, "./team.yaml")
    (ws.product / "team.yaml").write_text(yaml.safe_dump(TEAM), encoding="utf-8")

    with _locked(ws, shape):
        assert owner_of(ws.contract) == TEAM

    (warning,) = ignored(loader_log)
    assert warning.getMessage()
    assert len(reason(warning)) < 1000, "a 5000-character value is shown cut short"


@pytest.mark.parametrize("shape", _UNSTATTABLE)
def test_l21_an_unstattable_ref_root_gives_the_escape_error_not_an_oserror(
    tmp_path: Path, shape: str
) -> None:
    ws = _unstattable_ref_root(tmp_path, shape, "../../shared/team.yaml")

    with _locked(ws, shape):
        with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
            load_contract(ws.contract)
    assert "The workspace's ref_root was ignored" in str(err.value)


def test_l21_a_valid_fluid_ref_root_is_not_undone_by_an_unstattable_workspace_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader_log: List[logging.LogRecord]
) -> None:
    ws = _unstattable_ref_root(tmp_path, "one-name", "../../shared/team.yaml")
    monkeypatch.setenv("FLUID_REF_ROOT", str(ws.root))

    assert owner_of(ws.contract) == TEAM
    assert not ignored(loader_log), "a variable that applies leaves the workspace file unread"


def test_l21_the_ref_root_argument_never_reads_the_workspace_file(tmp_path: Path) -> None:
    """Why the argument is the one way out today: it does not consult the workspace."""
    ws = _unstattable_ref_root(tmp_path, "one-name", "../../shared/team.yaml")

    assert owner_of(ws.contract, ref_root=ws.root) == TEAM


# ── L-22 ────────────────────────────────────────────────────────────────


def test_l22_a_symlinked_workspace_file_is_read_through_and_anchored_where_it_was_found(
    tmp_path: Path,
) -> None:
    """SG-33 (characterised): ``.`` is the directory that holds the link, not the link's target."""
    ws = make_ws(tmp_path, None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.yaml").write_text("name: shared-config\nref_root: .\n", encoding="utf-8")
    symlink(ws.root / "fluid.workspace.yaml", elsewhere / "config.yaml")

    assert owner_of(ws.contract) == TEAM


# ── L-23 ────────────────────────────────────────────────────────────────


def test_l23_validate_with_no_argument_validates_every_product_of_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = make_ws(tmp_path, names=("orders", "customers"))
    monkeypatch.chdir(ws.root)

    assert run_main("validate") == 0

    assert "All 2 products valid" in capsys.readouterr().out


def test_l23_without_the_setting_both_products_fail_with_the_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = make_ws(tmp_path, "name: commerce\n", names=("orders", "customers"))
    monkeypatch.chdir(ws.root)

    assert run_main("validate") == 1

    out = capsys.readouterr().out
    assert "0 passed, 2 failed" in out and "escapes the ref root" in " ".join(out.split())


# ── L-24 ────────────────────────────────────────────────────────────────


def test_l24_the_in_memory_forms_are_held_to_base_dir_whatever_the_workspace_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-22 / SG-33 (characterised): refRoot and ``FLUID_REF_ROOT`` reach only the file form.

    The docs promise that ``load_contract_from_text(text, base_dir=<its dir>)``
    equals ``load_contract(<that file>)``; with a widened root it does not.
    """
    from fluid_build.api.contract import (
        ContractLoadError,
        load_contract_from_dict,
        load_contract_from_text,
    )
    from fluid_build.api.contract import load_contract as api_load_contract

    ws = make_ws(tmp_path)
    text = ws.contract.read_text(encoding="utf-8")
    assert api_load_contract(ws.contract).contract["metadata"]["owner"] == TEAM

    with pytest.raises(ContractLoadError) as from_text:
        load_contract_from_text(text, base_dir=ws.product)
    monkeypatch.setenv("FLUID_REF_ROOT", str(ws.root))
    with pytest.raises(ContractLoadError) as from_dict:
        load_contract_from_dict(yaml.safe_load(text), base_dir=ws.product)

    for err in (from_text.value, from_dict.value):
        assert err.event == "contract_ref_unresolved"
        assert "escapes the ref root" in str(err)
        assert str(ws.product) in str(err), "the message names the directory the refs are held to"


# ── L-25 ────────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="BUG L-25: _NOTED_WORKSPACE_REF_ROOT_IGNORED is keyed by the workspace file's path, "
    "so a service that loads each upload from a fresh temp directory logs one WARNING per "
    "upload and grows the set without bound (the FLUID_REF_ROOT registry is keyed on the value "
    "for exactly this reason)",
)
@pytest.mark.slow
def test_l25_a_thousand_uploads_under_refused_workspaces_log_a_bounded_number_of_warnings(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    uploads = 1000
    for i in range(uploads):
        ws = make_ws(tmp_path / f"upload-{i}", setting("refRoot", ".."))
        with pytest.raises(RefConfinementError):
            load_contract(ws.contract)

    assert (
        len(ignored(loader_log)) <= 50
    ), f"{len(ignored(loader_log))} warnings for {uploads} loads"
    assert len(loader._NOTED_WORKSPACE_REF_ROOT_IGNORED) <= 50


def test_l25_the_same_workspace_file_is_reported_once(
    tmp_path: Path, loader_log: List[logging.LogRecord]
) -> None:
    ws = make_ws(tmp_path, setting("refRoot", ".."), names=("orders", "customers"))

    for path in (*ws.contracts.values(), *ws.contracts.values()):
        with pytest.raises(RefConfinementError):
            load_contract(path)

    assert len(ignored(loader_log)) == 1


# ── L-26 ────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_l26_sub_the_generated_ci_validate_step_passes_with_no_ref_root_variable(
    tmp_path: Path,
) -> None:
    ws = make_ws(tmp_path)
    ci = tmp_path / "ci.yml"

    generated = fluid(
        [
            "generate",
            "ci",
            "--system",
            "github-actions",
            "--out",
            ci,
            "products/orders/contract.fluid.yaml",
        ],
        cwd=ws.root,
    )
    assert generated.returncode == 0, generated.output

    text = ci.read_text(encoding="utf-8")
    steps = re.findall(r"fluid validate (\S+)", text)
    assert steps, "the generated CI has no validate step to run"
    contract = re.sub(r"\$\{CONTRACT:-([^}]+)\}", r"\1", steps[0])
    assert contract == "products/orders/contract.fluid.yaml"
    assert "REF_ROOT" not in text, "the generated CI must not need the variable"

    run = fluid(["validate", contract], cwd=ws.root)  # from the repo root, in a clean env
    assert run.returncode == 0, run.output


# ── L-27 ────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_l27_sub_service_mode_refuses_the_workspace_file_an_uploaded_tarball_carries(
    tmp_path: Path,
) -> None:
    staging = make_ws(tmp_path / "staging", f"refRoot: .\nnote: {SECRET}\n")
    (staging.root / "shared" / "team.yaml").write_text(
        f"team: {SECRET}\nemail: team@example.com\n", encoding="utf-8"
    )
    upload = tmp_path / "upload.tgz"
    with tarfile.open(upload, "w:gz") as archive:
        archive.add(staging.root, arcname="project")
    service_dir = tmp_path / "service" / "upload-1"
    service_dir.mkdir(parents=True)
    with tarfile.open(upload) as archive:
        try:
            archive.extractall(service_dir, filter="data")
        except TypeError:  # Python without extraction filters
            archive.extractall(service_dir)  # nosec B202 - the archive was written above
    contract = service_dir / "project" / "products" / "orders" / "contract.fluid.yaml"

    served = fluid(["validate", contract], cwd=service_dir, env_extra={OFF_SWITCH: "1"})
    unguarded = fluid(["validate", contract], cwd=service_dir)

    assert served.returncode == 1, served.output
    assert "escapes the ref root" in served.output
    assert SECRET not in served.output, "the refused file's contents reached the output"
    assert unguarded.returncode == 0, "control: without the switch the upload's own file widens it"
