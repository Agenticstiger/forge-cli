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

"""``fluid diff`` on what a streaming build reads: a reader schema, and its mapping."""

from __future__ import annotations

import copy

import pytest

from fluid_build.api.changelog import compare_contracts
from fluid_build.api.changelog_stream_rules import diff_streams
from fluid_build.streaming.compiler import validation_errors


@pytest.fixture
def baseline(contract):
    """The example, reading two fields no output uses, so either can change."""
    stream = contract["builds"][0]["properties"]["stream"]
    stream["schema"] += [
        {"name": "note", "type": "string"},
        {"name": "quantity", "type": "int"},
    ]
    return contract


def _stream(contract):
    return contract["builds"][0]["properties"]["stream"]


_NEW: list = []


def _registry(stream, url):
    """The contract being mutated also names the registry its new format needs."""
    _NEW[-1]["builds"][0]["properties"]["parameters"]["flink"]["source"]["schemaRegistryUrl"] = url


def _kinds(baseline, new):
    assert validation_errors(new) == []
    report = compare_contracts(baseline, new)
    found = {(c.kind, c.severity) for c in report.breaking + report.non_breaking + report.info}
    return {k for k in found if k[0].startswith("stream_")}


def test_an_unchanged_stream_reports_nothing(baseline):
    assert diff_streams(baseline, copy.deepcopy(baseline)) == []


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda s: s.update(topic="orders-v2"), ("stream_topic_changed", "breaking")),
        (
            lambda s: s.update(format="avro-confluent") or _registry(s, "https://registry.example"),
            ("stream_format_changed", "breaking"),
        ),
        # The job stops reading a field: an output still reading it would fail validate.
        (lambda s: s["schema"].pop(), ("stream_field_removed", "non_breaking")),
        (
            lambda s: s["schema"].append({"name": "channel", "type": "string"}),
            ("stream_field_added", "non_breaking"),
        ),
        (
            lambda s: s["schema"].append({"name": "channel", "type": "string", "required": True}),
            ("stream_field_added", "breaking"),
        ),
        (
            lambda s: s["schema"][-1].update(type="long"),
            ("stream_column_type_widened", "non_breaking"),
        ),
        (lambda s: s["schema"][-2].update(type="int"), ("stream_column_type_changed", "breaking")),
        (
            lambda s: s["schema"][-2].update(required=True),
            ("stream_nullable_tightened", "breaking"),
        ),
        (
            lambda s: s["schema"][1]["fields"].append({"name": "tier", "type": "string"}),
            ("stream_field_added", "non_breaking"),
        ),
        (
            lambda s: s.update(metadata=[{"name": "kafka_offset", "from": "offset"}]),
            None,
        ),
    ],
)
def test_reader_schema_rules(baseline, mutate, expected):
    new = copy.deepcopy(baseline)
    _NEW.append(new)
    mutate(_stream(new))
    kinds = _kinds(baseline, new)
    assert kinds == ({expected} if expected else set()), kinds


def test_a_nested_change_names_its_path(baseline):
    new = copy.deepcopy(baseline)
    _stream(new)["schema"][1]["fields"].append({"name": "tier", "type": "string"})
    (change,) = diff_streams(baseline, new)
    assert change.path == "builds[0].properties.stream.schema.customer.fields.tier"
    assert "the job now reads 'customer.fields.tier'" in change.description


def test_key_metadata_explode_and_sources(baseline):
    _stream(baseline)["key"] = {"format": "raw", "schema": [{"name": "k", "type": "string"}]}
    _stream(baseline)["metadata"] = [{"name": "kafka_meta", "from": "offset"}]
    new = copy.deepcopy(baseline)
    _stream(new)["key"]["format"] = "json"
    _stream(new)["metadata"][0]["from"] = "partition"
    assert _kinds(baseline, new) == {
        ("stream_key_format_changed", "breaking"),
        ("stream_metadata_source_changed", "breaking"),
    }

    rows = copy.deepcopy(baseline)
    rows["builds"][0]["properties"]["mapping"] = {
        "explode": {"path": "items", "as": "item"},
        "columns": [{"name": "sku", "from": "item.sku"}, {"name": "label", "from": "note"}],
    }
    rows["exposes"][0]["contract"]["schema"] = [
        {"name": "event_id", "type": "string", "required": True},
        {"name": "sku", "type": "string"},
        {"name": "label", "type": "string"},
    ]
    assert ("stream_explode_changed", "breaking") in _kinds(baseline, rows)

    moved = copy.deepcopy(rows)
    moved["builds"][0]["properties"]["mapping"]["columns"][1]["from"] = "customer.id"
    assert _kinds(rows, moved) == {("stream_column_source_changed", "non_breaking")}


def test_builds_without_a_stream_are_left_alone(baseline):
    other = copy.deepcopy(baseline)
    other["builds"][0]["id"] = "renamed"
    assert diff_streams(baseline, other) == []
