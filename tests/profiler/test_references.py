# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tables released whole: which a profile publishes, how the rows are written, what it refuses.

The records are a library's branches. A branch table is published whole only where the contract
marks it public, a counted relationship reaches it, it is small, every column is unlabelled and
its key is unique and never null.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.profiler import references
from fluid_build.profiler._stats import Request, RequestColumn, RequestTable, load_backend
from fluid_build.profiler.runner import ExposeSource, profile_exposes
from tests.profiler import _library as library

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

BRANCHES = library.keys(12)
MEMBERS = library.spread(BRANCHES, [20] * 12)


def _run(
    tmp_path: Path, built: Dict[str, Any], branch_rows: Any = None, members: Any = None
) -> Any:
    return library.profile(
        tmp_path,
        built,
        library.branches(BRANCHES) if branch_rows is None else branch_rows,
        library.cards(MEMBERS if members is None else members),
    )


def _notes(result: Any) -> Dict[str, str]:
    return dict(result.not_measured)


# -- the gate ------------------------------------------------------------------------------------


def _many(count: int) -> Dict[str, Any]:
    return {"branch_rows": library.branches(library.keys(count))}


def _cases() -> Dict[str, Dict[str, Any]]:
    """Each case: the arguments of the contract and the rows, and whether the branch is published."""
    cases: Dict[str, Dict[str, Any]] = {
        "public": {"expect": True},
        "internal": {"contract": {"classification": "Internal"}, "expect": False},
        "confidential": {"contract": {"classification": "Confidential"}, "expect": False},
        "restricted": {"contract": {"classification": "Restricted"}, "expect": False},
        "lower case": {"contract": {"classification": "public"}, "expect": False},
        "no label": {"contract": {"classification": None}, "expect": False},
        "contract level only": {
            "contract": {"classification": None, "metadata": {"classification": "public"}},
            "expect": False,
        },
        "contract level beside another": {
            "contract": {"classification": "Internal", "metadata": {"classification": "public"}},
            "expect": False,
        },
        "not referenced": {"contract": {"related": False}, "expect": False},
        "versioned": {
            "contract": {"versioned": True},
            "branch_rows": [
                {**row, "valid_from": "2020-01-01", "valid_to": None}
                for row in library.branches(BRANCHES)
            ],
            "expect": False,
        },
        "64 rows": {
            **_many(64),
            "members": library.spread(library.keys(64), [1] * 64),
            "expect": True,
        },
        "65 rows": {
            **_many(65),
            "members": library.spread(library.keys(65), [1] * 65),
            "expect": False,
        },
        "duplicate key": {
            "branch_rows": library.branches(BRANCHES) + [{"branch_id": BRANCHES[0], "city": "x"}],
            "expect": False,
        },
        "null key": {
            "branch_rows": library.branches(BRANCHES) + [{"branch_id": None, "city": "x"}],
            "expect": False,
        },
        "float column": {
            "contract": {"branch_columns": [{"name": "area", "type": "float"}]},
            "branch_rows": [{**row, "area": "1.5"} for row in library.branches(BRANCHES)],
            "expect": False,
        },
        "composite key": {
            "contract": {"composite": True},
            "branch_rows": [{**row, "region": "north"} for row in library.branches(BRANCHES)],
            "composite": True,
            "expect": False,
        },
        "restricted column": {
            "contract": {
                "policy": {
                    "authz": {
                        "columnRestrictions": [
                            {"principal": "group:staff", "columns": ["city"], "access": "deny"}
                        ]
                    }
                }
            },
            "expect": False,
        },
        "masked column": {
            "contract": {
                "policy": {"privacy": {"masking": [{"column": "city", "strategy": "hash"}]}}
            },
            "expect": False,
        },
        "row filter": {
            "contract": {
                "policy": {
                    "authz": {
                        "rowFilters": [{"principal": "group:staff", "where": "city = 'north'"}]
                    }
                }
            },
            "expect": False,
        },
        "row level policy": {
            "contract": {"policy": {"privacy": {"rowLevelPolicy": {"expression": "true"}}}},
            "expect": False,
        },
        "no row filter": {"contract": {"policy": {"authz": {"rowFilters": []}}}, "expect": True},
    }
    for tag in ("pii", "PHI", "Sensitive", "phi"):
        cases[f"tag {tag}"] = {
            "contract": {
                "branch_columns": [{"name": "label", "type": "string", "tags": ["geo", tag]}]
            },
            "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
            "expect": False,
        }
    for tag in ("pii", "Sensitive"):
        # a schema-invalid single tag (a string, not a list) is still one tag
        cases[f"tag {tag} as a plain string"] = {
            "contract": {"branch_columns": [{"name": "label", "type": "string", "tags": tag}]},
            "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
            "expect": False,
        }
    cases["a plain string tag that is no label"] = {
        "contract": {"branch_columns": [{"name": "label", "type": "string", "tags": "geo"}]},
        "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
        "expect": True,
    }
    cases["tag that is no label"] = {
        "contract": {
            "branch_columns": [{"name": "label", "type": "string", "tags": ["geo", "reference"]}]
        },
        "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
        "expect": True,
    }
    for level in (
        "internal",
        "confidential",
        "restricted",
        "pii",
        "phi",
        "cleartext",
        "treated",
        "anonymized",
        "pseudonymized",
        "tokenized",
        "encrypted",
    ):
        cases[f"sensitivity {level}"] = {
            "contract": {
                "branch_columns": [{"name": "label", "type": "string", "sensitivity": level}]
            },
            "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
            "expect": False,
        }
    cases["sensitivity none"] = {
        "contract": {
            "branch_columns": [{"name": "label", "type": "string", "sensitivity": "none"}]
        },
        "branch_rows": [{**row, "label": "x"} for row in library.branches(BRANCHES)],
        "expect": True,
    }
    return cases


