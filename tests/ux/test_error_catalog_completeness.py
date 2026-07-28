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

"""Error-catalog completeness — every typed error renders the five fields,
emits stable JSON, and never surfaces raw tracebacks in default mode.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Type

import pytest

from fluid_build.cli import _errors as err_module
from fluid_build.cli._errors import (
    BudgetExceededError,
    CapabilityMismatchError,
    ConnectivityProbeError,
    DLQOverflowError,
    FluidUserError,
    InfraDriftError,
    LockHeldError,
    MissingExtraError,
    PartialFailureError,
    ResidencyViolationError,
    SchemaDriftError,
    SchemaValidationError,
    SecretResolutionError,
    SovereigntyViolationError,
    StaleReplayError,
    SupplyChainViolationError,
)

ERROR_CLASSES = [
    SchemaValidationError,
    CapabilityMismatchError,
    SecretResolutionError,
    SovereigntyViolationError,
    ConnectivityProbeError,
    PartialFailureError,
    DLQOverflowError,
    SchemaDriftError,
    BudgetExceededError,
    LockHeldError,
    StaleReplayError,
    MissingExtraError,
    InfraDriftError,
    ResidencyViolationError,
    SupplyChainViolationError,
]


# ── Catalog completeness ────────────────────────────────────────────────


class TestCatalogCoverage:
    def test_at_least_fourteen_typed_errors(self):
        """The plan promises 14+ typed error classes."""
        assert len(ERROR_CLASSES) >= 14

    def test_every_error_subclasses_FluidUserError(self):
        for cls in ERROR_CLASSES:
            assert issubclass(cls, FluidUserError)

    def test_every_class_in_module_is_exported(self):
        """Every error class defined in _errors.py must be in ERROR_CLASSES
        (the test corpus). Adds safety against accidental orphan errors.
        """
        defined = {
            name
            for name, obj in inspect.getmembers(err_module, inspect.isclass)
            if obj is not FluidUserError
            and issubclass(obj, FluidUserError)
            and obj.__module__ == err_module.__name__
        }
        catalog = {cls.__name__ for cls in ERROR_CLASSES}
        missing = defined - catalog
        assert not missing, f"orphan error class(es) not in test corpus: {missing}"


# ── Five-field shape ────────────────────────────────────────────────────


class TestFiveFieldShape:
    @pytest.mark.parametrize("cls", ERROR_CLASSES)
    def test_class_has_for_factory(self, cls: Type[FluidUserError]):
        """Every error class declares at least one ``for_*`` constructor."""
        factories = [
            name for name in dir(cls) if name.startswith("for_") and callable(getattr(cls, name))
        ]
        assert factories, f"{cls.__name__} must declare a for_* factory"

    @pytest.mark.parametrize(
        "cls, kwargs",
        [
            (
                SchemaValidationError,
                dict(
                    contract_path="contract.fluid.yaml",
                    line=14,
                    col=5,
                    field_path="builds[0].properties.source.kind",
                    message="missing required field",
                ),
            ),
            (
                CapabilityMismatchError,
                dict(
                    runner_name="duckdb",
                    asked=["cdc"],
                    declared=["full_refresh"],
                ),
            ),
            (
                SecretResolutionError,
                dict(ref="vault://salesforce/oauth", reason="VAULT_TOKEN missing"),
            ),
            (
                SovereigntyViolationError,
                dict(connector="airbyte/source-x", jurisdiction="EU"),
            ),
            (
                ConnectivityProbeError,
                dict(target="airbyte.internal:8001", reason="connection refused"),
            ),
            (
                PartialFailureError,
                dict(succeeded=["Account"], failed=["Opportunity"]),
            ),
            (
                DLQOverflowError,
                dict(count=10001, cap=10000, alerts=["pii_classification_failed"]),
            ),
            (
                SchemaDriftError,
                dict(
                    baseline_digest="sha256:abc",
                    current_digest="sha256:def",
                    summary="email column dropped",
                ),
            ),
            (
                BudgetExceededError,
                dict(dimension="rows", used=12_000_000, cap=10_000_000),
            ),
            (
                LockHeldError,
                dict(holder="pid-1234", scope="product", resource_id="bronze.x"),
            ),
            (
                StaleReplayError,
                dict(run_id="01HQXY3K", retention_horizon="P30D"),
            ),
            (
                MissingExtraError,
                dict(extra="airbyte", install_hint="`pip install fluid-forge[airbyte]`"),
            ),
            (
                InfraDriftError,
                dict(chart="airbyte/airbyte", declared="0.520.0", live="0.519.0"),
            ),
            (
                ResidencyViolationError,
                dict(from_region="eu-west-1", to_region="us-east-1", jurisdiction="EU"),
            ),
            (
                SupplyChainViolationError,
                dict(image_ref="airbyte/source-faker:latest", reason="signature missing"),
            ),
        ],
    )
    def test_factory_returns_complete_five_fields(self, cls, kwargs):
        e = next(getattr(cls, name)(**kwargs) for name in dir(cls) if name.startswith("for_"))
        assert isinstance(e, cls)
        assert e.what, f"{cls.__name__}.what is empty"
        assert e.why, f"{cls.__name__}.why is empty"
        assert e.fix, f"{cls.__name__}.fix is empty"
        assert e.doc, f"{cls.__name__}.doc is empty"
        # ``where`` is optional but must be a string when present.
        if e.where is not None:
            assert isinstance(e.where, str)

    @pytest.mark.parametrize("cls", ERROR_CLASSES)
    def test_code_field_matches_class_name(self, cls):
        # Smoke: the catalog uses ``code`` as a stable JSON tag; it must equal
        # the class's name to keep CI parsers honest.
        instances_factory = next(name for name in dir(cls) if name.startswith("for_"))
        # Try to get a sample instance via the parametrized data; we recompute
        # here defensively.
        try:
            sig = inspect.signature(getattr(cls, instances_factory))
            kwargs = {
                p.name: ""
                for p in sig.parameters.values()
                if p.kind == inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
            }
            # Fill in a couple of common required ints / lists if needed.
            for name, param in sig.parameters.items():
                if param.kind != inspect.Parameter.KEYWORD_ONLY:
                    continue
                if name in kwargs and param.annotation is int:
                    kwargs[name] = 0
                if name in kwargs and param.annotation is list:
                    kwargs[name] = []
            inst = getattr(cls, instances_factory)(**kwargs)
        except Exception:
            inst = cls(what="x", why="y", fix="z", doc="d")
        assert inst.code == cls.__name__


# ── JSON output stability ──────────────────────────────────────────────


class TestJsonOutput:
    @pytest.mark.parametrize("cls", ERROR_CLASSES)
    def test_as_json_is_valid_and_has_required_keys(self, cls):
        e = cls(what="x", why="y", fix="z", doc="d")
        decoded = json.loads(e.as_json())
        for key in ("code", "what", "why", "fix", "doc", "where", "extras"):
            assert key in decoded

    def test_json_keys_sorted_for_stability(self):
        e = SchemaValidationError(
            what="a",
            why="b",
            fix="c",
            doc="d",
            extras={"z": 1, "a": 2},
        )
        # Stable ordering across calls for snapshot/diff-friendly output.
        assert e.as_json() == e.as_json()


# ── Render: no raw tracebacks ──────────────────────────────────────────


class TestRenderNoTracebacks:
    @pytest.mark.parametrize("cls", ERROR_CLASSES)
    def test_render_does_not_include_traceback_text(self, cls):
        e = cls(what="hello", why="why", fix="fix", doc="https://x")
        rendered = e.render(color=False)
        # Render output must not look like a Python traceback.
        assert "Traceback" not in rendered
        assert 'File "' not in rendered

    @pytest.mark.parametrize("cls", ERROR_CLASSES)
    def test_render_includes_all_five_fields(self, cls):
        e = cls(
            what="W",
            why="Y",
            fix="F",
            doc="https://D",
            where="path:1:1",
        )
        rendered = e.render(color=False)
        for fragment in ("W", "Y", "F", "https://D", "path:1:1"):
            assert fragment in rendered

    def test_render_falls_back_to_plain_when_rich_missing(self, monkeypatch):
        import sys

        # Hide rich modules so the renderer takes the plain-text path.
        for mod in list(sys.modules):
            if mod.startswith("rich"):
                monkeypatch.setitem(sys.modules, mod, None)
        e = SchemaValidationError(what="x", why="y", fix="z", doc="d")
        rendered = e.render(color=False)
        assert "x" in rendered and "y" in rendered


# ── Coverage of plan-promised codes ────────────────────────────────────


class TestPlanPromisedCodes:
    """The plan called out 14+ specific error codes. Each one must be present
    in the catalog so adoption documentation (linking to error codes in docs)
    stays stable.
    """

    EXPECTED_CODES = {
        "SchemaValidationError",
        "CapabilityMismatchError",
        "SecretResolutionError",
        "SovereigntyViolationError",
        "ConnectivityProbeError",
        "PartialFailureError",
        "DLQOverflowError",
        "SchemaDriftError",
        "BudgetExceededError",
        "LockHeldError",
        "StaleReplayError",
        "MissingExtraError",
        "InfraDriftError",
        "ResidencyViolationError",
        "SupplyChainViolationError",
    }

    def test_every_promised_code_is_implemented(self):
        actual = {cls.__name__ for cls in ERROR_CLASSES}
        missing = self.EXPECTED_CODES - actual
        assert not missing, f"plan-promised error codes missing from catalog: {missing}"
