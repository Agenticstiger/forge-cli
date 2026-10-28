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

"""``fluid validate`` and ``--fragment`` (scenarios K-10 to K-20).

Spec 9: ``fluid validate --fragment <$defs name> <file>`` checks one fragment; the schema
version comes from ``--schema-version`` or the nearest root contract. ``fluid validate`` on a
whole contract validates the composed document against the canonical schema, never the
authoring one.

A SPEC GAP scenario (SG-30: K-14's exit code, K-17, K-18) pins today's behaviour. A test marked
``xfail(strict=True)`` shows a defect; the reason names it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema
import pytest
import yaml

import fluid_build

_REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = Path(fluid_build.__file__).parent / "schemas"


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


def _expose() -> Dict[str, Any]:
    return {
        "exposeId": "orders",
        "kind": "table",
        "version": "1.0.0",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": "out/orders.csv"},
        },
        "contract": {
            "schema": [
                {"name": "order_id", "type": "string", "required": True},
                {"name": "amount", "type": "number"},
            ],
            "quality": [
                {"rule": "id_present", "expression": "order_id IS NOT NULL", "severity": "error"}
            ],
        },
    }


def _root_doc(version: str = "0.7.6") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "frag.test",
        "name": "Fragment test",
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": {"team": "ops", "email": "ops@example.com"}},
        "exposes": [{"$ref": "./fragments/exposes/orders.yaml"}],
    }


def _dump(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _project(
    tmp_path: Path,
    expose: Optional[Dict[str, Any]] = None,
    version: str = "0.7.6",
    name: str = "proj",
) -> Tuple[Path, Path]:
    """``(root contract, expose fragment)``: a contract whose one expose is kept in a fragment."""
    root = tmp_path / name
    contract = _dump(root / "contract.fluid.yaml", _root_doc(version))
    fragment = _dump(root / "fragments" / "exposes" / "orders.yaml", expose or _expose())
    return contract, fragment


def _validate(capsys: pytest.CaptureFixture, *argv: Any) -> Tuple[int, str, str]:
    from fluid_build.cli import main

    rc = main(["validate", *_CACHE, *[str(a) for a in argv]])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _json_errors(out: str) -> List[str]:
    return json.loads(out)["errors"]


def _split_error(text: str) -> Tuple[str, str]:
    """``(path, message)`` of an error, with or without the ``file:line (path): `` prefix."""
    located = re.match(r"^\S+:\d+ \((?P<path>[^)]*)\): (?P<msg>.*)$", text, re.S)
    if located:
        return located["path"], located["msg"]
    plain = re.match(r"^(?P<path>[^:]*): (?P<msg>.*)$", text, re.S)
    assert plain, text
    return plain["path"], plain["msg"]


def _as_expose_errors(errors: List[str], prefix: str) -> List[Tuple[str, str]]:
    """The errors under ``prefix`` (``exposes[0]``) with the prefix taken off, sorted."""
    out = []
    for text in errors:
        path, message = _split_error(text)
        if path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "["):
            out.append((path[len(prefix) :].lstrip(".") or "root", message))
    return sorted(out)


# ── K-10 ────────────────────────────────────────────────────────────────


def test_k10_a_dangling_ref_fails_validate_and_names_the_ref_the_file_and_the_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract, fragment = _project(tmp_path)
    fragment.unlink()

    rc, out, err = _validate(capsys, contract)

    assert rc == 1
    text = out + err
    assert "contract_load_failed" in text
    assert "./fragments/exposes/orders.yaml" in text  # the ref, as written
    assert str(contract.resolve()) in text  # the file it is written in
    assert "/exposes/0" in text  # where in that file


def test_k10_a_composed_contract_that_is_invalid_fails_with_the_canonical_schema_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.loader import compose_contract

    broken = _expose()
    broken["kind"] = 42
    contract, _ = _project(tmp_path, broken)
    schema = json.loads((_SCHEMAS / "fluid-schema-0.7.6.json").read_text("utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    expected = sorted(e.message for e in validator.iter_errors(compose_contract(contract).document))
    assert expected

    rc, out, _ = _validate(capsys, contract, "--format", "json")

    assert rc == 1
    got = sorted(_split_error(text)[1] for text in _json_errors(out))
    assert got == expected


def test_k10_validate_checks_the_canonical_schema_never_the_authoring_one(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.schema_manager import FluidSchemaManager

    contract, fragment = _project(tmp_path)
    used: List[Dict[str, Any]] = []
    original = FluidSchemaManager._validate_with_jsonschema

    def spy(self: Any, document: Any, schema: Dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        used.append(schema)
        return original(self, document, schema, *args, **kwargs)

    monkeypatch.setattr(FluidSchemaManager, "_validate_with_jsonschema", spy)
    assert _validate(capsys, contract)[0] == 0
    assert _validate(capsys, "--fragment", "expose", fragment)[0] == 0

    assert len(used) == 2
    for schema in used:
        assert "authoring" not in str(schema.get("$id", ""))
        assert "fragmentRef" not in schema.get("$defs", {})
    assert "fluid-schema-0.7.6" in used[0]["$id"]


# ── K-11 ────────────────────────────────────────────────────────────────


def test_k11_a_valid_fragment_passes_and_the_output_names_the_def_and_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _, fragment = _project(tmp_path)

    rc, out, _ = _validate(capsys, "--fragment", "expose", fragment)

    assert rc == 0
    assert "Valid expose fragment (schema v0.7.6)" in out


def test_k11_the_fragment_of_a_split_contract_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    contract = _dump(
        tmp_path / "p" / "contract.fluid.yaml", {**_root_doc(), "exposes": [_expose()]}
    )
    assert main(["split", str(contract)]) == 0
    capsys.readouterr()

    rc, out, _ = _validate(
        capsys, "--fragment", "expose", contract.parent / "fragments/exposes/orders.yaml"
    )

    assert rc == 0 and "Valid expose fragment (schema v0.7.6)" in out


# ── K-12 ────────────────────────────────────────────────────────────────


def _kind_and_platform(e: Dict[str, Any]) -> None:
    e["kind"] = "not-a-kind"
    del e["binding"]["platform"]


def _unknown_key(e: Dict[str, Any]) -> None:
    e["bogus"] = 1


def _no_contract(e: Dict[str, Any]) -> None:
    del e["contract"]


def _location_type(e: Dict[str, Any]) -> None:
    e["binding"]["location"] = 5


def _column_type(e: Dict[str, Any]) -> None:
    e["contract"]["schema"][1]["type"] = 42


def _version_type(e: Dict[str, Any]) -> None:
    e["version"] = 5


@pytest.mark.parametrize(
    "mutate",
    [_kind_and_platform, _unknown_key, _no_contract, _location_type, _column_type, _version_type],
    ids=lambda f: f.__name__.strip("_"),
)
def test_k12_a_fragment_reports_the_schema_errors_the_whole_contract_does(
    tmp_path: Path, capsys: pytest.CaptureFixture, mutate: Callable[[Dict[str, Any]], None]
) -> None:
    broken = _expose()
    mutate(broken)
    contract, fragment = _project(tmp_path, broken)

    rc_whole, out_whole, _ = _validate(capsys, contract, "--format", "json")
    rc_alone, out_alone, _ = _validate(capsys, "--fragment", "expose", fragment, "--format", "json")

    assert rc_whole == rc_alone == 1
    alone = sorted(_split_error(text) for text in _json_errors(out_alone))
    whole = _as_expose_errors(_json_errors(out_whole), "exposes[0]")
    assert alone and alone == whole


def test_k12_the_errors_of_the_narrow_case_name_the_fragment_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    broken = _expose()
    _kind_and_platform(broken)
    contract, fragment = _project(tmp_path, broken)
    lines = fragment.read_text("utf-8").splitlines()
    kind_line = next(n for n, text in enumerate(lines, 1) if text.startswith("kind:"))

    _, out_whole, _ = _validate(capsys, contract)
    _, out_alone, _ = _validate(capsys, "--fragment", "expose", fragment)

    assert f"fragments/exposes/orders.yaml:{kind_line} (exposes[0].kind)" in out_whole
    assert f"fragments/exposes/orders.yaml:{kind_line} (kind)" in out_alone


# ── K-13 ────────────────────────────────────────────────────────────────


def _loose_fragment(tmp_path: Path) -> Path:
    """A self-contained expose fragment with no contract anywhere above it."""
    return _dump(tmp_path / "loose" / "fragments" / "orders.yaml", _expose())


def test_k13_without_a_root_contract_the_run_never_falls_back_to_an_older_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, out, err = _validate(capsys, "--fragment", "expose", _loose_fragment(tmp_path))

    assert "schema v0.7.4" not in out + err  # the silent retry draft 26 warned about
    assert "retrying previous" not in out + err
    assert rc in (0, 1)


@pytest.mark.xfail(
    strict=True,
    reason="BUG K13-NO-ROOT-VERSION: with no root contract above the fragment and no "
    "--schema-version, validate --fragment guesses the newest STABLE schema (0.7.5) and exits 0 "
    "instead of asking for --schema-version (spec 9: the version comes from the flag or the "
    "nearest root contract)",
)
def test_k13_without_a_root_contract_the_run_asks_for_a_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, out, err = _validate(capsys, "--fragment", "expose", _loose_fragment(tmp_path))

    assert rc != 0
    assert "--schema-version" in out + err


@pytest.mark.parametrize("root", ["none", "unreadable"])
@pytest.mark.xfail(
    strict=True,
    reason="BUG K13-NO-ROOT-VERSION: with no readable root contract above it and no "
    "--schema-version, a fragment valid in 0.7.6 is judged by the guessed newest STABLE schema "
    "(0.7.5) and gets a false error ('id' was unexpected); an unreadable root is not mentioned",
)
def test_k13_a_fragment_valid_in_0_7_6_gets_no_false_error_from_a_guessed_older_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture, root: str
) -> None:
    fragment = _expose()
    fragment["contract"]["id"] = "stable-contract-id-1"  # NEW in v0.7.6: older schemas refuse it
    where = _dump(tmp_path / "loose" / "sub" / "orders.yaml", fragment)
    if root == "unreadable":
        (tmp_path / "loose" / "contract.fluid.yaml").write_text("fluidVersion: [\n", "utf-8")
    assert _validate(capsys, "--fragment", "expose", where, "--schema-version", "0.7.6")[0] == 0

    rc, out, err = _validate(capsys, "--fragment", "expose", where)

    assert "'id' was unexpected" not in out + err


def test_k13_with_a_schema_version_flag_the_loose_fragment_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, out, _ = _validate(
        capsys, "--fragment", "expose", _loose_fragment(tmp_path), "--schema-version", "0.7.6"
    )

    assert rc == 0
    assert "Valid expose fragment (schema v0.7.6)" in out


@pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
def test_k13_the_version_comes_from_the_nearest_root_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture, version: str
) -> None:
    _, fragment = _project(tmp_path, version=version)

    rc, out, _ = _validate(capsys, "--fragment", "expose", fragment)

    assert rc == 0 and f"(schema v{version})" in out


def test_k13_the_flag_wins_over_the_root_contract_and_the_nearest_contract_wins_over_a_farther_one(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _dump(tmp_path / "contract.fluid.yaml", _root_doc("0.7.4"))  # a farther contract
    _, fragment = _project(tmp_path, version="0.7.6", name="inner")

    assert "(schema v0.7.6)" in _validate(capsys, "--fragment", "expose", fragment)[1]
    assert (
        "(schema v0.7.5)"
        in _validate(capsys, "--fragment", "expose", fragment, "--schema-version", "0.7.5")[1]
    )


# ── K-14 ────────────────────────────────────────────────────────────────


def test_k14_an_unknown_kind_is_refused_and_the_valid_kinds_are_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _, fragment = _project(tmp_path)

    rc, out, err = _validate(capsys, "--fragment", "exposee", fragment)

    text = out + err
    assert rc != 0  # SG-30: the exit code for an unknown name has no oracle; today it is 1
    assert "'exposee' is not a kind of fragment in schema 0.7.6" in text
    for kind in ("expose", "build", "column", "sovereignty", "accessPolicy", "quality", "schema"):
        assert re.search(rf"\b{kind}\b", text.split("kinds:", 1)[1]), kind


def test_k14_a_kind_the_schema_version_lacks_is_an_error_naming_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    fragment = _dump(tmp_path / "d.yaml", {"purpose": "x"})

    rc, out, err = _validate(
        capsys, "--fragment", "descriptionObject", fragment, "--schema-version", "0.7.5"
    )

    assert rc != 0
    assert "'descriptionObject' is not a kind of fragment in schema 0.7.5" in out + err
    # and the same kind is fine where it exists
    assert (
        _validate(capsys, "--fragment", "descriptionObject", fragment, "--schema-version", "0.7.6")[
            0
        ]
        == 0
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG K14-NO-FILE: validate --fragment KIND with the file left out validates the "
    "contract found in the working directory as if it were the fragment (6 misleading errors, "
    "schema 0.7.5) instead of asking for the fragment file",
)
def test_k14x_a_fragment_run_without_a_file_does_not_validate_the_contract_beside_it(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, _ = _project(tmp_path)
    monkeypatch.chdir(contract.parent)

    rc, out, err = _validate(capsys, "--fragment", "expose")

    assert rc != 0
    assert "Invalid expose fragment" not in out + err
    assert "required" in (out + err).lower()


def test_k14x_an_empty_kind_is_refused_not_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _, fragment = _project(tmp_path)

    rc, out, err = _validate(capsys, "--fragment", "", fragment)

    assert rc != 0
    assert "kinds:" in out + err  # the valid kinds are listed, as for any unknown kind


# ── K-15 ────────────────────────────────────────────────────────────────

_VALID = {
    "build": {
        "id": "transform",
        "pattern": "embedded-logic",
        "engine": "sql",
        "properties": {"sql": "SELECT 1"},
    },
    "sovereignty": {"jurisdiction": "EU", "dataResidency": True},
    "accessPolicy": {"grants": [{"principal": "group:a@example.com", "permissions": ["read"]}]},
    "column": {"name": "id", "type": "string", "required": True},
    "dqRule": {"id": "r1", "type": "completeness", "severity": "error"},
    "dqSpec": {"rules": [{"id": "r1", "type": "completeness", "severity": "error"}]},
    "exposePolicy": {"authn": "none"},
    "teamMember": {"username": "jdoe", "email": "jdoe@example.com"},
    "binding": {"platform": "local", "format": "csv", "location": {"path": "out/a.csv"}},
    "exposeContract": {"schema": [{"name": "id", "type": "string"}]},
    "descriptionObject": {"purpose": "P", "usage": "U", "limitations": "L"},
    "consumeRef": {"productId": "up.stream", "exposeId": "events"},
    "labels": {"team": "ops"},
    "tags": ["finance", "core"],
    "semver": "1.0.0",
    "isoDuration": "PT1H",
}


@pytest.mark.parametrize("kind", sorted(_VALID))
def test_k15_a_valid_fragment_of_each_kind_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture, kind: str
) -> None:
    fragment = _dump(tmp_path / f"{kind}.yaml", _VALID[kind])

    rc, out, err = _validate(capsys, "--fragment", kind, fragment, "--schema-version", "0.7.6")

    assert rc == 0, out + err
    assert f"Valid {kind} fragment (schema v0.7.6)" in out


@pytest.mark.parametrize(
    "kind", ["build", "column", "sovereignty", "binding", "teamMember", "semver"]
)
def test_k15_a_wrong_fragment_of_the_same_kinds_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture, kind: str
) -> None:
    wrong: Any = [1, 2] if kind != "semver" else 12
    fragment = _dump(tmp_path / f"{kind}.yaml", wrong)

    rc, out, _ = _validate(capsys, "--fragment", kind, fragment, "--schema-version", "0.7.6")

    assert rc == 1 and f"Invalid {kind} fragment" in out


@pytest.mark.slow
def test_k15_every_defs_name_of_the_schema_is_a_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    schema = json.loads((_SCHEMAS / "fluid-schema-0.7.6.json").read_text("utf-8"))
    fragment = _dump(tmp_path / "empty-object.yaml", {})
    for name in sorted(schema["$defs"]):
        rc, out, err = _validate(capsys, "--fragment", name, fragment, "--schema-version", "0.7.6")
        assert rc in (0, 1), name
        assert "not a kind of fragment" not in out + err, name
        assert "Traceback" not in out + err and "cli_unhandled_exception" not in out + err, name


# ── K-16 ────────────────────────────────────────────────────────────────


def test_k16_a_fragment_run_applies_no_contract_wide_check(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build import loader
    from fluid_build.cli import validate

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a whole-contract step ran for a fragment")

    for name in (
        "_reject_pre_07_contract",
        "_run_contract_rules",
        "_run_extension_validators",
        "_run_role_validators",
        "_load_with_overlay_warnings",
    ):
        monkeypatch.setattr(validate, name, forbidden)
    monkeypatch.setattr(loader, "load_with_overlay", forbidden)
    monkeypatch.setattr(loader, "load_overlay_document", forbidden)
    contract, fragment = _project(tmp_path)

    rc, out, _ = _validate(capsys, "--fragment", "expose", fragment)

    assert rc == 0 and "Valid expose fragment" in out
    # the same patches do stop a whole-contract run, so the fragment run really avoided them
    rc, out, err = _validate(capsys, contract)
    assert rc == 1 and "a whole-contract step ran" in out + err


def test_k16_the_kind_of_an_expose_is_not_checked_against_the_contract_kinds(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _, fragment = _project(tmp_path)
    assert yaml.safe_load(fragment.read_text("utf-8"))["kind"] == "table"
    assert _validate(capsys, "--fragment", "expose", fragment)[0] == 0

    bad = _expose()
    bad["kind"] = "DataProduct"  # a contract kind, not an expose kind
    _, bad_fragment = _project(tmp_path, bad, name="other")
    rc, out, err = _validate(capsys, "--fragment", "expose", bad_fragment)
    assert rc == 1
    assert "'DataProduct' is not one of ['table'" in out + err  # the expose kinds, not contract's


# ── K-17, K-18 (SG-30) ──────────────────────────────────────────────────


def _with_policy_ref(tmp_path: Path, ref: str) -> Tuple[Path, Path]:
    broken = _expose()
    broken["policy"] = {"$ref": ref}
    contract, fragment = _project(tmp_path, broken)
    _dump(contract.parent / "policy.yaml", {"gold": {"authn": "none"}})
    _dump(tmp_path / "outside.yaml", {"authn": "none"})
    return contract, fragment


def test_k17_a_ref_inside_the_fragment_resolves_against_the_root_contracts_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract, fragment = _with_policy_ref(tmp_path, "../../policy.yaml#/gold")

    assert _validate(capsys, contract)[0] == 0  # in the whole contract
    rc, out, err = _validate(capsys, "--fragment", "expose", fragment)  # and on its own
    assert rc == 0, out + err


def test_k17_a_ref_that_leaves_the_root_contracts_directory_is_refused_in_both(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract, fragment = _with_policy_ref(tmp_path, "../../../outside.yaml")

    for argv in ((contract,), ("--fragment", "expose", fragment)):
        rc, out, err = _validate(capsys, *argv)
        assert rc == 1
        assert "escapes the ref root" in out + err and "../../../outside.yaml" in out + err


def test_k18_a_list_root_quality_fragment_is_checked_as_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    good = _dump(tmp_path / "good.yaml", _expose()["contract"]["quality"])
    wrong = _dump(tmp_path / "wrong.yaml", [{"rule": "r", "expression": "x", "severity": "loud"}])
    not_a_list = _dump(tmp_path / "dict.yaml", {"rule": "r"})

    assert _validate(capsys, "--fragment", "quality", good, "--schema-version", "0.7.6")[0] == 0
    rc, out, _ = _validate(capsys, "--fragment", "quality", wrong, "--schema-version", "0.7.6")
    assert rc == 1 and "'loud' is not one of" in out
    rc, out, _ = _validate(capsys, "--fragment", "quality", not_a_list, "--schema-version", "0.7.6")
    assert rc == 1 and "is not of type 'array'" in out


def test_k18_a_text_fragment_is_refused_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SG-30: a .sql file is no fragment of any kind; the path gate refuses the extension."""
    sql = tmp_path / "transform.sql"
    sql.write_text("SELECT 1 AS id\n", encoding="utf-8")

    rc, out, err = _validate(capsys, "--fragment", "build", sql, "--schema-version", "0.7.6")

    assert rc == 1 and "Traceback" not in out + err
    assert "invalid_file_extension" in out + err