def _outcomes(tmp_path: Path) -> Dict[str, bool]:
    found: Dict[str, bool] = {}
    for number, (name, case) in enumerate(_cases().items()):
        folder = tmp_path / str(number)
        folder.mkdir()
        result = library.profile(
            folder,
            library.contract(**case.get("contract", {})),
            case.get("branch_rows", library.branches(BRANCHES)),
            library.cards(
                case.get("members", MEMBERS), region="north" if case.get("composite") else None
            ),
        )
        found[name] = "branch" in result.document.get("references", {})
    return found


def _expected() -> Dict[str, bool]:
    return {name: case["expect"] for name, case in _cases().items()}


def test_the_gate_publishes_only_a_referenced_unlabelled_public_table_of_at_most_64_keyed_rows(
    tmp_path,
):
    assert _outcomes(tmp_path) == _expected()


def _folder(tmp_path: Path, name: str) -> Path:
    (tmp_path / name).mkdir()
    return tmp_path / name


def test_a_table_marked_public_and_left_out_is_named_with_why_and_a_published_one_is_not_named(
    tmp_path,
):
    published = _run(_folder(tmp_path, "a"), library.contract())
    assert "branch" in published.document["references"]
    assert not any("reference table" in what for what, _ in published.not_measured)
    assert not any("published whole" in why for _, why in published.not_measured)
    unreached = _run(_folder(tmp_path, "b"), library.contract(related=False))
    assert "no relationship" in _notes(unreached)["reference table branch"]
    labelled = library.contract(
        branch_columns=[{"name": "label", "type": "string", "sensitivity": "pii"}]
    )
    rows = [{**row, "label": "secret"} for row in library.branches(BRANCHES)]
    said = _notes(_run(_folder(tmp_path, "c"), labelled, rows))["reference table branch"]
    assert "'label'" in said and "secret" not in said


def _profile_cli(folder: Path, built: Dict[str, Any], monkeypatch: Any, capsys: Any) -> str:
    import yaml

    from fluid_build.cli import main

    monkeypatch.chdir(folder)
    (folder / "contract.fluid.yaml").write_text(yaml.safe_dump(built), encoding="utf-8")
    library.write(folder / "branch.ndjson", library.branches(BRANCHES))
    library.write(folder / "card.ndjson", library.cards(MEMBERS))
    code = main(
        [
            "profile",
            "contract.fluid.yaml",
            "--source",
            "branch=branch.ndjson",
            "--source",
            "card=card.ndjson",
        ]
    )
    out = " ".join(capsys.readouterr().out.split())
    assert code == 0, out
    return out


