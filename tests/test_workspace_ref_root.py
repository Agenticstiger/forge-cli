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

"""``ref_root`` in ``fluid.workspace.yaml`` widens the ``$ref`` root, within its own tree.

A workspace that keeps fragments shared by its products (a team, glossary
terms) declares the root once instead of every process setting
``FLUID_REF_ROOT``. A project file must not reach outside its own tree, so the
root stays at or below the workspace file's directory, contains the contract,
and passes the DuckDB sandbox's workspace vetting (not a home, a temp root or
the filesystem root, not above the contract's repository). ``ref_root=`` and
``FLUID_REF_ROOT`` still win; ``FLUID_IGNORE_WORKSPACE_REF_ROOT`` turns it off.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from fluid_build import loader
from fluid_build.loader import RefConfinementError, load_contract

_CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.orders
name: Orders
domain: commerce
metadata:
  layer: Silver
  owner: {$ref: ../../shared/team.yaml}
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
        - {name: order_id, type: string, required: true}
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    loader._NOTED_WORKSPACE_REF_ROOT_IGNORED.clear()
    loader._NOTED_REF_ROOT_ENV_IGNORED.clear()
    yield


def _workspace(tmp_path: Path, setting: str = "ref_root: .") -> Path:
    ws = tmp_path / "ws"
    (ws / "shared").mkdir(parents=True)
    (ws / "shared" / "team.yaml").write_text(
        "team: commerce-data\nemail: commerce@example.com\n", encoding="utf-8"
    )
    (ws / "fluid.workspace.yaml").write_text(f"name: commerce\n{setting}\n", encoding="utf-8")
    product = ws / "products" / "orders"
    product.mkdir(parents=True)
    (product / "contract.fluid.yaml").write_text(_CONTRACT, encoding="utf-8")
    return product / "contract.fluid.yaml"


def _owner(path: Path) -> Any:
    return load_contract(path)["metadata"]["owner"]


def test_validate_passes_from_any_directory_with_no_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    path = _workspace(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    for cwd in (tmp_path / "ws", path.parent, elsewhere):
        monkeypatch.chdir(cwd)
        assert main(["validate", str(path)]) == 0, cwd


def test_the_workspace_root_composes_a_shared_fragment(tmp_path: Path) -> None:
    assert _owner(_workspace(tmp_path)) == {
        "team": "commerce-data",
        "email": "commerce@example.com",
    }


@pytest.mark.parametrize("setting", ["refRoot: .", "workspace: {ref_root: .}"])
def test_the_spellings_the_workspace_file_already_uses(tmp_path: Path, setting: str) -> None:
    assert _owner(_workspace(tmp_path, setting))["team"] == "commerce-data"


def test_without_the_setting_the_contracts_directory_is_the_root(tmp_path: Path) -> None:
    path = _workspace(tmp_path, "domain: commerce")

    with pytest.raises(RefConfinementError, match="escapes the ref root") as err:
        load_contract(path)
    assert "declare ref_root in the workspace's fluid.workspace.yaml" in str(err.value)


@pytest.mark.parametrize(
    ("setting", "why"),
    [
        ("ref_root: ..", "outside the workspace directory"),
        ("ref_root: /", "must be a path relative to the workspace file's directory"),
        ("ref_root: ~/", "must be a path relative to the workspace file's directory"),
        ("ref_root: shared", "does not contain the contract"),
        ("ref_root: missing", "is not a directory"),
        ("ref_root: 42", "must be a directory path relative to the workspace file"),
    ],
)
def test_a_root_a_workspace_file_may_not_declare_is_ignored(
    tmp_path: Path, setting: str, why: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = _workspace(tmp_path, setting)

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(path)

    warned = [
        r.getMessage() for r in caplog.records if "workspace_ref_root_ignored" in r.getMessage()
    ]
    assert len(warned) == 1 and why in warned[0]


def test_a_workspace_above_the_contracts_repository_widens_nothing(tmp_path: Path) -> None:
    """A fluid.workspace.yaml in ~/src would otherwise reach every clone under it."""
    ws = tmp_path / "src"
    (ws / "shared").mkdir(parents=True)
    (ws / "shared" / "team.yaml").write_text("team: other\n", encoding="utf-8")
    (ws / "fluid.workspace.yaml").write_text("ref_root: .\n", encoding="utf-8")
    product = ws / "repo" / "products" / "orders"
    product.mkdir(parents=True)
    (ws / "repo" / ".git").mkdir()
    path = product / "contract.fluid.yaml"
    path.write_text(_CONTRACT.replace("../../shared", "../../../shared"), encoding="utf-8")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)


def test_a_workspace_at_a_home_directory_widens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.providers import _duckdb_sandbox

    path = _workspace(tmp_path)
    monkeypatch.setattr(_duckdb_sandbox, "_home_dirs", lambda: (str((tmp_path / "ws").resolve()),))

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)


def test_an_operator_can_switch_it_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.credentials.protected_env import is_protected_env_key

    path = _workspace(tmp_path)
    monkeypatch.setenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, "1")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)
    # A project .env cannot switch it back on for a service that turned it off.
    assert is_protected_env_key(loader.IGNORE_WORKSPACE_REF_ROOT_ENV)


def test_the_variable_and_the_argument_still_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _workspace(tmp_path, "ref_root: products")
    # The workspace root (products/) does not hold shared/: only a wider one does.
    with pytest.raises(RefConfinementError):
        load_contract(path)

    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path / "ws"))
    assert _owner(path)["team"] == "commerce-data"
    monkeypatch.delenv("FLUID_REF_ROOT")
    assert load_contract(path, ref_root=tmp_path / "ws")["metadata"]["owner"]["team"] == (
        "commerce-data"
    )


def test_an_ignored_variable_falls_back_to_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _workspace(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FLUID_REF_ROOT", str(elsewhere))

    assert _owner(path)["team"] == "commerce-data"


def test_a_contract_without_file_refs_never_reads_the_workspace(tmp_path: Path) -> None:
    path = _workspace(tmp_path)
    (tmp_path / "ws" / "fluid.workspace.yaml").write_text("- not: a mapping\n", encoding="utf-8")
    path.write_text(_CONTRACT.replace("{$ref: ../../shared/team.yaml}", "{team: t}"), "utf-8")

    assert load_contract(path)["metadata"]["owner"] == {"team": "t"}


def test_an_unreadable_workspace_file_declares_nothing(tmp_path: Path) -> None:
    path = _workspace(tmp_path)
    (tmp_path / "ws" / "fluid.workspace.yaml").write_text("- not: a mapping\n", encoding="utf-8")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)
