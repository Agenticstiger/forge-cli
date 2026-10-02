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

"""``$ref`` targets are confined to the root contract's directory tree.

Before this, a relative ``$ref`` was joined to the contract's directory with
``../`` honoured, and only absolute paths and system directories were blocked,
so a contract could compose any dict-rooted YAML/JSON file on the host into
itself — and ``fluid bundle`` printed it back. Every test below builds the
layout on disk and goes through the public loader entry points (or the real
``fluid`` CLI), with a secret file sitting just outside the contract directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fluid_build.loader import (
    REF_ROOT_ENV,
    RefConfinementError,
    RefResolutionError,
    compile_contract,
    load_contract,
    load_with_overlay,
)

SECRET = "SUPER-SECRET-VALUE"


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """``tmp/outside/secret.yaml`` next to ``tmp/proj/`` (the contract dir)."""
    monkeypatch.delenv(REF_ROOT_ENV, raising=False)
    _write(tmp_path / "outside" / "secret.yaml", {"api_key": SECRET})
    proj = tmp_path / "proj"
    proj.mkdir()
    return tmp_path, proj


def _contract(proj: Path, ref: str, name: str = "contract.fluid.yaml") -> Path:
    return _write(proj / name, {"id": "p", "labels": {"$ref": ref}})


def _symlink(link: Path, target: Path, *, is_dir: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):  # Windows without symlink privilege
        pytest.skip("symlinks unavailable on this platform")


# ---------------------------------------------------------------------------
# Escapes are refused — by every entry point
# ---------------------------------------------------------------------------


class TestEscapesRefused:
    @pytest.mark.parametrize("entry", ["load_contract", "compile_contract", "load_with_overlay"])
    def test_dotdot_escape_refused_with_ref_and_pointer(self, layout, entry):
        _, proj = layout
        contract = _contract(proj, "../outside/secret.yaml")
        fn = {
            "load_contract": load_contract,
            "compile_contract": compile_contract,
            "load_with_overlay": load_with_overlay,
        }[entry]
        with pytest.raises(RefConfinementError) as excinfo:
            fn(contract)
        err = excinfo.value
        msg = str(err)
        assert "'../outside/secret.yaml'" in msg
        assert "JSON pointer '/labels'" in msg
        assert "escapes the ref root" in msg
        assert SECRET not in msg
        assert err.ref == "../outside/secret.yaml"
        assert err.pointer == "/labels"
        assert err.source == str(contract.resolve())
        # Still the loader's typed error, so existing handlers catch it.
        assert isinstance(err, RefResolutionError)

    def test_absolute_path_refused_even_inside_root(self, layout):
        _, proj = layout
        inside = _write(proj / "frag.yaml", {"a": 1})
        contract = _contract(proj, str(inside.resolve()))
        with pytest.raises(RefConfinementError, match="must be a relative path"):
            load_contract(contract)

    @pytest.mark.parametrize(
        "ref",
        [
            "C:\\Windows\\win.ini",
            "C:relative-to-drive.yaml",
            "\\\\server\\share\\x.yaml",
            "\\rooted.yaml",
        ],
    )
    def test_windows_shaped_absolute_paths_refused(self, layout, ref):
        _, proj = layout
        with pytest.raises(RefConfinementError, match="must be a relative path"):
            load_contract(_contract(proj, ref))

    def test_file_url_refused_even_when_target_is_inside_root(self, layout):
        _, proj = layout
        inside = _write(proj / "frag.yaml", {"a": 1})
        contract = _contract(proj, inside.resolve().as_uri())
        with pytest.raises(RefConfinementError, match="is a URL"):
            load_contract(contract)

    @pytest.mark.parametrize(
        "ref",
        [
            "http://169.254.169.254/latest/meta-data/x.yaml",
            "https://example.com/frag.yaml#/x",
            "s3://bucket/frag.yaml",
            "FILE:///etc/hosts",
            "//example.com/frag.yaml",
        ],
    )
    def test_remote_refs_refused(self, layout, ref):
        _, proj = layout
        with pytest.raises(RefConfinementError, match="is a URL"):
            load_contract(_contract(proj, ref))

    def test_symlinked_file_pointing_out_is_refused(self, layout):
        tmp, proj = layout
        _symlink(proj / "innocent.yaml", tmp / "outside" / "secret.yaml")
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(_contract(proj, "./innocent.yaml"))

    def test_symlinked_directory_pointing_out_is_refused(self, layout):
        tmp, proj = layout
        _symlink(proj / "shared", tmp / "outside", is_dir=True)
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(_contract(proj, "./shared/secret.yaml"))

    def test_nested_ref_from_subdirectory_is_held_to_the_root(self, layout):
        """``sub/frag.yaml`` refs ``../../outside/...``: relative to the
        fragment that is only one level out of the root — still refused, and
        the error names the fragment and the pointer inside it."""
        _, proj = layout
        frag = _write(
            proj / "sub" / "frag.yaml",
            {"inner": [{"$ref": "../../outside/secret.yaml"}]},
        )
        contract = _contract(proj, "./sub/frag.yaml")
        with pytest.raises(RefConfinementError) as excinfo:
            load_contract(contract)
        assert excinfo.value.source == str(frag.resolve())
        assert excinfo.value.pointer == "/inner/0"

    def test_nested_ref_location_follows_the_fragment_pointer(self, layout):
        _, proj = layout
        _write(
            proj / "frag.yaml",
            {"section": {"deep": {"$ref": "../outside/secret.yaml"}}},
        )
        contract = _contract(proj, "./frag.yaml#/section")
        with pytest.raises(RefConfinementError) as excinfo:
            load_contract(contract)
        assert excinfo.value.pointer == "/section/deep"

    def test_refusal_does_not_reveal_whether_the_target_exists(self, layout):
        """Confinement runs before the existence check: an escaping ref to a
        file that does not exist fails the same way as one that does."""
        _, proj = layout
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(_contract(proj, "../outside/no-such-file.yaml"))


# ---------------------------------------------------------------------------
# Legitimate composition keeps working
# ---------------------------------------------------------------------------


class TestLegitimateRefsStillResolve:
    def test_sibling_and_subdirectory_refs(self, layout):
        _, proj = layout
        _write(proj / "owner.yaml", {"team": "data"})
        _write(proj / "fragments" / "builds" / "ingest.yaml", {"id": "ingest"})
        contract = _write(
            proj / "contract.fluid.yaml",
            {
                "owner": {"$ref": "./owner.yaml"},
                "builds": [{"$ref": "fragments/builds/ingest.yaml"}],
            },
        )
        result = load_contract(contract)
        assert result == {"owner": {"team": "data"}, "builds": [{"id": "ingest"}]}

    def test_nested_ref_climbing_back_inside_root(self, layout):
        """A fragment in ``sub/`` may ref ``../common.yaml``: relative to the
        fragment, inside the root — allowed."""
        _, proj = layout
        _write(proj / "common.yaml", {"tier": "gold"})
        _write(proj / "sub" / "frag.yaml", {"common": {"$ref": "../common.yaml"}})
        result = load_contract(_contract(proj, "./sub/frag.yaml"))
        assert result["labels"] == {"common": {"tier": "gold"}}

    def test_symlink_that_stays_inside_root(self, layout):
        _, proj = layout
        real = _write(proj / "real" / "frag.yaml", {"ok": True})
        _symlink(proj / "alias.yaml", real)
        assert load_contract(_contract(proj, "./alias.yaml"))["labels"] == {"ok": True}

    def test_contract_reached_through_a_symlinked_directory(self, tmp_path, monkeypatch):
        """The root is resolved too, so a contract opened via a symlinked
        path (``/tmp`` → ``/private/tmp`` on macOS) is not refused."""
        monkeypatch.delenv(REF_ROOT_ENV, raising=False)
        real = tmp_path / "real"
        _write(real / "frag.yaml", {"ok": True})
        _contract(real, "./frag.yaml")
        _symlink(tmp_path / "link", real, is_dir=True)
        result = load_contract(tmp_path / "link" / "contract.fluid.yaml")
        assert result["labels"] == {"ok": True}

    def test_same_document_refs_are_left_in_place(self, layout):
        _, proj = layout
        contract = _write(
            proj / "contract.fluid.yaml",
            {"defs": {"x": 1}, "use": {"$ref": "#/defs/x"}, "whole": {"$ref": "#"}},
        )
        result = load_contract(contract)
        assert result["use"] == {"$ref": "#/defs/x"}
        assert result["whole"] == {"$ref": "#"}


# ---------------------------------------------------------------------------
# The explicit opt-in: a caller-set ref root
# ---------------------------------------------------------------------------


class TestRefRootOptIn:
    def _monorepo(self, tmp: Path) -> Path:
        _write(tmp / "shared" / "policy.yaml", {"classification": "Internal"})
        product = tmp / "products" / "orders"
        return _contract(product, "../../shared/policy.yaml")

    def test_ref_root_argument_widens_the_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv(REF_ROOT_ENV, raising=False)
        contract = self._monorepo(tmp_path)
        with pytest.raises(RefConfinementError):
            load_contract(contract)
        result = load_contract(contract, ref_root=tmp_path)
        assert result["labels"] == {"classification": "Internal"}
        assert compile_contract(contract, ref_root=tmp_path)["labels"] == result["labels"]
        assert load_with_overlay(contract, ref_root=tmp_path)["labels"] == result["labels"]

    def test_env_var_widens_the_root(self, tmp_path, monkeypatch):
        contract = self._monorepo(tmp_path)
        monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path))
        assert load_contract(contract)["labels"] == {"classification": "Internal"}

    def test_widened_root_still_confines(self, tmp_path, monkeypatch):
        _write(tmp_path / "secret.yaml", {"api_key": SECRET})
        repo = tmp_path / "repo"
        contract = _contract(repo / "products" / "orders", "../../../secret.yaml")
        monkeypatch.setenv(REF_ROOT_ENV, str(repo))
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            load_contract(contract)

    def test_blank_env_var_means_the_confined_default(self, tmp_path, monkeypatch):
        contract = self._monorepo(tmp_path)
        monkeypatch.setenv(REF_ROOT_ENV, "   ")
        with pytest.raises(RefConfinementError):
            load_contract(contract)

    def test_root_that_does_not_contain_the_contract_is_rejected(self, tmp_path, monkeypatch):
        contract = self._monorepo(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv(REF_ROOT_ENV, str(elsewhere))
        with pytest.raises(RefResolutionError, match="the ref root must contain the contract"):
            load_contract(contract)

    def test_root_that_is_not_a_directory_is_rejected(self, tmp_path, monkeypatch):
        contract = self._monorepo(tmp_path)
        with pytest.raises(RefResolutionError, match="is not a directory"):
            load_contract(contract, ref_root=tmp_path / "missing")
        monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path / "missing"))
        with pytest.raises(RefResolutionError, match=f"{REF_ROOT_ENV}=.* is not a directory"):
            load_contract(contract)

    def test_stale_env_var_does_not_break_ref_free_contracts(self, tmp_path, monkeypatch):
        monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path / "missing"))
        contract = _write(tmp_path / "c.yaml", {"id": "p", "same": {"$ref": "#/id"}})
        assert load_contract(contract)["id"] == "p"

    def test_url_refs_stay_refused_under_the_opt_in(self, tmp_path, monkeypatch):
        contract = _contract(tmp_path / "p", "file:///etc/hosts")
        monkeypatch.setenv(REF_ROOT_ENV, str(tmp_path))
        with pytest.raises(RefConfinementError, match="is a URL"):
            load_contract(contract)


# ---------------------------------------------------------------------------
# The real CLI
# ---------------------------------------------------------------------------


def _fluid(*args: str, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop(REF_ROOT_ENV, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


_VALID_CONTRACT = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "example.ref_confinement",
    "name": "Ref confinement",
    "domain": "example",
    "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@example.com"}},
    "labels": {"$ref": "../outside/secret.yaml"},
    "exposes": [
        {
            "exposeId": "out",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": "runtime/out/x.csv"},
            },
            "contract": {"schema": [{"name": "message", "type": "string"}]},
        }
    ],
}


@pytest.mark.integration
class TestCli:
    def test_validate_refuses_an_escaping_contract(self, layout):
        _, proj = layout
        contract = _write(proj / "contract.fluid.yaml", _VALID_CONTRACT)
        result = _fluid("validate", str(contract), cwd=proj)
        # The Rich error panel wraps long lines; compare whitespace-normalised.
        out = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, out
        assert "../outside/secret.yaml" in out
        assert "escapes the ref root" in out
        assert SECRET not in out

    def test_bundle_does_not_print_the_outside_file(self, layout):
        _, proj = layout
        contract = _write(proj / "contract.fluid.yaml", _VALID_CONTRACT)
        result = _fluid("bundle", str(contract), cwd=proj)
        out = result.stdout + result.stderr
        assert result.returncode == 2, out
        assert "escapes the ref root" in out
        assert SECRET not in out

    def test_validate_accepts_it_under_the_documented_opt_in(self, layout):
        tmp, proj = layout
        contract = _write(proj / "contract.fluid.yaml", _VALID_CONTRACT)
        result = _fluid("validate", str(contract), cwd=proj, env_extra={REF_ROOT_ENV: str(tmp)})
        assert result.returncode == 0, result.stdout + result.stderr
