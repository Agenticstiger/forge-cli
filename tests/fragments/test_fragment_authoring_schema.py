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

"""The authoring schema editors check fragments with (scenarios K-01 to K-09).

Spec 9: a generated ``fluid-authoring-schema-0.7.6.json`` accepts ``{$ref}`` at every value
position, the canonical schemas are unchanged, and the file stays out of the
``fluid-schema-*.json`` glob that version discovery and several tests use.

K-04 is a generated property test over the contracts the repository ships: every value
position of every composed example takes a ``$ref``; every example split into every part has a
root and fragments the authoring schema accepts. K-21 (an editor modeline in VS Code) is
manual: no language server runs in this repository.

A SPEC GAP scenario (SG-29: K-03's glob, K-06's empty string, K-07's siblings) pins today's
behaviour. A test marked ``xfail(strict=True)`` shows a defect; the reason names it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import jsonschema
import pytest
import yaml

import fluid_build
from fluid_build import schema_authoring

_REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = Path(schema_authoring.__file__).parent / "schemas"
_AUTHORING_FILE = _SCHEMAS / "fluid-authoring-schema-0.7.6.json"
_PARTS = "sovereignty,accessPolicy,builds,exposes,sql,quality,schema,extensions,description"

#: sha256 of each canonical schema as v0.24.0 (0d55f519) shipped it: the golden for K-01.
_V0_24_0_SHA256 = {
    "0.7.1": "f23e6345d7cf4738d81634918481f9953d69ec660258ee488b4a650433d4130d",  # pragma: allowlist secret
    "0.7.2": "2c99ac5a4e82c9f7682b3114ff6855300f8b0f16e3a630be5606e48cf6add9fc",  # pragma: allowlist secret
    "0.7.3": "8fa6a9d988ac5a11d77707f5d14d858acef8ba75c9cdfb8ab1ea61361a0814b8",  # pragma: allowlist secret
    "0.7.4": "82977a9efbeaf0850f81cfbda090d0627b9d5e8699274e1a0e53cddac3d3cc5b",  # pragma: allowlist secret
    "0.7.5": "a1c33dc003599649f23c9bf100f9b8325a06fb9d7ae9f975bebda25ba43a8429",  # pragma: allowlist secret
    "0.7.6": "8c025e0466fcbe51cdc2ee30b00876cee6ea8633c7a164ca13b2bffc67bd4e78",  # pragma: allowlist secret
}


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_running() -> None:
    running = Path(fluid_build.__file__).resolve()
    assert running.is_relative_to(_REPO), f"{running} is not inside {_REPO}"


_CACHE: List[str] = []  # ["--cache-dir", <dir>]: validate keeps its schema cache in tmp_path


@pytest.fixture(scope="module", autouse=True)
def _private_schema_cache(tmp_path_factory: pytest.TempPathFactory) -> Any:
    _CACHE[:] = ["--cache-dir", str(tmp_path_factory.mktemp("schema-cache"))]
    yield
    _CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_loader_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fluid_build import loader

    def clear() -> None:
        for name in dir(loader):
            if name.startswith("_NOTED_") and isinstance(getattr(loader, name), set):
                getattr(loader, name).clear()

    clear()
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    yield
    clear()


@pytest.fixture(scope="module")
def canonical() -> Dict[str, Any]:
    return json.loads((_SCHEMAS / "fluid-schema-0.7.6.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def authoring() -> Dict[str, Any]:
    return json.loads(_AUTHORING_FILE.read_text("utf-8"))


@pytest.fixture(scope="module")
def canonical_validator(canonical: Dict[str, Any]) -> Any:
    return jsonschema.validators.validator_for(canonical)(canonical)


@pytest.fixture(scope="module")
def authoring_validator(authoring: Dict[str, Any]) -> Any:
    return jsonschema.validators.validator_for(authoring)(authoring)


def _problems(validator: Any, document: Any) -> List[Tuple[str, str]]:
    """``(path, message)`` of every error, sorted: what two schemas must agree on."""
    return sorted(
        ("/".join(str(p) for p in e.absolute_path), e.message)
        for e in validator.iter_errors(document)
    )


def _def_validator(schema: Dict[str, Any], pointer: str) -> Any:
    """A validator for the part of ``schema`` that ``pointer`` names, with all its ``$defs``."""
    wrapper = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": pointer}
    return jsonschema.validators.validator_for(wrapper)(wrapper)


#: The part of a contract each ``fluid validate --fragment`` kind stands for.
_POINTER = {
    "sovereignty": "#/$defs/sovereignty",
    "accessPolicy": "#/$defs/accessPolicy",
    "build": "#/$defs/build",
    "expose": "#/$defs/expose",
    "quality": "#/$defs/exposeContract/properties/quality",
    "schema": "#/$defs/exposeContract/properties/schema",
}


def _kind_of(relative: str) -> Optional[str]:
    """What ``fluid split`` wrote at ``relative``, as a ``--fragment`` kind (None: no schema)."""
    if relative == "fragments/sovereignty.yaml":
        return "sovereignty"
    if relative == "fragments/access-policy.yaml":
        return "accessPolicy"
    for folder, kind in (
        ("fragments/builds/", "build"),
        ("fragments/exposes/", "expose"),
        ("fragments/quality/", "quality"),
        ("fragments/schemas/", "schema"),
    ):
        if relative.startswith(folder):
            return kind
    return None


# A hand-written 0.7.6 contract with a comment everywhere, block scalars of both kinds, two
# builds, two exposes with columns and quality rules, and every section a fragment can hold.
_SPLIT_ALL = """\
# Orders: header comment
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.orders
name: Orders  # inline comment
description:
  purpose: One row per order.
  usage: Join on order_id.
  limitations: No refunds before 2024.