def test_the_report_names_each_table_published_whole_and_only_then(tmp_path, monkeypatch, capsys):
    out = _profile_cli(_folder(tmp_path, "a"), library.contract(), monkeypatch, capsys)
    assert "Published whole, as the contract marks them Public: branch" in out
    assert "b01" not in out and "Not measured: reference table" not in out
    plain = _profile_cli(
        _folder(tmp_path, "b"), library.contract(classification="Internal"), monkeypatch, capsys
    )
    assert "Published whole" not in plain


def test_the_report_still_names_a_public_table_that_is_left_out(tmp_path, monkeypatch, capsys):
    out = _profile_cli(_folder(tmp_path, "a"), library.contract(related=False), monkeypatch, capsys)
    assert "Not measured: reference table branch: no relationship" in out
    assert "Published whole" not in out


def test_a_table_not_marked_public_is_not_named_as_left_out(tmp_path):
    result = _run(tmp_path, library.contract(classification="Internal"))
    assert "reference table branch" not in _notes(result)


def test_no_note_names_a_key_or_a_count(tmp_path):
    keys = library.keys(65)
    result = library.profile(
        tmp_path,
        library.contract(),
        library.branches(keys),
        library.cards(library.spread(keys, [1] * 65)),
    )
    said = " ".join(f"{what} {why}" for what, why in result.not_measured)
    assert "65" not in said and "b01" not in said and "64" not in said


# -- the rows ------------------------------------------------------------------------------------


def test_the_document_names_format_1_3_0_with_the_public_rule_and_the_rows_read_back(tmp_path):
    result = _run(tmp_path, library.contract())
    document = result.document
    assert document["_schemaURL"].endswith("/1-3-0/FluxProfile.json")
    assert document["disclosureRules"]["public"] == {}
    entry = document["references"]["branch"]
    assert entry["disclosure"] == "public" and entry["columns"] == ["branch_id", "city"]
    assert [row[0] for row in entry["rows"]] == sorted(BRANCHES)
    read, _ = load_profile_text(result.canonical.decode("utf-8"))
    table = read.references["branch"]
    assert table.disclosure == "public"
    assert list(table.columns) == entry["columns"]
    assert [list(row) for row in table.rows] == entry["rows"]
    assert read.disclosure_rules["public"] is not None


def test_a_profile_without_a_public_table_has_no_references_and_keeps_its_format(tmp_path):
    result = _run(tmp_path, library.contract(classification="Internal"))
    assert "references" not in result.document
    assert "public" not in result.document["disclosureRules"]
    assert "references" not in json.loads(result.canonical)


def test_the_rows_are_sorted_by_the_text_of_the_key_and_the_columns_stay_in_contract_order(
    tmp_path,
):
    keys = library.keys(12, kind="integer")
    members = library.spread(keys, [20] * 12)
    built = library.contract(key_type="integer")
    rows = library.branches(keys)
    random.Random(7).shuffle(rows)
    result = library.profile(tmp_path, built, rows, library.cards(members))
    assert "history" in result.document["tables"]["card"]
    sent = result.document["references"]["branch"]
    assert [row[0] for row in sent["rows"]] == sorted(str(each) for each in keys)
    assert sent["rows"][0][0] == "1" and sent["rows"][1][0] == "10"  # text order, not numeric


