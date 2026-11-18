# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate --diagnostics`` and ``--format sarif``: each finding at the line its author wrote.

The contract is composed from a root file, ``$ref`` fragments and an ``--env``
overlay; a finding is reported at the file, line and column that wrote the
value, with the place in the root contract it sits under as a related location.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from fluid_build import loader
from fluid_build.diagnostics import (
    Diagnostic,
    DiagnosticReport,
    SourceLocation,
    UnsupportedDiagnosticsVersion,
)
from fluid_build.diagnostics.render import to_sarif

pytestmark = pytest.mark.unit

_CACHE: List[str] = []

ROOT = """\
fluidVersion: 0.7.7
kind: DataProduct
id: diag.test
name: Diagnostics test
domain: ops
metadata:
  layer: Silver
  owner:
    team: ops
exposes:
  - exposeId: orders
    kind: table
    version: 1.0.0
    binding:
      platform: local
      format: csv
      location:
        path: out/orders.csv
    contract:
      schema:
        $ref: ./fragments/columns.yaml
"""

COLUMNS = """\
- name: order_id
  type: string
  required: true
- name: shipping
  type: struct
  fields:
    - name: city
      type: string
    - name: zip
      type: {zip_type}
"""

OVERLAY = """\
exposes:
  - exposeId: orders
    binding:
      platform: nowhere
"""


@pytest.fixture(scope="module", autouse=True)
def _private_schema_cache(tmp_path_factory: pytest.TempPathFactory) -> Any:
    _CACHE[:] = ["--cache-dir", str(tmp_path_factory.mktemp("schema-cache"))]
    yield
    _CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_loader_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    def clear() -> None:
        for name in dir(loader):
            if name.startswith("_NOTED_") and isinstance(getattr(loader, name), set):
                getattr(loader, name).clear()

    clear()
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    yield
    clear()


def _project(
    tmp_path: Path, *, zip_type: str = "string", overlay: bool = False, root: str = ROOT
) -> Path:
    project = tmp_path / "p"
    (project / "fragments").mkdir(parents=True)
    (project / "contract.fluid.yaml").write_text(root, encoding="utf-8")
    (project / "fragments" / "columns.yaml").write_text(
        COLUMNS.format(zip_type=zip_type), encoding="utf-8"
    )
    if overlay:
        (project / "overlays").mkdir()
        (project / "overlays" / "prod.yaml").write_text(OVERLAY, encoding="utf-8")
    return project


def _validate(capsys: pytest.CaptureFixture, *argv: Any) -> Tuple[int, str, str]:
    from fluid_build.cli import main

    rc = main(["validate", "--offline", *_CACHE, *[str(a) for a in argv]])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _items(out: str) -> List[Dict[str, Any]]:
    return json.loads(out)["diagnostics"]["items"]


# ── acceptance: a nested column, an overlay, an additive field ─────────────────


def test_a_nested_column_in_a_fragment_points_at_its_authoring_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--diagnostics", "--format", "json")

    assert rc == 1
    [item] = _items(out)
    assert item["path"] == "/exposes/0/contract/schema/1/fields/1/type"
    # fragments/columns.yaml line 10 is "      type: 42": the key starts at column 7.
    assert item["location"] == {"file": "fragments/columns.yaml", "line": 10, "column": 7}
    # The root contract's ``schema:`` key, whose $ref brought the fragment in.
    assert item["related"] == [
        {
            "location": {"file": "contract.fluid.yaml", "line": 20, "column": 7},
            "message": "exposes[0].contract.schema in the root contract",
        }
    ]
    assert item["product"] == "diag.test" and item["port"] == "orders"
    assert item["environment"] is None
    # The location is in ``location``, not repeated at the head of the message.
    assert item["message"].startswith("exposes[0].contract.schema[1].fields[1].type: ")


def test_a_nested_column_written_inline_points_at_the_root_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    inline = ROOT.replace(
        "        $ref: ./fragments/columns.yaml\n",
        "        - name: shipping\n"
        "          type: struct\n"
        "          fields:\n"
        "            - name: zip\n"
        "              type: 42\n",
    )
    project = _project(tmp_path, root=inline)
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--diagnostics", "--format", "json")

    assert rc == 1
    [item] = _items(out)
    assert item["location"] == {"file": "contract.fluid.yaml", "line": 25, "column": 15}
    assert item["related"] == []  # written in the root contract: nothing else to name


