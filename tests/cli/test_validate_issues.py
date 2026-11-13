# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate``: the authoring lint, and the ``issues[]`` every finding becomes.

The agent eval of #851 (``tests/agent_evals``) found the validate loop to be what
makes an agent's contract right: an agent with the FLUID skills and a validate hook
passed 11 of 11 scenarios. Two things it could not catch then are pinned here:

* the authoring lint: a placeholder or invented email (the embedded forge arm made
  one up in all 9 of its runs), the starter template's values, TODO and ``<...>``
  stand-ins, and an SDP that joins or consumes another product;
* the JSON shape: each error, warning and note is also an ``issues[]`` entry with a
  stable code, a JSON pointer, a fix and an example, so an agent knows where to edit
  and what to write.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from fluid_build import _error_catalog as catalog
from fluid_build._validation_issues import ISSUE_KEYS, json_pointer, message_pointer

REPO = Path(__file__).resolve().parents[2]

#: A clean contract in the shape the eval's reference solutions have: a team, no email.
BASE: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "sales.orders_raw",
    "name": "orders_raw",
    "description": "Raw orders from the shop's daily CSV export, with types and checks.",
    "domain": "sales",
    "metadata": {"productType": "SDP", "layer": "Bronze", "owner": {"team": "sales-data"}},
    "builds": [
        {
            "id": "ingest_orders",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT * FROM read_csv_auto('data/orders.csv', header=true)"},
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 2 * * *"},
                "runtime": {"platform": "local"},
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "orders_raw",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "runtime/out/orders_raw.parquet"},
            },
            "contract": {
                "schema": [
                    {"name": "order_id", "type": "string", "required": True},
                    {"name": "amount", "type": "decimal(12,2)", "required": True},
                ]
            },
        }
    ],
}

LINT_CODES = {
    "ERR_CONTRACT_PLACEHOLDER_EMAIL",
    "ERR_CONTRACT_TEMPLATE_RESIDUE",
    "ERR_CONTRACT_PLACEHOLDER_VALUE",
    "ERR_CONTRACT_SDP_JOINS",
    "ERR_CONTRACT_SDP_CONSUMES_PRODUCTS",
}


def _contract(**changes: Any) -> Dict[str, Any]:
    """``BASE`` with ``changes`` merged in: a mapping merges, anything else replaces."""
    doc = copy.deepcopy(BASE)
    _merge(doc, changes)
    return doc


def _merge(into: Dict[str, Any], changes: Dict[str, Any]) -> None:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        elif isinstance(value, list) and isinstance(into.get(key), list):
            # Lists merge by position, so an example can patch the first expose.
            for index, item in enumerate(value):
                if index < len(into[key]) and isinstance(item, dict):
                    _merge(into[key][index], item)
                elif index < len(into[key]):
                    into[key][index] = item
                else:
                    into[key].append(item)
        else:
            into[key] = value


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """``fluid validate <contract> --offline [args]``, in-process: ``(exit code, stdout)``."""
    from fluid_build.cli import main
    from fluid_build.credentials.dotenv_store import DOTENV_SWITCH

    monkeypatch.delenv(DOTENV_SWITCH, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".fluid").mkdir()  # a workspace boundary: the composition scan stops here

    def invoke(contract: Dict[str, Any], *args: str) -> Tuple[int, str]:
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        capsys.readouterr()
        try:
            code = main(["validate", str(path), "--offline", *args])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, capsys.readouterr().out

    return invoke


def _json(run, contract: Dict[str, Any], *args: str) -> Tuple[int, Dict[str, Any]]:
    code, out = run(contract, "--format", "json", *args)
    return code, json.loads(out[out.index("{") :])


def _issues(doc: Dict[str, Any], code: str) -> List[Dict[str, Any]]:
    return [issue for issue in doc.get("issues", []) if issue["code"] == code]


def _resolve(document: Any, pointer: str) -> Any:
    """The value at an RFC 6901 ``pointer``; ``KeyError``/``IndexError`` when absent."""
    node = document
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


