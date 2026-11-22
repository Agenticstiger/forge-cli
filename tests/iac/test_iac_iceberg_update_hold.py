# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A live Iceberg table the plan would update in place is left as it is: the rule.

hashicorp/aws 6.73 updates an ``aws_glue_catalog_table`` with an ``UpdateTable`` that
carries no ``VersionId``, after re-reading the table's ``metadata_location``, so a commit
an Iceberg writer makes in between is reverted. Which tables the plan holds
(``aws_adoption.iceberg_update_hold``), what the note says the contract wanted, and how
the engine holds, refuses and audits (``cli/_apply_table_hold.py``). The plan changes have
the shape ``tofu show -json`` gives (OpenTofu 1.13, hashicorp/aws 6.73, read from moto);
``tests/iac/test_iac_aws_iceberg_update_hold_moto.py`` runs the same decisions on a real
plan.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build._contract_loader import CLIError
from fluid_build.cli import _apply_table_hold as hold
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.providers.aws_adoption import (
    ICEBERG_HELD,
    held_drift,
    iceberg_update_hold,
)
from fluid_build.iac.table_hold import TableHold
from tests.iac.test_iac_table_adoption import (
    ADDRESS,
    HIVE_PARQUET,
    ICEBERG,
    LIVE_ICEBERG,
    LOCATION,
    PARQUET,
    PARQUET_SERDE,
    _change,
    _column,
    _table,
)

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.iceberg.hold")

#: A table forge made through Glue's Iceberg create, as a state that kept it shows it:
#: the provider hid its table_type, and no engine has written to it yet.
MADE_BY_FORGE = _table(
    [_column("id", "bigint"), _column("amount", "decimal(12,2)")],
    open_table_format_input=[
        {"iceberg_input": [{"iceberg_table_input": [], "metadata_operation": "CREATE"}]}
    ],
)


def _update(before: Dict[str, Any], **after: Any) -> Dict[str, Any]:
    """A plan change that updates ``before`` in place, the contract's values in ``after``."""
    planned = copy.deepcopy(before)
    planned.update(after)
    change = _change(before, planned)
    change["change"].pop("importing")
    return change


def _plan(*changes: Dict[str, Any]) -> Dict[str, Any]:
    return {"resource_changes": list(changes)}


def _module(body: Dict[str, Any] = ICEBERG) -> str:
    return json.dumps({"resource": {"aws_glue_catalog_table": {"sales_orders_sales_orders": body}}})


def _args(**over: Any) -> argparse.Namespace:
    return argparse.Namespace(**{"allow_iceberg_update": False, **over})


# ── which tables are held ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "before, why",
    [
        (LIVE_ICEBERG, "its columns carry Iceberg field ids"),
        (
            _table(
                [_column("id", "bigint")],
                parameters={"previous_metadata_location": LOCATION + "/metadata/0.json"},
            ),
            "Glue keeps Iceberg metadata locations for it",
        ),
        (
            _table([_column("id", "bigint")], parameters={"table_type": "iceberg"}),
            "its table_type is ICEBERG",
        ),
        (MADE_BY_FORGE, "the state records its Iceberg create"),
    ],
)
def test_a_live_iceberg_table_is_held_whatever_the_binding_declares(before, why):
    """What the plan shows of the live table decides: an engine's commit, a table_type the
    provider did show (any case), the Iceberg create the state recorded."""
    for declared in (ICEBERG, PARQUET):
        found = iceberg_update_hold(_update(before)["change"], declared)

        assert found is not None
        assert (
            found.attributes
            == ICEBERG_HELD
            == (
                "description",
                "owner",
                "retention",
                "storage_descriptor",
            )
        )
        assert found.table == "sales.orders" and found.why == why


def test_a_table_forge_made_iceberg_and_re_attached_before_any_write_is_held():
    """A fresh state re-attaches it by import: no state record and no engine evidence, but
    the module declares it Iceberg and it has no Hive format."""
    fresh = _table([_column("id", "bigint"), _column("amount", "decimal(12,2)")])

    found = iceberg_update_hold(_update(fresh)["change"], ICEBERG)

    assert found is not None
    assert found.why == "the contract declares it an Iceberg table, and forge made it one"


