# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The release under differential privacy: planning, reading, drawing, the guarantee and the record."""

from __future__ import annotations

import dataclasses
import json
import random
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List

import pytest

from fluid_build.profiler.dp import bounding, consistency, release, selection
from fluid_build.profiler.dp.bounding import KEYED, RELATED, ROWS
from fluid_build.profiler.dp.release import ReleaseRefused
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _release_data as data
from tests.profiler.dp import _release_run as run
from tests.profiler.dp._privacy import need
from tests.profiler.dp._release_run import SEED, columns, contract, draw, request

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction


def _event(call: Any) -> str:
    with pytest.raises(ReleaseRefused) as info:
        call()
    return info.value.event


# ---- exact numbers ------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["1", "0.5", "0.000000000001", "12.50", "1" * 100])
def test_a_rho_is_decimal_text_above_zero(text):
    assert release.parse_rho(text) == F(text)


def test_a_rho_above_one_draws_a_warning_and_is_still_taken():
    assert release.parse_rho("1") == 1 and release.parse_rho("1.01") == F("1.01")
    assert release.rho_warning(release.parse_rho("1")) is None
    assert release.rho_warning(F("0.5")) is None
    warned = release.rho_warning(release.parse_rho("1.01"))
    assert warned is not None and warned.startswith("rho 1.01 is above 1, a large privacy loss")
    assert "OpenDP" in warned and "2020 Census" in warned


@pytest.mark.parametrize(
    "text",
    [
        "0",
        "0.0",
        "-1",
        "+1",
        "1e3",
        "1E-2",
        " 1",
        "1 ",
        ".5",
        "5.",
        "abc",
        "",
        "1" * 101,
        "0." + "1" * 100,
    ],
)
def test_a_rho_that_is_not_plain_decimal_text_above_zero_is_refused(text):
    assert _event(lambda: release.parse_rho(text)) == "profile_release_rho_invalid"


@pytest.mark.parametrize(
    "value, text",
    [
        (F(1), "1"),
        (F(1, 2), "0.5"),
        (F(9, 10), "0.9"),
        (F(3, 1000), "0.003"),
        (F(-5, 4), "-1.25"),
        (F(0), "0"),
        (F(2041242, 10**6), "2.041242"),
        (F(100), "100"),
    ],
)
def test_decimal_text_spells_a_terminating_rational(value, text):
    assert release.decimal_text(value) == text


def test_decimal_text_refuses_a_rational_that_does_not_terminate():
    with pytest.raises(ValueError):
        release.decimal_text(F(1, 3))


def test_the_scale_is_the_least_step_that_spends_no_more_than_the_share():
    rng = random.Random(20261121)
    for _ in range(300):
        share = F(rng.randint(1, 10**9), 10 ** rng.randint(1, 12))
        sensitivity = rng.choice([1, 2, 4, 9, 25, 100, 1000])
        units = release.scale_units(share, sensitivity)
        spent = F(sensitivity) / (2 * F(units, 10**6) ** 2)
        assert spent <= share
        if units > 1:
            assert F(sensitivity) / (2 * F(units - 1, 10**6) ** 2) > share


def test_a_known_scale():
    # sigma^2 >= 1 / (2 * 0.12) is 4.1666..., sigma >= 2.041241..., so 2.041242
    assert release.scale_units(F(12, 100), 1) == 2_041_242


def test_the_budget_splits_a_tenth_to_the_selection_and_the_rest_over_the_tables():
    spent, share = release.split_budget(F(1), True, 3)
    assert spent == F(1, 10)
    assert share == F(3, 10)
    spent, share = release.split_budget(F(1), False, 3)
    assert spent == 0 and share == F(333333333333, 10**12)
    assert 3 * share <= 1


def test_the_parts_never_add_up_to_more_than_the_total():
    rng = random.Random(3)
    for _ in range(200):
        total = F(rng.randint(1, 10**6), 10 ** rng.randint(0, 9))
        tables = rng.randint(1, 20)
        spent, share = release.split_budget(total, rng.random() < 0.5, tables)
        assert spent + tables * share <= total


def test_a_share_that_floors_to_zero_is_refused_before_it_is_divided_by():
    assert (
        _event(lambda: release.split_budget(F(1, 10**12), False, 2))
        == "profile_release_rho_too_small"
    )
    assert (
        _event(lambda: release.split_budget(F(1, 10**400), True, 1))
        == "profile_release_rho_too_small"
    )


def test_the_conversion_to_epsilon_at_delta_is_rounded_up():
    assert release.epsilon_at_delta(F(1)) == Decimal("8.434")
    rho = F(1, 2)
    # 0.5 + 2 sqrt(0.5 * ln 1e6) = 0.5 + 2 * 2.62826... = 5.75652...
    assert release.epsilon_at_delta(rho) == Decimal("5.757")


def test_a_noise_stream_label_is_never_the_bounding_label():
    for names in (["a"], ["a", "b"], ["bound"], ["noise"], []):
        label = release.noise_label(names)
        assert label.startswith("noise/")
        assert label != "bound"
    plan = run.keyed_plan()
    drawn = draw(plan, data.orders())
    assert all(table.label == release.noise_label(table.columns) for table in drawn.tables)
    assert "bound" not in {table.label for table in drawn.tables}


# ---- the plan -----------------------------------------------------------------------------


def test_a_keyed_plan_releases_the_required_enums_and_the_ranged_number():
    plan = run.keyed_plan()
    assert plan.unit.mode == KEYED and plan.table == "orders"
    assert [(each.name, each.domain) for each in plan.columns] == [
        ("status", "category"),
        ("channel", "category"),
        ("amount", "number"),
    ]
    assert plan.column("status").levels == ("paid", "placed", "returned", "shipped")
    assert plan.column("channel").levels == ("online", "store")
    amount = plan.column("amount")
    assert amount.edge_texts[0] == "0" and amount.edge_texts[-1] == "1000"
    assert amount.size == len(amount.edges) - 1 == 10
    assert plan.selects
    assert [name for name, _ in plan.table_columns] == [
        "order_id",
        "customer_id",
        "status",
        "channel",
        "amount",
        "note",
    ]
    assert [column.name for column in plan.read] == ["customer_id", "status", "channel", "amount"]
    assert plan.dimensions == {"channel"}
    assert not plan.has_other_tables


