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

"""``fluid split`` in real processes: what needs isolation (catalog I-31 and friends).

* I-31: the splitter needs no ``ruamel`` (blocked here), and importing it
  imports none.
* The command as a user runs it: ``python -m fluid_build.cli split``, then
  ``bundle``, in a process that imports THIS checkout.
* I-18: an extension validator (an entry point, so it needs a real
  ``sys.path``) still sees an extension block that was moved to a file.
* Compatibility: the v0.24.0 loader, extracted from the release tag, reads
  what the default split writes to the same documents.

Every process gets an explicit ``PYTHONPATH`` (the checkout), cwd, ``HOME`` and
timeout, and no ambient ``FLUID_*`` setting.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
import yaml

from tests.fragments.test_split import (
    ALL_PARTS,
    DEFAULT_LAYOUT,
    NAME,
    PROD_OVERLAY,
    SPLIT_ALL,
    digest,
    files_of,
    write,
)

REPO = Path(__file__).resolve().parents[2]
FILES: Dict[str, str] = {NAME: SPLIT_ALL, "overlays/prod.yaml": PROD_OVERLAY}


def run_fluid(
    args: Sequence[Any],
    cwd: Path,
    *,
    checkout: Path = REPO,
    extra_path: Sequence[Path] = (),
    timeout: int = 240,
) -> "subprocess.CompletedProcess[str]":
    """``python -m fluid_build.cli <args>`` in ``cwd``, importing ``checkout``.

    HOME is a directory beside ``cwd``, never inside it: a file the CLI keeps
    there must not show up in a tree the test compares.
    """
    home = cwd.parent / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLUID_", "PYTHON"))}
    env.update(
        PYTHONPATH=os.pathsep.join(str(p) for p in (checkout, *extra_path)),
        HOME=str(home),
        NO_COLOR="1",
        COLUMNS="200",
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *map(str, args)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def make_trees(tmp_path: Path, files: Dict[str, str] = FILES) -> "tuple[Path, Path]":
    flat, split = tmp_path / "flat", tmp_path / "split"
    for side in (flat, split):
        for rel, content in files.items():
            write(side / rel, content)
    return flat, split


# ── I-31 ────────────────────────────────────────────────────────────────

_NO_RUAMEL = textwrap.dedent(
    """
    import sys

    # Block ruamel: any import of it now raises ImportError.
    sys.modules["ruamel"] = None
    sys.modules["ruamel.yaml"] = None


    def leaked():
        return sorted(
            name
            for name, module in sys.modules.items()
            if (name == "ruamel" or name.startswith("ruamel.")) and module is not None
        )


    import fluid_build
    import fluid_build.cli.split
    import fluid_build.cli._split_text
    import fluid_build.cli.forge_contract_fragments

    assert not leaked(), leaked()  # importing the command imports no ruamel
    from fluid_build.cli import main

    rc = main(["split", sys.argv[1], "--parts", sys.argv[2]])
    assert not leaked(), leaked()
    print("FLUID_BUILD_FILE=" + fluid_build.__file__)
    sys.exit(rc)
    """
)


@pytest.mark.parametrize(
    "parts", [",".join(["sovereignty", "accessPolicy", "builds", "exposes"]), ALL_PARTS]
)
def test_i31_split_works_and_keeps_the_comments_with_ruamel_blocked(
    tmp_path: Path, parts: str
) -> None:
    flat, split = make_trees(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLUID_", "PYTHON"))}
    env.update(PYTHONPATH=str(REPO), HOME=str(tmp_path / "home"))

    proc = subprocess.run(
        [sys.executable, "-c", _NO_RUAMEL, str(split / NAME), parts],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"FLUID_BUILD_FILE={REPO}" in proc.stdout  # this checkout was the one run
    root = (split / NAME).read_text("utf-8")
    for comment in (
        "# ===== Orders data product (header comment) =====",
        "# comment directly above a moved section",
        "builds:  # ours",
        "# between builds",
        "# end-of-file comment",
    ):
        assert comment in root
    assert "$ref" in root
    assert sorted(set(files_of(split)) - set(files_of(flat)))  # fragments were written


def test_i31_importing_the_split_modules_leaves_ruamel_unimported_even_when_it_is_installed() -> (
    None
):
    """Not blocked: if ruamel is installed, the split path still must not pull it in."""
    code = textwrap.dedent(
        """
        import sys
        import fluid_build.cli.split, fluid_build.cli._split_text, fluid_build.cli.forge_contract_fragments
        from fluid_build.cli import main
        assert not [m for m in sys.modules if m == "ruamel" or m.startswith("ruamel.")]
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLUID_", "PYTHON"))}
    env["PYTHONPATH"] = str(REPO)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── the command as a user runs it ───────────────────────────────────────