@pytest.mark.parametrize(
    "before, declared",
    [
        # A Hive table under a Hive binding keeps its in-place updates.
        (_table([_column("id", "bigint")], **HIVE_PARQUET), PARQUET),
        (_table([_column("id", "bigint")], serde=PARQUET_SERDE), PARQUET),
        (_table([_column("id", "bigint")]), PARQUET),
        # A live Hive table is not an Iceberg one whatever the binding declares.
        (_table([_column("id", "bigint")], **HIVE_PARQUET), ICEBERG),
        (_table([_column("id", "bigint")], serde=PARQUET_SERDE), ICEBERG),
    ],
)
def test_a_hive_table_is_not_held(before, declared):
    assert iceberg_update_hold(_update(before)["change"], declared) is None


def test_the_plugin_holds_only_glue_tables():
    plugin = get_iac_plugin("aws")
    table = _update(LIVE_ICEBERG, description="new")
    other = dict(copy.deepcopy(table), type="aws_s3_bucket")

    assert isinstance(plugin.update_hold(table, ICEBERG), TableHold)
    assert plugin.update_hold(other, ICEBERG) is None


# ── what the contract wanted ────────────────────────────────────────────────


def test_the_note_names_each_held_attribute_with_the_tables_value_and_the_contracts():
    before = _table(
        [
            _column("id", "bigint", field_id=1),
            _column("Amount", "decimal(12,2)", field_id=2, comment="old"),
            _column("dropped", "string", field_id=3),
            _column("legacy", "int", field_id=4, current=False),
        ],
        description="written by the stream",
        owner="hadoop",
    )
    after = copy.deepcopy(before)
    after.update(description="Order events", owner=None)
    descriptor = after["storage_descriptor"][0]
    descriptor["columns"] = [
        _column("id", "string"),
        _column("amount", "decimal(12,2)", comment="the amount"),
        _column("note", "string"),
    ]
    descriptor["location"] = LOCATION + "/v2"
    descriptor["compressed"] = True

    lines = held_drift(_change(before, after)["change"])

    assert lines == [
        "description: written by the stream in the table, Order events in the contract",
        "owner: hadoop in the table, none in the contract",
        "column id: bigint in the table, string in the contract",
        "column Amount: written amount in the contract",
        "comment of column amount: old in the table, the amount in the contract",
        "column note (string): in the contract, not in the table",
        "column dropped (string): in the table, not in the contract",
        "columns of older Iceberg schemas (legacy): listed by the table, not by the contract "
        "(Iceberg keeps listing them)",
        f"location: {LOCATION} in the table, {LOCATION}/v2 in the contract",
        "storage setting compressed: none in the table, True in the contract",
    ]


def test_the_note_quotes_catalog_text_safely_and_skips_what_the_plan_does_not_know():
    before = _table([_column("id", "bigint", field_id=1)], description="evil\x1b[2Jtext")
    after = copy.deepcopy(before)
    after["description"] = "x" * 500
    change = _change(before, after)["change"]
    change["after_unknown"] = {"owner": True, "storage_descriptor": [{"location": True}]}

    (line,) = held_drift(change)

    assert "\x1b" not in line and "evil?[2Jtext in the table" in line
    assert len(line) < 500 and line.endswith("… in the contract")


def test_the_order_of_the_same_columns_is_a_line_of_its_own():
    before = _table([_column("a", "int", field_id=1), _column("b", "int", field_id=2)])
    after = copy.deepcopy(before)
    after["storage_descriptor"][0]["columns"] = list(
        reversed(after["storage_descriptor"][0]["columns"])
    )

    assert held_drift(_change(before, after)["change"]) == [
        "column order: (a, b) in the table, (b, a) in the contract"
    ]


# ── the engine: what it holds ───────────────────────────────────────────────