def test_every_requested_item_not_released_is_named_with_why():
    plan = run.keyed_plan()
    said = dict(plan.not_released)
    assert said["rowCount of orders"]
    assert said["count, distinctCount, max, min, nullCount, quantiles, sum of orders.amount"]
    assert said["mean/1 of orders.amount"] and said["quantile/2 of orders.amount"]
    assert said["support/1 of orders.status"]
    assert "key column" in said["marginalHistogram/1 of orders.customer_id"]
    assert "key column" in said["marginalHistogram/1 of orders.order_id"]
    assert all(reason for reason in said.values())
    # a released histogram is not on the list
    assert "levelHistogram/1 of orders.status" not in said
    assert "marginalHistogram/1 of orders.amount" not in said


def test_a_related_plan_lists_the_other_tables_with_their_domains_only():
    plan = run.related_plan()
    assert plan.unit.mode == RELATED and plan.unit.relationship == "placed_by"
    assert plan.has_other_tables
    assert plan.other_tables == (
        ("customers", (("customer_id", "number"), ("region", "category"))),
    )
    said = dict(plan.not_released)
    assert "table customers" in said
    assert any(what.startswith("children per parent") for what in said)
    assert [each.name for each in plan.columns] == ["status", "channel", "amount"]


def test_a_rows_plan_has_each_row_an_entity_and_the_primary_key_unreleased():
    plan = run.rows_plan()
    assert plan.unit.mode == ROWS and plan.unit.key_columns == ("order_id",)
    # with no unit key, customer_id is a number like any other: a histogram on the type's ladder
    assert [column.name for column in plan.read] == [
        "order_id",
        "customer_id",
        "status",
        "channel",
        "amount",
    ]
    assert plan.column("customer_id").size > 50
    assert plan.sensitivity == 1


def test_a_plan_reads_no_record():
    class Guard(FakeBackend):
        def read_value(self, *a: Any, **k: Any) -> Any:  # a timestamp edge is the one allowed read
            if a[0] != "timestamp":
                raise AssertionError("a record was read while planning")
            return super().read_value(*a, **k)

    run.keyed_plan(Guard())


def test_a_nullable_column_is_released_with_its_known_values_and_joins_no_clique():
    text = contract("orders")
    schema = text["exposes"][0]["contract"]["schema"]
    schema[2] = {"name": "status", "type": "enum('paid','placed','returned','shipped')"}
    backend = FakeBackend()
    plan = release.plan_release(
        text,
        request("orders", backend),
        backend,
        unit="orders",
        rho=F(1),
        unit_key=("customer_id",),
        max_rows_per_unit=3,
    )
    assert [each.name for each in plan.columns] == ["status", "channel", "amount"]
    assert [each.nullable for each in plan.columns] == [True, False, False]
    assert [each.name for each in plan.eligible] == ["channel"]
    assert "levelHistogram/1 of orders.status" not in dict(plan.not_released)
    assert not plan.selects


def test_a_request_with_nothing_the_release_can_hold_is_refused():
    backend = FakeBackend()
    text = (data.FIXTURES / "orders.profile-request.yml").read_text(encoding="utf-8")
    text = "".join(
        line
        for line in text.splitlines(keepends=True)
        if '"levelHistogram/1"' not in line and '"marginalHistogram/1"' not in line
    )
    asked = backend.read_request(text, name="r.yml")
    assert (
        _event(
            lambda: release.plan_release(
                contract("orders"),
                asked,
                backend,
                unit="orders",
                rho=F(1),
                unit_key=("customer_id",),
            )
        )
        == "profile_release_nothing_to_release"
    )


def test_the_release_table_must_be_in_the_request():
    backend = FakeBackend()
    asked = request("shop", backend)
    asked = dataclasses.replace(
        asked, tables=tuple(each for each in asked.tables if each.expose_id != "orders")
    )
    assert (
        _event(
            lambda: release.plan_release(
                contract("shop"), asked, backend, unit="customers", release_table="orders", rho=F(1)
            )
        )
        == "profile_release_table_not_in_request"
    )


@pytest.mark.parametrize(
    "options, event",
    [
        (dict(unit="nowhere"), "unit_expose_unknown"),
        (dict(unit="orders", release_table="nowhere"), "unit_expose_unknown"),
        (dict(unit="orders", unit_key=("nope",)), "unit_key_column_unknown"),
        (dict(unit="orders", max_rows_per_unit=2), "unit_bound_in_rows_mode"),
        (
            dict(unit="orders", unit_key=("customer_id",), max_rows_per_unit=0),
            "unit_bound_out_of_range",
        ),
        (
            dict(
                unit="orders", unit_key=("customer_id",), max_rows_per_unit=2, max_rows_per_cell=3
            ),
            "unit_bound_out_of_range",
        ),
    ],
)
def test_a_unit_that_cannot_be_formed_is_refused_before_a_record_is_read(options, event):
    backend = FakeBackend()
    with pytest.raises(bounding.UnitRefused) as info:
        release.plan_release(
            contract("orders"), request("orders", backend), backend, rho=F(1), **options
        )
    assert info.value.event == event


def test_a_share_too_small_for_the_columns_is_refused_while_planning():
    assert _event(lambda: run.keyed_plan(rho="0.000000000002")) == "profile_release_rho_too_small"


def _wide(levels: int, count: int) -> Dict[str, Any]:
    text = contract("orders")
    schema = [
        text["exposes"][0]["contract"]["schema"][0],
        text["exposes"][0]["contract"]["schema"][1],
    ]
    for index in range(count):
        values = ",".join(f"'v{n}'" for n in range(levels))
        schema.append({"name": f"c{index}", "type": f"enum({values})", "required": True})
    text["exposes"][0]["contract"]["schema"] = schema
    text["exposes"][0]["semantics"] = {
        "entities": [{"name": "order", "type": "primary", "expr": "order_id"}]
    }
    return text


def _wide_request(count: int) -> Any:
    backend = FakeBackend()
    lines = [
        '"_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-0-0/FluxProfileRequest.json"',
        '"encoding":\n  "fractionalBits": 16\n  "rounding": "floor"',
        '"tables":\n  "orders":\n    "columns":',
    ]
    for index in range(count):
        lines.append(
            f'      "c{index}":\n        "domain": "category"\n        "statistics":\n'
            '          - "levelHistogram/1"'
        )
    lines.append('    "rowCount": false')
    return backend.read_request("\n".join(lines) + "\n", name="wide.yml")