# ---------------------------------------------------------------------------
# The base is clean, and a clean contract keeps the document it always had
# ---------------------------------------------------------------------------


def test_a_clean_contract_has_no_issues_and_the_document_it_always_had(run) -> None:
    code, doc = _json(run, BASE, "--strict")

    assert code == 0, doc
    assert list(doc) == ["valid", "schema_version", "errors", "warnings", "validation_time"]


# ---------------------------------------------------------------------------
# Authoring lint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "sales-data@example.com",  # what the forge arm invented in #851
        "orders@mail.example.org",
        "data-team@company.com",  # the starter template's
        "ops@billing.example",
        "ops@company.co.uk",
        "noreply@acme-retail.io",
        "owner@acme-retail.io",
        "test@acme-retail.io",
    ],
)
def test_a_placeholder_owner_email_is_a_warning_with_its_fix(run, address: str) -> None:
    contract = _contract(metadata={"owner": {"email": address}})

    plain, doc = _json(run, contract)
    strict, _ = _json(run, contract, "--strict")

    assert (plain, strict) == (0, 1), "a warning: validate passes, --strict fails"
    [issue] = _issues(doc, "ERR_CONTRACT_PLACEHOLDER_EMAIL")
    assert issue["severity"] == "warning"
    assert issue["pointer"] == "/metadata/owner/email"
    assert address in issue["message"]
    assert issue["fix"] == "Write the address the request gives; if it gives none, delete the key."
    assert "@" not in issue["example"], "the example must not teach another address"


@pytest.mark.parametrize(
    "address", ["sales-data@acme-retail.io", "orders-team@shop.co.uk", "dpo@examplefirm.com"]
)
def test_an_address_that_may_be_real_is_not_flagged(run, address: str) -> None:
    code, doc = _json(run, _contract(metadata={"owner": {"email": address}}), "--strict")

    assert code == 0, doc
    assert "issues" not in doc


def test_a_placeholder_address_inside_a_text_is_flagged_where_it_is(run) -> None:
    contract = _contract(description="Questions go to analysts@example.com.")

    _, doc = _json(run, contract)

    [issue] = _issues(doc, "ERR_CONTRACT_PLACEHOLDER_EMAIL")
    assert issue["pointer"] == "/description"
    assert issue["fix"] == "Replace it with the real address the request gives, or remove it."


@pytest.mark.parametrize(
    ("changes", "pointer"),
    [
        ({"id": "starter-product.starter"}, "/id"),
        ({"name": "starter-product"}, "/name"),
        ({"metadata": {"owner": {"team": "data-team"}}}, "/metadata/owner/team"),
        (
            {
                "builds": [
                    {"properties": {"sql": "SELECT 1 AS id, CURRENT_TIMESTAMP AS created_at"}}
                ]
            },
            "/builds/0/properties/sql",
        ),
        ({"exposes": [{"exposeId": "starter_table"}]}, "/exposes/0/exposeId"),
    ],
)
def test_a_starter_template_value_is_a_warning_at_its_place(
    run, changes: Dict[str, Any], pointer: str
) -> None:
    plain, doc = _json(run, _contract(**changes))

    assert plain == 0
    [issue] = _issues(doc, "ERR_CONTRACT_TEMPLATE_RESIDUE")
    assert issue["pointer"] == pointer
    assert issue["fix"] and issue["fix"] in issue["message"]


def test_the_starter_id_fix_names_the_contracts_own_domain_and_name(run) -> None:
    _, doc = _json(run, _contract(id="starter-product.starter"))

    [issue] = _issues(doc, "ERR_CONTRACT_TEMPLATE_RESIDUE")
    assert issue["fix"] == "Write <domain>.<name> from the request, for example sales.orders_raw."