def test_only_an_in_place_update_is_held_never_a_replace_or_a_create():
    plugin = get_iac_plugin("aws")
    update = _update(LIVE_ICEBERG, description="new")
    replace = copy.deepcopy(update)
    replace["change"]["actions"] = ["delete", "create"]
    create = copy.deepcopy(update)
    create["change"].update(actions=["create"], before=None)
    noop = copy.deepcopy(update)
    noop["change"]["actions"] = ["no-op"]

    for change in (replace, create, noop):
        assert hold.planned_updates(plugin, _module(), _plan(change)) == {}
    found = hold.planned_updates(plugin, _module(), _plan(update))

    assert list(found) == [ADDRESS]
    assert found[ADDRESS].table == "sales.orders" and not found[ADDRESS].imported
    assert found[ADDRESS].hold.drift == ("description: none in the table, new in the contract",)


def test_an_import_that_updates_the_table_is_held_and_known_as_imported():
    found = hold.planned_updates(
        get_iac_plugin("aws"), _module(), _plan(_change(LIVE_ICEBERG, LIVE_ICEBERG))
    )

    assert found[ADDRESS].imported


def test_a_plugin_without_the_capability_or_a_plan_not_read_holds_nothing():
    update = _plan(_update(LIVE_ICEBERG, description="new"))

    assert hold.planned_updates(object(), _module(), update) == {}
    assert hold.planned_updates(get_iac_plugin("aws"), _module(), None) == {}


def test_the_flag_holds_nothing():
    found = hold.planned_updates(
        get_iac_plugin("aws"), _module(), _plan(_update(LIVE_ICEBERG, description="new"))
    )

    assert hold.to_hold(_args(), found) == found
    assert hold.to_hold(_args(allow_iceberg_update=True), found) == {}


def test_holding_adds_the_attributes_to_the_tables_ignore_changes_only(tmp_path: Path):
    body = dict(copy.deepcopy(ICEBERG), lifecycle={"ignore_changes": ["parameters"]})
    other = {"bucket": "lake"}
    module = tmp_path / "main.tf.json"
    module.write_text(
        json.dumps(
            {
                "resource": {
                    "aws_glue_catalog_table": {"sales_orders_sales_orders": body},
                    "aws_s3_bucket": {"lake": other},
                }
            }
        ),
        encoding="utf-8",
    )
    held = hold.planned_updates(
        get_iac_plugin("aws"), _module(), _plan(_update(LIVE_ICEBERG, description="new"))
    )

    assert hold.hold_still(module, held) is True
    assert hold.hold_still(module, {}) is False

    written = json.loads(module.read_text(encoding="utf-8"))["resource"]
    assert written["aws_glue_catalog_table"]["sales_orders_sales_orders"]["lifecycle"] == {
        "ignore_changes": ["parameters", "description", "owner", "retention", "storage_descriptor"]
    }
    assert written["aws_s3_bucket"]["lake"] == other


def _held(*, imported: bool = False) -> Dict[str, hold.Held]:
    table_hold = TableHold("sales.orders", ICEBERG_HELD, ("description: a, b",), "why")
    return {ADDRESS: hold.Held(ADDRESS, table_hold, imported)}


def test_the_plan_made_again_must_leave_the_held_table_with_no_change():
    noop = _update(LIVE_ICEBERG)
    noop["change"]["actions"] = ["no-op"]
    hold.check_held(_plan(noop), _held())

    with pytest.raises(CLIError) as raised:
        hold.check_held(_plan(_update(LIVE_ICEBERG, description="new")), _held())
    assert raised.value.event == "iceberg_table_update_in_place"
    assert "the plan still update" in raised.value.context["error"]

    with pytest.raises(CLIError) as raised:
        hold.check_held(_plan(), _held())
    assert "leaves it out" in raised.value.context["error"]


def test_an_adopted_table_the_plan_still_updates_keeps_the_adoptions_event():
    with pytest.raises(CLIError) as raised:
        hold.check_held(
            _plan(_change(LIVE_ICEBERG, LIVE_ICEBERG)), _held(imported=True), adopted=[ADDRESS]
        )

    assert raised.value.event == "table_adoption_mismatch"


# ── the note ────────────────────────────────────────────────────────────────