def test_a_cell_is_spelled_as_the_format_says(tmp_path):
    extra = [
        {"name": "price", "type": "decimal(10,2)"},
        {"name": "opened", "type": "date"},
        {"name": "stamped", "type": "timestamp"},
        {"name": "open", "type": "boolean"},
        {"name": "note", "type": "string"},
    ]
    rows = [
        {
            **row,
            "price": "12.50" if number % 2 else "-0.0",
            "opened": "2021-03-04",
            "stamped": "2021-03-04T10:00:00Z",
            "open": number % 2 == 0,
            "note": "Zürich" if number == 0 else None,
        }
        for number, row in enumerate(library.branches(BRANCHES))
    ]
    result = _run(tmp_path, library.contract(branch_columns=extra), rows)
    entry = result.document["references"]["branch"]
    assert entry["columns"] == ["branch_id", "city", "price", "opened", "stamped", "open", "note"]
    first = next(row for row in entry["rows"] if row[0] == "b01")
    second = next(row for row in entry["rows"] if row[0] == "b02")
    assert first[2:] == ["0", "2021-03-04", "1614852000000", "true", "Zürich"]
    assert second[2:] == ["12.5", "2021-03-04", "1614852000000", "false", None]


def test_shuffled_source_rows_give_the_same_bytes(tmp_path):
    first = _run(tmp_path, library.contract())
    (tmp_path / "again").mkdir()
    rows = library.branches(BRANCHES)
    random.Random(3).shuffle(rows)
    again = _run(tmp_path / "again", library.contract(), rows)
    assert first.canonical == again.canonical and first.digest == again.digest


def test_the_decimal_text_of_a_number_is_canonical():
    from fractions import Fraction

    assert references.decimal_text(Fraction(0)) == "0"
    assert references.decimal_text(Fraction(-5)) == "-5"
    assert references.decimal_text(Fraction(15, 10)) == "1.5"
    assert references.decimal_text(Fraction(-1, 8)) == "-0.125"
    assert references.decimal_text(Fraction(1, 1000)) == "0.001"
    with pytest.raises(ValueError):
        references.decimal_text(Fraction(1, 3))


# -- what writes none ----------------------------------------------------------------------------


def test_a_request_profile_writes_no_references(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable("branch", True, (RequestColumn("city", "category"),)),
            RequestTable("card", True, (RequestColumn("branch_id", "category"),)),
        ),
    )
    result = profile_exposes(
        library.contract(),
        [
            ExposeSource(
                "branch", library.write(tmp_path / "b.ndjson", library.branches(BRANCHES))
            ),
            ExposeSource("card", library.write(tmp_path / "c.ndjson", library.cards(MEMBERS))),
        ],
        backend=load_backend(),
        request=asked,
    )
    assert "references" not in result.document
    assert "public" not in result.document["disclosureRules"]
    assert not any("reference table" in what for what, _ in result.not_measured)


def test_a_private_release_writes_no_references_whatever_the_contract_marks_public():
    from fluid_build.profiler.dp import release
    from tests.profiler._fake_backend import FakeBackend
    from tests.profiler.dp import _release_data as data
    from tests.profiler.dp import _release_run as run

    pytest.importorskip("flux_stats.noise")
    marked = run.contract("orders")
    for expose in marked["exposes"]:
        expose["policy"] = {"classification": "Public"}
    backend = FakeBackend()
    plan = release.plan_release(
        marked,
        run.request("orders", backend),
        backend,
        unit="orders",
        rho=release.parse_rho("1"),
        unit_key=("customer_id",),
        max_rows_per_unit=3,
    )
    drawn = run.draw(plan, data.orders())
    document = json.loads(drawn.canonical)
    assert "references" not in document
    assert document["disclosureRules"] == {"noisy": {}}
    load_profile_text(drawn.canonical.decode("utf-8"))


# -- mutants -------------------------------------------------------------------------------------


def _differs(tmp_path: Path) -> bool:
    return _outcomes(tmp_path) != _expected()


