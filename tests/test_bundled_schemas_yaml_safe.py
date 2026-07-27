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

"""Every bundled JSON Schema must be readable by YAML parsers, not just json.load.

Forge itself loads its schemas with ``json.load`` (``schema_manager.py``), which
accepts UTF-16 surrogate-pair escapes such as ``\\ud83d\\udd25`` for an emoji.
That is valid JSON. It is *not* valid YAML 1.1, and libyaml's scanner rejects it
outright.

This matters because JSON is a subset of YAML, so downstream tooling routinely
reads a JSON Schema through a YAML parser. ``datamodel-code-generator`` — used by
the FLUID Command Center to generate its contract model from these very files —
parses via ``yaml.CSafeLoader``. A surrogate escape therefore makes the schema
un-generatable for consumers while forge's own tests stay green, so nothing here
catches it.

That is exactly what happened to ``fluid-schema-0.7.5.json``: six emoji in the
top-level ``description`` were written as surrogate escapes, and the GA schema
could not be code-generated at all. It went unnoticed because forge never parses
these files as YAML, and because the 0.7.6 preview happened to drop the emoji —
so the newest schema worked while the *stable* one did not.

Emoji are fine. Emoji written as surrogate escapes are not: they must be literal
UTF-8 in the file.

**Why this file also checks the built distribution.** The fix for 0.7.5 landed
~2 hours AFTER the v0.13.0 tag was cut, so the guard below went green on a
source tree whose released wheel was still broken — and the Command Center
installs ``data-product-forge`` unpinned from PyPI at every build, so every
container built since got the broken schema. A source-tree assertion cannot
catch that. :class:`TestTheShippedArtifact` re-runs the same checks against the
schemas of the *installed* ``fluid_build`` package and, when a ``dist/`` exists,
against the wheels and sdists inside it — so a release job that builds and then
runs this file cannot publish a schema a YAML parser rejects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

try:  # libyaml is what downstream tooling actually uses; prefer it when present.
    from yaml import CSafeLoader as SafeLoader
except ImportError:  # pragma: no cover - pure-Python fallback
    from yaml import SafeLoader  # type: ignore[assignment]

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "fluid_build" / "schemas"

# A lone high surrogate escape (\ud800-\udbff). Matching the high half alone is
# enough — a valid pair always starts with one, and an unpaired one is worse.
SURROGATE_ESCAPE = re.compile(r"\\u(?:d[89ab][0-9a-f]{2})", re.IGNORECASE)


def _schema_files() -> list[Path]:
    files = sorted(SCHEMA_DIR.glob("fluid-schema-*.json"))
    assert files, f"no bundled schemas found under {SCHEMA_DIR}"
    return files


@pytest.mark.parametrize("schema_path", _schema_files(), ids=lambda p: p.name)
def test_bundled_schema_has_no_surrogate_escapes(schema_path: Path) -> None:
    """Non-BMP characters must be literal UTF-8, never ``\\udXXX`` escapes."""
    raw = schema_path.read_text(encoding="utf-8")
    found = SURROGATE_ESCAPE.findall(raw)
    assert not found, (
        f"{schema_path.name} contains {len(found)} UTF-16 surrogate escape(s) "
        f"(e.g. {found[0]}). Valid JSON, but YAML parsers reject it, which breaks "
        f"downstream codegen. Write the character literally instead."
    )


@pytest.mark.parametrize("schema_path", _schema_files(), ids=lambda p: p.name)
def test_bundled_schema_parses_as_yaml(schema_path: Path) -> None:
    """The real guarantee: a YAML loader can read the file, and agrees with json."""
    raw = schema_path.read_text(encoding="utf-8")
    try:
        via_yaml = yaml.load(raw, Loader=SafeLoader)
    except yaml.YAMLError as exc:  # pragma: no cover - the failure we're guarding
        pytest.fail(
            f"{schema_path.name} is not YAML-parseable, so downstream tooling "
            f"(e.g. datamodel-code-generator) cannot consume it: {exc}"
        )
    assert via_yaml == json.loads(
        raw
    ), f"{schema_path.name} decodes differently via YAML than via JSON"


# ---------------------------------------------------------------------------
# The artifact that actually ships.
# ---------------------------------------------------------------------------

DIST_DIR = SCHEMA_DIR.parent.parent / "dist"


def _installed_schema_dir() -> Path:
    import fluid_build

    return Path(fluid_build.__file__).resolve().parent / "schemas"


def _assert_yaml_safe(name: str, raw: str) -> None:
    found = SURROGATE_ESCAPE.findall(raw)
    assert not found, (
        f"{name} contains {len(found)} UTF-16 surrogate escape(s) (e.g. {found[0]}). "
        "Valid JSON, but YAML parsers reject it, which breaks downstream codegen."
    )
    try:
        via_yaml = yaml.load(raw, Loader=SafeLoader)
    except yaml.YAMLError as exc:  # pragma: no cover - the failure we're guarding
        pytest.fail(f"{name} is not YAML-parseable: {exc}")
    assert via_yaml == json.loads(raw), f"{name} decodes differently via YAML than via JSON"


class TestTheShippedArtifact:
    """Run the same checks against what a consumer installs, not the repo tree."""

    @pytest.mark.parametrize(
        "schema_path",
        sorted(_installed_schema_dir().glob("fluid-schema-*.json")),
        ids=lambda p: p.name,
    )
    def test_the_installed_package_schemas_are_yaml_safe(self, schema_path: Path) -> None:
        _assert_yaml_safe(schema_path.name, schema_path.read_text(encoding="utf-8"))

    def test_the_installed_copy_matches_the_repo_tree(self) -> None:
        """A wheel that shipped a stale schema fails here even if the tree is clean."""
        installed = _installed_schema_dir()
        if installed == SCHEMA_DIR:
            pytest.skip("editable/source install — the two paths are the same directory")
        for source in _schema_files():
            shipped = installed / source.name
            assert shipped.exists(), f"{source.name} is missing from the installed package"
            assert shipped.read_bytes() == source.read_bytes(), (
                f"{source.name} differs between the repo tree and the installed package — "
                "the released artifact is not what this repo tests"
            )

    def test_any_built_distribution_is_yaml_safe(self) -> None:
        """Release gate: build first (`make build-by-profile`), then run this file.

        The 0.7.5 emoji fix landed after the v0.13.0 tag was cut, so a green
        source tree published a broken wheel. Reading the archives directly is
        the only check that cannot be outrun by commit ordering.
        """
        import tarfile
        import zipfile

        if not DIST_DIR.is_dir():
            pytest.skip("no dist/ — nothing has been built in this tree")
        archives = sorted(DIST_DIR.glob("*.whl")) + sorted(DIST_DIR.glob("*.tar.gz"))
        if not archives:
            pytest.skip("dist/ holds no wheels or sdists")

        checked = 0
        for archive in archives:
            if archive.suffix == ".whl":
                with zipfile.ZipFile(archive) as zf:
                    for name in zf.namelist():
                        if "fluid_build/schemas/fluid-schema-" in name and name.endswith(".json"):
                            _assert_yaml_safe(
                                f"{archive.name}:{name}", zf.read(name).decode("utf-8")
                            )
                            checked += 1
            else:
                with tarfile.open(archive) as tf:
                    for member in tf.getmembers():
                        if (
                            "fluid_build/schemas/fluid-schema-" in member.name
                            and member.name.endswith(".json")
                        ):
                            handle = tf.extractfile(member)
                            assert handle is not None
                            _assert_yaml_safe(
                                f"{archive.name}:{member.name}", handle.read().decode("utf-8")
                            )
                            checked += 1
        assert checked, f"no bundled schemas found inside {[a.name for a in archives]}"