def test_the_note_is_printed_with_the_remedy_and_logged_at_warning(capfd, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        hold.say_held(_LOG, _held())

    out = " ".join(capfd.readouterr().out.split())
    assert "sales.orders is a live Iceberg table (why): the plan leaves it as it is" in out
    assert "description: a, b" in out
    assert "change it through the table's Iceberg catalog" in out
    assert "pause the table's writers and re-run with --allow-iceberg-update" in out
    (record,) = [r for r in caplog.records if "iceberg_table_update_held" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert '"table": "sales.orders"' in record.getMessage()


# ── the backstop ────────────────────────────────────────────────────────────


def test_the_backstop_refuses_an_update_the_hold_missed_unless_the_run_asked():
    plugin, update = get_iac_plugin("aws"), _plan(_update(LIVE_ICEBERG, description="new"))

    with pytest.raises(CLIError) as raised:
        hold.review(plugin, _module(), update, _args())
    assert raised.value.event == "iceberg_table_update_in_place"
    assert raised.value.context["tables"][0]["table"] == "sales.orders"
    assert "no VersionId" in raised.value.context["error"]

    (allowed,) = hold.review(plugin, _module(), update, _args(allow_iceberg_update=True))
    assert allowed.table == "sales.orders"


def test_a_saved_plan_may_update_only_the_tables_its_record_names(capfd):
    plugin, update = get_iac_plugin("aws"), _plan(_update(LIVE_ICEBERG, description="new"))
    asked = _args(allow_iceberg_update=True)

    (allowed,) = hold.review(plugin, _module(), update, _args(), recorded=["sales.orders"])
    assert allowed.table == "sales.orders"
    for recorded in ([], ["sales.other"]):
        with pytest.raises(CLIError) as raised:
            # The apply's own flag decides nothing for a saved plan.
            hold.review(plugin, _module(), update, asked, recorded=recorded)
        assert raised.value.event == "iceberg_table_update_in_place"
        assert "its record does not name" in raised.value.context["error"]
        assert "fluid plan --tofu" in raised.value.context["remediation"][0]


def test_a_plan_not_read_back_is_refused_whatever_the_flags():
    for args in (_args(), _args(allow_iceberg_update=True)):
        with pytest.raises(CLIError) as raised:
            hold.review(get_iac_plugin("aws"), _module(), None, args)
        assert "could not be read back" in raised.value.context["error"]


def test_plan_tofu_only_says_the_apply_will_refuse(capfd):
    update = _plan(_update(LIVE_ICEBERG, description="new"))

    assert hold.review(get_iac_plugin("aws"), _module(), update, _args(), enforce=False) == []

    assert "`fluid apply` will refuse it" in " ".join(capfd.readouterr().out.split())


def test_a_plan_with_no_held_update_passes_the_backstop():
    noop = _update(LIVE_ICEBERG)
    noop["change"]["actions"] = ["no-op"]
    hive = _update(_table([_column("id", "bigint")], **HIVE_PARQUET), description="new")

    assert hold.review(get_iac_plugin("aws"), _module(PARQUET), _plan(noop, hive), _args()) == []


# ── the record and the audit ────────────────────────────────────────────────


def test_the_record_names_the_updated_tables_and_only_when_there_are_some():
    updated = list(_held().values())

    assert hold.record_field(updated) == {"icebergUpdates": ["sales.orders"]}
    assert hold.record_field([]) == {}
    assert hold.recorded({"icebergUpdates": ["sales.orders"]}) == ["sales.orders"]
    assert hold.recorded({}) == [] and hold.recorded({"icebergUpdates": "x"}) == []


def test_an_update_the_run_makes_is_audited_at_warning(capfd, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        hold.audit_updated(_LOG, list(_held().values()))
        hold.audit_updated(_LOG, [])

    (record,) = [r for r in caplog.records if "iceberg_table_update_accepted" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "sales.orders updated in place with no VersionId" in " ".join(
        capfd.readouterr().out.split()
    )


def test_the_bound_apply_names_what_its_plan_updates_and_that_its_flag_changes_nothing(capfd):
    hold.say_bound(_args(allow_iceberg_update=True), {"icebergUpdates": ["sales.orders"]})

    out = " ".join(capfd.readouterr().out.split())
    assert "the saved plan updates in place, as its plan asked" in out and "sales.orders" in out
    assert "--allow-iceberg-update changes nothing here" in out