def test_the_cells_of_the_columns_are_capped_before_a_record_is_read(monkeypatch):
    # status (4 levels), channel (2) and amount (10 bins) hold 16 cells at the least
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", 15)
    assert _event(run.keyed_plan) == "profile_release_too_many_cells"


def test_a_selection_over_more_than_the_columns_it_is_offered_is_refused_while_planning():
    backend = FakeBackend()
    count = selection.MOST_SELECTION_COLUMNS + 1
    assert (
        _event(
            lambda: release.plan_release(
                _wide(2, count), _wide_request(count), backend, unit="orders", rho=F(1)
            )
        )
        == "profile_release_too_many_columns"
    )


def test_the_cells_of_the_cliques_chosen_are_capped_before_a_table_is_allocated(monkeypatch):
    plan = run.keyed_plan()
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(_orders()), backend)

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a table was allocated")

    monkeypatch.setattr(bounding, "capped_counts", never)
    # the clique holds 8 cells and the histogram of amount 10: more than 17 in all
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", 17)
    run.keyed_plan()  # the 16 cells every structure holds at the least still fit
    refused = _event(lambda: release.draw(plan, encoded, SEED, backend, cliques=run.CLIQUES))
    assert refused == "profile_release_too_many_cells"


# ---- reading the records ---------------------------------------------------------------------


def _orders(count: int = 40) -> List[data.Order]:
    return data.orders()[:count]


def _refuse_rows(plan: release.ReleasePlan, rows: List[data.Order]) -> ReleaseRefused:
    with pytest.raises(ReleaseRefused) as info:
        release.encode_rows(plan, columns(rows), FakeBackend())
    return info.value


def test_a_missing_value_is_a_null_and_a_value_outside_the_levels_or_bins_is_refused():
    plan = run.keyed_plan()
    rows = _orders()
    base = columns(rows)

    def refusing(column: str, row: int, value: Any) -> ReleaseRefused:
        values = {name: list(each) for name, each in base.values.items()}
        values[column][row] = value
        with pytest.raises(ReleaseRefused) as info:
            release.encode_rows(plan, dataclasses.replace(base, values=values), FakeBackend())
        return info.value

    null = refusing("status", 3, None)
    assert null.event == "profile_release_null"
    assert "column 'status' row 3" in str(null)
    outside = refusing("channel", 4, "kiosk")
    assert outside.event == "profile_record_outside_declared_domain"
    assert "column 'channel' row 4" in str(outside)
    assert "kiosk" not in str(outside) and "kiosk" not in json.dumps(outside.context)
    low = refusing("amount", 5, "-0.01")
    high = refusing("amount", 6, "1000.01")
    assert low.event == high.event == "profile_record_outside_declared_domain"
    assert "column 'amount' row 5" in str(low) and "1000.01" not in str(high)
    for edge in ("0", "1000", "0.00", "1000.00"):
        values = {name: list(each) for name, each in base.values.items()}
        values["amount"][8] = edge
        release.encode_rows(plan, dataclasses.replace(base, values=values), FakeBackend())


def test_a_repeated_or_null_primary_key_refuses_a_release_of_each_row():
    plan = run.rows_plan()
    rows = _orders()
    again = list(rows)
    again[9] = (rows[2][0], *again[9][1:])
    refused = _refuse_rows(plan, again)
    assert refused.event == "profile_release_repeated_key"
    assert "order_id" in str(refused)
    assert str(rows[2][0]) not in str(refused).replace("order_id", "")
    base = columns(rows)
    values = {name: list(each) for name, each in base.values.items()}
    values["order_id"][4] = None
    with pytest.raises(ReleaseRefused) as info:
        release.encode_rows(plan, dataclasses.replace(base, values=values), FakeBackend())
    assert info.value.event == "profile_release_null_key"
    assert "row 4" in str(info.value)


def test_a_key_is_read_in_its_domain_so_one_entity_is_never_two_units():
    # 7, 7.0 and 7.00 are one customer: a number key is always read as one exact value
    plan = run.keyed_plan()
    rows = _orders(6)
    base = columns(rows)
    values = {name: list(each) for name, each in base.values.items()}
    values["customer_id"][:3] = ["7", "7.0", "7.00"]
    encoded = release.encode_rows(plan, dataclasses.replace(base, values=values), FakeBackend())
    assert len({unit for unit in encoded.units[:3]}) == 1
    assert encoded.units[0] is not None


def test_rows_with_no_key_value_are_one_unit_and_every_other_key_is_a_unit_of_its_own():
    plan = run.keyed_plan()
    rows = _orders(6)
    base = columns(rows)
    values = {name: list(each) for name, each in base.values.items()}
    values["customer_id"][1] = None
    values["customer_id"][4] = None
    encoded = release.encode_rows(plan, dataclasses.replace(base, values=values), FakeBackend())
    assert encoded.units[1] == encoded.units[4] == bounding.NULL_KEY
    assert encoded.units[0] != bounding.NULL_KEY and encoded.units[0] is not None
    related = release.encode_rows(
        run.related_plan(), dataclasses.replace(base, values=values), FakeBackend()
    )
    assert related.units[1] == related.units[4] == bounding.NULL_KEY


# ---- the release ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def keyed():
    plan = run.keyed_plan()
    return plan, draw(plan, data.orders())


@pytest.fixture(scope="module")
def chosen():
    """A release whose cliques were chosen by a selection, so its record states one."""
    plan = run.keyed_plan()
    return plan, draw(plan, data.orders(), cliques=None, select=run.choose, structure=run.keep)


def test_the_same_seed_gives_the_same_bytes_and_another_seed_other_noise(keyed):
    plan, first = keyed
    again = draw(plan, data.orders())
    assert again.canonical == first.canonical and again.digest == first.digest
    other = draw(plan, data.orders(), seed=bytes(range(2, 34)))
    assert other.canonical != first.canonical


def test_the_order_of_the_rows_changes_nothing(keyed):
    plan, first = keyed
    shuffled = data.orders()
    random.Random(1).shuffle(shuffled)
    assert draw(plan, shuffled).canonical == first.canonical


