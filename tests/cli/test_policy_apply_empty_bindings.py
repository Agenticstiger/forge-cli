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

"""Pin the ``fluid policy-apply`` empty-bindings fast-path.

The branch ``feat/opentofu-iac-autogen`` added an empty-bindings
short-circuit to ``cli/policy_apply.py::run`` — a contract with no
``accessPolicy`` grants (e.g., every raw bronze / SDP acquisition
product) compiles to zero bindings. The previous behaviour failed with
``provider_not_specified`` because ``build_provider`` ran on an empty
bindings file before the provider could be inferred; the fix returns a
clean noop (status=noop, exit code 0).

This test pins that fast-path so a future refactor does not silently
regress acquisition-only contracts back to a hard error.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest


def _make_args(bindings_path: Path, **overrides):
    base = {
        "bindings": str(bindings_path),
        "mode": "check",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def logger():
    return logging.getLogger("test_policy_apply_empty_bindings")


class TestPolicyApplyEmptyBindings:
    pytestmark = pytest.mark.unit

    def test_empty_bindings_list_returns_zero_noop(self, tmp_path, logger):
        """A bindings file with ``"bindings": []`` → return 0, no provider
        resolution, no exception. Covers the acquisition-only contract."""
        from fluid_build.cli.policy_apply import run

        bindings_file = tmp_path / "bindings.json"
        bindings_file.write_text(json.dumps({"bindings": []}))
        rc = run(_make_args(bindings_file), logger)
        assert rc == 0

    def test_missing_bindings_key_returns_zero_noop(self, tmp_path, logger):
        """A bindings file missing the ``bindings`` key entirely — same
        path (no grants to apply)."""
        from fluid_build.cli.policy_apply import run

        bindings_file = tmp_path / "bindings.json"
        bindings_file.write_text(json.dumps({"contract": {"id": "x"}}))
        rc = run(_make_args(bindings_file), logger)
        assert rc == 0

    def test_null_bindings_value_returns_zero_noop(self, tmp_path, logger):
        """A bindings file with ``"bindings": null`` — the ``or []``
        guard treats it as empty."""
        from fluid_build.cli.policy_apply import run

        bindings_file = tmp_path / "bindings.json"
        bindings_file.write_text(json.dumps({"bindings": None}))
        rc = run(_make_args(bindings_file), logger)
        assert rc == 0

    def test_empty_bindings_does_not_call_build_provider(self, tmp_path, logger, monkeypatch):
        """The whole point of the short-circuit: ``build_provider`` is
        never reached for an empty bindings file (which would otherwise
        raise ``provider_not_specified`` for any contract without an
        explicit ``binding.platform``)."""
        from fluid_build.cli import policy_apply

        bindings_file = tmp_path / "bindings.json"
        bindings_file.write_text(json.dumps({"bindings": []}))

        called = {"value": False}

        def _fake_build_provider(*_args, **_kwargs):
            called["value"] = True
            raise RuntimeError("build_provider should not be called for empty bindings")

        monkeypatch.setattr(policy_apply, "build_provider", _fake_build_provider)
        rc = policy_apply.run(_make_args(bindings_file), logger)
        assert rc == 0
        assert called["value"] is False


class TestPolicyApplyNoApplierProvider:
    """When the resolved provider has no ``apply_policy`` (AWS / Snowflake /
    local), policy-apply is a no-op — but it must SAY so visibly rather than
    exiting 0 silently as if the GRANT/IAM bindings had been enforced."""

    pytestmark = pytest.mark.unit

    def test_no_applier_warns_visibly_and_returns_zero(self, tmp_path, logger, monkeypatch, capsys):
        from fluid_build.cli import policy_apply

        bindings_file = tmp_path / "bindings.json"
        bindings_file.write_text(
            json.dumps(
                {
                    "provider": "aws",
                    "bindings": [
                        {"provider": "aws", "principal": "role:analyst", "permissions": ["read"]}
                    ],
                }
            )
        )

        class _NoApplierProvider:  # deliberately has no ``apply_policy``
            pass

        monkeypatch.setattr(policy_apply, "build_provider", lambda *a, **k: _NoApplierProvider())
        rc = policy_apply.run(_make_args(bindings_file, mode="enforce"), logger)
        assert rc == 0
        # Flatten Rich line-wrapping before asserting on the message.
        flat = " ".join(capsys.readouterr().out.split()).lower()
        assert "no policy bindings were enforced" in flat
        assert "stage 7" in flat
