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
