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

"""Tests for fluid_build/errors.py and fluid_build/policy/guardrails.py."""

from types import SimpleNamespace

import pytest

from fluid_build.errors import (
    AuthenticationError,
    ConfigurationError,
    DependencyError,
    FileSystemError,
    FluidError,
    NetworkError,
    ValidationError,
    wrap_error,
)
from fluid_build.policy.guardrails import validate_no_overgrant

# ═══════════════════════════════════════════════════════════════════════
# errors.py
# ═══════════════════════════════════════════════════════════════════════


class TestFluidError:
    def test_basic(self):
        err = FluidError("something went wrong")
        assert str(err) == "something went wrong"
        assert err.message == "something went wrong"
        assert err.context == {}
        assert err.original_error is None

    def test_with_context(self):
        err = FluidError("fail", context={"key": "val"})
        assert err.context == {"key": "val"}

    def test_with_original_error(self):
        orig = ValueError("root cause")
        err = FluidError("wrapper", original_error=orig)
        assert err.original_error is orig
        assert "caused by" in str(err)

    def test_inherits_exception(self):
        err = FluidError("x")
        assert isinstance(err, Exception)


class TestErrorSubclasses:
    @pytest.mark.parametrize(
        "cls",
        [
            ValidationError,
            ConfigurationError,
            FileSystemError,
            NetworkError,
            DependencyError,
            AuthenticationError,
        ],
    )
    def test_is_fluid_error(self, cls):
        err = cls("test")
        assert isinstance(err, FluidError)
        assert isinstance(err, Exception)

    def test_validation_error(self):
        err = ValidationError("bad input", context={"field": "name"})
        assert err.message == "bad input"
        assert err.context["field"] == "name"


class TestWrapError:
    def test_basic_wrap(self):
        orig = RuntimeError("disk full")
        wrapped = wrap_error(orig, "Cannot write output")
        assert isinstance(wrapped, FluidError)
        assert wrapped.original_error is orig
        assert "Cannot write output" in str(wrapped)

    def test_custom_class(self):
        orig = OSError("no access")
        wrapped = wrap_error(orig, "Auth failed", error_class=AuthenticationError)
        assert isinstance(wrapped, AuthenticationError)

    def test_with_context(self):
        orig = ValueError("x")
        wrapped = wrap_error(orig, "err", context={"path": "/tmp"})
        assert wrapped.context["path"] == "/tmp"


# ═══════════════════════════════════════════════════════════════════════
# policy/guardrails.py
# ═══════════════════════════════════════════════════════════════════════


def _make_action(resource_id, roles):
    """Create a simple action-like namespace with payload."""
    return SimpleNamespace(
        resource_id=resource_id,
        payload={"roles": roles},
    )


class TestValidateNoOvergrant:
    def test_safe_roles(self):
        actions = [
            _make_action("dataset1", ["roles/bigquery.dataViewer"]),
            _make_action("bucket1", ["roles/storage.objectViewer"]),
        ]
        assert validate_no_overgrant(actions) is True

    def test_owner_role_detected(self):
        actions = [_make_action("dataset1", ["roles/bigquery.dataOwner"])]
        with pytest.raises(ValueError, match="Over-broad role"):
            validate_no_overgrant(actions)

    def test_case_insensitive_owner(self):
        actions = [_make_action("ds", ["roles/bigquery.DataOwner"])]
        with pytest.raises(ValueError):
            validate_no_overgrant(actions)

    def test_empty_roles(self):
        actions = [_make_action("ds", [])]
        assert validate_no_overgrant(actions) is True

    def test_no_payload(self):
        actions = [SimpleNamespace(resource_id="x", payload=None)]
        assert validate_no_overgrant(actions) is True

    def test_empty_actions(self):
        assert validate_no_overgrant([]) is True

    def test_payload_not_dict(self):
        actions = [SimpleNamespace(resource_id="x", payload="string")]
        assert validate_no_overgrant(actions) is True