def _p3(drawn: release.Release, plan: release.ReleasePlan) -> None:
    from flux_stats.profile import load_profile_text

    text = drawn.canonical.decode("utf-8")
    load_profile_text(text)  # the registry's reader refuses what format 1-2-0 does not hold
    document = json.loads(text)
    privacy = document["privacy"]
    assert privacy["measure"] == "zeroConcentratedDP" and privacy["publicStructure"] is True
    assert privacy["privacyUnit"] == {"expose": plan.table}
    assert document["disclosureRules"] == {"noisy": {}}
    assert not {"relationships", "queries", "facts", "invariants", "columnPairs", "periods"} & set(
        document
    )
    total = Fraction(privacy["privacyLoss"]["rho"])
    parts: List[Fraction] = []
    for name, table in document["tables"].items():
        assert "rowCount" not in table and not {"columnPairs", "periods"} & set(table)
        if name != plan.table:
            assert set(table) == {"columns"}
            assert all(set(column) == {"domain"} for column in table["columns"].values())
            continue
        blocks = []
        for column in table["columns"].values():
            assert "metrics" not in column
            for statistic in column.get("statistics", []):
                assert statistic["definition"] in ("levelHistogram/1", "marginalHistogram/1")
                assert "noise" in statistic and statistic["disclosure"] == "noisy"
                assert statistic["sampleSize"] == {"min": 0}
                assert [group["status"] for group in statistic["groups"]] == ["F"]
                blocks.append(statistic["noise"])
        for clique in table.get("cliques", []):
            assert clique["definition"] == "cliqueMarginal/1"
            blocks.append(clique["noise"])
        assert blocks
        for noise in blocks:
            assert noise["mechanism"] == "discreteGaussian"
            share, scale = Fraction(noise["privacyLoss"]["rho"]), Fraction(noise["scale"])
            assert share >= Fraction(plan.sensitivity) / (2 * scale * scale)
            parts.append(share)
    assert parts and sum(parts) <= total


def test_the_release_meets_the_conditions_of_a_p3_profile(keyed):
    _p3(keyed[1], keyed[0])


def test_a_related_release_meets_them_and_lists_the_other_table_with_domains_only():
    plan = run.related_plan()
    drawn = draw(plan, data.orders())
    _p3(drawn, plan)
    document = drawn.document
    assert document["tables"]["customers"] == {
        "columns": {"customer_id": {"domain": "number"}, "region": {"domain": "category"}}
    }
    assert document["privacy"]["privacyUnit"] == {"expose": "orders"}


def test_a_release_of_each_row_meets_them():
    plan = run.rows_plan()
    _p3(draw(plan, data.orders()), plan)


def test_a_release_of_one_categorical_column_chooses_no_cliques_and_spends_all_on_the_tables():
    backend = FakeBackend()
    plan = release.plan_release(
        contract("shop"), request("shop", backend), backend, unit="customers", rho=F(1)
    )
    assert not plan.selects and [each.name for each in plan.columns] == ["region"]
    values = {
        "customer_id": [str(n) for n in range(1, 401)],
        "region": ["east", "north", "west", "east"] * 100,
    }
    read = run.sources.SourceColumns("c", 400, values)
    encoded = release.encode_rows(plan, read, backend)
    drawn = release.draw(plan, encoded, SEED, backend)
    _p3(drawn, plan)
    assert drawn.selected is None and drawn.selection_rho == 0 and drawn.cliques == ()
    (table,) = drawn.tables
    assert table.kind == release.LEVELS and drawn.share == 1
    assert "cliques" not in drawn.document["tables"]["customers"]


def test_the_cliques_and_one_way_tables_cover_the_columns(keyed):
    _, drawn = keyed
    assert [(table.kind, table.columns) for table in drawn.tables] == [
        ("clique", ("status", "channel")),
        ("bins", ("amount",)),
    ]
    assert drawn.cliques == (("status", "channel"),) and drawn.selected is None
    plan = run.keyed_plan()
    alone = draw(plan, data.orders(), cliques=())
    assert [(table.kind, table.columns) for table in alone.tables] == [
        ("levels", ("status",)),
        ("levels", ("channel",)),
        ("bins", ("amount",)),
    ]
    assert alone.share == F(3, 10)


def test_the_tables_agree_exactly_on_what_they_share(keyed):
    _, drawn = keyed
    statuses = drawn.document["tables"]["orders"]["cliques"][0]["noisyCounts"]
    amount = drawn.document["tables"]["orders"]["columns"]["amount"]["statistics"][0]["groups"][0]
    assert sum(map(Fraction, statuses)) == sum(map(Fraction, amount["noisyCounts"]))


def test_a_selection_is_given_the_bounded_rows_the_share_and_the_distance(monkeypatch):
    calls = run.stub_selection(monkeypatch)
    plan = run.keyed_plan(c=4, k=2)
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(data.orders()), backend)
    drawn = release.draw(plan, encoded, SEED, backend)
    ((rows, names, weights, rho, contributions),) = calls
    assert names == ("status", "channel") and rho == F(1, 10)
    assert contributions == bounding.selection_distance(plan.unit) == 4
    assert weights == {("status", "channel"): 2}  # channel is a categorical dimension
    assert set(rows[0]) <= {"paid", "placed", "returned", "shipped", "online", "store"}
    assert drawn.selected == (("status", "channel"),) and drawn.selection_rho == F(1, 10)
    # at most four rows of an entity reach the selection
    assert len(rows) <= 4 * len(set(unit for unit in encoded.units if unit is not None)) + sum(
        unit is None for unit in encoded.units
    )
    assert len(rows) < len(data.orders())


def test_a_selection_that_fails_names_the_class_and_no_message(monkeypatch):
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("a secret value 4242")

    plan = run.keyed_plan()
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(_orders()), backend)
    with pytest.raises(ReleaseRefused) as info:
        release.draw(plan, encoded, SEED, backend, cliques=None, select=boom)
    assert info.value.event == "profile_release_selection_failed"
    assert "RuntimeError" in str(info.value) and "4242" not in str(info.value)


def test_missing_packages_refuse_before_a_record_is_read(monkeypatch):
    import importlib.util

    plan = run.keyed_plan()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert (
        _event(lambda: release.check_selection_available(plan))
        == "profile_release_selection_unavailable"
    )


