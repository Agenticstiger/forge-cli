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

"""Overlays patch builds, exposes and consumes by identity from fluidVersion 0.7.6.

Merging a list of maps by position retargets every overlay as soon as the base
gains, loses or reorders an entry: a ``backfill`` build inserted first made
``overlays/prod.yaml``'s patch for ``transform`` land on ``ingest``, silently.
From 0.7.6 the three top-level lists merge by their identity key; older
contracts keep the positional merge, and warn when a patch lands on an entry
that names another id.
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.loader import (
    _NOTED_OVERLAY_MERGE_WARNINGS,
    OverlayMergeError,
    _deep_merge,
)


@pytest.fixture(autouse=True)
def _fresh_warnings():
    # Each merge warning is logged once per process; every test starts afresh.
    _NOTED_OVERLAY_MERGE_WARNINGS.clear()
    yield
    _NOTED_OVERLAY_MERGE_WARNINGS.clear()


#: The base contract's builds, with ``backfill`` inserted before the two the
#: prod overlay was written against.
BUILDS = [
    {"id": "backfill", "engine": "spark"},
    {"id": "ingest", "engine": "kafka-connect"},
    {"id": "transform", "engine": "spark", "properties": {"executors": 2}},
]


def _base(version: str) -> Dict[str, Any]:
    return {"fluidVersion": version, "builds": copy.deepcopy(BUILDS)}


PROD = {"builds": [{"id": "transform", "properties": {"executors": 8}}]}


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    return _deep_merge(copy.deepcopy(base), copy.deepcopy(overlay))


def _events(caplog: pytest.LogCaptureFixture) -> list:
    return [getattr(r, "event", None) for r in caplog.records if r.name == "fluid.loader"]


class TestKeyedMerge:
    def test_a_patch_follows_its_build_when_the_base_is_reordered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        builds = _merge(_base("0.7.6"), PROD)["builds"]
        assert [b["id"] for b in builds] == ["backfill", "ingest", "transform"]
        assert builds[2]["properties"] == {"executors": 8}
        assert "properties" not in builds[0]
        assert builds[2]["engine"] == "spark"  # unmentioned fields are kept
        assert _events(caplog) == []

    def test_an_overlay_entry_matching_nothing_is_appended_and_named(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A new entry, or a patch whose id has a typo: either way, say so.
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        overlay = {"builds": [{"id": "transfrom", "engine": "spark"}, PROD["builds"][0]]}
        builds = _merge(_base("0.7.6"), overlay)["builds"]
        assert [b["id"] for b in builds] == [
            "backfill",
            "ingest",
            "transform",
            "transfrom",
        ]
        assert builds[2]["properties"] == {"executors": 8}
        assert _events(caplog) == ["overlay_entry_added"]
        assert "adds builds id transfrom" in caplog.records[0].getMessage()
        assert "backfill, ingest, transform" in caplog.records[0].getMessage()

    @pytest.mark.parametrize("version", ["0.7.10", "0.8", "1.0"])
    def test_later_versions_merge_by_identity(self, version: str) -> None:
        assert _merge(_base(version), PROD)["builds"][2]["properties"] == {"executors": 8}

    @pytest.mark.parametrize("version", ["0.7.6-rc1", "0.7.\u00b2", None])
    def test_a_version_that_is_not_plain_numbers_merges_by_position(self, version) -> None:
        base = _base("0.7.6")
        base["fluidVersion"] = version
        assert _merge(base, PROD)["builds"][0]["properties"] == {"executors": 8}

    def test_exposes_match_by_expose_id(self) -> None:
        base = {
            "fluidVersion": "0.7.6",
            "exposes": [
                {"exposeId": "a", "binding": {"location": {"topic": "a.dev"}}},
                {"exposeId": "b", "binding": {"location": {"topic": "b.dev"}}},
            ],
        }
        overlay = {"exposes": [{"exposeId": "b", "binding": {"location": {"topic": "b.prod"}}}]}
        exposes = _merge(base, overlay)["exposes"]
        assert exposes[0]["binding"]["location"]["topic"] == "a.dev"
        assert exposes[1]["binding"]["location"]["topic"] == "b.prod"

    def test_consumes_match_on_product_and_expose_together(self) -> None:
        base = {
            "fluidVersion": "0.7.6",
            "consumes": [
                {"productId": "p", "exposeId": "x", "versionConstraint": "^1.0"},
                {"productId": "p", "exposeId": "y", "versionConstraint": "^1.0"},
            ],
        }
        overlay = {"consumes": [{"productId": "p", "exposeId": "y", "versionConstraint": "^2.0"}]}
        consumes = _merge(base, overlay)["consumes"]
        assert [c["versionConstraint"] for c in consumes] == ["^1.0", "^2.0"]

    def test_an_overlay_can_opt_in_by_raising_the_version(self) -> None:
        overlay = {"fluidVersion": "0.7.6", **PROD}
        assert _merge(_base("0.7.5"), overlay)["builds"][2]["properties"] == {"executors": 8}

    def test_lists_inside_a_matched_entry_still_merge_by_position(self) -> None:
        base = {
            "fluidVersion": "0.7.6",
            "exposes": [
                {
                    "exposeId": "a",
                    "contract": {"schema": [{"name": "id"}, {"name": "v"}]},
                },
                {"exposeId": "b"},
            ],
        }
        overlay = {"exposes": [{"exposeId": "a", "contract": {"schema": [{"type": "string"}]}}]}
        schema = _merge(base, overlay)["exposes"][0]["contract"]["schema"]
        assert schema == [{"name": "id", "type": "string"}, {"name": "v"}]


class TestPositionalMerge:
    def test_before_0_7_6_a_reordered_base_retargets_the_patch_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        builds = _merge(_base("0.7.5"), PROD)["builds"]
        # Unchanged behaviour: the patch lands on index 0, keeping its id...
        assert builds[0]["properties"] == {"executors": 8}
        assert builds[2]["properties"] == {"executors": 2}
        # ...and now says so.
        assert _events(caplog) == ["overlay_patch_retargeted"]
        message = caplog.records[0].getMessage()
        assert "builds[0] names id transform but patches the base's builds[0], id backfill" in (
            message
        )

    def test_a_patch_in_the_base_order_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        overlay = {"builds": [{"id": "backfill"}, {"id": "ingest", "engine": "debezium"}]}
        builds = _merge(_base("0.7.5"), overlay)["builds"]
        assert builds[1]["engine"] == "debezium"
        assert _events(caplog) == []

    @pytest.mark.parametrize(
        "overlay, problem",
        [
            pytest.param(
                {"builds": [{"properties": {"executors": 8}}]},
                "builds[0] names no id",
                id="no-id",
            ),
            pytest.param(
                {"builds": [{"id": "transform"}, {"id": "transform"}]},
                "builds[0] and builds[1] both name id transform",
                id="repeated-id",
            ),
        ],
    )
    def test_0_7_6_refuses_an_overlay_entry_it_cannot_match(
        self, overlay: Dict[str, Any], problem: str
    ) -> None:
        # Falling back to position would retarget it, the failure this fixes.
        with pytest.raises(OverlayMergeError, match=re.escape(problem)):
            _merge(_base("0.7.6"), overlay)

    def test_0_7_6_merges_by_position_when_the_base_has_no_unique_ids(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        base = {
            "fluidVersion": "0.7.6",
            "builds": [{"engine": "sql"}, {"engine": "dbt"}],
        }
        merged = _merge(base, {"builds": [{"engine": "spark"}]})
        assert [b["engine"] for b in merged["builds"]] == ["spark", "dbt"]
        assert _events(caplog) == ["overlay_merged_by_position"]
        assert "give every base entry of builds a unique id" in caplog.records[0].getMessage()

    def test_a_single_entry_is_not_ambiguous(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        base = {"fluidVersion": "0.7.6", "builds": [{"id": "only", "engine": "sql"}]}
        merged = _merge(base, {"builds": [{"engine": "dbt"}]})
        assert merged["builds"] == [{"id": "only", "engine": "dbt"}]
        assert _events(caplog) == []

    def test_a_nested_key_named_builds_is_not_an_identity_list(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        base = {
            "fluidVersion": "0.7.6",
            "extensions": {"x": {"builds": [{"id": "a", "v": 1}, {"id": "b", "v": 1}]}},
        }
        overlay = {"extensions": {"x": {"builds": [{"id": "b", "v": 2}]}}}
        nested = _merge(base, overlay)["extensions"]["x"]["builds"]
        assert nested == [{"id": "b", "v": 2}, {"id": "b", "v": 1}]
        assert _events(caplog) == []

    def test_a_retarget_on_part_of_a_compound_key_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        base = {
            "fluidVersion": "0.7.5",
            "consumes": [
                {"productId": "a", "exposeId": "e"},
                {"productId": "b", "exposeId": "e"},
            ],
        }
        _merge(base, {"consumes": [{"productId": "b", "purpose": "x"}]})
        assert _events(caplog) == ["overlay_patch_retargeted"]
        assert "names productId b but patches" in caplog.records[0].getMessage()

    def test_each_warning_is_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        # `fluid apply` loads the contract twice.
        caplog.set_level(logging.WARNING, logger="fluid.loader")
        _merge(_base("0.7.5"), PROD)
        _merge(_base("0.7.5"), PROD)
        assert _events(caplog) == ["overlay_patch_retargeted"]


@pytest.mark.parametrize("version, executors", [("0.7.6", [2, None, 8]), ("0.7.5", [8, None, 2])])
def test_load_contract_with_overlay_applies_the_rule(
    tmp_path: Path, version: str, executors: list
) -> None:
    from fluid_build.loader import load_with_overlay

    base = _base(version)
    base["builds"][0]["properties"] = {"executors": 2}
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(yaml.safe_dump(PROD), encoding="utf-8")
    contract = load_with_overlay(tmp_path / "contract.fluid.yaml", env="prod")
    assert [(b.get("properties") or {}).get("executors") for b in contract["builds"]] == (executors)


def test_load_with_overlay_reports_an_entry_it_cannot_match(tmp_path: Path) -> None:
    from fluid_build.loader import load_with_overlay

    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_base("0.7.6")), encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        yaml.safe_dump({"builds": [{"properties": {"executors": 8}}]}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match=r"prod\.yaml: the overlay's builds\[0\] names no id"):
        load_with_overlay(tmp_path / "contract.fluid.yaml", env="prod")


def test_the_contract_api_reports_an_unmatched_entry_as_a_load_error() -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract_from_dict

    with pytest.raises(ContractLoadError) as info:
        load_contract_from_dict(_base("0.7.6"), overlay={"builds": [{"engine": "dbt"}]})
    assert info.value.event == "contract_load_failed"
    assert "builds[0] names no id" in str(info.value)


def test_bundle_names_the_overlay_it_cannot_apply(tmp_path: Path) -> None:
    from fluid_build.cli.bundle import materialize_contract

    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_base("0.7.6")), encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(
        yaml.safe_dump({"builds": [{"engine": "dbt"}]}), encoding="utf-8"
    )
    with pytest.raises(OverlayMergeError, match=r"overlays/prod\.yaml: the overlay's builds\[0\]"):
        materialize_contract(tmp_path / "contract.fluid.yaml", "prod", logging.getLogger("t"))


def test_a_warning_names_its_contract_and_each_contract_gets_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="fluid.loader")
    for product in ("p1", "p2"):
        base = _base("0.7.5")
        base["id"] = product
        _merge(base, PROD)
    messages = [r.getMessage() for r in caplog.records if r.name == "fluid.loader"]
    assert len(messages) == 2
    assert "in p1, the overlay's builds[0]" in messages[0]
    assert "in p2, the overlay's builds[0]" in messages[1]


def test_an_added_entry_names_its_contract_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="fluid.loader")
    for product in ("p1", "p2"):
        base = _base("0.7.6")
        base["id"] = product
        _merge(base, {"builds": [{"id": "reindex", "engine": "spark"}]})
    messages = [r.getMessage() for r in caplog.records if r.name == "fluid.loader"]
    assert [m.split(" adds ")[0] for m in messages] == [
        "overlay_entry_added: the overlay for p1",
        "overlay_entry_added: the overlay for p2",
    ]
