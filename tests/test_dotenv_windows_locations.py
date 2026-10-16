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

"""A project dotenv file cannot move Windows' profile, temp or shell locations.

``HOME`` and the ``XDG_*`` directories are protected because they decide where
trusted configuration is read. Windows readers use other names for the same
thing: ``%ProgramData%`` (FLUID's system config, Git for Windows' config),
``%LOCALAPPDATA%`` (the keyring's ``keyringrc.cfg``, whose ``keyring-path`` is
put on ``sys.path``), ``%APPDATA%`` (Python's user site-packages), and
``%ComSpec%`` / ``%SystemRoot%`` (the shell ``subprocess`` starts). The
temporary directory (``TMPDIR`` / ``TEMP`` / ``TMP``, read on every platform)
is where the coding agents run. Windows names are case-insensitive, so every
spelling is refused.

Readers that branch on ``os.name`` see a fake ``"nt"`` only in their own
module; nothing else in the process changes platform.
"""

from __future__ import annotations

import os
import site
import tempfile
import types
from pathlib import Path
from typing import Iterator, List

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from fluid_build.credentials.protected_env import is_protected_env_key  # noqa: E402
from tests._dotenv_pairing_support import drop_new_names, hermetic_env  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    before = hermetic_env(monkeypatch, tmp_path / "home")
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


class _WindowsOs(types.ModuleType):
    """``os`` as a Windows reader sees it: ``name == "nt"``, the rest real."""

    def __getattr__(self, attr: str) -> object:
        return getattr(os, attr)


def _windows_os() -> types.ModuleType:
    fake = _WindowsOs("os")
    fake.name = "nt"  # type: ignore[attr-defined]
    return fake


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "planted").mkdir(parents=True)
    return project


def _inside(path: object, root: Path) -> bool:
    return str(root) in str(path)


def _env_names() -> List[str]:
    return [key.upper() for key in os.environ]


@pytest.mark.parametrize(
    "name",
    [
        "ProgramData",
        "PROGRAMDATA",
        "programdata",
        "APPDATA",
        "AppData",
        "LOCALAPPDATA",
        "LocalAppData",
        "ALLUSERSPROFILE",
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "WINDIR",
        "ComSpec",
        "COMSPEC",
        "TEMP",
        "Temp",
        "TMP",
        "TMPDIR",
        "tmpdir",
    ],
)
def test_a_project_dotenv_cannot_set_a_windows_location(tmp_path: Path, name: str) -> None:
    project = _project(tmp_path)
    (project / ".env").write_text(f"{name}={project / 'planted'}\n")

    hydrate_dotenv(project)

    assert is_protected_env_key(name)
    assert name.upper() not in _env_names()


def test_the_system_config_stays_in_the_real_programdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # config_manager builds %ProgramData%\fluid\config.yaml on Windows.
    import fluid_build.config_manager as config_manager

    project = _project(tmp_path)
    (project / "planted" / "fluid").mkdir()
    (project / "planted" / "fluid" / "config.yaml").write_text("logging:\n  level: DEBUG\n")
    (project / ".env").write_text(f"ProgramData={project / 'planted'}\n")
    merged: List[tuple] = []
    monkeypatch.setattr(
        config_manager.FluidConfig,
        "_merge_config_file",
        lambda self, path, source: merged.append((source, path)),
    )

    hydrate_dotenv(project)
    monkeypatch.setattr(config_manager, "os", _windows_os())
    config_manager.FluidConfig()

    (system,) = [path for source, path in merged if source == "system"]
    assert not _inside(system, project), system


@pytest.mark.parametrize("name", ["LOCALAPPDATA", "ProgramData"])
def test_the_keyring_config_stays_in_the_real_profile(tmp_path: Path, name: str) -> None:
    # keyring reads keyringrc.cfg from here on Windows; its ``keyring-path``
    # goes on sys.path and its ``default-keyring`` is imported.
    platform_ = pytest.importorskip("keyring.util.platform_")
    project = _project(tmp_path)
    (project / "planted" / "Python Keyring").mkdir()
    (project / "planted" / "Python Keyring" / "keyringrc.cfg").write_text(
        f"[backend]\nkeyring-path={project / 'planted'}\ndefault-keyring=planted.Backend\n"
    )
    (project / ".env").write_text(f"{name}={project / 'planted'}\n")

    hydrate_dotenv(project)

    assert not _inside(platform_._config_root_Windows(), project)


def test_the_user_site_packages_stay_in_the_real_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # site puts %APPDATA%\Python\...\site-packages on sys.path on Windows and
    # runs the .pth files there, in every Python the CLI starts.
    project = _project(tmp_path)
    (project / ".env").write_text(f"APPDATA={project / 'planted'}\n")

    hydrate_dotenv(project)
    monkeypatch.setattr(site, "os", _windows_os())

    assert not _inside(site._getuserbase(), project)


@pytest.mark.parametrize("name", ["TMPDIR", "TEMP", "TMP"])
def test_the_coding_agents_do_not_run_in_a_project_chosen_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # llm/coding_agent.py runs the agent CLI in tempfile.gettempdir(), and
    # Claude Code loads the .claude/settings.json hooks it finds there.
    from fluid_build.llm import coding_agent

    project = _project(tmp_path)
    (project / "planted" / ".claude").mkdir()
    (project / "planted" / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
    (project / ".env").write_text(f"{name}={project / 'planted'}\n")
    monkeypatch.setattr(tempfile, "tempdir", None)

    hydrate_dotenv(project)

    assert not any(_inside(d, project) for d in tempfile._candidate_tempdir_list())  # type: ignore[attr-defined]
    assert not _inside(coding_agent._scratch_cwd(), project)
