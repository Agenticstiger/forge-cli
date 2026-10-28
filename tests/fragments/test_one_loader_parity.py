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

"""One loader serves every command and the library (scenarios G-01..G-29, B-17, B-18).

A contract kept in fragments must read as ONE document wherever it is read:
``validate``, ``plan``, ``apply``, ``bundle``, every ``generate`` subcommand,
``docs``, ``diff``, ``verify``, ``publish``, the policy commands, the library
and the MCP tools. FX-PARITY (section 2 of the scenario catalog) uses every
mechanism at once: a sibling merge, a text file, a list fragment, a pointer
into another fragment, a pointer that only the overlay changes, and an overlay
that is itself made of fragments. It carries five sentinels under ``--env prod``:

* S1 ``runtime/out/S1-prod.csv``: the path, set by the overlay's fragment and
  read through a ``#/extensions/paths/out`` pointer;
* S2 ``S2-PROD``: the SQL text, from the overlay's text fragment;
* S3 ``mode: S3-prod``: a sibling-merged parameter;
* S4 ``P-TEXT``: the value of a pointer into ``description``;
* S5 ``S5-USAGE``: Markdown text.

The expected document is written out below from the same constants the fixture
files are, never from the loader: a flat twin built from it is the oracle for
every command's output.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

import pytest
import yaml

from fluid_build import loader

_CHECKOUT = Path(__file__).resolve().parents[2]

# ── the sentinels ───────────────────────────────────────────────────────

S1_PATH, DEV_PATH = "runtime/out/S1-prod.csv", "runtime/out/dev.csv"
S2_SQL, DEV_SQL = "-- S2-PROD\nSELECT 1 AS id\n", "-- S2-DEV\nSELECT 1 AS id\n"
S3_MODE, DEV_MODE = "S3-prod", "dev"
S4_TEXT = "P-TEXT"
S5_USAGE = "# Usage\n\nS5-USAGE: join on `id`.\n"

TEAM = {"team": "platform-ops", "email": "ops@example.com"}
PARAMS = {"batch_size": 100, "retries": 3}
ACME = {"region": "eu", "tier": "gold"}
SCHEMA = [
    {"name": "id", "type": "integer", "required": True},
    {"name": "label", "type": "string"},
]
QUALITY = [{"rule": "id_present", "expression": "id IS NOT NULL", "severity": "error"}]


# ── isolation ───────────────────────────────────────────────────────────


def _clear_loader_registries() -> None:
    """Empty every process-wide warn-once set of the loader (``_NOTED_*``)."""
    for name, value in vars(loader).items():
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_imported() -> None:
    imported = Path(loader.__file__).resolve()
    assert imported.is_relative_to(_CHECKOUT), f"{imported} is not inside {_CHECKOUT}"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    for name in ("FLUID_REF_ROOT", "FLUID_ENV", "FLUID_PROVIDER", "FLUID_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    _clear_loader_registries()
    yield
    _clear_loader_registries()


# ── small helpers ───────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _has_ref_key(node: Any) -> bool:
    """True when some mapping anywhere in ``node`` has a ``$ref`` KEY (substrings are text)."""
    stack = [node]
    seen = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            if "$ref" in current:
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _tree_documents(directory: Path) -> List[Tuple[str, Any]]:
    out = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix in (".yaml", ".yml", ".json"):
            out.append((p.relative_to(directory).as_posix(), _read(p)))
    return out


def cli(argv: Sequence[Any], capsys: pytest.CaptureFixture) -> Tuple[int, str]:
    """``fluid <argv>`` in process: ``(exit code, stdout + stderr)``."""
    from fluid_build.cli import main

    rc = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


# ── FX-PARITY ───────────────────────────────────────────────────────────


class Profile(NamedTuple):
    """Where FX-PARITY lands its table: the binding, and the path in dev and in prod."""

    platform: str
    format: str
    location: Dict[str, Any]
    dev_path: str
    prod_path: str


PROFILES = {
    "local": Profile("local", "csv", {}, DEV_PATH, S1_PATH),
    "aws": Profile(
        "aws",
        "parquet",
        {
            "bucket": "parity-lake",
            "database": "parity_db",
            "table": "out",
            "region": "eu-central-1",
        },
        "curated/dev/",
        "curated/S1-prod/",
    ),
}


def _path(env: Optional[str], profile: str) -> str:
    return PROFILES[profile].prod_path if env == "prod" else PROFILES[profile].dev_path


def _binding(profile: str, path: Any) -> Dict[str, Any]:
    p = PROFILES[profile]
    return {
        "platform": p.platform,
        "format": p.format,
        "location": {**p.location, "path": path},
    }


def expected_document(env: Optional[str], profile: str = "local") -> Dict[str, Any]:
    """The composed FX-PARITY contract, written out by hand for ``env`` (None or 'prod')."""
    prod = env == "prod"
    path = _path(env, profile)
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.parity",
        "name": "Parity product",
        "description": {"purpose": S4_TEXT, "usage": S5_USAGE, "limitations": "none"},
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": dict(TEAM)},
        "builds": [
            {
                "id": "transform",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {
                    "sql": S2_SQL if prod else DEV_SQL,
                    "parameters": {**PARAMS, "mode": S3_MODE if prod else DEV_MODE},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "version": "1.0.0",
                "description": S4_TEXT,
                "binding": _binding(profile, path),
                "contract": {"schema": copy.deepcopy(SCHEMA), "quality": copy.deepcopy(QUALITY)},
            }
        ],
        "extensions": {"acme": dict(ACME), "paths": {"out": path}},
    }


def build_parity(
    root: Path,
    *,
    profile: str = "local",
    overlay_fragment: Optional[Dict[str, Any]] = None,
    base_quality_ref: str = "../../quality/out.yaml",
    access_policy: Optional[Dict[str, Any]] = None,
    orchestration: Optional[Dict[str, Any]] = None,
) -> Path:
    """FX-PARITY under ``root``; the contract path.

    ``overlay_fragment`` replaces ``fragments/prod-binding.yaml`` (FX-PARITY-BAD,
    FX-BROKEN); ``base_quality_ref`` is the quality ref of the expose fragment
    (FX-BROKEN points it at a file that is not there); ``access_policy`` and
    ``orchestration`` each go in a fragment of their own.
    """
    expose_fragment: Dict[str, Any] = {
        "exposeId": "out",
        "kind": "table",
        "version": "1.0.0",
        "description": {"$ref": "#/description/purpose"},
        "binding": _binding(profile, {"$ref": "#/extensions/paths/out"}),
        "contract": {
            "schema": {"$ref": "../../schemas/out-columns.yaml"},
            "quality": {"$ref": base_quality_ref},
        },
    }
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.parity",
        "name": "Parity product",
        "description": {
            "purpose": S4_TEXT,
            "usage": {"$ref": "./docs/usage.md"},
            "limitations": "none",
        },
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": {"$ref": "./shared/team.yaml"}},
        "builds": [{"$ref": "./fragments/builds/transform.yaml"}],
        "exposes": [{"$ref": "./fragments/exposes/out.yaml"}],
        "extensions": {
            "acme": {"$ref": "./ext/acme.yaml"},
            "paths": {"out": _path(None, profile)},
        },
    }
    files: Dict[str, str] = {
        "shared/team.yaml": _dump(TEAM),
        "shared/params.yaml": _dump(PARAMS),
        "ext/acme.yaml": _dump(ACME),
        "docs/usage.md": S5_USAGE,
        "sql/transform.sql": DEV_SQL,
        "sql/transform.prod.sql": S2_SQL,
        "schemas/out-columns.yaml": _dump(SCHEMA),
        "quality/out.yaml": _dump(QUALITY),
        "fragments/builds/transform.yaml": _dump(
            {
                "id": "transform",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {
                    "sql": {"$ref": "../../sql/transform.sql"},
                    "parameters": {"$ref": "../../shared/params.yaml", "mode": DEV_MODE},
                },
            }
        ),
        "fragments/exposes/out.yaml": _dump(expose_fragment),
        "fragments/prod-binding.yaml": _dump(
            overlay_fragment
            if overlay_fragment is not None
            else {"location": {"path": {"$ref": "#/extensions/paths/out"}}}
        ),
        "overlays/prod.yaml": _dump(
            {
                "extensions": {"paths": {"out": _path("prod", profile)}},
                "builds": [
                    {
                        "id": "transform",
                        "properties": {
                            "sql": {"$ref": "../sql/transform.prod.sql"},
                            "parameters": {"mode": S3_MODE},
                        },
                    }
                ],
                "exposes": [
                    {"exposeId": "out", "binding": {"$ref": "../fragments/prod-binding.yaml"}}
                ],
            }
        ),
    }
    if access_policy is not None:
        contract["accessPolicy"] = {"$ref": "./fragments/access-policy.yaml"}
        files["fragments/access-policy.yaml"] = _dump(access_policy)
    if orchestration is not None:
        contract["orchestration"] = {"$ref": "./fragments/orchestration.yaml"}
        files["fragments/orchestration.yaml"] = _dump(orchestration)
    files["contract.fluid.yaml"] = _dump(contract)
    for rel, text in files.items():
        _write(root / rel, text)
    return root / "contract.fluid.yaml"


def build_flat(
    root: Path,
    *,
    profile: str = "local",
    access_policy: Optional[Dict[str, Any]] = None,
    orchestration: Optional[Dict[str, Any]] = None,
) -> Path:
    """The flat twin of FX-PARITY: the dev contract with no ref in it, and a prod
    overlay with none either, so every command can run ``--env prod`` on both."""
    base = expected_document(None, profile)
    prod = expected_document("prod", profile)
    if access_policy is not None:
        base["accessPolicy"] = copy.deepcopy(access_policy)
    if orchestration is not None:
        base["orchestration"] = copy.deepcopy(orchestration)
    overlay = {
        "extensions": {"paths": {"out": _path("prod", profile)}},
        "builds": [
            {
                "id": "transform",
                "properties": {
                    "sql": prod["builds"][0]["properties"]["sql"],
                    "parameters": {"mode": S3_MODE},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "out",
                "binding": {"location": {"path": _path("prod", profile)}},
            }
        ],
    }
    _write(root / "overlays" / "prod.yaml", _dump(overlay))
    return _write(root / "contract.fluid.yaml", _dump(base))


@pytest.fixture(scope="module")
def parity_master(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """FX-PARITY, built once for the module (read it; copy it to write beside it)."""
    return build_parity(tmp_path_factory.mktemp("fx-parity")).parent


@pytest.fixture(scope="module")
def flat_master(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The flat twin of FX-PARITY, built once for the module."""
    return build_flat(tmp_path_factory.mktemp("fx-flat")).parent