def test_a_selection_put_in_its_place_needs_no_extra(monkeypatch):
    import importlib.util

    run.stub_selection(monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    release.check_selection_available(run.keyed_plan())


def test_a_disagreement_after_the_decimal_text_is_a_defect(monkeypatch, keyed):
    plan, _ = keyed
    real = consistency.to_decimal_text

    def broken(tables: Any, values: Any, forest: Any, places: int = 12) -> Any:
        texts = [list(each) for each in real(tables, values, forest, places)]
        texts[0][0] = str(Fraction(texts[0][0]) + 1)
        return tuple(tuple(each) for each in texts)

    monkeypatch.setattr(consistency, "to_decimal_text", broken)
    assert _event(lambda: draw(plan, data.orders())) == "profile_release_inconsistent"


def test_a_set_of_tables_with_no_junction_forest_is_refused_before_any_agreement(
    monkeypatch, keyed
):
    plan, _ = keyed
    called = []
    monkeypatch.setattr(
        consistency,
        "make_consistent",
        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError),
    )

    def no_forest(tables: Any) -> Any:
        raise ValueError("no forest")

    monkeypatch.setattr(consistency, "junction_forest", no_forest)
    assert _event(lambda: draw(plan, data.orders())) == "profile_release_not_a_forest"
    assert not called


def test_a_structure_too_large_refuses_after_the_selection_and_names_what_was_spent():
    plan = run.keyed_plan()
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(_orders()), backend)

    def too_big(*a: Any, **k: Any) -> Any:
        raise ValueError("the maximal cliques hold 99999 cells, more than the 65,536 allowed")

    with pytest.raises(ReleaseRefused) as info:
        release.draw(
            plan, encoded, SEED, backend, cliques=None, select=run.choose, structure=too_big
        )
    refused = info.value
    assert refused.event == "profile_release_structure_too_large"
    message = " ".join(str(refused).split())
    assert "more cells than a release may measure" in message
    assert (
        "spent the selection share of this release's budget, rho 0.1, on these records" in message
    )
    assert "nothing was written" in message and "99999" not in message
    assert refused.context["written"] == "nothing"
    assert "list fewer categorical columns" in refused.context["next"]


def test_the_real_structure_refuses_cliques_that_hold_too_many_cells(monkeypatch):
    need("mbi")
    real = selection.release_structure

    def small(names: Any, sizes: Any, cliques: Any, **_: Any) -> Any:
        return real(names, sizes, cliques, max_cells=7)  # status x channel holds 8

    plan = run.keyed_plan()
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(_orders()), backend)
    with pytest.raises(ReleaseRefused) as info:
        release.draw(plan, encoded, SEED, backend, cliques=None, select=run.choose, structure=small)
    assert info.value.event == "profile_release_structure_too_large"


# ---- the guarantee: removing an entity ---------------------------------------------------------


def _counts(plan: release.ReleasePlan, rows: List[data.Order], names: Any) -> List[int]:
    """The unbounded counts of one table: every row counted once, in the table's cells."""
    encoded = release.encode_rows(plan, columns(rows), FakeBackend())
    sizes = [plan.column(name).size for name in names]
    counts = [0] * _prod(sizes)
    for index in range(len(rows)):
        cell = 0
        for name, size in zip(names, sizes, strict=True):
            cell = cell * size + encoded.cells[name][index]
        counts[cell] += 1
    return counts


def _prod(values: List[int]) -> int:
    out = 1
    for value in values:
        out *= value
    return out


def _check_difference(
    plan: release.ReleasePlan, with_it: release.Release, without: release.Release
) -> None:
    spent = Fraction(0)
    weighted = {"noisy": Fraction(0), "consistent": Fraction(0)}
    moved = False
    for position, table in enumerate(with_it.tables):
        variance = Fraction(table.scale_units**2, 10**12)
        spent += Fraction(table.share)
        for name, one, two in (
            ("noisy", with_it.noisy[position], without.noisy[position]),
            ("consistent", with_it.consistent[position], without.consistent[position]),
        ):
            squared = sum(((x - y) ** 2 for x, y in zip(one, two, strict=True)), Fraction(0))
            if name == "noisy":
                assert squared <= plan.sensitivity
                moved = moved or squared > 0
            weighted[name] += squared / (2 * variance)
    assert moved
    assert weighted["noisy"] <= spent
    assert weighted["consistent"] <= weighted["noisy"] <= spent


@pytest.mark.parametrize(
    "plan_of, c",
    [
        (lambda: run.keyed_plan(c=3), 3),
        (lambda: run.keyed_plan(c=4, k=2), 4),
        (lambda: run.keyed_plan(c=1), 1),
        (lambda: run.related_plan(c=2), 2),
        (lambda: run.related_plan(c=5, k=3), 5),
    ],
    ids=["keyed-3", "keyed-4-2", "keyed-1", "related-2", "related-5-3"],
)
def test_removing_the_entity_with_the_most_rows_moves_the_release_by_no_more_than_one_entity(
    plan_of, c
):
    plan = plan_of()
    rows = data.orders()
    without = data.without_customer(rows, data.HEAVY_CUSTOMER)
    assert len(rows) - len(without) == data.HEAVY_ORDERS
    one = draw(plan, rows)
    two = draw(plan, without)
    _check_difference(plan, one, two)
    # the same removal without the bounding moves the raw counts by more than the bound allows
    names = ("status", "channel")
    raw_with, raw_without = _counts(plan, rows, names), _counts(plan, without, names)
    assert sum((x - y) ** 2 for x, y in zip(raw_with, raw_without, strict=True)) > plan.sensitivity
    assert plan.unit.max_rows_per_unit == c


def test_removing_one_row_moves_a_release_of_each_row_by_one_cell():
    plan = run.rows_plan()
    rows = data.orders()
    for dropped in (0, 17, len(rows) - 1):
        _check_difference(plan, draw(plan, rows), draw(plan, rows[:dropped] + rows[dropped + 1 :]))


