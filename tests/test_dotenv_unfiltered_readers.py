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

"""Nothing reads the project's dotenv files around the filter.

``credentials.dotenv_store`` drops the names a project file may not set. Two
tools FLUID runs read ``.env`` on their own with python-dotenv: dbt loads the
nearest one when it starts (``find_dotenv(usecwd=True)``), and litellm loads
one when it is imported, found by walking up from its install directory, so a
virtualenv inside the project finds the project's. Both would put back what
the filter dropped. Once the CLI has read the project's files, and from the
start of every CLI process, ``PYTHON_DOTENV_DISABLED`` turns that off.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    drop_new_names,
    hermetic_env,
)

PACKAGE = Path(__file__).resolve().parents[1] / "fluid_build"
_REFUSED = ("PGHOST", "HTTPS_PROXY")
_PROJECT_ENV = "PGHOST=pg.project.test\nHTTPS_PROXY=http://proxy.project.test:3128\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    before = hermetic_env(monkeypatch, tmp_path / "home")
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # PGHOST is refused because the operator's own password is in play,
    # HTTPS_PROXY because a project file never sets it.
    monkeypatch.setenv("PGPASSWORD", OPERATOR_KEY)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text(_PROJECT_ENV)
    return root


def _child_sees(code: str, *, cwd: Path, extra_path: Optional[Path] = None) -> Dict[str, str]:
    """Run ``code`` in a fresh interpreter and report the refused names it sees."""
    env = dict(os.environ)
    if extra_path is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(extra_path), env.get("PYTHONPATH", "")) if p
        )
    report = (
        "\nimport json, os\n"
        f"print(json.dumps({{k: os.environ[k] for k in {list(_REFUSED)!r} if k in os.environ}}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code + report],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _litellm_inside(project: Path) -> Path:
    """litellm as a virtualenv inside the project has it: under the project."""
    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("litellm not installed")
    site = project / ".venv" / "lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "litellm").symlink_to(list(spec.submodule_search_locations)[0])
    return site


def test_dbt_does_not_reload_the_project_dotenv(project: Path) -> None:
    # dbt.cli.main runs ``load_dotenv(find_dotenv(usecwd=True))`` on import,
    # and fluid runs dbt with the contract's directory as its cwd.
    if importlib.util.find_spec("dbt.cli") is None:
        pytest.skip("dbt-core not installed")

    hydrate_dotenv(project)
    seen = _child_sees("import dbt.cli.main", cwd=project)

    assert seen == {}


def test_litellm_does_not_reload_the_project_dotenv_after_hydration(project: Path) -> None:
    site = _litellm_inside(project)

    hydrate_dotenv(project)
    seen = _child_sees("import litellm", cwd=project, extra_path=site)

    assert seen == {}


def test_a_cli_process_never_lets_litellm_load_the_project_dotenv(project: Path) -> None:
    # ``fluid forge`` imports litellm without hydrating the project's files
    # first, so the switch has to be on from the start of the process.
    site = _litellm_inside(project)
    code = (
        "from fluid_build.cli import main\n"
        "try:\n"
        "    main(['--version'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "import litellm\n"
    )

    seen = _child_sees(code, cwd=project, extra_path=site)

    assert seen == {}


def test_an_operator_s_own_switch_is_kept(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")

    hydrate_dotenv(project)

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "0"


def test_a_project_dotenv_cannot_turn_the_switch_back_on(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("PYTHON_DOTENV_DISABLED=0\nLITELLM_MODE=DEV\n")

    hydrate_dotenv(root)

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert "LITELLM_MODE" not in os.environ


def test_the_operator_secrets_file_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hydrate_dotenv loads FLUID_SECRETS_FILE last; with python-dotenv's loader
    # switched off it exports the values itself, and they still win.
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text(f"OPENAI_API_KEY={PROJECT_KEY}\nFLUID_LOG_LEVEL=INFO\n")
    secrets = tmp_path / "operator.env"
    secrets.write_text(f"OPENAI_API_KEY={OPERATOR_KEY}\nDMM_API_KEY=${{OPENAI_API_KEY}}\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    hydrate_dotenv(root)

    assert os.environ["OPENAI_API_KEY"] == OPERATOR_KEY
    assert os.environ["DMM_API_KEY"] == OPERATOR_KEY
    assert os.environ["FLUID_LOG_LEVEL"] == "INFO"


def test_dbt_in_docker_gets_the_switch(project: Path) -> None:
    # The dbt container mounts the project, so its dbt would find the .env too.
    from fluid_build.build_runners.dbt.runner import _collect_dbt_container_env

    hydrate_dotenv(project)

    assert _collect_dbt_container_env().get("PYTHON_DOTENV_DISABLED") == "1"


def test_dbt_in_docker_gets_what_the_project_files_set_after_the_filter(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # dbt in the container used to read the mounted .env itself; with the
    # switch on it gets the project's settings from the CLI, filtered.
    from fluid_build.build_runners.dbt.runner import (
        _build_containerized_dbt_command,
        _collect_dbt_container_env,
    )

    secrets = tmp_path / "operator.env"
    secrets.write_text("REPLACED_BY_OPERATOR=operator\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("SHELL_ONLY_SETTING", "operator")
    with (project / ".env").open("a") as env_file:
        env_file.write(
            "TARGET_SCHEMA=analytics\nEMPTY_SETTING=\nSHELL_ONLY_SETTING=project\n"  # pragma: allowlist secret
            "REPLACED_BY_OPERATOR=project\n"
        )

    hydrate_dotenv(project)
    forwarded = _collect_dbt_container_env()
    cmd = _build_containerized_dbt_command("duckdb", ["build"], project, None)

    assert forwarded["TARGET_SCHEMA"] == "analytics"
    assert forwarded["EMPTY_SETTING"] == ""
    assert cmd[cmd.index("TARGET_SCHEMA") - 1] == "-e"
    assert "analytics" not in " ".join(cmd)
    # refused and protected names stay out, and so does a value the operator
    # set (in the shell or FLUID_SECRETS_FILE) that is not on the forward list
    for name in (*_REFUSED, "SHELL_ONLY_SETTING", "REPLACED_BY_OPERATOR"):
        assert name not in forwarded, name


def test_containerised_dbt_resolves_a_project_env_var(project: Path, tmp_path: Path) -> None:
    # What dbt sees in the container: only the forwarded names, the mounted
    # project as its working directory, and the switch on.
    pytest.importorskip("dbt.cli.main")
    from fluid_build.build_runners.dbt.runner import _collect_dbt_container_env

    (project / ".env").write_text("TARGET_SCHEMA=analytics\n")
    hydrate_dotenv(project)
    container_env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HOME": str(tmp_path / "home"),
        **_collect_dbt_container_env(),
    }

    done = subprocess.run(
        [sys.executable, "-c", "import os, dbt.cli.main; print(os.environ.get('TARGET_SCHEMA'))"],
        cwd=project,
        env=container_env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert container_env["PYTHON_DOTENV_DISABLED"] == "1"
    assert done.stdout.strip().splitlines()[-1] == "analytics"


# python-dotenv added PYTHON_DOTENV_DISABLED in 1.2.0: 1.0.x and 1.1.x load
# whatever it says, and litellm (python-dotenv>=1.0.0) alone resolves to them.
_FIRST_DOTENV_WITH_THE_SWITCH = "1.2.0"


def test_the_python_dotenv_floor_has_the_switch() -> None:
    specifiers = pytest.importorskip("packaging.specifiers")
    text = (PACKAGE.parent / "pyproject.toml").read_text(encoding="utf-8")
    (floor,) = re.findall(r'"python-dotenv([^";]*)"', text)

    allowed = specifiers.SpecifierSet(floor)
    assert _FIRST_DOTENV_WITH_THE_SWITCH in allowed
    for older in ("1.0.0", "1.0.1", "1.1.0", "1.1.1"):
        assert older not in allowed, older


def test_the_installed_python_dotenv_honours_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dotenv import load_dotenv

    probe = tmp_path / "probe.env"
    probe.write_text("PROBE_SETTING=loaded\n")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    assert load_dotenv(probe) is False
    assert "PROBE_SETTING" not in os.environ


def test_only_the_dotenv_store_and_the_secrets_file_read_dotenv_files() -> None:
    # A new reader would bypass the filter; route it through
    # credentials.dotenv_store instead.
    readers: List[str] = []
    call = re.compile(r"\b(?:load_dotenv|dotenv_values|find_dotenv)\s*\(")
    for path in sorted(PACKAGE.rglob("*.py")):
        if call.search(path.read_text(encoding="utf-8")):
            readers.append(path.relative_to(PACKAGE).as_posix())

    # forge/federation.py reads only the key names of project dotenv files, so
    # it can refuse a federation policy variable that came from one; it never
    # exports a value. It is on the federation credentials fix branch.
    allowed = {"cli/_common.py", "credentials/dotenv_store.py", "forge/federation.py"}
    assert {"cli/_common.py", "credentials/dotenv_store.py"} <= set(readers)
    assert set(readers) <= allowed, sorted(set(readers) - allowed)


# ── the project's own code is not dbt or litellm ───────────────────────────
#
# A python build step is the checkout's own code: it may load a dotenv file
# of its own with python-dotenv, and the switch would make that a silent
# no-op while protecting nothing.

_STEP = (
    "import json, os\n"
    "from pathlib import Path\n"
    "from dotenv import load_dotenv\n"
    "loaded = load_dotenv('config/app.env')\n"
    "Path('seen.json').write_text(json.dumps({'loaded': loaded,"
    " 'APP_SETTING': os.environ.get('APP_SETTING'),"
    " 'switch': os.environ.get('PYTHON_DOTENV_DISABLED')}))\n"
)


def _python_build_project(root: Path) -> Path:
    root.mkdir()
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "demo.own_dotenv",
        "name": "Own dotenv",
        "description": "a python build step that loads its own config",
        "domain": "Demo",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "hybrid-reference",
                "engine": "python",
                "repository": "./",
                "properties": {"model": "ingest"},
                "execution": {
                    "trigger": {"type": "manual", "iterations": 1, "delaySeconds": 0},
                    "runtime": {"platform": "local"},
                },
                "outputs": ["rows"],
            }
        ],
        "exposes": [
            {
                "exposeId": "rows",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "./out/rows.parquet"},
                },
                "contract": {"schema": [{"name": "id", "type": "INTEGER", "required": True}]},
            }
        ],
    }
    (root / "contract.fluid.yaml").write_text(json.dumps(contract), encoding="utf-8")
    (root / "ingest.py").write_text(_STEP)
    (root / "config").mkdir()
    (root / "config" / "app.env").write_text("APP_SETTING=from-app-env\n")
    # the project's .env still goes through the filter for fluid itself
    (root / ".env").write_text(_PROJECT_ENV)
    return root


@pytest.mark.parametrize(
    "operator_switch,loads",
    [(None, True), ("1", False), ("0", True)],
    ids=["fluid-set", "operator-1", "operator-0"],
)
def test_a_python_build_step_loads_its_own_dotenv_unless_the_operator_said_not_to(
    tmp_path: Path, operator_switch: Optional[str], loads: bool
) -> None:
    project = _python_build_project(tmp_path / "proj")
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HOME": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
        "FLUID_NONINTERACTIVE": "1",
        "PGPASSWORD": OPERATOR_KEY,
    }
    if operator_switch is not None:
        env["PYTHON_DOTENV_DISABLED"] = operator_switch

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "apply",
            "contract.fluid.yaml",
            "--mode",
            "amend-and-build",
            "--yes",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    seen_file = project / "seen.json"
    assert seen_file.exists(), (done.stdout + done.stderr)[-3000:]
    seen = json.loads(seen_file.read_text())
    assert seen["switch"] == operator_switch
    assert seen["loaded"] is loads
    assert seen["APP_SETTING"] == ("from-app-env" if loads else None)


def test_dbt_keeps_the_switch_the_project_s_own_code_does_not(project: Path) -> None:
    from fluid_build.build_runners.dbt.runner import _collect_dbt_container_env
    from fluid_build.credentials.dotenv_store import environment_for_project_code

    hydrate_dotenv(project)

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert _collect_dbt_container_env()["PYTHON_DOTENV_DISABLED"] == "1"
    assert "PYTHON_DOTENV_DISABLED" not in environment_for_project_code()
    # what the filter refused is not in the step's environment either
    assert not set(_REFUSED) & set(environment_for_project_code())


def test_a_switch_from_the_operator_s_secrets_file_reaches_the_project_s_code(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.credentials.dotenv_store import environment_for_project_code

    secrets = tmp_path / "operator.env"
    secrets.write_text("PYTHON_DOTENV_DISABLED=1\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))

    hydrate_dotenv(project)

    assert environment_for_project_code()["PYTHON_DOTENV_DISABLED"] == "1"


def test_fluid_ship_stages_set_the_switch_themselves(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each stage is a fluid process whose main() turns the switch on and so
    # knows it is its own; inherited, it would pass it on to a python step.
    import logging

    from fluid_build.cli import ship

    seen: List[Optional[str]] = []

    def run(cmd: List[str], **kwargs: object) -> subprocess.CompletedProcess:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        seen.append(env.get("PYTHON_DOTENV_DISABLED"))
        return subprocess.CompletedProcess(cmd, 0)

    hydrate_dotenv(project)
    monkeypatch.setattr(ship.subprocess, "run", run)
    ship._run_stage(["fluid"], "apply", [], logging.getLogger("test"))

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert seen == [None]