def test_k18_a_fragment_that_holds_a_pointer_asks_for_the_whole_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    broken = _expose()
    broken["description"] = {"$ref": "#/description/purpose"}
    _, fragment = _project(tmp_path, broken)

    rc, out, err = _validate(capsys, "--fragment", "expose", fragment)

    assert rc == 1
    assert "same-document pointers" in out + err and "validate the contract" in out + err


def test_k18_a_fragment_run_refuses_an_env(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _, fragment = _project(tmp_path)

    rc, out, err = _validate(capsys, "--fragment", "expose", fragment, "--env", "prod")

    assert rc == 1
    assert "fragment_env_unsupported" in out + err and "--env" in out + err


def test_k18_json_and_yml_fragments_validate_like_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "e.json").write_text(json.dumps(_expose()), encoding="utf-8")
    (tmp_path / "e.yml").write_text(yaml.safe_dump(_expose()), encoding="utf-8")
    for name in ("e.json", "e.yml"):
        rc, out, err = _validate(
            capsys, "--fragment", "expose", tmp_path / name, "--schema-version", "0.7.6"
        )
        assert rc == 0, out + err


def test_k18_a_nested_ref_error_names_the_file_it_was_written_in(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    nested = _expose()
    nested["contract"]["schema"] = {"$ref": "../schemas/orders.yaml"}
    contract, fragment = _project(tmp_path, nested)
    columns = _dump(
        contract.parent / "fragments" / "schemas" / "orders.yaml", [{"name": "a", "type": 42}]
    )
    line = columns.read_text("utf-8").splitlines().index("  type: 42") + 1

    _, out_whole, _ = _validate(capsys, contract)
    rc, out_alone, _ = _validate(capsys, "--fragment", "expose", fragment)

    assert rc == 1
    assert f"fragments/schemas/orders.yaml:{line} (exposes[0].contract.schema[0].type)" in out_whole
    assert f"fragments/schemas/orders.yaml:{line} (contract.schema[0].type)" in out_alone


# ── K-19 ────────────────────────────────────────────────────────────────


def test_k19_the_json_output_has_the_version_and_the_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    broken = _expose()
    _kind_and_platform(broken)
    _, fragment = _project(tmp_path, broken)

    rc, out, _ = _validate(capsys, "--fragment", "expose", fragment, "--format", "json")

    document = json.loads(out)
    assert rc == 1 and document["valid"] is False and document["schema_version"] == "0.7.6"
    assert len(document["errors"]) == 2


@pytest.mark.xfail(
    strict=True,
    reason="BUG K19-JSON-FRAGMENT: --format json for --fragment has no 'fragment' field, so a "
    "consumer cannot tell which kind of fragment the result is about (catalog K-19; not in spec 9)",
)
def test_k19_the_json_output_names_the_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _, fragment = _project(tmp_path)

    _, out, _ = _validate(capsys, "--fragment", "expose", fragment, "--format", "json")

    document = json.loads(out)
    assert "fragment" in document


# ── K-20 ────────────────────────────────────────────────────────────────


def test_k20_a_path_with_dotdot_meets_the_same_gate_as_plain_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, fragment = _project(tmp_path, name="x")
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")

    rc_plain, out_plain, err_plain = _validate(capsys, "../x/contract.fluid.yaml")
    rc_frag, out_frag, err_frag = _validate(
        capsys, "--fragment", "expose", "../x/fragments/exposes/orders.yaml"
    )

    assert rc_plain == rc_frag == 1
    assert "path_traversal_detected" in out_plain + err_plain
    assert "path_traversal_detected" in out_frag + err_frag
    # and with the same spelling absent, the same fragment is fine
    assert _validate(capsys, "--fragment", "expose", fragment)[0] == 0


def test_a_symlinked_contract_above_a_fragment_does_not_choose_its_ref_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planted ``contract.fluid.yaml`` link must not point a fragment's root at a home
    directory: the link is not taken, so the fragment keeps its own directory's root."""
    from fluid_build.cli import main

    home = tmp_path / "home"
    (home / ".docker").mkdir(parents=True)
    (home / ".profile").write_text("# profile\n", encoding="utf-8")
    (home / ".docker" / "config.json").write_text('{"auths": {"r": {"auth": "SECRET-1"}}}', "utf-8")
    repo = tmp_path / "repo"
    (repo / "fragments" / "exposes").mkdir(parents=True)
    (repo / "contract.fluid.yaml").symlink_to(home / ".profile")
    fragment = repo / "fragments" / "exposes" / "orders.yaml"
    fragment.write_text(
        "exposeId: {$ref: ../../../home/.docker/config.json}\nkind: table\n", encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    code = main(["validate", "--fragment", "expose", "fragments/exposes/orders.yaml"])
    out = capsys.readouterr()

    assert code == 1
    assert "SECRET-1" not in out.out + out.err
    assert "escapes the ref root" in out.out + out.err


def test_the_contract_lookup_stops_at_the_fragments_repository(tmp_path: Path) -> None:
    from fluid_build.cli.validate import _nearest_contract

    (tmp_path / "contract.fluid.yaml").write_text("id: outer\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    fragment = repo / "fragments" / "x.yaml"
    fragment.parent.mkdir()
    fragment.write_text("a: 1\n", encoding="utf-8")

    assert _nearest_contract(fragment) is None
    (repo / "contract.fluid.yaml").write_text("id: inner\n", encoding="utf-8")
    assert _nearest_contract(fragment) == (repo / "contract.fluid.yaml").resolve()


def test_a_contract_in_a_home_or_temp_root_is_never_a_fragments_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside any repository the walk stops before a home directory, a temp root or ``/``:
    a contract lying there belongs to no fragment below it."""
    from fluid_build.cli.validate import _nearest_contract
    from fluid_build.providers import _duckdb_sandbox

    home = (tmp_path / "home").resolve()
    (home / "Downloads").mkdir(parents=True)
    (home / "contract.fluid.yaml").write_text("id: anyone\n", encoding="utf-8")
    fragment = home / "Downloads" / "x.yaml"
    fragment.write_text("a: 1\n", encoding="utf-8")
    monkeypatch.setattr(_duckdb_sandbox, "_home_dirs", lambda: (str(home),))

    assert _nearest_contract(fragment) is None