def test_an_entity_with_many_rows_keeps_the_same_rows_whoever_else_is_in_the_data():
    plan = run.keyed_plan(c=3)
    backend = FakeBackend()
    rows = data.orders()
    full = release.encode_rows(plan, columns(rows), backend)
    kept = set(bounding.bound_rows(full.units, full.contents, SEED, 3))
    rest = data.without_customer(rows, 5)
    partial = release.encode_rows(plan, columns(rest), backend)
    kept_rest = set(bounding.bound_rows(partial.units, partial.contents, SEED, 3))
    heavy = {i for i, row in enumerate(rows) if row[1] == data.HEAVY_CUSTOMER}
    heavy_rest = {i for i, row in enumerate(rest) if row[1] == data.HEAVY_CUSTOMER}
    assert len(kept & heavy) == len(kept_rest & heavy_rest) == 3
    assert [rows[i] for i in sorted(kept & heavy)] == [
        rest[i] for i in sorted(kept_rest & heavy_rest)
    ]


# ---- the guarantee in closed form ---------------------------------------------------------------
#
# Every number asserted here is written in the test: none is computed by ``sensitivity_squared``.
# The orders fixture releases two tables, the clique status x channel (8 cells) and a histogram of
# amount; a row is ``(order_id, customer_id, status, channel, amount)``.

_FIRST, _SECOND, _THIRD = ("paid", "online"), ("placed", "online"), ("returned", "store")
_SEEDS = [bytes([n]) * 32 for n in range(1, 61)]
_ENTITY = 1000  # a customer the background orders (customers 1 to 400) never hold


def _row(order_id: int, customer: int, cell: Any = _FIRST, amount: str = "10.00") -> data.Order:
    return (order_id, customer, cell[0], cell[1], amount)


def _background() -> List[data.Order]:
    return data.orders()[:60]


def _run(plan, rows, *, seed=SEED, null_rows=()):
    """The release drawn, and each table's exact counts before noise, as ``draw`` counts them."""
    base = columns(rows)
    values = {name: list(each) for name, each in base.values.items()}
    for index in null_rows:
        values["customer_id"][index] = None
    backend = FakeBackend()
    encoded = release.encode_rows(plan, dataclasses.replace(base, values=values), backend)
    drawn = release.draw(plan, encoded, seed, backend, cliques=run.CLIQUES)
    unit = plan.unit
    kept = bounding.bound_rows(encoded.units, encoded.contents, seed, unit.max_rows_per_unit)
    kept_units = [encoded.units[i] for i in kept]
    exact: List[List[int]] = []
    for table in drawn.tables:
        cell = [0] * len(kept)
        for name, size in zip(table.columns, table.sizes, strict=True):
            cell = [v * size + encoded.cells[name][i] for v, i in zip(cell, kept, strict=True)]
        exact.append(bounding.capped_counts(kept_units, cell, table.cells, unit.max_rows_per_cell))
    return drawn, exact


def _moves(one: List[List[int]], two: List[List[int]]) -> List[int]:
    """The squared L2 distance between two sets of tables, one number per table."""
    return [
        sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) for a, b in zip(one, two, strict=True)
    ]


def test_the_exact_counts_are_the_noisy_counts_less_the_noise():
    plan = run.keyed_plan(c=2)
    rows = _background() + [_row(9001, _ENTITY), _row(9002, _ENTITY, _SECOND)]
    one, exact_one = _run(plan, rows)
    two, exact_two = _run(plan, rows[:-1])
    # one seed gives one noise stream per table, so the noisy tables differ as the exact ones do
    for position in range(len(one.tables)):
        shift = [x - y for x, y in zip(one.noisy[position], two.noisy[position], strict=True)]
        assert shift == [
            Fraction(x - y) for x, y in zip(exact_one[position], exact_two[position], strict=True)
        ]


@pytest.mark.parametrize(
    "plan_of",
    [lambda: run.keyed_plan(c=1), lambda: run.related_plan(c=1)],
    ids=["keyed", "related"],
)
def test_one_more_row_for_an_entity_with_one_kept_row_moves_the_counts_by_two_and_no_more(plan_of):
    plan = plan_of()
    rows = _background() + [_row(9001, _ENTITY, _FIRST)]
    more = rows + [_row(9002, _ENTITY, _SECOND)]  # the same amount: the histogram never moves
    seen = set()
    for seed in _SEEDS:
        _, before = _run(plan, rows, seed=seed)
        _, after = _run(plan, more, seed=seed)
        clique, histogram = _moves(before, after)
        assert clique in (0, 2) and histogram == 0
        seen.add(clique)
    assert seen == {0, 2}  # the new row is dropped, or it takes the place of the kept one


@pytest.mark.parametrize(
    "plan_of, cells, clique, histogram",
    [
        # c rows in one cell: removing the entity moves a table by c * c
        (lambda: run.keyed_plan(c=3), [_FIRST] * 5, 9, 9),
        (lambda: run.related_plan(c=3), [_FIRST] * 5, 9, 9),
        (lambda: run.keyed_plan(c=2), [_THIRD] * 2, 4, 4),
        # k < c: an entity with c rows in one cell moves a table by k * k
        (lambda: run.keyed_plan(c=4, k=2), [_FIRST] * 4, 4, 4),
        (lambda: run.related_plan(c=6, k=3), [_SECOND] * 6, 9, 9),
        # q cells full and one holding the remainder: q * k * k + r * r = 2 * 4 + 1
        (
            lambda: run.keyed_plan(c=5, k=2),
            [_FIRST, _FIRST, _SECOND, _SECOND, _THIRD],
            9,
            4,  # the histogram of amount sees one bin: k * k
        ),
        # 3 full cells of 3 and a remainder of 2: 3 * 9 + 4
        (
            lambda: run.related_plan(c=11, k=3),
            [_FIRST] * 3 + [_SECOND] * 3 + [_THIRD] * 3 + [("shipped", "store")] * 2,
            31,
            9,
        ),
    ],
    ids=[
        "keyed-3",
        "related-3",
        "keyed-2",
        "keyed-4-2",
        "related-6-3",
        "keyed-5-2",
        "related-11-3",
    ],
)
def test_removing_an_entity_moves_a_table_by_the_closed_form_number(
    plan_of, cells, clique, histogram
):
    plan = plan_of()
    background = _background()
    entity = [_row(9000 + i, _ENTITY, cell) for i, cell in enumerate(cells)]
    for seed in _SEEDS[:5]:
        _, with_it = _run(plan, background + entity, seed=seed)
        _, without = _run(plan, background, seed=seed)
        assert _moves(with_it, without) == [clique, histogram]