@pytest.mark.slow
def test_i01_a_real_process_splits_bundles_and_splits_again_to_the_same_bytes(
    tmp_path: Path,
) -> None:
    flat, split = make_trees(tmp_path)

    probe = subprocess.run(
        [sys.executable, "-c", "import fluid_build; print(fluid_build.__file__)"],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.stdout.strip().startswith(str(REPO)), probe.stdout + probe.stderr
    done = run_fluid(["split", NAME, "--parts", ALL_PARTS], split)
    assert done.returncode == 0, done.stderr
    assert "Split into" in done.stderr and "contract.fluid.yaml" in done.stderr

    for env in (None, "prod"):
        args: List[Any] = ["--env", env] if env else []
        tag = env or "base"
        flat_out, split_out = tmp_path / f"b-flat-{tag}.yaml", tmp_path / f"b-split-{tag}.yaml"
        assert run_fluid(["bundle", NAME, "--out", flat_out, *args], flat).returncode == 0
        assert run_fluid(["bundle", NAME, "--out", split_out, *args], split).returncode == 0
        assert flat_out.read_bytes() == split_out.read_bytes()

    before = digest(split)
    again = run_fluid(["split", NAME, "--parts", ALL_PARTS], split)
    assert again.returncode == 0 and "Nothing to split" in again.stderr
    assert digest(split) == before


def test_i39_split_help_in_a_real_process_lists_the_parts(tmp_path: Path) -> None:
    proc = run_fluid(["split", "-h"], tmp_path)

    assert proc.returncode == 0
    for name in (
        "--parts",
        "sovereignty",
        "accessPolicy",
        "builds",
        "exposes",
        "sql",
        "quality",
        "schema",
        "extensions",
        "description",
    ):
        assert name in proc.stdout, name


# ── I-18: an extension validator sees a block that moved to a file ──────

_VALIDATOR = textwrap.dedent(
    """
    def validate(extensions, errors):
        acme = extensions.get("acme")
        if not isinstance(acme, dict):
            errors.append("acme block missing")
        elif acme.get("region") != "eu":
            errors.append("region must be eu, got %r" % (acme.get("region"),))
    """
)

_EXTENSION_CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: ext.x
name: Ext
domain: d
metadata: {layer: Silver, owner: {team: t, email: t@example.com}}
extensions:
  acme:
    region: REGION
exposes:
  - exposeId: o
    kind: table
    binding: {platform: local, format: csv, location: {path: out/o.csv}}
    contract:
      schema: [{name: a, type: string}]
"""


def make_plugin(tmp_path: Path) -> Path:
    """A one-module distribution whose entry point validates ``extensions.acme``."""
    root = tmp_path / "plugin"
    write(root / "miniacme.py", _VALIDATOR)
    info = root / "miniacme-0.0.1.dist-info"
    write(info / "METADATA", "Metadata-Version: 2.1\nName: miniacme\nVersion: 0.0.1\n")
    write(
        info / "entry_points.txt", "[fluid_build.extension_validators]\nacme = miniacme:validate\n"
    )
    return root


def _validate(cwd: Path, plugin: Path) -> "tuple[int, Dict[str, Any]]":
    proc = run_fluid(["validate", NAME, "--offline", "--format", "json"], cwd, extra_path=[plugin])
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.slow
@pytest.mark.parametrize("region,valid", [("us", False), ("eu", True)])
def test_i18_the_validator_sees_the_moved_extension_block_as_it_sees_the_inline_one(
    tmp_path: Path, region: str, valid: bool
) -> None:
    plugin = make_plugin(tmp_path)
    text = _EXTENSION_CONTRACT.replace("REGION", region)
    flat, split = make_trees(tmp_path, {NAME: text})

    moved = run_fluid(["split", NAME, "--parts", "extensions.acme"], split)
    assert moved.returncode == 0, moved.stderr
    assert (split / "fragments" / "extensions" / "acme.yaml").is_file()
    assert "$ref" in (split / NAME).read_text("utf-8")

    flat_rc, flat_report = _validate(flat, plugin)
    split_rc, split_report = _validate(split, plugin)

    assert flat_report["valid"] is valid and split_report["valid"] is valid
    assert flat_rc == split_rc == (0 if valid else 1)
    if not valid:
        # The plugin ran on both trees and said the same thing; the split tree's
        # message also names the fragment the block came from.
        assert any("region must be eu, got 'us'" in e for e in flat_report["errors"])
        assert any("region must be eu, got 'us'" in e for e in split_report["errors"])
        assert any("fragments/extensions/acme.yaml" in e for e in split_report["errors"])


# The regression corpus of the fragment work, with its vendored customScaffold
# plugin; only where FLUID_BASELINE_DIR names it.
BASELINE = Path(os.environ.get("FLUID_BASELINE_DIR") or "/nonexistent-fluid-baseline")


@pytest.mark.slow
@pytest.mark.skipif(
    not (BASELINE / "plugins" / "customscaffold").is_dir(),
    reason="the baseline corpus and its vendored customScaffold plugin are not on this machine",
)
@pytest.mark.parametrize("fixture,valid", [("batch-flat", True), ("batch-ext-inline-bad", False)])
def test_i18_the_real_custom_scaffold_validator_reports_the_same_from_a_fragment(
    tmp_path: Path, fixture: str, valid: bool
) -> None:
    plugin = BASELINE / "plugins" / "customscaffold"
    flat, split = tmp_path / "flat", tmp_path / "split"
    for side in (flat, split):
        shutil.copytree(BASELINE / "fixtures" / fixture, side)

    moved = run_fluid(["split", NAME, "--parts", "extensions.customScaffold"], split)
    assert moved.returncode == 0, moved.stderr
    assert (split / "fragments" / "extensions" / "customscaffold.yaml").is_file()

    flat_rc, flat_report = _validate(flat, plugin)
    split_rc, split_report = _validate(split, plugin)

    assert flat_rc == split_rc == (0 if valid else 1)
    assert flat_report["valid"] is valid and split_report["valid"] is valid
    assert len(flat_report["errors"]) == len(split_report["errors"]) == (0 if valid else 3)
    for flat_error, split_error in zip(flat_report["errors"], split_report["errors"], strict=True):
        # The plugin's own message is the same; the split tree's names the fragment
        # (and line) the block came from where the flat one names the extension.
        assert split_error.startswith("fragments/extensions/customscaffold.yaml:")
        assert split_error.split("): ", 1)[1] == flat_error.split(": ", 1)[1]


# ── compatibility: the v0.24.0 loader reads the default split ───────────


def _extract_release(tmp_path: Path, tag: str = "v0.24.0") -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    archive = subprocess.run(
        [git, "archive", tag, "fluid_build"], cwd=str(REPO), capture_output=True, timeout=120
    )
    if archive.returncode != 0:
        pytest.skip(f"tag {tag} is not in this clone")
    dest = tmp_path / f"release-{tag}"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        try:
            tar.extractall(dest, filter="data")  # type: ignore[call-arg]
        except TypeError:  # Python without extraction filters
            tar.extractall(dest)
    return dest


@pytest.mark.slow
@pytest.mark.parametrize("env", [None, "prod"])
def test_i01_the_0_24_0_loader_reads_the_default_split_to_the_same_document(
    tmp_path: Path, env: Optional[str]
) -> None:
    old = _extract_release(tmp_path)
    flat, split = make_trees(tmp_path)
    assert run_fluid(["split", NAME], split).returncode == 0
    assert sorted(set(files_of(split)) - set(files_of(flat))) == DEFAULT_LAYOUT
    args: List[Any] = ["--env", env] if env else []

    out = {name: tmp_path / f"{name}.yaml" for name in ("old-flat", "old-split", "new-split")}
    old_flat = run_fluid(["bundle", NAME, "--out", out["old-flat"], *args], flat, checkout=old)
    old_split = run_fluid(["bundle", NAME, "--out", out["old-split"], *args], split, checkout=old)
    new_split = run_fluid(["bundle", NAME, "--out", out["new-split"], *args], split)

    assert old_flat.returncode == old_split.returncode == new_split.returncode == 0, (
        old_split.stderr + new_split.stderr
    )
    flat_bytes = out["old-flat"].read_bytes()
    assert out["old-split"].read_bytes() == flat_bytes
    assert out["new-split"].read_bytes() == flat_bytes
    assert yaml.safe_load(flat_bytes)["builds"][0]["id"] == "ingest.orders"