def test_an_overlay_introduced_error_links_the_overlay_and_the_base(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, overlay=True)
    monkeypatch.chdir(project)

    rc, out, _ = _validate(
        capsys, "contract.fluid.yaml", "--env", "prod", "--diagnostics", "--format", "json"
    )

    assert rc == 1
    [item] = _items(out)
    assert item["code"] == "ERR_CONTRACT_SCHEMA_ENUM"
    assert item["environment"] == "prod"
    assert item["location"] == {"file": "overlays/prod.yaml", "line": 4, "column": 7}
    assert item["related"][0]["location"] == {
        "file": "contract.fluid.yaml",
        "line": 15,
        "column": 7,
    }


def test_a_reader_tolerates_an_unknown_additive_field() -> None:
    document = {
        "version": "1.4",
        "producedBy": "a later fluid",  # a top-level key 1.0 does not know
        "items": [
            {
                "code": "ERR_X",
                "severity": "warning",
                "message": "m",
                "path": "/a",
                "location": {"file": "c.yaml", "line": 2, "column": 3, "endLine": 2},
                "related": [{"location": {"file": "b.yaml"}, "message": "r", "kind": "ref"}],
                "product": "p",
                "environment": None,
                "port": None,
                "confidence": 0.9,
            }
        ],
    }

    report = DiagnosticReport.from_dict(document)

    [item] = report.diagnostics
    assert item.location == SourceLocation("c.yaml", 2, 3)
    assert item.related[0].location == SourceLocation("b.yaml")
    assert report.version == "1.4" and report.extra == {"producedBy": "a later fluid"}


def test_a_reader_refuses_an_unknown_major_version() -> None:
    with pytest.raises(UnsupportedDiagnosticsVersion, match="2.0"):
        DiagnosticReport.from_dict({"version": "2.0", "items": []})


def test_the_document_round_trips() -> None:
    item = Diagnostic(
        code="ERR_X",
        severity="error",
        message="m",
        path="/exposes/0",
        location=SourceLocation("c.yaml", 4, 5),
        port="orders",
        fix="do it",
    )
    report = DiagnosticReport((item,))

    assert DiagnosticReport.from_dict(json.loads(json.dumps(report.to_dict()))) == report


# ── opt-in: the default outputs keep the document they always had ──────────────


def test_without_the_flag_json_and_text_are_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--format", "json")
    assert rc == 1 and "diagnostics" not in json.loads(out)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml")
    assert rc == 1
    assert " 1. fragments/columns.yaml:10 (exposes[0].contract.schema[1].fields[1].type)" in out
    assert ":10:7:" not in out