def _copy_of(master: Path, tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(master, target)
    return target / "contract.fluid.yaml"


@pytest.fixture
def fx(tmp_path: Path, parity_master: Path) -> Path:
    """A private copy of FX-PARITY: the contract path (commands may write beside it)."""
    return _copy_of(parity_master, tmp_path, "fx")


@pytest.fixture
def flat(tmp_path: Path, flat_master: Path) -> Path:
    """A private copy of the flat twin."""
    return _copy_of(flat_master, tmp_path, "flat")


class Composed(NamedTuple):
    """One call of the single composition entry point."""

    path: Path
    env: Optional[str]
    document: Dict[str, Any]


@pytest.fixture
def composition_spy(monkeypatch: pytest.MonkeyPatch) -> List[Composed]:
    """A spy on ``fluid_build.loader.compose_contract``, the one entry point: every
    call it serves, with the document it returned."""
    calls: List[Composed] = []
    real = loader.compose_contract

    def spy(contract_path: Any, env: Optional[str] = None, **kwargs: Any) -> Any:
        result = real(contract_path, env, **kwargs)
        calls.append(Composed(Path(contract_path), env, result.document))
        return result

    monkeypatch.setattr(loader, "compose_contract", spy)
    return calls


def _assert_sentinels(document: Dict[str, Any], env: Optional[str]) -> None:
    """S1..S5 under ``--env prod``, their dev values without an env."""
    prod = env == "prod"
    expose = document["exposes"][0]
    build = document["builds"][0]["properties"]
    assert expose["binding"]["location"]["path"] == (S1_PATH if prod else DEV_PATH)  # S1
    assert build["sql"] == (S2_SQL if prod else DEV_SQL)  # S2
    assert build["parameters"]["mode"] == (S3_MODE if prod else DEV_MODE)  # S3
    assert expose["description"] == S4_TEXT  # S4
    assert document["description"]["usage"] == S5_USAGE  # S5
    assert not _has_ref_key(document)


# ── G-01 · the library and plan agree ──────────────────────────────────────


def test_g01_the_library_contract_is_the_plan_contract_and_holds_every_sentinel(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.api.contract import load_contract

    plan = tmp_path / "plan.json"
    rc, text = cli(["plan", fx, "--env", "prod", "--out", plan], capsys)
    assert rc == 0, text

    loaded = load_contract(fx, env="prod")

    planned = json.loads(plan.read_text(encoding="utf-8"))["contract"]
    assert loaded.contract == planned
    assert loaded.contract == expected_document("prod")
    assert loaded.unresolved_refs == ()
    _assert_sentinels(loaded.contract, "prod")
    assert not _has_ref_key(planned)


def test_g01_without_an_env_both_hold_the_dev_values(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.api.contract import load_contract

    plan = tmp_path / "plan.json"
    assert cli(["plan", fx, "--out", plan], capsys)[0] == 0

    loaded = load_contract(fx)

    assert loaded.contract == json.loads(plan.read_text(encoding="utf-8"))["contract"]
    assert loaded.contract == expected_document(None)
    _assert_sentinels(loaded.contract, None)


# ── G-21 · text and path forms are one ─────────────────────────────────────


def test_g21_the_text_form_with_its_overlay_equals_the_path_form(fx: Path) -> None:
    from fluid_build.api.contract import load_contract, load_contract_from_text

    overlay = _read(fx.parent / "overlays" / "prod.yaml")

    from_text = load_contract_from_text(
        fx.read_text(encoding="utf-8"),
        base_dir=fx.parent,
        overlay=overlay,
        overlay_dir=fx.parent / "overlays",
    )
    from_path = load_contract(fx, env="prod")

    assert from_text.contract == from_path.contract == expected_document("prod")
    assert from_text.digest == from_path.digest
    assert from_text.unresolved_refs == ()


def test_g21_an_overlay_without_its_directory_is_read_relative_to_the_base_dir(fx: Path) -> None:
    """SG-22, characterization: the overlay's refs are relative to ``overlay_dir``,
    default ``base_dir``. ``../sql/...`` written for ``overlays/`` therefore climbs out
    of the root from ``base_dir`` and is refused (typed, naming the ref), never read."""
    from fluid_build.api.contract import ContractLoadError, load_contract_from_text

    overlay = _read(fx.parent / "overlays" / "prod.yaml")

    with pytest.raises(ContractLoadError) as err:
        load_contract_from_text(fx.read_text(encoding="utf-8"), base_dir=fx.parent, overlay=overlay)

    assert err.value.event == "contract_ref_unresolved"
    assert "../sql/transform.prod.sql" in str(err.value)


# ── G-22 · one composed document ───────────────────────────────────────────


def test_g22_every_loader_entry_returns_the_one_composed_document(fx: Path) -> None:
    from fluid_build.cli.bundle import materialize_contract

    log = logging.getLogger("test.g22")

    base = loader.load_contract(fx)
    assert base == loader.compile_contract(fx) == loader.load_with_overlay(fx, None)
    assert base == expected_document(None)

    prod = loader.load_with_overlay(fx, "prod")
    assert prod == materialize_contract(fx, "prod", log)[0] == expected_document("prod")
    assert loader.compose_contract(fx, "prod").document == prod


_ALIAS_CONTRACT = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "refs.alias",
    "name": "Alias",
    "exposes": [{"$ref": "./e.yaml"}],
}
_ALIAS_EXPOSE = {
    "exposeId": "e",
    "kind": "topic",
    "binding": {"platform": "kafka", "format": "kafka-topic", "location": {"topic": "t"}},
}


def test_g22_alias_values_are_rewritten_only_by_the_command_loader(tmp_path: Path) -> None:
    """Normalisation (alias values, ``build:`` -> ``builds:``) is the engine loader's step.
    The composition itself, and ``fluid bundle`` which freezes it, leave a value as written."""
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.cli.bundle import materialize_contract

    path = _write(tmp_path / "p" / "contract.fluid.yaml", _dump(_ALIAS_CONTRACT))
    _write(path.parent / "e.yaml", _dump(_ALIAS_EXPOSE))
    log = logging.getLogger("test.g22.alias")

    def fmt(document: Dict[str, Any]) -> str:
        return document["exposes"][0]["binding"]["format"]

    assert fmt(loader.load_contract(path)) == "kafka-topic"
    assert fmt(loader.load_with_overlay(path, None)) == "kafka-topic"
    assert fmt(materialize_contract(path, None, log)[0]) == "kafka-topic"
    assert fmt(load_contract_with_overlay(str(path), None, log)) == "kafka_topic"


# ── G-23 · composed once, no reload ────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["validate", "{C}", "--env", "prod"], id="validate"),
        pytest.param(["plan", "{C}", "--env", "prod", "--out", "{OUT}/plan.json"], id="plan"),
    ],
)
def test_g23_validate_and_plan_compose_the_contract_once_with_no_reload(
    tmp_path: Path,
    fx: Path,
    capsys: pytest.CaptureFixture,
    composition_spy: List[Composed],
    argv: List[str],
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    rc, text = cli([a.replace("{C}", str(fx)).replace("{OUT}", str(out)) for a in argv], capsys)

    assert rc == 0, text
    assert len(composition_spy) == 1
    assert composition_spy[0].env == "prod"
    assert not _has_ref_key(composition_spy[0].document)
    assert "compile_start" not in text and "compile_done" not in text


def test_g23_the_auto_bundle_reload_is_gone() -> None:
    from fluid_build import _contract_loader

    assert not hasattr(_contract_loader, "_auto_bundle_if_needed")
    assert not hasattr(_contract_loader, "_has_ref_pointers")


# ── running a command in a tree ─────────────────────────────────────────────


def run_in(
    directory: Path,
    argv: Sequence[Any],
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[int, str]:
    """``fluid <argv>`` from inside ``directory``, so a relative contract path reads alike
    in two trees and neither output holds the tree's own name."""
    monkeypatch.chdir(directory)
    return cli(argv, capsys)


def _squash(text: str) -> str:
    """``text`` with every whitespace gone: the console wraps a long path anywhere."""
    return "".join(text.split())


def _plain(text: str) -> str:
    """``text`` reduced to the characters of a path, name or value: what a table border
    and a wrapped line cannot split."""
    return re.sub(r"[^A-Za-z0-9._/:\\-]", "", text)


def _volatile_free(text: str) -> str:
    """``text`` without what differs between two runs of one command: clocks, durations."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,\d]*Z?", "<TIME>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", text)
    text = re.sub(r"\d+\.\d+s", "<SECONDS>", text)
    return re.sub(r"[⠁-⣿]", "", text)


def _files(directory: Path) -> Dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


#: What a command writes beside the contract is only what it created: the tree the
#: test copied before the command ran is the baseline.
def _created(directory: Path, before: Dict[str, bytes]) -> Dict[str, bytes]:
    return {
        name: data
        for name, data in _files(directory).items()
        if before.get(name) != data
        and not name.startswith((".fluid/", "runtime/validation-history"))
    }


# ── G-02 · validate gates what the overlay brings ──────────────────────────


@pytest.fixture(scope="module")
def parity_bad_master(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """FX-PARITY-BAD: the prod binding fragment also holds a key the binding refuses."""
    fragment = {"location": {"path": {"$ref": "#/extensions/paths/out"}}, "bogus": 1}
    return build_parity(tmp_path_factory.mktemp("fx-bad"), overlay_fragment=fragment).parent


def test_g02_validate_strict_passes_the_parity_contract_with_and_without_the_env(
    fx: Path, capsys: pytest.CaptureFixture
) -> None:
    for argv in (["validate", fx, "--env", "prod", "--strict"], ["validate", fx, "--strict"]):
        rc, text = cli(argv, capsys)
        assert rc == 0, text


def test_g02_a_key_only_the_prod_fragment_holds_fails_prod_and_passes_dev(
    tmp_path: Path, parity_bad_master: Path, capsys: pytest.CaptureFixture
) -> None:
    bad = _copy_of(parity_bad_master, tmp_path, "bad")

    rc_prod, text = cli(["validate", bad, "--env", "prod", "--strict"], capsys)
    rc_dev, dev_text = cli(["validate", bad], capsys)

    assert rc_prod == 1, text
    assert "bogus" in text
    # The error names the fragment the key came from.
    assert "fragments/prod-binding.yaml" in _squash(text)
    # The key is prod-only: without an env nothing sees the fragment.
    assert rc_dev == 0, dev_text


# ── G-03 · a plan of the contract and a plan of its bundle ─────────────────


def test_g03_the_plan_of_a_bundle_is_the_plan_of_the_contract(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture
) -> None:
    """The catalog expects equal ``planDigest`` values for the two plans. The digest
    covers ``bindingMode`` and ``contract_metadata`` (the source path or the bundle),
    which differ between a contract and its bundle by design; so the substance is
    compared: the contract and every action are the same, and each plan's digest is
    stable across two runs on the same input."""
    bundle = tmp_path / "bundle.tgz"
    assert cli(["bundle", fx, "--env", "prod", "--format", "tgz", "--out", bundle], capsys)[0] == 0
    plans = {}
    for name, source in (("raw", fx), ("bound", bundle)):
        for run_number in (1, 2):
            out = tmp_path / f"{name}-{run_number}.json"
            rc, text = cli(["plan", source, "--env", "prod", "--out", out], capsys)
            assert rc == 0, text
            plans[(name, run_number)] = json.loads(out.read_text(encoding="utf-8"))

    raw, bound = plans[("raw", 1)], plans[("bound", 1)]
    assert raw["contract"] == bound["contract"] == expected_document("prod")
    assert raw["actions"] == bound["actions"]
    assert raw["dependency_graph"] == bound["dependency_graph"]
    assert plans[("raw", 1)]["planDigest"] == plans[("raw", 2)]["planDigest"]
    assert plans[("bound", 1)]["planDigest"] == plans[("bound", 2)]["planDigest"]
    assert not _has_ref_key(raw) and not _has_ref_key(bound)


# ── G-04 · bundle, in every format ─────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_g04_bundle_prod_holds_every_sentinel_in_a_single_document(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture, fmt: str
) -> None:
    out = tmp_path / f"bundle.{fmt}"

    rc, text = cli(["bundle", fx, "--env", "prod", "--format", fmt, "--out", out], capsys)

    assert rc == 0, text
    document = json.loads(out.read_text("utf-8")) if fmt == "json" else _read(out)
    _assert_sentinels(document, "prod")
    assert document == expected_document("prod")


def test_g04_the_tgz_holds_only_the_manifest_and_the_resolved_contract(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "bundle.tgz"

    rc, text = cli(["bundle", fx, "--env", "prod", "--format", "tgz", "--out", out], capsys)

    assert rc == 0, text
    with tarfile.open(out, "r:gz") as tar:
        names = sorted(tar.getnames())
        resolved_yaml = yaml.safe_load(tar.extractfile("contract.resolved.yaml").read())  # type: ignore[union-attr]
        resolved_json = json.loads(tar.extractfile("contract.resolved.json").read())  # type: ignore[union-attr]
    assert names == ["MANIFEST.json", "contract.resolved.json", "contract.resolved.yaml"]
    assert resolved_yaml == resolved_json == expected_document("prod")
    _assert_sentinels(resolved_yaml, "prod")


# ── G-05 · the standard exports, against the flat twin ─────────────────────


def _mask_dates(text: str) -> str:
    """ODPS v4.1 stamps ``created`` / ``updated`` with today's UTC date."""
    return re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", text)


@pytest.mark.parametrize("env", ["prod", None], ids=["prod", "no-env"])
@pytest.mark.parametrize("fmt", ["odcs", "odps", "odps-v4.1"])
def test_g05_a_standard_export_equals_the_flat_twins(
    fx: Path,
    flat: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fmt: str,
    env: Optional[str],
) -> None:
    outputs = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        argv = ["generate", "standard", "contract.fluid.yaml", "--format", fmt]
        rc, text = run_in(
            contract.parent,
            [*argv, *(["--env", env] if env else []), "--out", "export.out"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        outputs[name] = (contract.parent / "export.out").read_text(encoding="utf-8")

    assert _mask_dates(outputs["fx"]) == _mask_dates(outputs["flat"])
    assert not _has_ref_key(yaml.safe_load(outputs["fx"]))


def test_g05_the_odcs_export_carries_the_prod_values(
    fx: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, text = run_in(
        fx.parent,
        ["generate", "standard", "contract.fluid.yaml", "--format", "odcs", "--env", "prod"]
        + ["--out", "x.odcs.yaml"],
        capsys,
        monkeypatch,
    )

    assert rc == 0, text
    exported = (fx.parent / "x.odcs.yaml").read_text(encoding="utf-8")
    assert S1_PATH in exported and S4_TEXT in exported and "S5-USAGE" in exported
    assert DEV_PATH not in exported


# ── G-06 · generate artifacts ──────────────────────────────────────────────


def _artifacts(
    contract: Path,
    out: str,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    *env: str,
) -> Dict[str, bytes]:
    rc, text = run_in(
        contract.parent,
        ["generate", "artifacts", contract.name, "--out", out, *(["--env", env[0]] if env else [])],
        capsys,
        monkeypatch,
    )
    assert rc == 0, text
    return _files(contract.parent / out)


def test_g06_the_prod_artifacts_have_no_ref_and_equal_the_flat_twins(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_fx = _artifacts(fx, "art", capsys, monkeypatch, "prod")
    from_flat = _artifacts(flat, "art", capsys, monkeypatch, "prod")

    assert from_fx == from_flat
    for name, data in from_fx.items():
        if name.endswith((".yaml", ".yml", ".json")):
            assert not _has_ref_key(yaml.safe_load(data)), name


def test_g06_without_an_env_the_artifacts_equal_those_of_the_no_env_bundle(
    tmp_path: Path, fx: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    flat_bundle = tmp_path / "nb" / "contract.fluid.yaml"
    assert cli(["bundle", fx, "--out", flat_bundle], capsys)[0] == 0

    from_contract = _artifacts(fx, "art", capsys, monkeypatch)
    from_bundle = _artifacts(flat_bundle, "art", capsys, monkeypatch)

    assert from_contract == from_bundle
    for name, data in from_contract.items():
        if name.endswith((".yaml", ".yml", ".json")):
            assert not _has_ref_key(yaml.safe_load(data)), name
    # The dev values, since no overlay was asked for.
    assert DEV_PATH in b"\n".join(from_contract.values()).decode("utf-8")


# ── G-07 · schedule artifacts and the deprecated alias ─────────────────────


def _without_clocks(data: bytes) -> str:
    return "\n".join(line for line in data.decode("utf-8").splitlines() if "Generated" not in line)


def test_g07_generate_schedule_reads_the_composed_contract(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    dags = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        argv = ["generate", "schedule", "contract.fluid.yaml", "--scheduler", "airflow"]
        rc, text = run_in(
            contract.parent, [*argv, "--env", "prod", "--out", "dags"], capsys, monkeypatch
        )
        assert rc == 0, text
        assert "$ref" not in text
        dags[name] = _files(contract.parent / "dags")

    assert dags["fx"].keys() == dags["flat"].keys() == {"refs_parity_dag.py"}
    assert _without_clocks(dags["fx"]["refs_parity_dag.py"]) == _without_clocks(
        dags["flat"]["refs_parity_dag.py"]
    )
    assert "refs.parity" in dags["fx"]["refs_parity_dag.py"].decode("utf-8")


def test_g07_the_deprecated_generate_airflow_alias_agrees(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    dags = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(
            contract.parent,
            ["generate-airflow", "contract.fluid.yaml", "--env", "prod", "-o", "dag.py"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        dags[name] = (contract.parent / "dag.py").read_bytes()

    assert _without_clocks(dags["fx"]) == _without_clocks(dags["flat"])
    assert "FLUID Data Product: Parity product" in dags["fx"].decode("utf-8")


# ── G-08 · generate ci ─────────────────────────────────────────────────────


def test_g08_generate_ci_sees_the_composed_documents_and_writes_the_flat_twins_pipeline(
    fx: Path,
    flat: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    composition_spy: List[Composed],
) -> None:
    written = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        before = _files(contract.parent)
        rc, text = run_in(
            contract.parent,
            ["generate", "ci", "contract.fluid.yaml", "--system", "github-actions"]
            + ["--out", "ci.yml"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        written[name] = _created(contract.parent, before)

    assert written["fx"] == written["flat"]
    assert "ci.yml" in written["fx"]
    # Every document its scan composed (the contract and each overlay) has no ref.
    assert composition_spy, "generate ci never reached the composition entry point"
    assert not any(_has_ref_key(call.document) for call in composition_spy)
    assert {call.env for call in composition_spy} >= {None, "prod"}


# ── G-09 · generate iac (an AWS binding) ───────────────────────────────────


@pytest.fixture
def no_cloud(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """No credentials, and no client: ``generate iac`` must emit without any cloud call."""
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FLUID_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    made: List[str] = []
    session = pytest.importorskip("botocore.session")

    def refuse(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        made.append(service_name)
        raise AssertionError(f"a boto3 {service_name} client was constructed")

    monkeypatch.setattr(session.Session, "create_client", refuse)
    return made


def test_g09_generate_iac_emits_the_flat_twins_module_with_no_ref_in_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    no_cloud: List[str],
) -> None:
    fx_aws = build_parity(tmp_path / "fx-aws", profile="aws")
    flat_aws = build_flat(tmp_path / "flat-aws", profile="aws")
    modules = {}
    for name, contract in (("fx", fx_aws), ("flat", flat_aws)):
        rc, text = run_in(
            contract.parent,
            ["generate", "iac", "contract.fluid.yaml", "--env", "prod", "--out", "iac"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        modules[name] = (contract.parent / "iac" / "main.tf.json").read_text(encoding="utf-8")

    assert modules["fx"] == modules["flat"]
    assert "$ref" not in modules["fx"] and "l_-ref" not in modules["fx"]
    assert PROFILES["aws"].prod_path in modules["fx"]
    assert PROFILES["aws"].dev_path not in modules["fx"]
    assert no_cloud == []


# ── G-10 · apply ───────────────────────────────────────────────────────────


def test_g10_apply_dry_run_plans_the_prod_contract(
    fx: Path, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    rc, text = cli(["apply", fx, "--env", "prod", "--dry-run"], capsys)

    assert rc == 0, text
    assert "$ref" not in text
    assert not (fx.parent / "runtime").exists()
    # The actions ``apply`` runs are those ``plan`` writes: S1 is in them, never the dev path.
    plan = tmp_path / "plan.json"
    assert cli(["plan", fx, "--env", "prod", "--out", plan], capsys)[0] == 0
    actions = json.dumps(json.loads(plan.read_text(encoding="utf-8"))["actions"])
    assert S1_PATH in actions and DEV_PATH not in actions


def test_g10_the_build_runs_the_sql_text_of_the_prod_fragment(
    fx: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("duckdb")
    import fluid_build.providers.local.local as local_provider

    executed: List[str] = []
    real = local_provider.query_relation

    def spy(con: Any, sql: str) -> Any:
        executed.append(sql)
        return real(con, sql)

    monkeypatch.setattr(local_provider, "query_relation", spy)

    rc, text = run_in(
        fx.parent,
        ["apply", "contract.fluid.yaml", "--env", "prod", "--mode", "amend-and-build", "--yes"],
        capsys,
        monkeypatch,
    )

    assert rc == 0, text
    # S2: the text of sql/transform.prod.sql ran, not the base's text.
    assert [sql for sql in executed if "S2-PROD" in sql], executed
    assert not [sql for sql in executed if "S2-DEV" in sql]
    # S1: and its result landed at the prod path.
    assert (fx.parent / S1_PATH).read_text(encoding="utf-8").split() == ["id", "1"]
    assert not (fx.parent / DEV_PATH).exists()


# ── G-11 · the policy commands ─────────────────────────────────────────────

_ACCESS_POLICY = {
    "grants": [
        {"principal": "role:data-analyst", "permissions": ["read", "select"]},
        {"principal": "role:data-engineer", "permissions": ["read", "write"]},
    ]
}


def test_g11_policy_compile_and_check_read_an_access_policy_kept_in_a_fragment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    no_cloud: List[str],
) -> None:
    fx_policy = build_parity(tmp_path / "fx-pol", profile="aws", access_policy=_ACCESS_POLICY)
    flat_policy = build_flat(tmp_path / "flat-pol", profile="aws", access_policy=_ACCESS_POLICY)
    compiled, checked = {}, {}
    for name, contract in (("fx", fx_policy), ("flat", flat_policy)):
        rc, text = run_in(
            contract.parent,
            ["policy-compile", "contract.fluid.yaml", "--env", "prod", "--out", "pol.json"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        compiled[name] = (contract.parent / "pol.json").read_bytes()
        rc, text = run_in(
            contract.parent,
            ["policy-check", "contract.fluid.yaml", "--env", "prod"],
            capsys,
            monkeypatch,
        )
        checked[name] = (rc, _volatile_free(text))

    assert compiled["fx"] == compiled["flat"]
    # Not vacuous: the grants of the fragment became bindings.
    bindings = json.loads(compiled["fx"])["bindings"]
    assert {b["principal"] for b in bindings} == {"role:data-analyst", "role:data-engineer"}
    assert checked["fx"] == checked["flat"]
    assert checked["fx"][0] == 0


# ── G-12 · contract-validation, contract-tests, test ───────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ["contract-validation", "contract.fluid.yaml", "--env", "prod"], id="validation"
        ),
        pytest.param(["contract-tests", "contract.fluid.yaml", "--env", "prod"], id="tests"),
        pytest.param(["test", "contract.fluid.yaml", "--env", "prod"], id="test"),
    ],
)
def test_g12_the_checks_give_the_flat_twins_results(
    fx: Path,
    flat: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    argv: List[str],
) -> None:
    results = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(contract.parent, argv, capsys, monkeypatch)
        results[name] = (rc, _volatile_free(text.replace(str(contract.parent), "<DIR>")))

    assert results["fx"][0] == results["flat"][0]
    assert results["fx"][1] == results["flat"][1]
    assert "$ref" not in results["fx"][1]


def test_g12_the_checks_look_for_the_prod_file(
    fx: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that composed the base only would look for ``dev.csv``: S1 proves the overlay."""
    rc, text = run_in(
        fx.parent,
        ["contract-validation", "contract.fluid.yaml", "--env", "prod"],
        capsys,
        monkeypatch,
    )

    assert rc == 1
    assert "S1-prod.csv" in _plain(text) and "dev.csv" not in _plain(text)


def test_g12_a_contract_tests_baseline_is_the_flat_twins_and_sees_a_column_removed_in_a_fragment(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``contract-tests`` reads ``exposes[].contract.schema``: the check that does see 0.7.x
    columns (``diff`` does not: G-13)."""
    baselines = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        argv = ["contract-tests", "contract.fluid.yaml", "--env", "prod"]
        rc, text = run_in(
            contract.parent, [*argv, "--write-baseline", "b.json"], capsys, monkeypatch
        )
        assert rc == 0, text
        baselines[name] = (contract.parent / "b.json").read_bytes()
    assert baselines["fx"] == baselines["flat"]

    argv = ["contract-tests", "contract.fluid.yaml", "--env", "prod", "--baseline", "b.json"]
    assert run_in(fx.parent, argv, capsys, monkeypatch)[0] == 0
    _write(fx.parent / "schemas" / "out-columns.yaml", _dump(SCHEMA[:1]))
    rc, text = run_in(fx.parent, argv, capsys, monkeypatch)

    assert rc != 0
    assert "out.label: column removed" in text


# ── G-13 · diff ────────────────────────────────────────────────────────────


def test_g13_the_diff_of_a_ref_contract_against_its_flat_twin_has_no_changes(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, text = cli(["diff", fx, "--baseline", flat], capsys)

    assert rc == 0, text
    assert "No changes detected" in text


def test_g13_an_expose_the_baseline_has_and_the_ref_contract_lost_is_a_breaking_change(
    tmp_path: Path, fx: Path, flat: Path, capsys: pytest.CaptureFixture
) -> None:
    """Both sides are composed: matched by ``exposeId``, the ref contract's one expose
    is ``out`` (raw, it would be an id-less ``$ref`` node and 'out' would be 'removed')."""
    document = _read(flat)
    extra = copy.deepcopy(document["exposes"][0])
    extra["exposeId"] = "extra"
    document["exposes"].append(extra)
    baseline = _write(tmp_path / "baseline" / "contract.fluid.yaml", _dump(document))

    rc, text = cli(["diff", fx, "--baseline", baseline, "--fail-on-breaking"], capsys)

    assert rc == 1, text
    assert "expose 'extra' removed" in text
    assert "expose 'out' removed" not in text and "added" not in text


@pytest.mark.xfail(
    strict=True,
    reason="BUG G13-diff-columns: fluid diff --baseline reads exposes[].schema, but a 0.7.x "
    "contract keeps its columns in exposes[].contract.schema, so a removed column is "
    "'No changes detected' (a flat contract too)",
)
def test_g13_a_column_removed_inside_the_schema_fragment_is_detected(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture
) -> None:
    schema = fx.parent / "schemas" / "out-columns.yaml"
    _write(schema, _dump(_read(schema)[:1]))

    rc, text = cli(["diff", fx, "--baseline", flat, "--fail-on-breaking"], capsys)

    assert rc == 1, text
    assert "column 'label' removed" in text


@pytest.mark.xfail(
    strict=True,
    reason="BUG G13-diff-columns: fluid diff --baseline reads exposes[].schema, but a 0.7.x "
    "contract keeps its columns in exposes[].contract.schema, so a removed column is "
    "'No changes detected' (flat contract too)",
)
def test_g13_a_column_removed_from_a_flat_contract_is_detected_too(
    tmp_path: Path, flat: Path, capsys: pytest.CaptureFixture
) -> None:
    """The same removal in the flat twin: the gap is in ``diff``, not in the refs."""
    document = _read(flat)
    document["exposes"][0]["contract"]["schema"] = document["exposes"][0]["contract"]["schema"][:1]
    changed = _write(tmp_path / "changed" / "contract.fluid.yaml", _dump(document))

    rc, text = cli(["diff", changed, "--baseline", flat, "--fail-on-breaking"], capsys)

    assert rc == 1, text
    assert "column 'label' removed" in text


# ── G-14 · the exports ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, out",
    [
        pytest.param(["export-opds", "contract.fluid.yaml", "--env", "prod"], "o.json", id="opds"),
        pytest.param(
            ["odcs", "export", "contract.fluid.yaml", "--env", "prod"], "o.odcs", id="odcs"
        ),
        pytest.param(
            ["odps-bitol", "export", "contract.fluid.yaml", "--env", "prod"], "o.odps", id="bitol"
        ),
    ],
)
def test_g14_an_export_equals_the_flat_twins(
    fx: Path,
    flat: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    argv: List[str],
    out: str,
) -> None:
    exported = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(contract.parent, [*argv, "--out", out], capsys, monkeypatch)
        assert rc == 0, text
        exported[name] = (contract.parent / out).read_text(encoding="utf-8")

    assert _mask_dates(exported["fx"]) == _mask_dates(exported["flat"])
    assert not _has_ref_key(yaml.safe_load(exported["fx"]))


def test_g14_export_hands_the_provider_the_composed_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fluid export`` needs an orchestration block; kept in a fragment, the provider must
    still get it, composed, and the same document the flat twin gives."""
    orchestration = {"tasks": [{"id": "t1", "type": "sql", "depends_on": []}]}
    fx_orch = build_parity(tmp_path / "fx-orch", orchestration=orchestration)
    flat_orch = build_flat(tmp_path / "flat-orch", orchestration=orchestration)
    import fluid_build.cli.export as export_cmd

    received: List[Dict[str, Any]] = []

    class FakeProvider:
        def export(self, *, contract: Dict[str, Any], engine: str, output_dir: str) -> str:
            received.append(copy.deepcopy(contract))
            out = Path(output_dir) / "dag.py"
            out.write_text("# dag\n", encoding="utf-8")
            return str(out)

    monkeypatch.setattr(export_cmd, "build_provider", lambda *a, **k: FakeProvider())
    for contract in (fx_orch, flat_orch):
        rc, text = run_in(
            contract.parent,
            ["export", "contract.fluid.yaml", "--env", "prod", "--provider", "aws"]
            + ["--output-dir", "exported"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text

    assert len(received) == 2
    assert received[0] == received[1]
    assert received[0]["orchestration"] == orchestration
    assert not _has_ref_key(received[0])


# ── G-15 · viz-graph ───────────────────────────────────────────────────────


def test_g15_the_graph_of_a_ref_contract_is_the_flat_twins(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphs = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(
            contract.parent,
            ["viz-graph", "contract.fluid.yaml", "--env", "prod", "--out", "graph"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        assert "$ref" not in text
        graphs[name] = next(contract.parent.glob("graph.*")).read_text(encoding="utf-8")

    assert graphs["fx"] == graphs["flat"]
    assert "refs_parity" in graphs["fx"] and "$ref" not in graphs["fx"]


# ── G-17 · docs ────────────────────────────────────────────────────────────


def test_g17_docs_show_the_port_by_its_exposeid_with_its_columns(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(
            contract.parent,
            ["docs", "--files", "contract.fluid.yaml", "--out", "site"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        pages[name] = _files(contract.parent / "site")

    assert pages["fx"] == pages["flat"]
    page = pages["fx"]["contract-refs-parity.html"].decode("utf-8")
    assert '<h3 id="expose-0-h"><code>out</code></h3>' in page
    assert "<td><code>id</code></td><td>integer</td><td>no</td>" in page
    assert "<td><code>label</code></td><td>string</td><td>yes</td>" in page
    assert "expose-0</code>" not in page
    index = json.loads(pages["fx"]["index.json"])
    assert index[0]["id"] == "refs.parity" and "error" not in index[0]


# ── G-16 · every other command reaches the one loader ──────────────────────

#: Commands whose contract load is observable offline. Each runs ``--env prod`` on FX-PARITY
#: under the spy; some then fail for a reason of their own (no live target, no API key),
#: which is why only the composition is checked.
_G16_COMMANDS = {
    "verify": lambda c, o: ["verify", c, "--env", "prod"],
    "publish": lambda c, o: ["publish", c, "--env", "prod", "--dry-run"],
    "datamesh-manager": lambda c, o: ["datamesh-manager", "publish", c, "--dry-run"],
    "scaffold-composer": lambda c, o: ["scaffold-composer", c, "--env", "prod", "--out", o / "c"],
    "dbt-tests": lambda c, o: ["generate", "dbt-tests", c, "--env", "prod", "--out", o / "d"],
    "vector": lambda c, o: ["generate", "vector", c, "--env", "prod", "--out", o / "v"],
    "transformation": lambda c, o: [
        "generate",
        "transformation",
        c,
        "--env",
        "prod",
        "--out",
        o / "t",
    ],
    "mcp-output-port": lambda c, o: ["mcp", "output-port", "list", c, "--env", "prod"],
    "apply-build-runner": lambda c, o: [
        "apply",
        c,
        "--env",
        "prod",
        "--mode",
        "amend-and-build",
        "--yes",
    ],
}


@pytest.mark.parametrize(
    "command",
    [
        # The first ``generate dbt-tests`` of a process imports its generators (4 s alone).
        pytest.param(name, marks=pytest.mark.slow) if name == "dbt-tests" else name
        for name in sorted(_G16_COMMANDS)
    ],
)
def test_g16_the_command_composes_through_the_one_entry_point(
    tmp_path: Path,
    fx: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    composition_spy: List[Composed],
    command: str,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(fx.parent)

    _rc, text = cli(_G16_COMMANDS[command](fx, out), capsys)

    assert composition_spy, f"{command} never called fluid_build.loader.compose_contract: {text}"
    # ``datamesh-manager publish`` takes no ``--env``; ``apply`` also reads the base alone,
    # for the Command Center report (an env of None).
    env = None if command == "datamesh-manager" else "prod"
    served = [call for call in composition_spy if call.env == env]
    assert served, [call.env for call in composition_spy]
    assert not any(_has_ref_key(call.document) for call in composition_spy)
    assert served[0].document == expected_document(env)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["verify", "contract.fluid.yaml", "--env", "prod"], id="verify"),
        pytest.param(
            ["publish", "contract.fluid.yaml", "--env", "prod", "--dry-run"], id="publish"
        ),
        pytest.param(
            ["mcp", "output-port", "list", "contract.fluid.yaml", "--env", "prod"], id="mcp"
        ),
    ],
)
def test_g16_verify_publish_and_mcp_list_answer_as_they_do_for_the_flat_twin(
    fx: Path,
    flat: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    argv: List[str],
) -> None:
    answers = {}
    for name, contract in (("fx", fx), ("flat", flat)):
        rc, text = run_in(contract.parent, argv, capsys, monkeypatch)
        # The console wraps a long path anywhere: compare the text with its whitespace gone.
        squashed = _squash(_volatile_free(text))
        answers[name] = (rc, squashed.replace(_squash(str(contract.parent)), "<DIR>"))

    assert answers["fx"] == answers["flat"]
    assert "$ref" not in answers["fx"][1]


def test_g16_mcp_output_port_serve_loads_the_composed_contract(
    fx: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    composition_spy: List[Composed],
) -> None:
    import fluid_build.output_ports.mcp as mcp_port

    served: List[Any] = []
    monkeypatch.setattr(mcp_port, "run_stdio", lambda *a, **k: served.append((a, k)) or 0)

    rc, text = cli(["mcp", "output-port", "serve", fx, "--env", "prod"], capsys)

    assert rc == 0, text
    assert composition_spy and not any(_has_ref_key(c.document) for c in composition_spy)
    # What reached the server is the composed contract, not the raw root.
    assert served and not _has_ref_key(served[0][1]["contract"])
    assert served[0][1]["contract"] == expected_document("prod")


def test_g16_the_init_dag_helper_loads_through_the_command_loader(
    fx: Path, composition_spy: List[Composed]
) -> None:
    from fluid_build.cli._init_dag_helpers import _load_as_the_command_does

    document = _load_as_the_command_does(fx, logging.getLogger("test.g16"))

    assert document == expected_document(None)
    assert len(composition_spy) == 1 and not _has_ref_key(composition_spy[0].document)


def test_g16_an_embedded_sql_build_reads_an_upstream_product_composed(
    fx: Path, composition_spy: List[Composed]
) -> None:
    from fluid_build.build_runners._embedded_sql_io import _upstream_expose

    upstream, expose = _upstream_expose(fx, "refs.parity", "out", "prod", logging.getLogger("g16"))

    assert expose["binding"]["location"]["path"] == S1_PATH
    assert not _has_ref_key(upstream)
    assert composition_spy and not any(_has_ref_key(c.document) for c in composition_spy)


def test_g16_schedule_sync_reads_its_contracts_through_the_command_loader() -> None:
    """``schedule-sync`` loads a contract only for its acquisition artifacts, and no flag of the
    command sets ``contracts`` (so the loop is not reachable from the CLI): checked in the
    source, with the loader it calls shown to compose."""
    import ast
    import inspect

    from fluid_build.cli import schedule_sync

    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(schedule_sync)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "load_contract_with_overlay" in called
    assert not {"_parse_file", "load_yaml_safe"} & called


# ── G-18 · the MCP tools read a fragmented contract whole ──────────────────

_SEMANTICS = {
    "name": "out",
    "description": "The output port.",
    "entities": [{"name": "row", "type": "primary", "expr": "id"}],
    "dimensions": [{"name": "label", "type": "categorical"}],
    "measures": [{"name": "rows", "agg": "count", "expr": "id"}],
    "metrics": [{"name": "row_count", "type": "simple", "measure": "rows"}],
}


def _semantic_contract(root: Path) -> Path:
    """A contract whose one expose keeps its ``semantics`` in a fragment of its own."""
    contract = expected_document(None)
    contract["exposes"][0]["semantics"] = {"$ref": "./semantics.yaml"}
    _write(root / "semantics.yaml", _dump(_SEMANTICS))
    return _write(root / "contract.fluid.yaml", _dump(contract))


def test_g18_the_mcp_validate_tool_composes_a_ref_that_stays_inside_its_directory(
    tmp_path: Path,
) -> None:
    from fluid_build.cli.mcp.dispatch import _call_tool

    path = _semantic_contract(tmp_path / "p")
    flat = tmp_path / "flat" / "contract.fluid.yaml"
    document = expected_document(None)
    document["exposes"][0]["semantics"] = copy.deepcopy(_SEMANTICS)
    _write(flat, _dump(document))

    report = _call_tool("validate_contract", {"contract_path": str(path)}, read_only=True)
    flat_report = _call_tool("validate_contract", {"contract_path": str(flat)}, read_only=True)

    assert report["passes_schema"] is True, report
    assert report == flat_report
    assert "$ref" not in json.dumps(report)


def test_g18_the_mcp_tools_resolve_a_contract_path_to_the_composed_document(fx: Path) -> None:
    from fluid_build.cli.mcp.dispatch import _resolve_contract_argument

    assert _resolve_contract_argument({"contract_path": str(fx)}) == expected_document(None)


def test_g18_score_contract_quality_judges_the_composed_contract(
    fx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge is stubbed (the real one calls a model): what is checked is the contract it
    is handed."""
    from fluid_build.cli.mcp import dispatch
    from fluid_build.copilot.agents import judge_agent

    seen: List[Dict[str, Any]] = []

    class Result:
        total, model, critique_applied, axes = 0, "stub", False, {}

    class StubJudge:
        def judge(self, contract: Dict[str, Any], build_artifacts: Any = None) -> Result:
            seen.append(contract)
            return Result()

    monkeypatch.setattr(judge_agent, "JudgeAgent", StubJudge)

    dispatch._call_tool("score_contract_quality", {"contract_path": str(fx)}, read_only=True)

    assert seen == [expected_document(None)]
    assert not _has_ref_key(seen[0])


# ── G-19 / G-28 · a ref that cannot be resolved ────────────────────────────

#: FX-BROKEN: a dangling ref in a fragment of the base, and one in a fragment of the overlay.
_BROKEN = {
    "base_fragment": {
        "build": {"base_quality_ref": "../../quality/missing.yaml"},
        "ref": "../../quality/missing.yaml",
        "file": "fragments/exposes/out.yaml",
        "pointer": "/contract/quality",
    },
    "overlay_fragment": {
        "build": {
            "overlay_fragment": {
                "location": {"path": {"$ref": "#/extensions/paths/out"}},
                "extra": {"$ref": "./missing.yaml"},
            }
        },
        "ref": "./missing.yaml",
        "file": "fragments/prod-binding.yaml",
        "pointer": "/extra",
    },
}


@pytest.fixture(scope="module")
def broken_masters(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    return {
        name: build_parity(tmp_path_factory.mktemp(f"fx-broken-{name}"), **spec["build"]).parent
        for name, spec in _BROKEN.items()
    }


class Command(NamedTuple):
    """A command of the matrix: its arguments for a contract, and what it must not write."""

    argv: Callable[[str], List[Any]]
    writes: Tuple[str, ...] = ()
    #: False when the command composes no overlay, so a broken overlay fragment is not read.
    reads_overlay: bool = True


#: Every command of G-01..G-17 that composes the contract and can run offline.
_MATRIX: Dict[str, Command] = {
    "validate": Command(lambda c: ["validate", c, "--env", "prod"]),
    "bundle": Command(lambda c: ["bundle", c, "--env", "prod", "--out", "o/b.yaml"], ("o/b.yaml",)),
    "bundle-tgz": Command(
        lambda c: ["bundle", c, "--env", "prod", "--format", "tgz", "--out", "o/b.tgz"],
        ("o/b.tgz",),
    ),
    "plan": Command(
        lambda c: ["plan", c, "--env", "prod", "--out", "o/plan.json"], ("o/plan.json",)
    ),
    "standard-odcs": Command(
        lambda c: ["generate", "standard", c, "--format", "odcs", "--env", "prod", "--out", "o/s"],
        ("o/s",),
    ),
    "standard-odps": Command(
        lambda c: ["generate", "standard", c, "--format", "odps", "--env", "prod", "--out", "o/s"],
        ("o/s",),
    ),
    "standard-odps-v4.1": Command(
        lambda c: [
            "generate",
            "standard",
            c,
            "--format",
            "odps-v4.1",
            "--env",
            "prod",
            "--out",
            "o/s",
        ],
        ("o/s",),
    ),
    "artifacts": Command(
        lambda c: ["generate", "artifacts", c, "--env", "prod", "--out", "o/a"], ("o/a",)
    ),
    "schedule": Command(
        lambda c: ["generate", "schedule", c, "--scheduler", "airflow", "--env", "prod"]
        + ["--out", "o/d"],
        ("o/d",),
    ),
    "generate-airflow": Command(
        lambda c: ["generate-airflow", c, "--env", "prod", "-o", "o/dag.py"], ("o/dag.py",)
    ),
    "dbt-tests": Command(
        lambda c: ["generate", "dbt-tests", c, "--env", "prod", "--out", "o/t"], ("o/t",)
    ),
    "transformation": Command(
        lambda c: ["generate", "transformation", c, "--env", "prod", "--out", "o/x"], ("o/x",)
    ),
    "vector": Command(
        lambda c: ["generate", "vector", c, "--env", "prod", "--out", "o/v"], ("o/v",)
    ),
    "apply-dry-run": Command(lambda c: ["apply", c, "--env", "prod", "--dry-run"]),
    "apply-amend-and-build": Command(
        lambda c: ["apply", c, "--env", "prod", "--mode", "amend-and-build", "--yes"],
        ("runtime/out",),
    ),
    "policy-check": Command(lambda c: ["policy-check", c, "--env", "prod"]),
    "policy-compile": Command(
        lambda c: ["policy-compile", c, "--env", "prod", "--out", "o/p.json"], ("o/p.json",)
    ),
    "contract-validation": Command(lambda c: ["contract-validation", c, "--env", "prod"]),
    "contract-tests": Command(lambda c: ["contract-tests", c, "--env", "prod"]),
    "test": Command(lambda c: ["test", c, "--env", "prod"]),
    # With no ``--env`` the overlay is never composed: only a broken base fragment shows.
    "validate-no-env": Command(lambda c: ["validate", c], reads_overlay=False),
    "bundle-no-env": Command(lambda c: ["bundle", c, "--out", "o/b.yaml"], ("o/b.yaml",), False),
    "plan-no-env": Command(lambda c: ["plan", c, "--out", "o/plan.json"], ("o/plan.json",), False),
    "standard-odcs-no-env": Command(
        lambda c: ["generate", "standard", c, "--format", "odcs", "--out", "o/s"], ("o/s",), False
    ),
    "artifacts-no-env": Command(
        lambda c: ["generate", "artifacts", c, "--out", "o/a"], ("o/a",), False
    ),
    "diff": Command(lambda c: ["diff", c, "--baseline", c], reads_overlay=False),
    "export-opds": Command(
        lambda c: ["export-opds", c, "--env", "prod", "--out", "o/e.json"], ("o/e.json",)
    ),
    "odcs-export": Command(
        lambda c: ["odcs", "export", c, "--env", "prod", "--out", "o/e.odcs"], ("o/e.odcs",)
    ),
    "odps-bitol-export": Command(
        lambda c: ["odps-bitol", "export", c, "--env", "prod", "--out", "o/e.odps"], ("o/e.odps",)
    ),
    "viz-graph": Command(
        lambda c: ["viz-graph", c, "--env", "prod", "--out", "o/g"], ("o/g.dot", "o/g")
    ),
    "verify": Command(lambda c: ["verify", c, "--env", "prod"]),
    "publish": Command(lambda c: ["publish", c, "--env", "prod", "--dry-run"]),
    "scaffold-composer": Command(
        lambda c: ["scaffold-composer", c, "--env", "prod", "--out", "o/sc"], ("o/sc",)
    ),
    "mcp-output-port": Command(lambda c: ["mcp", "output-port", "list", c, "--env", "prod"]),
    "generate-ci": Command(
        lambda c: ["generate", "ci", c, "--system", "jenkins", "--out", "o/ci.yml"],
        ("o/ci.yml",),
    ),
}

#: Commands that read the contract and swallow what they cannot read. See the findings.
_SWALLOWS = {
    "generate-ci": pytest.mark.xfail(
        strict=True,
        reason="BUG G19-generate-ci-swallows-ref-errors: `generate ci` falls back to its "
        "defaults when the contract does not compose, exits 0 and writes a pipeline without the "
        "contract's package extras, saying nothing of the unresolved ref",
    )
}


def _matrix_params() -> List[Any]:
    """Each command against each broken fragment it reads: a command that composes no overlay
    (no ``--env``) cannot meet the fragment of one."""
    return [
        pytest.param(name, variant, id=f"{name}-{variant}", marks=_SWALLOWS.get(name, ()))
        for name in sorted(_MATRIX)
        for variant in sorted(_BROKEN)
        if variant == "base_fragment" or _MATRIX[name].reads_overlay
    ]


@pytest.mark.parametrize("command, variant", _matrix_params())
def test_g19_a_dangling_ref_fails_the_command_naming_the_ref_the_file_and_the_pointer(
    tmp_path: Path,
    broken_masters: Dict[str, Path],
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    variant: str,
) -> None:
    spec, cmd = _BROKEN[variant], _MATRIX[command]
    broken = _copy_of(broken_masters[variant], tmp_path, "broken")
    (broken.parent / "o").mkdir()
    before = _files(broken.parent)

    rc, text = run_in(broken.parent, cmd.argv("contract.fluid.yaml"), capsys, monkeypatch)

    assert rc != 0, text
    plain = _plain(text)
    assert _plain(spec["ref"]) in plain, text
    assert _plain(spec["file"]) in plain, text
    assert _plain(spec["pointer"]) in plain, text
    # No partial output: nothing the command would have written exists, and the tree holds
    # no new file but the run's own bookkeeping (``.fluid/``).
    for name in cmd.writes:
        assert not (broken.parent / name).exists(), name
    assert list(_created(broken.parent, before)) == []


@pytest.mark.parametrize("variant", ["base_fragment"])
def test_g19_docs_record_an_error_row_and_still_render_the_rest(
    tmp_path: Path,
    broken_masters: Dict[str, Path],
    fx: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    root = tmp_path / "catalog"
    shutil.copytree(fx.parent, root / "good")
    shutil.copytree(broken_masters[variant], root / "broken")
    spec = _BROKEN[variant]

    rc, text = run_in(
        tmp_path, ["docs", "--src", root, "--out", tmp_path / "site"], capsys, monkeypatch
    )

    index = json.loads((tmp_path / "site" / "index.json").read_text(encoding="utf-8"))
    rows = {Path(row["path"]).parent.name: row for row in index}
    assert rc == 0, text
    assert rows["good"].get("error") is None and rows["good"]["id"] == "refs.parity"
    error = _plain(rows["broken"]["error"])
    assert _plain(spec["ref"]) in error and _plain(spec["file"]) in error
    assert _plain(spec["pointer"]) in error
    pages = sorted(p.name for p in (tmp_path / "site").glob("contract-*.html"))
    assert pages == ["contract-refs-parity.html"]


@pytest.mark.parametrize("variant", sorted(_BROKEN))
def test_g28_the_exit_codes_of_a_dangling_ref_are_the_ones_each_command_has_today(
    tmp_path: Path,
    broken_masters: Dict[str, Path],
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """SG-25 is open (one failure, three codes): validate 1, bundle 2, generate standard 1,
    plan 1. Pinned, so unifying them is a decision and not an accident."""
    broken = _copy_of(broken_masters[variant], tmp_path, "broken")
    (broken.parent / "o").mkdir()
    codes = {}
    for name in ("validate", "bundle", "standard-odcs", "plan"):
        rc, _text = run_in(
            broken.parent, _MATRIX[name].argv("contract.fluid.yaml"), capsys, monkeypatch
        )
        codes[name] = rc

    assert codes == {"validate": 1, "bundle": 2, "standard-odcs": 1, "plan": 1}


@pytest.mark.xfail(
    strict=True,
    reason="BUG G29-validate-json-on-load-failure: `validate --format json` prints text, "
    "not JSON, when the contract does not load",
)
def test_g29_validate_format_json_prints_json_when_the_contract_does_not_load(
    tmp_path: Path,
    broken_masters: Dict[str, Path],
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = _copy_of(broken_masters["base_fragment"], tmp_path, "broken")
    monkeypatch.chdir(broken.parent)
    from fluid_build.cli import main

    rc = main(["validate", "contract.fluid.yaml", "--env", "prod", "--format", "json"])
    stdout = capsys.readouterr().out

    assert rc == 1
    report = json.loads(stdout)
    assert report["valid"] is False


# ── G-24 · workspace mode composes each product as the explicit form does ──

_PRODUCT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.{name}
name: {name}
domain: commerce
metadata:
  layer: Silver
  owner: {{$ref: ../../shared/team.yaml}}
exposes:
  - exposeId: {name}
    kind: table
    binding: {{platform: local, format: csv, location: {{path: out/{name}.csv}}}}
    contract:
      schema:
        - {{name: order_id, type: string, required: true}}
"""


def _workspace(root: Path, *, team: Dict[str, Any]) -> Path:
    """``ws/`` with a ``ref_root`` and two products that both reach ``../../shared``."""
    ws = root / "ws"
    _write(ws / "shared" / "team.yaml", _dump(team))
    _write(ws / "fluid.workspace.yaml", "name: shop\nref_root: .\n")
    for name in ("orders", "customers"):
        _write(ws / "products" / name / "contract.fluid.yaml", _PRODUCT.format(name=name))
    return ws


def test_g24_validate_with_no_argument_composes_every_product_as_the_explicit_form_does(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path, team={"team": "commerce-data", "email": "c@example.com"})

    rc, text = run_in(ws, ["validate"], capsys, monkeypatch)
    explicit = [
        run_in(ws, ["validate", f"products/{n}/contract.fluid.yaml"], capsys, monkeypatch)[0]
        for n in ("orders", "customers")
    ]

    assert rc == 0, text
    assert "Validating 2 products" in text and "All 2 products valid" in text
    assert explicit == [0, 0]


def test_g24_an_error_in_a_shared_fragment_fails_each_product_in_both_forms(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path, team={"team": 5, "bogus": 1})

    rc, text = run_in(ws, ["validate"], capsys, monkeypatch)
    explicit_rc, explicit_text = run_in(
        ws, ["validate", "products/orders/contract.fluid.yaml"], capsys, monkeypatch
    )

    assert rc == 1 and explicit_rc == 1
    assert "Result: 0 passed, 2 failed" in text
    # The fragment is named in both forms, with the line of the bad key.
    for output in (text, explicit_text):
        assert "shared/team.yaml:2" in _plain(output) and "bogus" in output


# ── G-25 · the contract digest (SG-24) ─────────────────────────────────────


def test_g25_the_contract_digest_is_unchanged_by_a_fragment_edit(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-24, characterization. ``fluid contract digest`` hashes the root file as parsed, so
    removing a column inside a fragment leaves the digest, and any upstream pin made from
    it, as it was. Reported as a finding."""
    before = run_in(fx.parent, ["contract", "digest", "contract.fluid.yaml"], capsys, monkeypatch)
    _write(fx.parent / "schemas" / "out-columns.yaml", _dump(SCHEMA[:1]))
    after = run_in(fx.parent, ["contract", "digest", "contract.fluid.yaml"], capsys, monkeypatch)
    from_flat = run_in(
        flat.parent, ["contract", "digest", "contract.fluid.yaml"], capsys, monkeypatch
    )

    assert before[0] == after[0] == from_flat[0] == 0
    assert before[1].strip() == after[1].strip()
    assert from_flat[1].strip() != before[1].strip()


def test_g25_the_library_digest_is_the_composed_contracts_and_does_change(fx: Path) -> None:
    from fluid_build.api.contract import load_contract

    before = load_contract(fx).digest
    _write(fx.parent / "schemas" / "out-columns.yaml", _dump(SCHEMA[:1]))

    assert load_contract(fx).digest != before


# ── G-26 · commands that write a contract back (SG-41) ─────────────────────


def test_g26_product_add_exposure_does_not_see_an_expose_that_lives_in_a_fragment(
    fx: Path, flat: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-41, characterization. ``product-add`` edits the raw root. Adding the exposure the
    contract already has: on the flat twin it is 'already present'; on the fragment contract
    it is added a second time (two exposes named ``out``), and ``validate`` accepts it.
    Reported as a finding."""
    flat_rc, flat_text = run_in(
        flat.parent,
        ["product-add", "contract.fluid.yaml", "exposure", "--id", "out"],
        capsys,
        monkeypatch,
    )
    fx_rc, fx_text = run_in(
        fx.parent,
        ["product-add", "contract.fluid.yaml", "exposure", "--id", "out"],
        capsys,
        monkeypatch,
    )

    assert flat_rc == 0 and "already present" in flat_text
    assert fx_rc == 0 and "Added exposure 'out'" in fx_text
    composed = loader.load_contract(fx)
    assert [e["exposeId"] for e in composed["exposes"]] == ["out", "out"]
    assert run_in(fx.parent, ["validate", "contract.fluid.yaml"], capsys, monkeypatch)[0] == 0


def test_g26_product_add_dq_lands_beside_the_ref_and_merges_into_the_fragments_expose(
    fx: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, text = run_in(
        fx.parent,
        ["product-add", "contract.fluid.yaml", "dq", "--id", "r1", "--type", "completeness"],
        capsys,
        monkeypatch,
    )

    assert rc == 0, text
    root = _read(fx)
    # Written beside the ``$ref`` (a sibling), so the fragment file itself is untouched.
    assert root["exposes"][0]["$ref"] == "./fragments/exposes/out.yaml"
    assert root["exposes"][0]["contract"]["dq"]["rules"][0]["id"] == "r1"
    composed = loader.load_contract(fx)["exposes"][0]["contract"]
    assert composed["dq"]["rules"][0]["id"] == "r1"
    assert composed["schema"] == SCHEMA and composed["quality"] == QUALITY


# ── G-27 · env placeholders and Jinja in a text fragment ───────────────────

_TEMPLATED_SQL = (
    "{{ config(materialized='table') }}\n"
    "-- db: {{ env.G27_DATABASE }}\n"
    "SELECT 1 AS id, '{{ env.G27_DATABASE }}' AS db\n"
)


def _templated(root: Path, *, as_ref: bool) -> Path:
    sql: Any = {"$ref": "./sql/t.sql"} if as_ref else _TEMPLATED_SQL
    contract = _base_for_text(sql)
    files = {"contract.fluid.yaml": _dump(contract)}
    if as_ref:
        files["sql/t.sql"] = _TEMPLATED_SQL
    for rel, text in files.items():
        _write(root / rel, text)
    return root / "contract.fluid.yaml"


def _base_for_text(sql: Any) -> Dict[str, Any]:
    document = expected_document(None)
    document["builds"][0]["properties"] = {"sql": sql}
    return document


def test_g27_env_placeholders_and_jinja_in_a_text_fragment_are_handled_as_inline_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SG-40, characterization: the fragment's text is the inline text, in the bundle (verbatim,
    placeholders unexpanded) and in the plan, even with the variable set."""
    monkeypatch.setenv("G27_DATABASE", "prod_db")
    inline = _templated(tmp_path / "inline", as_ref=False)
    refd = _templated(tmp_path / "refd", as_ref=True)
    bundles, plans = {}, {}
    for name, contract in (("inline", inline), ("refd", refd)):
        rc, text = run_in(
            contract.parent,
            ["bundle", "contract.fluid.yaml", "--out", "b.yaml"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        bundles[name] = (contract.parent / "b.yaml").read_bytes()
        rc, text = run_in(
            contract.parent,
            ["plan", "contract.fluid.yaml", "--out", "plan.json"],
            capsys,
            monkeypatch,
        )
        assert rc == 0, text
        plans[name] = json.loads((contract.parent / "plan.json").read_text("utf-8"))["contract"]

    assert bundles["inline"] == bundles["refd"]
    assert plans["inline"] == plans["refd"]
    sql = _read(refd.parent / "b.yaml")["builds"][0]["properties"]["sql"]
    assert sql == _TEMPLATED_SQL
    assert "{{ env.G27_DATABASE }}" in sql and "{{ config(materialized='table') }}" in sql


# ── B-17 · siblings on a shared table definition (draft 23, parameters) ────

_FLINK_TABLE = {
    "table": "SharedInput",
    "connector": "kafka",
    "topic": "orders.raw",
    "properties.bootstrap.servers": "kafka:9092",
    "format": "json",
    "scan.startup.mode": "latest-offset",
}
_INPUT_TABLE = {
    "table": "Input",  # the sibling wins over the shared definition's ``SharedInput``
    "connector": "kafka",
    "topic": "orders.raw",
    "properties.bootstrap.servers": "kafka:9092",
    "format": "json",
    "scan.startup.mode": "earliest",
}


def _flink_product(root: Path) -> Path:
    contract = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "repro.ref23.case_b",
        "name": "Ref23 case B",
        "description": "A shared Flink table definition reused with one option changed.",
        "domain": "repro",
        "metadata": {"layer": "Silver", "productType": "ADP", "owner": {"team": "repro-team"}},
        "builds": [
            {
                "id": "flink_job",
                "engine": "flink",
                "pattern": "embedded-logic",
                "properties": {
                    "language": "flink_sql",
                    "sql": "INSERT INTO orders_out SELECT order_id, amount FROM Input\n",
                    "parameters": {
                        "parallelism": 2,
                        "upgradeMode": "savepoint",
                        "inputTable": {
                            "$ref": "./shared/tables/input.yaml",
                            "table": "Input",
                            "scan.startup.mode": "earliest",
                        },
                    },
                },
                "execution": {"runtime": {"platform": "kubernetes"}},
            }
        ],
        "exposes": [
            {
                "exposeId": "orders_out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/orders_out.csv"},
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string"},
                        {"name": "amount", "type": "double"},
                    ]
                },
            }
        ],
    }
    overlay = {
        "description": "PROD OVERLAY APPLIED",
        "builds": [{"id": "flink_job", "properties": {"parameters": {"parallelism": 8}}}],
    }
    return _put_files(
        root,
        {
            "contract.fluid.yaml": _dump(contract),
            "shared/tables/input.yaml": _dump(_FLINK_TABLE),
            "overlays/prod.yaml": _dump(overlay),
        },
    )


def _put_files(root: Path, files: Dict[str, str]) -> Path:
    for rel, text in files.items():
        _write(root / rel, text)
    return root / "contract.fluid.yaml"


def _input_table(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["builds"][0]["properties"]["parameters"]["inputTable"]


def test_b17_the_sibling_form_keeps_a_dotted_option_as_one_literal_key(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _flink_product(tmp_path / "p")
    out = tmp_path / "b.yaml"

    rc, text = cli(["bundle", path, "--out", out], capsys)

    assert rc == 0, text
    table = _input_table(_read(out))
    # ``scan.startup.mode`` stays one top-level key (never nested under ``scan``), the
    # sibling's value over the target's, and the shared options all arrive.
    assert table == _INPUT_TABLE
    assert "scan" not in table and "properties" not in table
    assert not _has_ref_key(_read(out))


def test_b17_the_plan_and_the_actions_carry_the_merged_table_with_no_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _flink_product(tmp_path / "p")
    out = tmp_path / "plan.json"

    rc, text = cli(["plan", path, "--env", "prod", "--out", out], capsys)

    assert rc == 0, text
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert _input_table(plan["contract"]) == _INPUT_TABLE
    assert plan["contract"]["builds"][0]["properties"]["parameters"]["parallelism"] == 8
    assert plan["contract"]["description"] == "PROD OVERLAY APPLIED"
    carried = [
        a["params"]["contract"] for a in plan["actions"] if "contract" in a.get("params", {})
    ]
    assert carried, "no action carries the contract"
    for contract in carried:
        assert _input_table(contract) == _INPUT_TABLE
    assert not _has_ref_key(plan)


def test_b17_every_artifact_carries_the_merged_table_and_no_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _flink_product(tmp_path / "p")
    artifacts, odcs = tmp_path / "artifacts", tmp_path / "x.odcs.yaml"

    rc1, text1 = cli(["generate", "artifacts", path, "--out", artifacts], capsys)
    rc2, text2 = cli(["generate", "standard", path, "--format", "odcs", "--out", odcs], capsys)

    assert (rc1, rc2) == (0, 0), (text1, text2)
    documents = _tree_documents(artifacts) + [("x.odcs.yaml", _read(odcs))]
    assert not any(_has_ref_key(doc) for _, doc in documents), [
        name for name, doc in documents if _has_ref_key(doc)
    ]
    # The shared options are in the places the draft named: ODCS fluidExtras, OPDS x-fluid.builds.
    carriers = {name: text for name, text in ((n, json.dumps(d)) for n, d in documents)}
    assert any("orders.raw" in text and "earliest" in text for text in carriers.values())
    assert any("opds" in name and "orders.raw" in text for name, text in carriers.items())
    assert any("odcs" in name and "kafka:9092" in text for name, text in carriers.items())
    assert "latest-offset" not in " ".join(carriers.values())


# ── B-18 · siblings on a labels map (draft 23, generate iac) ───────────────

_LABELS = {"owner": "repro-team", "data-classification": "internal"}


def _labelled_aws_product(root: Path) -> Path:
    contract = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "repro.ref23.case_f",
        "name": "Ref23 case F",
        "description": "Shared labels reused with one label added.",
        "domain": "repro",
        "labels": {"$ref": "./shared/labels.yaml", "cost-center": "cc-42"},
        "metadata": {"layer": "Silver", "productType": "ADP", "owner": {"team": "repro-team"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "repro_db",
                        "table": "orders",
                        "bucket": "repro-bucket",
                        "path": "curated/orders/",
                        "region": "eu-central-1",
                    },
                },
                "labels": {"$ref": "./shared/labels.yaml", "tier": "gold"},
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string"},
                        {"name": "amount", "type": "double"},
                    ]
                },
            }
        ],
    }
    return _put_files(
        root,
        {"contract.fluid.yaml": _dump(contract), "shared/labels.yaml": _dump(_LABELS)},
    )


def test_b18_labels_merge_and_the_iac_module_applies_them_with_no_ref_tag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    no_cloud: List[str],
) -> None:
    path = _labelled_aws_product(tmp_path / "p")
    out = tmp_path / "b.yaml"
    rc, text = cli(["bundle", path, "--out", out], capsys)
    assert rc == 0, text
    bundled = _read(out)
    assert bundled["labels"] == {**_LABELS, "cost-center": "cc-42"}
    assert bundled["exposes"][0]["labels"] == {**_LABELS, "tier": "gold"}

    rc, text = run_in(
        path.parent,
        ["generate", "iac", "contract.fluid.yaml", "--out", "iac"],
        capsys,
        monkeypatch,
    )

    assert rc == 0, text
    module = (path.parent / "iac" / "main.tf.json").read_text(encoding="utf-8")
    assert "l_-ref" not in module and '"$ref"' not in module
    # The shared labels are applied, with the sibling labels beside them.
    for expected in ("data-classification", "internal", "cost-center", "cc-42", "tier", "gold"):
        assert f'"{expected}"' in module, expected
    # And the contract copy the Glue table carries holds no ref text.
    glue = json.loads(module)["resource"]["aws_glue_catalog_table"]
    parameters = next(iter(glue.values()))["parameters"]
    assert "$ref" not in parameters["fluid_contract"]
    assert no_cloud == []