def _pool(size: int) -> List[data.Order]:
    cells = [_FIRST, _SECOND, _THIRD, ("shipped", "store"), ("paid", "store"), ("placed", "store")]
    return [_row(8000 + i, 0, cells[i % len(cells)], f"{(i * 37) % 1000}.00") for i in range(size)]


@pytest.mark.parametrize("plan_of", [run.keyed_plan, run.related_plan], ids=["keyed", "related"])
def test_fifty_rows_with_no_key_value_move_the_counts_by_no_more_than_one_entity(plan_of):
    plan = plan_of(c=1)
    background = _background()
    pool = _pool(50)
    everyone = background + pool
    nulls = set(range(len(background), len(everyone)))
    for seed in _SEEDS[:20]:
        _, base = _run(plan, everyone, seed=seed, null_rows=nulls)
        # one more row with no key value
        extra = everyone + [_row(8999, 0, _THIRD, "999.00")]
        _, added = _run(plan, extra, seed=seed, null_rows=nulls | {len(everyone)})
        assert max(_moves(base, added)) <= 2
        # the whole pool gone: only its one kept row leaves each table
        _, gone = _run(plan, background, seed=seed)
        assert _moves(base, gone) == [1, 1]
    # one row of the pool gone, each in turn
    for index in sorted(nulls)[::7]:
        rest = everyone[:index] + everyone[index + 1 :]
        shifted = {n if n < index else n - 1 for n in nulls if n != index}
        _, base = _run(plan, everyone, null_rows=nulls)
        _, removed = _run(plan, rest, null_rows=shifted)
        assert max(_moves(base, removed)) <= 2


def test_rows_with_no_key_value_are_kept_to_c_and_k_like_any_unit():
    plan = run.keyed_plan(c=3, k=2)
    background = _background()
    pool = _pool(50)
    everyone = background + pool
    nulls = set(range(len(background), len(everyone)))
    _, base = _run(plan, everyone, null_rows=nulls)
    _, gone = _run(plan, background)
    # three kept rows, at most two to a cell: no more than 2 * 2 + 1 * 1 in a table
    assert max(_moves(base, gone)) <= 5
    assert sum(sum(table) for table in base) - sum(sum(table) for table in gone) == 3 + 3


def test_a_swap_in_a_unit_of_one_row_reaches_the_floor_of_two_and_the_share_covers_it():
    plan = run.keyed_plan(c=1)
    assert plan.sensitivity == 2  # not 1: c = 1 leaves a swap, which one row can cause
    rows = _background() + [_row(9001, _ENTITY, _FIRST)]
    more = rows + [_row(9002, _ENTITY, _SECOND)]
    reached = 0
    for seed in _SEEDS:
        one, before = _run(plan, rows, seed=seed)
        two, after = _run(plan, more, seed=seed)
        squared = _moves(before, after)
        for table, move in zip(one.tables, squared, strict=True):
            # the loss of this move at this table's scale is within the table's stated share
            weighted = Fraction(move) / (2 * Fraction(table.scale_units**2, 10**12))
            assert weighted <= Fraction(table.share)
        assert max(squared) <= 2
        reached = max(reached, max(squared))
        if max(squared) == 2:
            # a scale sized for a change of 1 would not cover the swap: at the least scale that
            # spends no more than a table's share on 1, a move of 2 costs more than the share
            for table in one.tables:
                share = Fraction(table.share)
                smaller = Fraction(release.scale_units(share, 1), 10**6)
                assert smaller < Fraction(table.scale_units, 10**6)
                assert Fraction(2) / (2 * smaller**2) > share
    assert reached == 2


# ---- the record ---------------------------------------------------------------------------------


def _record(plan: release.ReleasePlan, drawn: release.Release, *, selected: bool = False):
    request_bytes = (data.FIXTURES / "orders.profile-request.yml").read_bytes()
    return release.build_record(
        plan, drawn, SEED, request_bytes=request_bytes, contract_digest="sha256:" + "ab" * 32
    )


def test_a_record_holds_what_is_needed_to_draw_the_release_again(keyed):
    plan, drawn = keyed
    record = _record(plan, drawn)
    assert record.seed == SEED and record.profile_digest == drawn.digest
    assert dict(record.unit) == {
        "expose": "orders",
        "releaseTable": "orders",
        "mode": "keyed",
        "keyColumns": ["customer_id"],
        "relationship": None,
        "c": 3,
        "k": 3,
    }
    assert dict(record.privacy_loss) == {"rho": "1"}
    assert [dict(each) for each in record.tables] == [
        {
            "label": "noise/status/channel",
            "columns": ["status", "channel"],
            "scale": drawn.tables[0].scale,
            "share": "0.45",
        },
        {
            "label": "noise/amount",
            "columns": ["amount"],
            "scale": drawn.tables[0].scale,
            "share": "0.45",
        },
    ]
    assert record.versions["forge-cli"] and record.versions["opendp"] is None
    assert SEED.hex() not in drawn.canonical.decode("utf-8")
    assert SEED.hex() not in repr(record) and SEED.hex() not in repr(drawn)


def test_a_record_of_a_selection_states_the_share_and_the_cliques_chosen(chosen):
    plan, drawn = chosen
    record = _record(plan, drawn)
    assert dict(record.selection or {}) == {"rho": "0.1", "cliques": [["status", "channel"]]}
    assert release.check_record(plan, record) == (("status", "channel"),)


def test_drawing_again_from_the_record_gives_the_same_bytes(chosen):
    plan, drawn = chosen
    record = _record(plan, drawn)
    plan_of_record = run.keyed_plan()
    assert release.check_record(plan_of_record, record) == (("status", "channel"),)
    again = draw(
        plan_of_record, data.orders(), cliques=release.check_record(plan_of_record, record)
    )
    assert again.canonical == drawn.canonical


