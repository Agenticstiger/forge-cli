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
import json
import logging
import os
import re
import subprocess
import sys
import tarfile
from datetime import datetime
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


# --strict: ``fluid validate`` and ``fluid contract-validation`` take a
# retargeted patch from the loader and report it as their own warning, so
# ``--strict`` fails on it. The loader used to log it, which --strict never read.


class TestCollectOverlayWarnings:
    def test_the_block_takes_a_retargeted_patch_instead_of_logging_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fluid_build.loader import OverlayWarning, collect_overlay_warnings

        caplog.set_level(logging.DEBUG, logger="fluid.loader")
        with collect_overlay_warnings() as taken:
            _merge(_base("0.7.5"), PROD)
        assert [w.event for w in taken] == ["overlay_patch_retargeted"]
        assert isinstance(taken[0], OverlayWarning)
        assert "names id transform but patches the base's builds[0], id backfill" in (
            taken[0].message
        )
        # Said once, by the command: the log keeps it at DEBUG.
        levels = [r.levelno for r in caplog.records if r.name == "fluid.loader"]
        assert levels == [logging.DEBUG]

    def test_an_added_entry_stays_a_log_line(self, caplog: pytest.LogCaptureFixture) -> None:
        # Adding a build only prod runs is often intended, and --strict could
        # not be silenced on it.
        from fluid_build.loader import collect_overlay_warnings

        caplog.set_level(logging.WARNING, logger="fluid.loader")
        with collect_overlay_warnings() as taken:
            _merge(_base("0.7.6"), {"builds": [{"id": "reindex", "engine": "spark"}]})
        assert taken == []
        assert _events(caplog) == ["overlay_entry_added"]

    def test_a_warning_the_block_took_is_still_logged_by_a_later_load(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fluid_build.loader import collect_overlay_warnings

        caplog.set_level(logging.WARNING, logger="fluid.loader")
        with collect_overlay_warnings() as taken:
            _merge(_base("0.7.5"), PROD)
            _merge(_base("0.7.5"), PROD)
        assert len(taken) == 1
        assert _events(caplog) == []
        _merge(_base("0.7.5"), PROD)
        assert _events(caplog) == ["overlay_patch_retargeted"]

    def test_outside_a_block_nothing_is_collected(self) -> None:
        from fluid_build.loader import _OVERLAY_WARNING_SINK, collect_overlay_warnings

        with collect_overlay_warnings():
            pass
        assert _OVERLAY_WARNING_SINK.get() is None

    def test_a_block_that_raises_logs_what_it_took(self, caplog: pytest.LogCaptureFixture) -> None:
        # Its caller reports the warnings only when the block completes: a
        # patch retargeted before another entry failed the merge was lost.
        from fluid_build.loader import collect_overlay_warnings

        caplog.set_level(logging.WARNING, logger="fluid.loader")
        with pytest.raises(OverlayMergeError):
            with collect_overlay_warnings():
                _merge(_base("0.7.5"), PROD)
                raise OverlayMergeError("the next list failed")
        assert _events(caplog) == ["overlay_patch_retargeted"]


def _write_product(tmp_path: Path, version: str, overlay: Dict[str, Any]) -> None:
    contract = {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "network.service-instances",
        "name": "Service instances",
        "metadata": {"owner": {"team": "data-engineering"}},
        "builds": [{"id": b["id"], "engine": "custom", "description": b["id"]} for b in BUILDS],
        "exposes": [
            {
                "exposeId": "service_instances",
                "kind": "topic",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "network.service-instances"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")


#: Written against ``[ingest, transform]``: its second entry patches transform.
PROD_BY_POSITION = {"builds": [{}, {"id": "transform", "description": "8 executors"}]}

#: Environment variables a CLI subprocess here must not inherit: API keys, and
#: the debug level ``tests/cli/conftest.py`` sets, which some xdist workers keep.
#: At DEBUG the loader also logs a warning the command reports, and the tests
#: below count that the terminal shows it once.
_NOT_INHERITED = ("FLUID_LOG_LEVEL", "FLUID_DEBUG", "FLUID_SHOW_REGISTRATION")


def _cli_env() -> Dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("_API_KEY") and k not in _NOT_INHERITED
    }


def _validate(tmp_path: Path, *argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "validate", "contract.fluid.yaml"]
        + ["--env", "prod", "--offline", *argv],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=_cli_env(),
    )


class TestValidateStrict:
    def test_the_retargeted_patch_is_a_warning(self, tmp_path: Path) -> None:
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        result = _validate(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout + result.stderr
        assert "Validation Warnings:" in result.stdout
        # Reported by validate, not also logged by the loader.
        assert out.count("overlay_patch_retargeted") == 1
        assert "the overlay's builds[1] names id transform but patches the base's builds[1], " in (
            " ".join(result.stdout.split())
        )

    def test_strict_fails_on_it_and_says_why(self, tmp_path: Path) -> None:
        # The summary above reads "Valid FLUID contract".
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        result = _validate(tmp_path, "--strict")
        assert result.returncode == 1
        assert "Validation failed: warnings treated as errors (--strict)" in result.stdout

    def test_quiet_still_shows_it(self, tmp_path: Path) -> None:
        # --quiet prints errors only, but the loader always printed this one.
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        result = _validate(tmp_path, "--quiet")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith("WARNING: overlay_patch_retargeted: in ")
        assert (result.stdout + result.stderr).count("overlay_patch_retargeted") == 1

    def test_quiet_strict_names_it_as_the_error(self, tmp_path: Path) -> None:
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        result = _validate(tmp_path, "--quiet", "--strict")
        assert result.returncode == 1
        assert result.stdout.startswith("ERROR (--strict): overlay_patch_retargeted: in ")
        assert (result.stdout + result.stderr).count("overlay_patch_retargeted") == 1

    def test_quiet_strict_names_it_beside_other_errors(self, tmp_path: Path) -> None:
        # --strict must not show less than --quiet alone.
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        contract = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text("utf-8"))
        contract["bogusTopLevel"] = 1
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")
        result = _validate(tmp_path, "--quiet", "--strict")
        assert result.returncode == 1
        assert "ERROR: root: Additional properties" in result.stdout
        assert "ERROR (--strict): overlay_patch_retargeted: in " in result.stdout

    def test_strict_json_lists_it(self, tmp_path: Path) -> None:
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        result = _validate(tmp_path, "--strict", "--format", "json")
        assert result.returncode == 1
        warnings = json.loads(result.stdout)["warnings"]
        assert [w.split(":")[0] for w in warnings] == ["overlay_patch_retargeted"]

    def test_keyed_merge_has_nothing_to_warn_about(self, tmp_path: Path) -> None:
        overlay = {"builds": [{"id": "transform", "description": "8 executors"}]}
        _write_product(tmp_path, "0.7.6", overlay)
        result = _validate(tmp_path, "--strict")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_added_build_does_not_fail_strict(self, tmp_path: Path) -> None:
        overlay = {"builds": [{"id": "reindex", "engine": "custom", "description": "reindex"}]}
        _write_product(tmp_path, "0.7.6", overlay)
        result = _validate(tmp_path, "--strict")
        assert result.returncode == 0, result.stdout + result.stderr


# A bundle is frozen: `fluid validate <bundle>.tgz` never merges the overlay
# again, so it cannot see a patch the merge retargeted. `fluid bundle --env`
# records the warning in the bundle's MANIFEST and validate reads it from
# there. Pipeline stage 2 runs exactly `fluid validate <bundle> --env <env>
# --strict`.


def _fluid(cwd: Path, *argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_cli_env(),
    )


def _bundle(tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    argv = ["contract.fluid.yaml", "--env", "prod", "--format", "tgz", "--out", "b.tgz"]
    built = _fluid(tmp_path, "bundle", *argv)
    assert built.returncode == 0, built.stdout + built.stderr
    return built


def _validate_bundle(tmp_path: Path, *argv: str) -> "subprocess.CompletedProcess[str]":
    return _fluid(tmp_path, "validate", "b.tgz", "--env", "prod", "--offline", *argv)


def _manifest_source(tgz: Path) -> Dict[str, Any]:
    with tarfile.open(tgz, "r:gz") as tar:
        member = tar.extractfile("MANIFEST.json")
        assert member is not None
        return json.loads(member.read().decode("utf-8"))["source"]


@pytest.fixture(scope="module")
def retargeted_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bundle ``fluid bundle --env prod`` built from a positional 0.7.5 overlay."""
    path = tmp_path_factory.mktemp("retargeted")
    _write_product(path, "0.7.5", PROD_BY_POSITION)
    _bundle(path)
    return path


class TestValidateBundleStrict:
    def test_the_bundle_records_it_and_says_it_once(self, tmp_path: Path) -> None:
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        built = _bundle(tmp_path)
        out = built.stdout + built.stderr
        # Said by the bundle command, not also logged by the loader.
        assert out.count("overlay_patch_retargeted") == 1
        assert "overlay warnings: 1 recorded in the bundle" in out
        recorded = _manifest_source(tmp_path / "b.tgz")["overlayWarnings"]
        assert [w.split(":")[0] for w in recorded] == ["overlay_patch_retargeted"]
        assert "the overlay's builds[1] names id transform but patches the base's builds[1], " in (
            recorded[0]
        )

    def test_the_retargeted_patch_is_a_warning(self, retargeted_bundle: Path) -> None:
        result = _validate_bundle(retargeted_bundle)
        assert result.returncode == 0, result.stdout + result.stderr
        # Listed although the bundle passes, as a contract file lists it.
        assert (
            "[warning] overlay: MANIFEST.json: overlay_patch_retargeted: in "
            "network.service-instances, the overlay's builds[1] names id transform"
        ) in " ".join(result.stdout.split())

    def test_strict_fails_on_it_and_says_why(self, retargeted_bundle: Path) -> None:
        result = _validate_bundle(retargeted_bundle, "--strict")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "overlay_patch_retargeted: in network.service-instances" in result.stdout
        assert "Validation failed: warnings treated as errors (--strict)" in result.stdout

    def test_quiet_still_shows_it(self, retargeted_bundle: Path) -> None:
        result = _validate_bundle(retargeted_bundle, "--quiet")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith(
            "WARNING: overlay: MANIFEST.json: overlay_patch_retargeted: in "
        )
        assert (result.stdout + result.stderr).count("overlay_patch_retargeted") == 1

    def test_quiet_strict_names_it_as_the_error(self, retargeted_bundle: Path) -> None:
        result = _validate_bundle(retargeted_bundle, "--quiet", "--strict")
        assert result.returncode == 1
        assert result.stdout.startswith(
            "ERROR (--strict): overlay: MANIFEST.json: overlay_patch_retargeted: in "
        )
        assert (result.stdout + result.stderr).count("overlay_patch_retargeted") == 1

    def test_strict_json_lists_it(self, retargeted_bundle: Path) -> None:
        result = _validate_bundle(retargeted_bundle, "--strict", "--format", "json")
        assert result.returncode == 1
        report = json.loads(result.stdout)
        assert report["status"] == "fail"
        assert [
            (i["severity"], i["file"], i["code"], i["message"].split(":")[0])
            for i in report["issues"]
        ] == [("warning", "MANIFEST.json", "OVERLAY-WARNING", "overlay_patch_retargeted")]

    def test_quiet_strict_names_it_beside_other_errors(self, tmp_path: Path) -> None:
        # --quiet printed nothing for a bundle, so --strict named the warning
        # as the error and hid the real one.
        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        contract = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text("utf-8"))
        contract["bogusTopLevel"] = 1
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")
        _bundle(tmp_path)
        result = _validate_bundle(tmp_path, "--quiet", "--strict")
        assert result.returncode == 1
        lines = result.stdout.splitlines()
        assert any(
            line.startswith("ERROR: json-schema: contract.resolved.yaml: ")
            and "bogusTopLevel" in line
            for line in lines
        ), result.stdout
        assert any(
            line.startswith("ERROR (--strict): overlay: MANIFEST.json: overlay_patch_retargeted")
            for line in lines
        ), result.stdout

    def test_a_keyed_merge_records_nothing(self, tmp_path: Path) -> None:
        _write_product(tmp_path, "0.7.6", {"builds": [{"id": "transform", "description": "8"}]})
        _bundle(tmp_path)
        assert "overlayWarnings" not in _manifest_source(tmp_path / "b.tgz")
        result = _validate_bundle(tmp_path, "--strict")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_merge_that_then_fails_still_names_the_retargeted_patch(self, tmp_path: Path) -> None:
        # Two defects in one overlay: a patch retargeted in builds (whose ids
        # repeat, so they merge by position), then an exposes entry with no id.
        _write_product(tmp_path, "0.7.6", {})
        contract = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text("utf-8"))
        contract["builds"][0]["id"] = "ingest"
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "service_instances_v2"
        contract["exposes"].append(second)
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")
        overlay = dict(PROD_BY_POSITION, exposes=[{"description": "no exposeId"}])
        (tmp_path / "overlays" / "prod.yaml").write_text(yaml.safe_dump(overlay), "utf-8")
        argv = ["contract.fluid.yaml", "--env", "prod", "--format", "tgz", "--out", "b.tgz"]
        built = _fluid(tmp_path, "bundle", *argv)
        out = built.stdout + built.stderr
        assert built.returncode == 1, out
        assert "exposes[0] names no exposeId" in out
        assert out.count("overlay_patch_retargeted") == 1

    def test_an_edited_record_cannot_drive_the_terminal(self, tmp_path: Path) -> None:
        # The record is outside the merkle root: editing it keeps the digest.
        # A lone surrogate made validate crash before writing --report.
        from fluid_build.forge.core.bundle import write_tgz

        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        _bundle(tmp_path)
        files: Dict[str, bytes] = {}
        with tarfile.open(tmp_path / "b.tgz", "r:gz") as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                if handle is not None:
                    files[member.name] = handle.read()
        manifest = json.loads(files["MANIFEST.json"])
        manifest["source"]["overlayWarnings"] = ["x\x1b[1A\x1b[2K\n✅ forged \ud800"]
        files["MANIFEST.json"] = json.dumps(manifest).encode("ascii")
        write_tgz(tmp_path / "b.tgz", files)
        result = _validate_bundle(tmp_path, "--report", "r.json")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "\x1b" not in result.stdout
        assert "x\\x1b[1A\\x1b[2K\\n✅ forged \\ud800" in result.stdout
        issues = json.loads((tmp_path / "r.json").read_text("utf-8"))["issues"]
        assert [i["code"] for i in issues] == ["OVERLAY-WARNING"]

    def test_a_bundle_that_records_none_still_validates(self, tmp_path: Path) -> None:
        # Bundles built before the record existed carry no overlayWarnings:
        # none recorded, whatever the overlay did.
        from fluid_build.cli.bundle import bundle_contract_id, materialize_contract
        from fluid_build.forge.core.bundle import build_bundle_tgz, make_bundle_source

        _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
        contract = tmp_path / "contract.fluid.yaml"
        compiled, overlay_path = materialize_contract(contract, "prod", logging.getLogger("t"))
        out = tmp_path / "b.tgz"
        source = make_bundle_source(contract, out, env="prod", overlay_path=overlay_path)
        build_bundle_tgz(compiled, out, contract_id=bundle_contract_id(compiled), source=source)
        assert "overlayWarnings" not in _manifest_source(out)
        result = _validate_bundle(tmp_path, "--strict")
        assert result.returncode == 0, result.stdout + result.stderr


def test_a_bundle_reads_back_only_the_warnings_it_can_print() -> None:
    # The source block is outside the merkle root: a hint, read defensively.
    from fluid_build.forge.core.bundle import bundle_overlay_warnings

    assert bundle_overlay_warnings(None) == []
    assert bundle_overlay_warnings({"env": "prod"}) == []
    assert bundle_overlay_warnings({"overlayWarnings": "overlay_patch_retargeted: x"}) == []
    recorded = {"overlayWarnings": ["overlay_patch_retargeted: a", 1, "", None, {"m": 1}, "b"]}
    assert bundle_overlay_warnings(recorded) == ["overlay_patch_retargeted: a", "b"]
    # Printable text is kept as written; anything else is escaped as repr writes it.
    edited = {"overlayWarnings": ["é [/] a\x1b[2Kb\nc\ud800\u202e\t"]}
    assert bundle_overlay_warnings(edited) == ["é [/] a\\x1b[2Kb\\nc\\ud800\\u202e\\t"]


def test_workspace_validate_strict_fails_on_it(tmp_path: Path) -> None:
    # `fluid validate --env prod --strict` with no contract validates every
    # product in the workspace.
    (tmp_path / "fluid.workspace.yaml").write_text(
        yaml.safe_dump({"products_dir": "products"}), encoding="utf-8"
    )
    product = tmp_path / "products" / "svc"
    product.mkdir(parents=True)
    _write_product(product, "0.7.5", PROD_BY_POSITION)
    run = [sys.executable, "-m", "fluid_build.cli", "validate", "--env", "prod", "--offline"]
    env = _cli_env()
    lenient = subprocess.run(run, cwd=tmp_path, capture_output=True, text=True, env=env)
    strict = subprocess.run(
        run + ["--strict"], cwd=tmp_path, capture_output=True, text=True, env=env
    )
    assert lenient.returncode == 0, lenient.stdout + lenient.stderr
    assert "overlay_patch_retargeted" in lenient.stdout
    assert strict.returncode == 1
    assert "1 overlay warning(s), treated as errors (--strict)" in strict.stdout


def test_fluid_test_shows_it_as_a_row() -> None:
    # `fluid test --strict` failed on it without printing it.
    from fluid_build.cli.contract_validation import ValidationReport
    from fluid_build.cli.test import _build_check_rows

    report = ValidationReport("c.yaml", "p", "1", datetime.now(), 0.0)
    report.add_issue("warning", "overlay", "overlay_patch_retargeted: in p, the overlay's ...")
    rows = {name: (status, detail) for status, name, detail in _build_check_rows(report)}
    status, detail = rows["Environment overlay"]
    assert status != rows["Schema syntax"][0]
    assert detail.startswith("overlay_patch_retargeted: in p")
    report.issues.clear()
    assert "Environment overlay" not in [name for _, name, _ in _build_check_rows(report)]


def test_an_id_that_reads_as_markup_does_not_break_either_table(capsys) -> None:
    # The message quotes the contract's ids; "[/]" made Rich raise MarkupError.
    from fluid_build.cli.contract_validation import ValidationReport, output_rich_report
    from fluid_build.cli.test import _output_rich

    message = "overlay_patch_retargeted: in p, the overlay's builds[1] names id [/] but ..."
    report = ValidationReport("c.yaml", "p", "1", datetime.now(), 0.0)
    report.add_issue("warning", "overlay", message)
    output_rich_report(report)
    _output_rich(report)
    # Each table shows the id as written (the tables wrap the rest of the line).
    assert capsys.readouterr().out.count("[/]") == 2


def test_contract_validation_reports_it_as_a_warning(tmp_path: Path) -> None:
    from fluid_build.cli.contract_validation import ContractValidator

    _write_product(tmp_path, "0.7.5", PROD_BY_POSITION)
    report = ContractValidator(
        tmp_path / "contract.fluid.yaml", env="prod", check_data=False, track_history=False
    ).validate()
    overlay = [w for w in report.get_warnings() if w.category == "overlay"]
    assert [w.message.split(":")[0] for w in overlay] == ["overlay_patch_retargeted"]
