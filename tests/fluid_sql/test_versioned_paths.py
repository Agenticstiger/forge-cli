# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A path through a relationship into a versioned table (RFC-versioned-relationships section 2).

``fs.check`` on hand-built models: a link reaches an entity when its ``toColumns`` are the
target's natural key; as of a date it reads every column, by entity the key and the stable
columns, and every other path is refused with reason ``validity-window`` and a message that
names both remedies.
"""

from __future__ import annotations

from typing import Optional, Tuple

import pytest

from fluid_build import fluid_sql as fs

pytestmark = pytest.mark.unit


def _model(name: str, columns: dict, **more) -> fs.Model:
    return fs.Model(
        name, {c: fs.Column(c, t, fs.column_type(t)) for c, t in columns.items()}, **more
    )


def _station_versions(keyed: bool = True) -> fs.Model:
    return _model(
        "station_version",
        {"station_id": "string", "network_id": "string", "sensor_model": "string"},
        validity_window=True,
        natural_key=("station_id",) if keyed else (),
        stable_columns=frozenset({"network_id"}),
    )


def _readings(
    to_columns: Tuple[str, ...] = ("station_id",), as_of: Optional[str] = None, keyed: bool = True
) -> fs.Model:
    reading = _model(
        "reading", {"station_id": "string", "reading_date": "date", "network_id": "string"}
    )
    reading.links["station"] = fs.Link(
        "station",
        _station_versions(keyed),
        target_name="station_version",
        to_columns=to_columns,
        as_of=as_of,
    )
    return reading


def _check(model: fs.Model, text: str) -> fs.Checked:
    return fs.check(fs.parse(text), model, "predicate")


def _refused(model: fs.Model, text: str) -> fs.FluidSqlError:
    with pytest.raises(fs.FluidSqlError) as caught:
        _check(model, text)
    return caught.value


def test_reach_names_the_read():
    assert _readings().links["station"].reach() == fs.BY_ENTITY
    assert _readings(as_of="reading_date").links["station"].reach() == fs.AS_OF
    assert _readings(to_columns=("network_id",)).links["station"].reach() is None
    assert _readings(keyed=False).links["station"].reach() is None


def test_as_of_reads_every_column():
    reading = _readings(as_of="reading_date")
    for text in (
        "station.sensor_model = 'A1'",
        "station.network_id = network_id",
        "station.station_id IS NOT NULL",
    ):
        assert _check(reading, text).type.kind == "bool"


def test_by_entity_reads_the_key_and_the_stable_columns():
    reading = _readings()
    assert _check(reading, "station.station_id IS NOT NULL").type.kind == "bool"
    assert _check(reading, "network_id = station.network_id").type.kind == "bool"


def test_by_entity_refuses_an_unstable_column():
    error = _refused(_readings(), "station.sensor_model = 'A1'")
    assert error.reason == "validity-window"
    assert "not stable per entity" in str(error)
    assert fs.VERSIONED_REMEDY in str(error)


def test_a_link_not_on_the_natural_key_reaches_no_entity():
    for as_of in (None, "reading_date"):
        error = _refused(
            _readings(to_columns=("network_id",), as_of=as_of), "station.sensor_model = 'A1'"
        )
        assert error.reason == "validity-window"
        assert "reaches no entity" in str(error)
        assert fs.VERSIONED_REMEDY in str(error)


def test_a_target_without_a_natural_key_reaches_no_entity():
    error = _refused(_readings(as_of="reading_date", keyed=False), "station.sensor_model = 'A1'")
    assert error.reason == "validity-window"
    assert "natural entity" in str(error)
    assert "fluid validate" not in str(error)
    assert "not well formed" in str(error) and "contract.schema" in str(error)
    assert fs.VERSIONED_REMEDY in str(error)


@pytest.mark.parametrize("as_of", [None, "reading_date"])
def test_an_unknown_column_through_a_link_that_reaches_an_entity(as_of):
    error = _refused(_readings(as_of=as_of), "station.nope = 1")
    assert error.reason == "unknown-column"


def test_an_unknown_column_through_a_link_that_reaches_no_entity_is_a_window_refusal():
    error = _refused(_readings(to_columns=("network_id",)), "station.nope = 1")
    assert error.reason == "validity-window"


def test_versioned_refusal_is_none_where_the_read_is_allowed():
    link = _readings(as_of="reading_date").links["station"]
    assert fs.versioned_refusal(link, "sensor_model") is None
    by_entity = _readings().links["station"]
    assert fs.versioned_refusal(by_entity, "sensor_model") is not None
    assert fs.versioned_refusal(by_entity, "network_id") is None


@pytest.mark.parametrize(
    "open_end,closed,expected",
    [
        (None, False, '("e" IS NULL OR "s" < "e")'),
        (None, True, '("e" IS NULL OR "s" <= "e")'),
        ("DATE '9999-12-31'", False, '("e" = DATE \'9999-12-31\' OR "s" < "e")'),
        ("DATE '9999-12-31'", True, '("e" = DATE \'9999-12-31\' OR "s" <= "e")'),
    ],
)
def test_as_of_nonempty_keeps_open_and_non_empty_versions(open_end, closed, expected):
    assert fs.as_of_nonempty('"s"', '"e"', open_end, closed) == expected