def test_a_record_that_states_other_than_what_the_plan_computes_is_refused(chosen):
    plan, drawn = chosen
    record = _record(plan, drawn)
    cheaper = dict(record.tables[0], scale="1.5")
    bigger = dict(record.tables[0], share="0.5")
    relabelled = dict(record.tables[0], label="noise/status")
    tampered = [
        dataclasses.replace(record, tables=(cheaper, *record.tables[1:])),
        dataclasses.replace(record, tables=(bigger, *record.tables[1:])),
        dataclasses.replace(record, tables=(relabelled, *record.tables[1:])),
        dataclasses.replace(record, tables=record.tables[:1]),
        dataclasses.replace(
            record, tables=(*record.tables, dict(record.tables[1], label="noise/x"))
        ),
        dataclasses.replace(record, tables=({"label": 5, "columns": 5}, *record.tables[1:])),
        dataclasses.replace(record, unit={**record.unit, "c": 2}),
        dataclasses.replace(record, unit={**record.unit, "keyColumns": ["order_id"]}),
        dataclasses.replace(record, privacy_loss={"rho": "2"}),
        dataclasses.replace(record, selection={"rho": "0.2", "cliques": [["status", "channel"]]}),
        dataclasses.replace(record, selection=None),
    ]
    for each in tampered:
        assert _event(lambda: release.check_record(plan, each)) == "profile_release_record_mismatch"
    assert release.check_record(plan, record) == (("status", "channel"),)


def test_a_clique_the_columns_do_not_allow_is_refused_in_a_record(chosen):
    plan, drawn = chosen
    record = _record(plan, drawn)
    for columns_ in (
        ["channel", "status"],
        ["status", "status"],
        ["status", "amount"],
        ["status", "nope"],
    ):
        bad = dataclasses.replace(
            record, tables=(dict(record.tables[0], columns=columns_), *record.tables[1:])
        )
        assert _event(lambda: release.check_record(plan, bad)) == "profile_release_record_mismatch"


def test_the_document_is_checked_by_the_registry_before_anything_is_written(keyed):
    plan, _ = keyed

    class Checked(FakeBackend):
        seen: List[str] = []

        def check_document(self, text: str) -> None:
            self.seen.append(text)
            super().check_document(text)

    backend = Checked()
    drawn = draw(plan, data.orders(), backend=backend)
    assert backend.seen == [drawn.canonical.decode("utf-8")]


def test_the_schema_url_is_the_one_flux_stats_reads():
    from flux_stats import profile

    assert release.SCHEMA_URL_1_2_0 == profile.SCHEMA_URL_1_2_0
    assert release.MECHANISM == "discreteGaussian" and release.RULE_NAME == "noisy"


def test_nothing_in_the_release_or_its_refusals_holds_a_count_of_the_records(keyed):
    plan, drawn = keyed
    rows = data.orders()
    units = len({row[1] for row in rows})
    text = json.dumps(drawn.document) + repr(drawn) + repr(plan.not_released)
    for secret in (len(rows), len(rows) - units):
        assert f'"{secret}"' not in text


# ---- what the null-key pool does not cover at the stated loss -------------------------------
#
# The pool is one unit, so the stated loss holds for it as a whole and for each of its rows. A
# real entity that holds only part of the pool, or rows both with and without a key value, can
# move a table further than one unit: removing it changes the pool's rows (two unit changes) and
# perhaps a keyed unit (one more). By group privacy for zero-concentrated DP its squared move is
# then at most 4 or 9 times one unit's, which is what the help and the report state.

_POOL_SEED = bytes(range(1, 33))


def _pool_priority(content: bytes) -> bytes:
    from fluid_build.profiler.dp.randomness import priority

    key = bounding.NULL_KEY
    return priority(_POOL_SEED, "bound", len(key).to_bytes(8, "big") + key + content)


def _pool_move(rows: List[Any], without: List[Any], c: int, k: int, size: int) -> int:
    def counts(each: List[Any]) -> List[int]:
        units = [unit for unit, _, _ in each]
        kept = bounding.bound_rows(units, [content for _, content, _ in each], _POOL_SEED, c)
        return bounding.capped_counts([units[i] for i in kept], [each[i][2] for i in kept], size, k)

    return sum((a - b) ** 2 for a, b in zip(counts(rows), counts(without), strict=True))


def test_an_entity_that_holds_part_of_the_pool_moves_a_table_past_one_unit_and_within_four():
    candidates = sorted((bounding.encode_values([f"r{i}"]) for i in range(400)), key=_pool_priority)
    cells = {content: position % 2 for position, content in enumerate(candidates)}
    first = [content for content in candidates if cells[content] == 0][:3]
    last = [content for content in candidates if cells[content] == 1][-3:]
    pool = bounding.NULL_KEY
    rows = [(pool, content, cells[content]) for content in first + last]
    plan = run.keyed_plan(c=3, k=3)
    bound = bounding.sensitivity_squared(plan.unit)
    moved = _pool_move(rows, rows[3:], 3, 3, 2)  # remove the entity of the first three rows
    assert bound == 9 and moved == 18  # past one unit: the help says so
    assert moved <= 4 * bound


def test_an_entity_with_rows_in_and_out_of_the_pool_moves_a_table_within_nine_units():
    candidates = sorted((bounding.encode_values([f"r{i}"]) for i in range(50)), key=_pool_priority)
    mine, other = candidates[0], candidates[1]
    keyed = bounding.encode_values([42])
    pool = bounding.NULL_KEY
    rows = [(keyed, b"k", 0), (pool, mine, 0), (pool, other, 1)]
    plan = run.keyed_plan(c=1)
    bound = bounding.sensitivity_squared(plan.unit)
    moved = _pool_move(rows, rows[2:], 1, 1, 2)  # remove the entity of the keyed and pooled row
    assert bound == 2 and moved == 5  # past one unit
    assert moved <= 9 * bound


def test_too_many_cells_after_the_selection_names_the_share_it_spent(monkeypatch):
    plan = run.keyed_plan()
    backend = FakeBackend()
    encoded = release.encode_rows(plan, columns(_orders()), backend)
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", 9)  # status x channel holds 8
    with pytest.raises(ReleaseRefused) as info:
        release.draw(
            plan,
            encoded,
            SEED,
            backend,
            cliques=None,
            select=lambda *a, **k: (("status", "channel"),),
            structure=lambda names, sizes, cliques, **_: cliques,
        )
    refused = info.value
    message = " ".join(str(refused).split())
    assert refused.event == "profile_release_too_many_cells"
    assert "spent the selection share of this release's budget, rho 0.1" in message
    assert refused.context["written"] == "nothing"