domain: commerce
tags: [finance, core]
labels: {cost_center: "CC-1234"}
metadata:
  layer: Silver
  owner: {team: commerce-data, email: commerce@example.com}
sovereignty:
  jurisdiction: EU
  dataResidency: true
accessPolicy:
  grants:
    - {principal: "group:analysts@example.com", permissions: [read]}
extensions:
  acme: {team: growth}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        SELECT order_id FROM src
  - id: rollup
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |-
        SELECT 1 AS n
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
        - {name: order_id, type: string, required: true}
        - {name: email, type: string, sensitivity: pii}
      quality:
        - {rule: id_present, expression: order_id IS NOT NULL, severity: error}
  - exposeId: summary
    kind: table
    binding: {platform: local, format: csv, location: {path: out/summary.csv}}
    contract:
      schema:
        - {name: day, type: date, required: true}
# trailing comment
"""


def _split_project(tmp_path: Path, parts: Optional[str] = None) -> Path:
    from fluid_build.cli import main

    project = tmp_path / "p"
    project.mkdir()
    contract = project / "contract.fluid.yaml"
    contract.write_text(_SPLIT_ALL, encoding="utf-8")
    argv = ["split", str(contract)] + (["--parts", parts] if parts else [])
    assert main(argv) == 0
    return contract


def _fragments(project: Path) -> Iterator[Tuple[str, str, Any]]:
    """``(kind, relative path, parsed)`` of every fragment ``fluid split`` wrote that has a kind."""
    for path in sorted(project.rglob("*.yaml")):
        relative = path.relative_to(project).as_posix()
        kind = _kind_of(relative)
        if kind is not None:
            yield kind, relative, yaml.safe_load(path.read_text("utf-8"))


# ── K-01 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", sorted(_V0_24_0_SHA256))
def test_k01_the_canonical_schemas_are_the_bytes_v0_24_0_shipped(version: str) -> None:
    digest = hashlib.sha256((_SCHEMAS / f"fluid-schema-{version}.json").read_bytes()).hexdigest()
    assert digest == _V0_24_0_SHA256[version], f"fluid-schema-{version}.json changed"


def test_k01_no_canonical_schema_was_added_or_dropped() -> None:
    names = sorted(p.name for p in _SCHEMAS.glob("fluid-schema-*.json"))
    assert names == [f"fluid-schema-{v}.json" for v in sorted(_V0_24_0_SHA256)]


# ── K-02 ────────────────────────────────────────────────────────────────


def test_k02_a_root_split_by_default_is_clean_for_the_authoring_schema_only(
    tmp_path: Path, canonical_validator: Any, authoring_validator: Any
) -> None:
    contract = _split_project(tmp_path)
    root = yaml.safe_load(contract.read_text("utf-8"))
    assert {"$ref"} == set(root["sovereignty"]) == set(root["accessPolicy"])

    assert _problems(authoring_validator, root) == []
    refused = [message for _, message in _problems(canonical_validator, root)]
    # report 26: a sovereignty, an accessPolicy, 2 builds and 2 exposes make 14 errors
    assert len(refused) == 14
    assert all(
        message == "Additional properties are not allowed ('$ref' was unexpected)"
        or re.fullmatch(r"'(binding|contract|exposeId|kind)' is a required property", message)
        for message in refused
    ), refused
    assert refused.count("Additional properties are not allowed ('$ref' was unexpected)") == 6


@pytest.mark.parametrize(
    "parts", [None, "builds,exposes,sql", _PARTS], ids=["default", "sql", "all"]
)
def test_k02_every_fragment_split_wrote_is_clean_for_the_authoring_schema(
    tmp_path: Path, authoring: Dict[str, Any], authoring_validator: Any, parts: Optional[str]
) -> None:
    contract = _split_project(tmp_path, parts)
    root = yaml.safe_load(contract.read_text("utf-8"))

    assert _problems(authoring_validator, root) == []
    seen = set()
    for kind, relative, document in _fragments(contract.parent):
        found = _problems(_def_validator(authoring, _POINTER[kind]), document)
        assert found == [], (relative, found)
        seen.add(kind)
    assert {"expose", "build"} <= seen


def test_k02_a_json_root_split_by_default_is_clean_too(
    tmp_path: Path, authoring: Dict[str, Any], authoring_validator: Any
) -> None:
    from fluid_build.cli import main

    project = tmp_path / "j"
    project.mkdir()
    contract = project / "contract.fluid.json"
    contract.write_text(json.dumps(yaml.safe_load(_SPLIT_ALL), indent=2), encoding="utf-8")
    assert main(["split", str(contract)]) == 0

    root = json.loads(contract.read_text("utf-8"))  # still a JSON document
    assert root["builds"] == [
        {"$ref": "./fragments/builds/transform.yaml"},
        {"$ref": "./fragments/builds/rollup.yaml"},
    ]
    assert _problems(authoring_validator, root) == []
    for kind, relative, document in _fragments(project):
        assert _problems(_def_validator(authoring, _POINTER[kind]), document) == [], relative


def test_k02_the_shipped_multifile_example_only_lacks_a_version_the_schema_accepts(
    tmp_path: Path, authoring: Dict[str, Any], authoring_validator: Any
) -> None:
    """examples/0.7.1/bitcoin-multifile declares fluidVersion 0.7.1, which no 0.7.6 schema
    accepts (its enum starts at 0.7.3). With the version bumped, its raw root is clean."""
    copied = tmp_path / "multifile"
    shutil.copytree(_REPO / "examples" / "0.7.1" / "bitcoin-multifile", copied)
    contract = copied / "contract.fluid.yaml"
    root = yaml.safe_load(contract.read_text("utf-8"))
    assert root["builds"] == [{"$ref": "./fragments/builds/ingestion.yaml"}]

    as_shipped = _problems(authoring_validator, root)
    assert [path for path, _ in as_shipped] == ["fluidVersion"]  # nothing about a single $ref

    root["fluidVersion"] = "0.7.6"
    assert _problems(authoring_validator, root) == []
    for kind, relative, document in _fragments(copied):
        found = _problems(_def_validator(authoring, _POINTER[kind]), document)
        assert found == [], (relative, found)


# ── K-03 ────────────────────────────────────────────────────────────────


def _golden_stable_versions(preview: Any) -> List[str]:
    return sorted(v for v in _V0_24_0_SHA256 if v not in preview)


def test_k03_the_authoring_file_is_not_a_schema_version(tmp_path: Path) -> None:
    from fluid_build.schema_manager import FluidSchemaManager

    stable = _golden_stable_versions(FluidSchemaManager.PREVIEW_VERSIONS)
    # NB the catalog says "still 0.7.6": 0.7.6 is the preview, so the stable default is 0.7.5.
    assert FluidSchemaManager.latest_bundled_version() == stable[-1]
    assert FluidSchemaManager.BUNDLED_VERSIONS == sorted(_V0_24_0_SHA256)
    manager = FluidSchemaManager(cache_dir=tmp_path / "cache")
    assert not [v for v in manager.list_available_versions() if "authoring" in v]
    assert not [v for v in manager._bundled_schemas if "authoring" in v]


def test_k03_no_version_listing_shows_the_authoring_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main, plan
    from fluid_build.schema_manager import FluidSchemaManager

    assert (
        main(["validate", "--list-versions", "--format", "json", "--cache-dir", str(tmp_path)]) == 0
    )
    listed = json.loads(capsys.readouterr().out)["available_versions"]
    assert "0.7.6" in listed and not [v for v in listed if "authoring" in v]

    assert main(["version"]) == 0
    shown = capsys.readouterr().out
    assert "authoring" not in shown
    assert re.search(r"Default: (\S+)", shown)[1] == FluidSchemaManager.latest_bundled_version()
    assert plan._default_fluid_version() == FluidSchemaManager.latest_bundled_version()


def test_k03_the_name_matches_no_glob_or_pattern_version_discovery_uses() -> None:
    """SG-29: spec 9 names the file ``fluid-schema-0.7.6.authoring.json``, which the glob
    ``fluid-schema-*.json`` would pick up as version ``0.7.6.authoring``. It ships as
    ``fluid-authoring-schema-0.7.6.json`` instead, which no discovery pattern matches."""
    import fnmatch

    from fluid_build.cli import version_cmd
    from fluid_build.schema_manager import SCHEMA_FILE_PATTERN

    name = _AUTHORING_FILE.name
    assert name != "fluid-schema-0.7.6.authoring.json"
    assert fnmatch.fnmatch("fluid-schema-0.7.6.authoring.json", "fluid-schema-*.json")
    assert not fnmatch.fnmatch(name, "fluid-schema-*.json")
    assert not SCHEMA_FILE_PATTERN.match(name)
    assert version_cmd._bundled_schema_versions() == sorted(_V0_24_0_SHA256)
    assert list(_SCHEMAS.glob("fluid-schema-*authoring*")) == []


@pytest.mark.slow
def test_k03_the_tests_that_glob_the_schema_directory_still_pass() -> None:
    """The three the catalog names, and the YAML-safety guard that globs the same directory."""
    files = [
        "tests/cli/discover/test_discover_type_canonicalization.py",
        "tests/iac/test_iac_lakeformation_tags.py",
        "tests/iac/test_iac_lakeformation_admins.py",
        "tests/test_bundled_schemas_yaml_safe.py",
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_")}
    env["PYTHONPATH"] = str(_REPO)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-q",
            *files,
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]


# ── K-04: a ref is accepted at every value position ─────────────────────


def _schema_positions(
    canonical: Dict[str, Any], node: Any, path: Tuple[Any, ...], seen: frozenset, found: set
) -> None:
    """Every value position below ``node``, a part of the canonical schema, as a path of keys
    (``0`` for the items of a list, ``anyKey`` for the values of a map), following ``$ref``."""
    if not isinstance(node, dict) or len(path) > 7:
        return
    if "$ref" in node:
        target = canonical
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        key = (node["$ref"], len(path))
        if key not in seen:
            _schema_positions(canonical, target, path, seen | {key}, found)
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(keyword) or []:
            _schema_positions(canonical, branch, path, seen, found)
    for keyword in ("then", "else"):
        _schema_positions(canonical, node.get(keyword), path, seen, found)
    for name, sub in (node.get("properties") or {}).items():
        found.add(path + (name,))
        _schema_positions(canonical, sub, path + (name,), seen, found)
    for keyword, step in (("items", 0), ("additionalProperties", "anyKey")):
        if isinstance(node.get(keyword), dict):
            found.add(path + (step,))
            _schema_positions(canonical, node[keyword], path + (step,), seen, found)


def _instance_with(path: Tuple[Any, ...], leaf: Any) -> Any:
    for step in reversed(path):
        leaf = [leaf] if isinstance(step, int) else {step: leaf}
    return leaf


def test_k04_a_ref_is_accepted_at_every_position_the_schema_describes(
    canonical: Dict[str, Any], authoring: Dict[str, Any]
) -> None:
    """Not the positions the shipped contracts happen to use, but every one the schema has: each
    property, list item and map value of the root and of all its ``$defs``, to depth 7."""
    refused: List[Tuple[str, str]] = []
    checked = 0
    targets = [("(root)", "#")] + [(name, f"#/$defs/{name}") for name in sorted(canonical["$defs"])]
    for name, pointer in targets:
        node = canonical if pointer == "#" else canonical["$defs"][name]
        found: set = set()
        _schema_positions(canonical, node, (), frozenset({(pointer, 0)}), found)
        if pointer == "#":
            validator = jsonschema.validators.validator_for(authoring)(authoring)
        else:
            validator = _def_validator(authoring, pointer)
        for position in sorted(found, key=lambda p: (len(p), [str(step) for step in p])):
            checked += 1
            instance = _instance_with(position, {"$ref": "./x.yaml"})
            for error in validator.iter_errors(instance):
                if list(error.absolute_path)[: len(position)] == list(position):
                    refused.append(
                        (f"{name}: " + "/".join(map(str, position)), error.message[:100])
                    )
                    break
    assert checked > 3000  # 4,570 in 0.7.6
    assert refused == []


def _corpus() -> List[Path]:
    found = sorted(p for p in (_REPO / "examples").rglob("contract.fluid.yaml"))
    found += sorted((_REPO / "fluid_build" / "templates").rglob("contract.fluid.yaml"))
    compat = _REPO / "tests" / "fixtures" / "contracts" / "compatibility"
    found += sorted(p for p in compat.glob("*") if p.suffix in (".yaml", ".yml", ".json"))
    return found


def _label(path: Path) -> str:
    return path.relative_to(_REPO).as_posix()


def _composed_076(path: Path, canonical_validator: Any) -> Optional[Dict[str, Any]]:
    """The contract at ``path`` composed, at fluidVersion 0.7.6; None unless that is valid."""
    from fluid_build.loader import compose_contract

    try:
        document = compose_contract(path).document
    except Exception:  # noqa: BLE001 - a fixture that does not compose is not a corpus member
        return None
    if not isinstance(document, dict) or "exposes" not in document:
        return None
    document["fluidVersion"] = "0.7.6"
    return document if not list(canonical_validator.iter_errors(document)) else None


def _positions(node: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Any, ...]]:
    """Every value below the root of ``node``: each dict value and each list item, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + (key,)
            yield from _positions(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield path + (index,)
            yield from _positions(value, path + (index,))


def _with_value(document: Any, position: Tuple[Any, ...], value: Any) -> Any:
    out = copy.deepcopy(document)
    parent = out
    for step in position[:-1]:
        parent = parent[step]
    parent[position[-1]] = value
    return out


def _value_at(document: Any, position: Tuple[Any, ...]) -> Any:
    for step in position:
        document = document[step]
    return document


_CORPUS = _corpus()
_CORPUS_IDS = [_label(p) for p in _CORPUS]


def test_k04_the_corpus_is_the_shipped_contracts() -> None:
    assert len(_CORPUS) >= 40
    assert any(p.name == "contract.fluid.yaml" and "examples" in p.parts for p in _CORPUS)


@pytest.mark.parametrize("path", _CORPUS, ids=_CORPUS_IDS)
def test_k04_a_ref_is_accepted_at_every_value_position(
    path: Path, canonical_validator: Any, authoring_validator: Any
) -> None:
    document = _composed_076(path, canonical_validator)
    if document is None:
        pytest.skip("not a valid 0.7.6 contract once its version is set to 0.7.6")

    refused = []
    positions = list(_positions(document))
    for position in positions:
        errors = list(
            authoring_validator.iter_errors(_with_value(document, position, {"$ref": "./x.yaml"}))
        )
        if errors:
            refused.append(("/".join(map(str, position)), errors[0].message[:120]))
    assert len(positions) > 10
    assert refused == []


def test_k04_a_ref_is_accepted_at_the_positions_the_catalog_lists(
    authoring_validator: Any,
) -> None:
    """metadata.owner, exposes[].policy, the whole exposes list, exposes[i], the schema list,
    schema[i], quality, properties.sql, description, extensions.x, sovereignty, accessPolicy
    and a label value."""
    document = yaml.safe_load(_SPLIT_ALL)
    document["exposes"][0]["policy"] = {"authn": "none"}
    positions = [
        ("metadata", "owner"),
        ("exposes", 0, "policy"),
        ("exposes",),
        ("exposes", 0),
        ("exposes", 0, "contract", "schema"),
        ("exposes", 0, "contract", "schema", 1),
        ("exposes", 0, "contract", "quality"),
        ("builds", 0, "properties", "sql"),
        ("description",),
        ("description", "usage"),
        ("extensions", "acme"),
        ("sovereignty",),
        ("accessPolicy",),
        ("labels", "cost_center"),
        ("tags",),
        ("builds",),
    ]
    assert _problems(authoring_validator, document) == []
    for position in positions:
        broken = _with_value(document, position, {"$ref": "./x.yaml"})
        assert _problems(authoring_validator, broken) == [], position


@pytest.mark.parametrize("path", _CORPUS, ids=_CORPUS_IDS)
def test_k04_the_loader_composes_back_what_the_authoring_schema_accepts(
    path: Path, tmp_path: Path, canonical_validator: Any, authoring_validator: Any
) -> None:
    """Move the value at a position into a file of its own and put a ref there: the raw root is
    clean for the authoring schema, and the loader composes the original document back."""
    from fluid_build.loader import compose_contract

    document = _composed_076(path, canonical_validator)
    if document is None:
        pytest.skip("not a valid 0.7.6 contract once its version is set to 0.7.6")

    mismatches = []
    for number, position in enumerate(list(_positions(document))[::9]):
        value = _value_at(document, position)
        if value is None:  # a YAML file holding null composes to {} (SG-07)
            continue
        project = tmp_path / f"p{number}"
        (project / "x").mkdir(parents=True)
        (project / "x" / "frag.yaml").write_text(
            yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        root = _with_value(document, position, {"$ref": "./x/frag.yaml"})
        text = yaml.safe_dump(root, sort_keys=False, allow_unicode=True)
        (project / "contract.fluid.yaml").write_text(text, encoding="utf-8")
        where = "/".join(map(str, position))
        if _problems(authoring_validator, yaml.safe_load(text)):
            mismatches.append((where, "refused by the authoring schema"))
        if compose_contract(project / "contract.fluid.yaml").document != document:
            mismatches.append((where, "composes to another document"))
    assert mismatches == []


def _fx_parity(root: Path) -> Path:
    """The catalog's FX-PARITY root: every mechanism of the resolver in one contract."""
    files = {
        "contract.fluid.yaml": (
            'fluidVersion: "0.7.6"\nkind: DataProduct\nid: refs.parity\nname: Parity\n'
            "domain: ops\n"
            "description: {purpose: P-TEXT, usage: {$ref: ./docs/usage.md}, limitations: none}\n"
            "metadata: {layer: Silver, owner: {$ref: ./shared/team.yaml}}\n"
            "builds: [{$ref: ./fragments/builds/transform.yaml}]\n"
            "exposes: [{$ref: ./fragments/exposes/out.yaml}]\n"
            "extensions: {acme: {$ref: ./ext/acme.yaml}, paths: {out: runtime/out/dev.csv}}\n"
        ),
        "fragments/builds/transform.yaml": (
            "id: transform\npattern: embedded-logic\nengine: sql\nproperties:\n"
            "  sql: {$ref: ../../sql/transform.sql}\n"
            "  parameters: {$ref: ../../shared/params.yaml, mode: dev}\n"
        ),
        "fragments/exposes/out.yaml": (
            'exposeId: out\nkind: table\nversion: "1.0.0"\n'
            'description: {$ref: "#/description/purpose"}\n'
            "binding:\n  platform: local\n  format: csv\n"
            '  location: {path: {$ref: "#/extensions/paths/out"}}\n'
            "contract:\n  schema: {$ref: ../../schemas/out-columns.yaml}\n"
            "  quality: {$ref: ../../quality/out.yaml}\n"
        ),
        "sql/transform.sql": "-- S2-DEV\nSELECT 1 AS id\n",
        "docs/usage.md": "# Usage\n\nS5-USAGE: join on `id`.\n",
        "shared/team.yaml": "team: platform-ops\nemail: ops@example.com\n",
        "shared/params.yaml": "retries: 3\n",
        "ext/acme.yaml": "team: growth\n",
        "schemas/out-columns.yaml": "- {name: id, type: integer}\n",
        "quality/out.yaml": "- {rule: id_present, expression: id IS NOT NULL, severity: error}\n",
    }
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root / "contract.fluid.yaml"


def test_k04_every_ref_shape_of_the_parity_contract_is_clean(
    tmp_path: Path, authoring: Dict[str, Any], authoring_validator: Any
) -> None:
    """Text refs, a ref with a sibling, a ref to a list, and same-document pointers."""
    from fluid_build.cli import main

    contract = _fx_parity(tmp_path)
    root = yaml.safe_load(contract.read_text("utf-8"))
    assert _problems(authoring_validator, root) == []
    for kind, relative in (
        ("build", "fragments/builds/transform.yaml"),
        ("expose", "fragments/exposes/out.yaml"),
    ):
        document = yaml.safe_load((tmp_path / relative).read_text("utf-8"))
        assert _problems(_def_validator(authoring, _POINTER[kind]), document) == [], relative
    assert main(["validate", *_CACHE, str(contract)]) == 0  # and the composed contract is sound


_EXAMPLES = [p for p in _CORPUS if "examples" in p.parts]


@pytest.mark.parametrize("path", _EXAMPLES, ids=[_label(p) for p in _EXAMPLES])
def test_k04_an_example_split_into_every_part_is_clean_for_the_authoring_schema(
    path: Path,
    tmp_path: Path,
    canonical_validator: Any,
    authoring: Dict[str, Any],
    authoring_validator: Any,
) -> None:
    from fluid_build.cli import main

    if _composed_076(path, canonical_validator) is None:
        pytest.skip("not a valid 0.7.6 contract once its version is set to 0.7.6")
    work = tmp_path / "work"
    shutil.copytree(path.parent, work)
    contract = work / path.name
    text = contract.read_text("utf-8")
    contract.write_text(
        re.sub(r"(?m)^fluidVersion:.*$", 'fluidVersion: "0.7.6"', text, count=1), "utf-8"
    )

    assert main(["split", str(contract), "--parts", _PARTS]) == 0

    root = yaml.safe_load(contract.read_text("utf-8"))
    assert _problems(authoring_validator, root) == []
    for kind, relative, document in _fragments(work):
        found = _problems(_def_validator(authoring, _POINTER[kind]), document)
        assert found == [], (relative, found)
        # the same fragment, checked on its own by the command
        assert main(["validate", *_CACHE, "--fragment", kind, str(work / relative)]) == 0, relative


# ── K-05: the authoring schema is not "anything goes" ───────────────────


def test_k05_a_wrong_value_gets_the_canonical_error(
    canonical_validator: Any, authoring_validator: Any
) -> None:
    base = yaml.safe_load(_SPLIT_ALL)
    cases = {}
    cases["kind"] = _with_value(base, ("exposes", 0, "kind"), 42)
    cases["unknown root key"] = {**base, "bogus": 1}
    cases["missing required"] = {k: v for k, v in base.items() if k != "metadata"}
    cases["wrong type"] = _with_value(base, ("tags",), "finance")
    cases["not an object"] = _with_value(base, ("builds", 0), "transform")
    for name, broken in cases.items():
        expected = _problems(canonical_validator, broken)
        assert expected, name
        assert _problems(authoring_validator, broken) == expected, name


@pytest.mark.parametrize("path", _CORPUS, ids=_CORPUS_IDS)
def test_k05_a_wrong_value_at_any_position_gets_the_canonical_error(
    path: Path, canonical_validator: Any, authoring_validator: Any
) -> None:
    document = _composed_076(path, canonical_validator)
    if document is None:
        pytest.skip("not a valid 0.7.6 contract once its version is set to 0.7.6")

    disagreements = []
    for position in list(_positions(document))[::7]:  # every 7th position keeps this quick
        for wrong in (12345, {"bogus": 1}):
            broken = _with_value(document, position, wrong)
            if _problems(authoring_validator, broken) != _problems(canonical_validator, broken):
                disagreements.append(("/".join(map(str, position)), wrong))
    assert disagreements == []


# ── K-06, K-07 (SG-29) ──────────────────────────────────────────────────


def _column_with(value: Any) -> Dict[str, Any]:
    document = yaml.safe_load(_SPLIT_ALL)
    document["exposes"][0]["contract"]["schema"][0] = value
    return document


@pytest.mark.parametrize(
    "ref",
    [42, None, True, ["./x.yaml"], {"a": 1}, 1.5],
    ids=["integer", "null", "boolean", "list", "object", "float"],
)
def test_k06_a_ref_that_is_not_a_string_is_refused(authoring_validator: Any, ref: Any) -> None:
    errors = _problems(authoring_validator, _column_with({"$ref": ref}))
    assert errors and all(path.startswith("exposes/0/contract/schema/0") for path, _ in errors)
    assert any("is not of type 'string'" in message for _, message in errors)


@pytest.mark.parametrize(
    "ref", ["#/a", "./c.yaml", "./c.yaml#/rules", "../shared/c.yaml", "#"], ids=lambda r: r
)
def test_k06_a_ref_string_is_accepted_whatever_it_names(authoring_validator: Any, ref: str) -> None:
    assert _problems(authoring_validator, _column_with({"$ref": ref})) == []


def test_k06_the_empty_ref_is_refused_today(authoring_validator: Any) -> None:
    """SG-29: the spec does not say; the schema requires a non-empty string."""
    errors = _problems(authoring_validator, _column_with({"$ref": ""}))
    assert errors and "non-empty" in errors[0][1]


def test_k07_siblings_beside_a_ref_are_accepted_and_not_checked(authoring_validator: Any) -> None:
    """SG-29: today any keys may stand beside ``$ref``; the schema validates none of them."""
    for column in (
        {"$ref": "./c.yaml#/a", "description": "x"},
        {"$ref": "./c.yaml", "type": 42},  # wrong, but a sibling: not checked
        {"$ref": "./c.yaml", "nothing": "known"},
    ):
        assert _problems(authoring_validator, _column_with(column)) == [], column


# ── K-08, K-09 ──────────────────────────────────────────────────────────


def test_k08_the_authoring_schema_is_a_valid_schema_of_its_own(
    canonical: Dict[str, Any], authoring: Dict[str, Any]
) -> None:
    jsonschema.Draft202012Validator.check_schema(authoring)
    assert authoring["$schema"] == canonical["$schema"]
    assert authoring["$id"] != canonical["$id"]
    assert authoring["$id"] == canonical["$id"].replace("fluid-schema-", "fluid-authoring-schema-")
    assert set(authoring["$defs"]) - set(canonical["$defs"]) == {"fragmentRef"}
    assert set(canonical["$defs"]) <= set(authoring["$defs"])
    for name in ("expose", "build", "sovereignty", "accessPolicy", "column", "dqRule"):
        assert name in authoring["$defs"]


def test_k08_the_id_with_a_defs_pointer_names_a_definition(authoring: Dict[str, Any]) -> None:
    """What a modeline ``$schema=<$id>#/$defs/expose`` asks an editor to resolve."""
    referencing = pytest.importorskip("referencing")
    Registry, Resource = referencing.Registry, referencing.Resource

    registry = Registry().with_resource(authoring["$id"], Resource.from_contents(authoring))
    for name in ("expose", "build", "sovereignty", "accessPolicy", "column"):
        resolved = registry.resolver().lookup(f"{authoring['$id']}#/$defs/{name}")
        assert resolved.contents == authoring["$defs"][name]


def test_k09_the_bundled_file_is_what_the_generator_writes() -> None:
    assert schema_authoring.AUTHORING_VERSIONS == ("0.7.6",)  # SG-29: no older variants
    for version in schema_authoring.AUTHORING_VERSIONS:
        path = schema_authoring.authoring_schema_path(version)
        assert path.read_bytes() == schema_authoring.render(version).encode("utf-8")
        assert schema_authoring.render(version) == schema_authoring.render(version)


def test_k09_generating_never_changes_the_canonical_schema(canonical: Dict[str, Any]) -> None:
    before = json.dumps(canonical, sort_keys=True)
    schema_authoring.authoring_schema(canonical)
    assert json.dumps(canonical, sort_keys=True) == before


def test_k09_the_file_is_shipped_in_the_package() -> None:
    from importlib import resources

    resource = resources.files("fluid_build.schemas").joinpath(_AUTHORING_FILE.name)
    assert resource.is_file() and resource.read_bytes() == _AUTHORING_FILE.read_bytes()


def test_k09_the_wheel_and_the_sdist_include_the_file() -> None:
    import glob

    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        tomllib = pytest.importorskip("tomli")

    config = tomllib.loads((_REPO / "pyproject.toml").read_text("utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["fluid_build"]
    package = Path(fluid_build.__file__).parent
    shipped = {
        Path(match).as_posix()
        for pattern in patterns
        for match in glob.glob(pattern, root_dir=package, recursive=True)
    }
    assert f"schemas/{_AUTHORING_FILE.name}" in shipped
    manifest = (_REPO / "MANIFEST.in").read_text("utf-8")
    assert re.search(r"recursive-include fluid_build .*\*\.json", manifest)


# ── K-21 ────────────────────────────────────────────────────────────────


@pytest.mark.skip(reason="MAN: needs VS Code and the YAML language server; none runs in this repo")
def test_k21_an_editor_modeline_resolves_the_authoring_schema() -> None:
    """``# yaml-language-server: $schema=<authoring $id>#/$defs/expose`` shows no false errors.
    The resolution half is automated in test_k08_the_id_with_a_defs_pointer_names_a_definition."""