def test_an_unedited_starter_scaffold_is_flagged_value_by_value(
    tmp_path: Path, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command the authoring skill tells agents to run first, exactly."""
    from fluid_build.cli import main

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    monkeypatch.chdir(scaffold)
    rc = main("forge --template starter --non-interactive --yes --target-dir . --no-memory".split())
    assert rc in (0, None)
    contract = yaml.safe_load((scaffold / "contract.fluid.yaml").read_text(encoding="utf-8"))

    plain, doc = _json(run, contract)

    assert plain == 0, "the starter is valid: the lint warns, it does not fail it"
    pointers = {i["pointer"] for i in _issues(doc, "ERR_CONTRACT_TEMPLATE_RESIDUE")}
    assert {"/id", "/name", "/metadata/owner/team", "/exposes/0/exposeId"} <= pointers
    assert any(p.startswith("/builds/0") for p in pointers)
    assert _issues(doc, "ERR_CONTRACT_PLACEHOLDER_EMAIL"), "the starter writes a stand-in email"


@pytest.mark.parametrize(
    "description", ["TODO", "Owner: TBD", "FIXME before go-live", "Owned by <your-team>"]
)
def test_a_placeholder_value_is_a_warning(run, description: str) -> None:
    _, doc = _json(run, _contract(description=description))

    [issue] = _issues(doc, "ERR_CONTRACT_PLACEHOLDER_VALUE")
    assert issue["pointer"] == "/description"


def test_operators_types_markup_and_lower_case_words_are_not_placeholders(run) -> None:
    contract = _contract(
        description="Orders of the <b>shop</b>, for the todo list app, at <https://shop.io>.",
        builds=[{"properties": {"sql": "SELECT * FROM o WHERE amount <> 0 AND qty < 5 OR x > 2"}}],
    )

    code, doc = _json(run, contract, "--strict")

    assert code == 0, doc.get("issues")


def test_an_sdp_build_that_joins_is_a_warning(run) -> None:
    sql = "SELECT o.*, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"
    _, doc = _json(run, _contract(builds=[{"properties": {"sql": sql}}]))

    [issue] = _issues(doc, "ERR_CONTRACT_SDP_JOINS")
    assert issue["pointer"] == "/builds/0/properties/sql"
    assert "ADP" in issue["fix"]


@pytest.mark.parametrize(
    ("metadata", "sql"),
    [
        ({"productType": "ADP", "layer": "Silver"}, "SELECT * FROM a JOIN b ON a.id = b.id"),
        ({}, "SELECT e.id, t.tag FROM events e CROSS JOIN UNNEST(e.tags) AS t(tag)"),
        ({}, "SELECT * FROM a -- a JOIN would go here\nWHERE note <> 'JOIN'"),
    ],
)
def test_a_join_an_sdp_may_have_or_an_adp_build_is_not_flagged(
    run, metadata: Dict[str, Any], sql: str
) -> None:
    contract = _contract(metadata=metadata, builds=[{"properties": {"sql": sql}}])

    _, doc = _json(run, contract)

    assert not _issues(doc, "ERR_CONTRACT_SDP_JOINS")


def test_an_sdp_that_consumes_an_unresolved_product_is_a_warning(run) -> None:
    contract = _contract(consumes=[{"productId": "crm.customers", "exposeId": "customers"}])

    plain, doc = _json(run, contract)

    assert plain == 0
    [issue] = _issues(doc, "ERR_CONTRACT_SDP_CONSUMES_PRODUCTS")
    assert issue["pointer"] == "/consumes/0"
    assert "crm.customers" in issue["message"] and "ADP" in issue["fix"]


def test_an_adp_may_consume_a_product_of_another_workspace(run) -> None:
    contract = _contract(
        metadata={"productType": "ADP", "layer": "Silver"},
        consumes=[{"productId": "crm.customers", "exposeId": "customers"}],
    )

    code, doc = _json(run, contract, "--strict")

    assert code == 0, doc.get("issues")


def test_text_output_prints_the_fix_with_the_warning(run) -> None:
    code, out = run(_contract(metadata={"owner": {"email": "sales-data@example.com"}}))

    assert code == 0
    assert "metadata.owner.email: 'sales-data@example.com' is a placeholder address" in out
    assert "if it gives none, delete the key" in out


# ---------------------------------------------------------------------------
# The issues[] shape
# ---------------------------------------------------------------------------


def _broken() -> Dict[str, Any]:
    """A contract with a schema error of each common kind and two rule findings."""
    contract = _contract(
        metadata={"productType": "CDP", "owner": {"email": "x@example.com"}},
        exposes=[{"kind": "tabel", "surprise": True}],
    )
    del contract["exposes"][0]["binding"]
    return contract


def test_every_finding_is_an_issue_with_code_pointer_fix_and_example(run) -> None:
    contract = _broken()

    code, doc = _json(run, contract)

    assert code == 1
    assert doc["errors"] and all(isinstance(e, str) for e in doc["errors"])
    assert list(doc) == [
        "valid",
        "schema_version",
        "errors",
        "warnings",
        "issues",
        "validation_time",
    ]
    issues = doc["issues"]
    expected = [("error", m) for m in doc["errors"]] + [("warning", m) for m in doc["warnings"]]
    assert [(i["severity"], i["message"]) for i in issues] == expected
    for issue in issues:
        assert tuple(issue) == ISSUE_KEYS
        assert re.fullmatch(r"ERR_[A-Z0-9_]+", issue["code"]), issue
        event = issue["code"][len("ERR_") :].lower()
        assert catalog.is_catalogued(event), f"{issue['code']} has no catalog entry"
        assert issue["pointer"] == "" or issue["pointer"].startswith("/"), issue
        assert issue["fix"].strip() and issue["example"].strip(), issue
        assert issue["docs_url"].startswith(catalog.DOC_BASE), issue


def test_schema_errors_carry_their_keyword_code_and_exact_pointer(run) -> None:
    _, doc = _json(run, _broken())

    found = {(i["code"], i["pointer"]) for i in doc["issues"]}
    assert ("ERR_CONTRACT_SCHEMA_ENUM", "/exposes/0/kind") in found
    assert ("ERR_CONTRACT_SCHEMA_UNKNOWN_FIELD", "/exposes/0/surprise") in found
    assert ("ERR_CONTRACT_SCHEMA_REQUIRED", "/exposes/0") in found


def test_a_schema_error_pointer_resolves_in_the_contract(run) -> None:
    contract = _broken()

    _, doc = _json(run, contract)

    for issue in doc["issues"]:
        if issue["code"].startswith("ERR_CONTRACT_SCHEMA_"):
            _resolve(contract, issue["pointer"])  # raises when the pointer is wrong


def test_a_schema_errors_example_is_the_block_it_is_in(run) -> None:
    contract = _contract(exposes=[{"colour": "blue", "binding": {"format": "parket"}}])

    _, doc = _json(run, contract)

    examples = {i["pointer"]: i["example"] for i in doc["issues"]}
    assert examples["/exposes/0/colour"].startswith("exposes:\n- exposeId:")
    assert examples["/exposes/0/binding/format"].startswith("# exposes[].binding")


def test_a_contract_rule_finding_carries_its_check_code(run) -> None:
    contract = _contract(metadata={"productType": "CDP", "layer": "Bronze"})

    code, doc = _json(run, contract)

    assert code == 1
    [issue] = _issues(doc, "ERR_CONTRACT_PRODUCT_TYPE_INCONSISTENT")
    assert issue["severity"] == "error" and issue["pointer"] == "/metadata"
    assert "SDP with Bronze" in issue["fix"]


# ---------------------------------------------------------------------------
# message_pointer and json_pointer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "pointer"),
    [
        ("exposes[0].binding.location: needs a path", "/exposes/0/binding/location"),
        ("exposes[orders_raw] declares a grant nothing enforces", "/exposes/0"),
        ("exposes[0].contract.schema.amount: bad nested type", "/exposes/0/contract/schema/1"),
        ("fragments/x.yaml:21 (exposes[0].binding): unknown", "/exposes/0/binding"),
        ("❌ exposes[0].binding: region outside allowedRegions", "/exposes/0/binding"),
        ("expose 'orders_raw': binding.location.catalog 'x' is unknown", "/exposes/0"),
        ("builds[ingest_orders] runs forever", "/builds/0"),
        ("metadata consistency: Silver and CDP disagree", "/metadata"),
        ("composition rule: nothing here", "/fallback"),
        ("exposes[9].binding: no such expose", "/exposes"),
    ],
)
def test_message_pointer_finds_the_place_a_message_names(message: str, pointer: str) -> None:
    assert message_pointer(message, BASE, default="/fallback") == pointer


def test_json_pointer_escapes_tilde_and_slash() -> None:
    assert json_pointer(["a/b", "c~d", 0]) == "/a~1b/c~0d/0"
    assert json_pointer([]) == ""


# ---------------------------------------------------------------------------
# The catalog covers every code validate reports
# ---------------------------------------------------------------------------

_SOURCES = ("fluid_build/cli/validate.py", "fluid_build/schema_manager.py")


def _event_literals() -> List[Tuple[str, int, str]]:
    """``(file, line, event)`` of every ``event=`` a finding is recorded with."""
    found = []
    for rel in _SOURCES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("add_error", "add_warning", "add_note"):
                kws = {k.arg: k.value for k in node.keywords}
                assert "event" in kws, f"{rel}:{node.lineno}: {name}() without event="
                value = kws["event"]
            elif name == "_add_findings":
                value = node.args[2]
            else:
                continue
            if isinstance(value, ast.Constant):
                found.append((rel, node.lineno, value.value))
    return found


def test_every_finding_validate_records_names_a_catalogued_check() -> None:
    literals = _event_literals()

    assert len(literals) >= 30, literals
    missing = [f"{f}:{n} {e}" for f, n, e in literals if not catalog.is_catalogued(e)]
    assert not missing, missing


def test_every_schema_keyword_event_and_lint_event_is_catalogued() -> None:
    from fluid_build.schema_manager import _SCHEMA_EVENTS

    events = set(_SCHEMA_EVENTS.values()) | {"contract_schema_invalid"}
    events |= {code[len("ERR_") :].lower() for code in LINT_CODES}
    assert not [e for e in sorted(events) if not catalog.is_catalogued(e)]


@pytest.mark.parametrize("event", catalog.validation_events())
def test_every_validation_code_has_a_fix_a_docs_page_and_an_example(event: str) -> None:
    assert catalog.fix_for(event).strip()
    assert catalog.docs_url_for(event).startswith(catalog.DOC_BASE)
    assert (catalog.example_for(event) or "").strip(), f"{event} has no example"


#: ``# exposes[].binding`` on an example's first line: the fragment kind it validates as.
FRAGMENT_KINDS = {
    "exposes[].binding": "binding",
    "exposes[].contract.schema[]": "column",
    "exposes[].contract.dq": "dqSpec",
    "builds[]": "build",
}
#: Every example an issue can carry: each event's own, and each block's for schema errors.
_EXAMPLES = sorted(
    {catalog.example_for(e) for e in catalog.catalogued_events() if catalog.example_for(e)}
    | set(catalog.block_examples())
)


@pytest.mark.parametrize("example", _EXAMPLES, ids=[e.splitlines()[0][:40] for e in _EXAMPLES])
def test_every_example_is_valid_fluid_that_passes_the_lint(
    example: str, run, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import build_parser, main

    if example.startswith("fluid "):
        build_parser().parse_args(shlex.split(example)[1:])  # a real command and flags
        return
    first = example.splitlines()[0].lstrip("# ").strip()
    if first in FRAGMENT_KINDS:
        fragment = tmp_path / "fragment.yaml"
        fragment.write_text(example, encoding="utf-8")
        doc = yaml.safe_load(example)
        for item in doc if isinstance(doc, list) else [doc]:
            fragment.write_text(yaml.safe_dump(item, sort_keys=False), encoding="utf-8")
            capsys.readouterr()
            code = main(
                ["validate", "--fragment", FRAGMENT_KINDS[first], str(fragment)]
                + ["--schema-version", "0.7.6"]
            )
            assert code == 0, capsys.readouterr().out
        return
    doc = yaml.safe_load(example)
    assert isinstance(doc, dict), example
    code, result = _json(run, _contract(**doc), "--strict")
    assert code == 0, f"{example}: {result}"