def test_mutant_every_expose_counts_as_public(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_marked_public", lambda expose: True)
    assert _differs(tmp_path)


def test_mutant_the_classification_is_read_without_its_case(tmp_path, monkeypatch):
    def folded(expose: Any) -> bool:
        policy = expose.get("policy")
        return isinstance(policy, dict) and str(policy.get("classification")).lower() == "public"

    monkeypatch.setattr(references, "_marked_public", folded)
    assert _differs(tmp_path)


def test_mutant_an_expose_with_no_label_counts_as_public(tmp_path, monkeypatch):
    def unlabelled(expose: Any) -> bool:
        policy = expose.get("policy") or {}
        return policy.get("classification") in ("Public", None)

    monkeypatch.setattr(references, "_marked_public", unlabelled)
    assert _differs(tmp_path)


def test_mutant_the_cap_is_one_row_short(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "MOST_REFERENCE_ROWS", 63)
    assert _differs(tmp_path)


def test_mutant_the_cap_is_one_row_over(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "MOST_REFERENCE_ROWS", 65)
    assert _differs(tmp_path)


def test_mutant_no_label_holds_a_column_back(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_labelled", lambda expose: frozenset())
    assert _differs(tmp_path)


def test_mutant_only_some_sensitivity_values_are_labels(tmp_path, monkeypatch):
    def partly(expose: Any) -> Any:
        schema = expose["contract"]["schema"]
        return frozenset(
            entry["name"] for entry in schema if entry.get("sensitivity") in ("pii", "phi")
        )

    monkeypatch.setattr(references, "_labelled", partly)
    assert _differs(tmp_path)


def test_mutant_a_row_filter_is_no_restriction(tmp_path, monkeypatch):
    def only_a_policy(expose: Any) -> bool:
        privacy = (expose.get("policy") or {}).get("privacy") or {}
        return privacy.get("rowLevelPolicy") is not None

    monkeypatch.setattr(references, "_restricts_rows", only_a_policy)
    assert _differs(tmp_path)


def test_mutant_a_row_level_policy_is_no_restriction(tmp_path, monkeypatch):
    def only_filters(expose: Any) -> bool:
        authz = (expose.get("policy") or {}).get("authz") or {}
        return bool(authz.get("rowFilters"))

    monkeypatch.setattr(references, "_restricts_rows", only_filters)
    assert _differs(tmp_path)


def test_mutant_no_tag_marks_a_column(tmp_path, monkeypatch):
    monkeypatch.setattr(references, "_tagged_sensitive", lambda entry: False)
    assert _differs(tmp_path)


def test_mutant_a_tag_that_is_a_plain_string_is_no_tag(tmp_path, monkeypatch):
    def only_lists(entry: Any) -> bool:
        tags = entry.get("tags")
        return isinstance(tags, list) and any(
            isinstance(tag, str) and tag.casefold() in ("pii", "phi", "sensitive") for tag in tags
        )

    monkeypatch.setattr(references, "_tagged_sensitive", only_lists)
    assert _differs(tmp_path)


def test_mutant_a_tag_is_read_without_folding_its_case(tmp_path, monkeypatch):
    monkeypatch.setattr(
        references,
        "_tagged_sensitive",
        lambda entry: any(tag in ("pii", "phi", "sensitive") for tag in entry.get("tags", ())),
    )
    assert _differs(tmp_path)


def test_a_table_with_a_restricted_row_or_a_sensitive_tag_is_named_without_a_value(tmp_path):
    for number, contract in enumerate(
        (
            {"policy": {"authz": {"rowFilters": [{"principal": "g", "where": "city = 'north'"}]}}},
            {"policy": {"privacy": {"rowLevelPolicy": {"expression": "city = 'north'"}}}},
            {"branch_columns": [{"name": "label", "type": "string", "tags": ["PII"]}]},
        )
    ):
        rows = [{**row, "label": "x"} for row in library.branches(BRANCHES)]
        result = library.profile(
            _folder(tmp_path, str(number)),
            library.contract(**contract),
            rows,
            library.cards(MEMBERS),
        )
        assert "references" not in result.document
        said = _notes(result)["reference table branch"]
        assert "north" not in said and "PII" not in said and said


def test_mutant_a_versioned_target_is_published(tmp_path, monkeypatch):
    import dataclasses

    original = references.form_references

    def forgetting(stage: Any) -> Any:
        return original(
            dataclasses.replace(
                stage,
                relationships=[
                    dataclasses.replace(each, entity=False) for each in stage.relationships
                ],
            )
        )

    monkeypatch.setattr(references, "form_references", forgetting)
    assert _differs(tmp_path)


def test_the_unmutated_gate_matches_its_expected_outcomes(tmp_path):
    assert not _differs(tmp_path)