def test_diagnostics_text_is_compiler_style(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--diagnostics")

    assert rc == 1
    assert (
        "fragments/columns.yaml:10:7: error ERR_CONTRACT_SCHEMA_NO_VARIANT: "
        "exposes[0].contract.schema[1].fields[1].type: " in out
    )
    assert (
        "  related: contract.fluid.yaml:20:7: exposes[0].contract.schema in the root contract"
        in out
    )


def test_diagnostics_text_under_quiet_lists_errors_only(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--diagnostics", "--quiet")

    assert rc == 1
    assert out.splitlines()[0].startswith("fragments/columns.yaml:10:7: error ")
    assert "Invalid FLUID contract" not in out


def test_a_clean_contract_has_no_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--diagnostics", "--format", "json")
    assert rc == 0 and json.loads(out)["diagnostics"] == {"version": "1.0", "items": []}

    rc, out, _ = _validate(capsys, "contract.fluid.yaml", "--format", "sarif")
    assert rc == 0 and json.loads(out)["runs"][0]["results"] == []


def test_a_fragment_run_reports_diagnostics_in_the_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)

    rc, out, _ = _validate(
        capsys,
        "--fragment",
        "schema",
        "fragments/columns.yaml",
        "--diagnostics",
        "--format",
        "json",
    )

    assert rc == 1
    [item] = _items(out)
    assert item["location"] == {"file": "fragments/columns.yaml", "line": 10, "column": 7}
    assert item["related"] == []
    # A fragment names no product, whatever its own ``id`` is.
    assert item["product"] is None


# ── SARIF ──────────────────────────────────────────────────────────────────────

#: What GitHub code scanning reads from a SARIF 2.1.0 upload (its documented required
#: properties). The OASIS schema itself is not redistributed here; this is checked
#: against it by hand before a release.
_CODE_SCANNING_SUBSET = {
    "type": "object",
    "required": ["version", "runs"],
    "properties": {
        "version": {"const": "2.1.0"},
        "runs": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["tool", "results"],
                "properties": {
                    "tool": {
                        "type": "object",
                        "required": ["driver"],
                        "properties": {
                            "driver": {
                                "type": "object",
                                "required": ["name", "rules"],
                                "properties": {
                                    "rules": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": [
                                                "id",
                                                "shortDescription",
                                                "fullDescription",
                                                "help",
                                            ],
                                        },
                                    }
                                },
                            }
                        },
                    },
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["ruleId", "message", "locations", "partialFingerprints"],
                            "not": {"anyOf": [{"required": ["fixes"]}]},
                            "properties": {
                                "level": {"enum": ["error", "warning", "note", "none"]},
                                "message": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {"text": {"type": "string", "minLength": 1}},
                                },
                                "locations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["physicalLocation"],
                                        "properties": {
                                            "physicalLocation": {
                                                "type": "object",
                                                "required": ["artifactLocation"],
                                                "properties": {
                                                    "artifactLocation": {
                                                        "type": "object",
                                                        "required": ["uri"],
                                                        "properties": {
                                                            "uri": {
                                                                "type": "string",
                                                                "minLength": 1,
                                                            }
                                                        },
                                                    },
                                                    "region": {
                                                        "type": "object",
                                                        "required": ["startLine"],
                                                        "not": {"required": ["snippet"]},
                                                        "properties": {
                                                            "startLine": {
                                                                "type": "integer",
                                                                "minimum": 1,
                                                            },
                                                            "startColumn": {
                                                                "type": "integer",
                                                                "minimum": 1,
                                                            },
                                                        },
                                                    },
                                                },
                                            }
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def _sarif(capsys: pytest.CaptureFixture, *argv: Any) -> Tuple[int, Dict[str, Any]]:
    import jsonschema

    rc, out, _ = _validate(capsys, *argv, "--format", "sarif")
    log = json.loads(out)
    jsonschema.validate(log, _CODE_SCANNING_SUBSET)
    return rc, log


def test_sarif_names_the_overlay_and_the_base(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, overlay=True)
    monkeypatch.chdir(project)

    rc, log = _sarif(capsys, "contract.fluid.yaml", "--env", "prod")

    assert rc == 1
    run = log["runs"][0]
    [result] = run["results"]
    assert run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"] == result["ruleId"]
    assert result["ruleId"] == "ERR_CONTRACT_SCHEMA_ENUM" and result["level"] == "error"
    physical = result["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"] == {"uri": "overlays/prod.yaml", "uriBaseId": "%SRCROOT%"}
    assert physical["region"] == {"startLine": 4, "startColumn": 7}
    [related] = result["relatedLocations"]
    assert related["physicalLocation"]["artifactLocation"]["uri"] == "contract.fluid.yaml"
    assert result["properties"]["environment"] == "prod"
    assert run["columnKind"] == "unicodeCodePoints"


def test_a_sarif_fingerprint_does_not_move_with_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, zip_type="42")
    monkeypatch.chdir(project)
    _, before = _sarif(capsys, "contract.fluid.yaml")

    columns = project / "fragments" / "columns.yaml"
    columns.write_text("# a comment above\n" + columns.read_text(encoding="utf-8"), "utf-8")
    _, after = _sarif(capsys, "contract.fluid.yaml")

    [b], [a] = before["runs"][0]["results"], after["runs"][0]["results"]
    assert a["locations"][0]["physicalLocation"]["region"]["startLine"] == 11
    assert a["partialFingerprints"] == b["partialFingerprints"]


def test_two_findings_at_one_place_keep_two_fingerprints() -> None:
    where = SourceLocation("contract.fluid.yaml", 1, 1)
    twice = [
        Diagnostic("ERR_CONTRACT_SCHEMA_REQUIRED", "error", f"'{key}' is required", "", where)
        for key in ("kind", "name")
    ]

    results = to_sarif(DiagnosticReport(tuple(twice)), version="0")["runs"][0]["results"]

    first, second = (r["partialFingerprints"]["fluidFinding/v1"] for r in results)
    assert first != second


def test_fixing_one_of_two_findings_keeps_the_other_fingerprint() -> None:
    where = SourceLocation("contract.fluid.yaml", 1, 1)

    def fingerprints(keys: Tuple[str, ...]) -> Dict[str, str]:
        items = tuple(
            Diagnostic("ERR_CONTRACT_SCHEMA_REQUIRED", "error", f"'{k}' is required", "", where)
            for k in keys
        )
        results = to_sarif(DiagnosticReport(items), version="0")["runs"][0]["results"]
        return {r["message"]["text"]: r["partialFingerprints"]["fluidFinding/v1"] for r in results}

    both, fixed = fingerprints(("kind", "name")), fingerprints(("name",))
    assert fixed["'name' is required"] == both["'name' is required"]


def test_a_rule_gives_a_fix_only_every_result_shares() -> None:
    items = (
        Diagnostic("ERR_X", "warning", "a", "/a", fix="fix A"),
        Diagnostic("ERR_X", "error", "b", "/b", fix="fix B"),
    )

    run = to_sarif(DiagnosticReport(items), version="0")["runs"][0]

    [rule] = run["tool"]["driver"]["rules"]
    assert rule["help"]["text"] == "X"
    assert [r["properties"]["fix"] for r in run["results"]] == ["fix A", "fix B"]
    assert [r["level"] for r in run["results"]] == ["warning", "error"]


def test_a_sarif_uri_is_percent_encoded() -> None:
    item = Diagnostic(
        "ERR_X", "error", "m", "/id", SourceLocation("my dir/c\u00e9.fluid.yaml", 2, 1)
    )

    [result] = to_sarif(DiagnosticReport((item,)), version="0")["runs"][0]["results"]

    uri = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "my%20dir/c%C3%A9.fluid.yaml"


def test_an_unexpected_failure_still_writes_a_sarif_log(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: [1]\nid: x\n", "utf-8")

    rc, log = _sarif(capsys, "contract.fluid.yaml")

    assert rc == 1
    [result] = log["runs"][0]["results"]
    assert result["ruleId"] == "ERR_CLI_UNHANDLED_EXCEPTION"


@pytest.mark.parametrize("flag", ["--show-schema", "--list-versions"])
def test_a_schema_listing_is_refused_under_sarif(
    flag: str, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    rc, log = _sarif(capsys, "contract.fluid.yaml", flag)

    assert rc == 1
    [result] = log["runs"][0]["results"]
    assert result["ruleId"] == "ERR_DIAGNOSTICS_SCOPE_UNSUPPORTED"
    assert flag in result["message"]["text"]


def test_a_contract_that_cannot_load_still_writes_a_sarif_log(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "contract.fluid.yaml"
    secret = "hunter2-value-in-yaml"  # pragma: allowlist secret
    broken.write_text(f"id: [broken\n  note: {secret}\n", encoding="utf-8")

    rc, log = _sarif(capsys, "contract.fluid.yaml")

    assert rc == 1
    [result] = log["runs"][0]["results"]
    assert result["ruleId"] == "ERR_CONTRACT_LOAD_FAILED"
    text = result["message"]["text"]
    # The first line of the parser's message only: the source line below it is a value.
    assert secret not in json.dumps(log) and "\n" not in text
    assert str(tmp_path) not in text and "contract.fluid.yaml" in text


@pytest.mark.parametrize("flag", [("--diagnostics",), ("--format", "sarif")])
def test_a_bundle_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, flag: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "product.tgz").write_bytes(b"")

    rc, out, err = _validate(capsys, "product.tgz", *flag)

    assert rc == 1 and "diagnostics_scope_unsupported" in out + err


def test_a_workspace_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fluid.workspace.yaml").write_text("name: ws\n", encoding="utf-8")
    for name in ("a", "b"):
        product = _project(tmp_path / name)
        product.rename(tmp_path / name / "product")
    monkeypatch.chdir(tmp_path)

    rc, out, err = _validate(capsys, "--diagnostics")

    assert rc == 1 and "diagnostics_scope_unsupported" in out + err


# ── what never leaves in a diagnostic ──────────────────────────────────────────


def test_a_credential_in_a_message_is_redacted() -> None:
    from fluid_build.diagnostics.build import build_report
    from fluid_build.schema_manager import ValidationResult

    token = "ghp_" + "a" * 36  # a GitHub token shape  # pragma: allowlist secret
    result = ValidationResult(is_valid=False)
    result.add_error(f"metadata.owner: {token} is not valid", event="contract_schema_type")

    report = build_report(result, {"metadata": {}}, None)

    assert token not in json.dumps(report.to_dict())


def test_no_result_carries_a_fix_a_tool_could_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, overlay=True)
    monkeypatch.chdir(project)

    _, log = _sarif(capsys, "contract.fluid.yaml", "--env", "prod")

    text = json.dumps(log)
    assert (
        '"fixes"' not in text
        and '"snippet"' not in text
        and '"nowhere"' not in yaml.dump(log["runs"][0]["tool"])
    )


def test_a_pointer_keeps_an_empty_first_key() -> None:
    from fluid_build.diagnostics.build import _tokens

    assert _tokens("//x") == ["", "x"]
    assert _tokens("/a~1b/0") == ["a/b", "0"]
    assert _tokens("") == []
