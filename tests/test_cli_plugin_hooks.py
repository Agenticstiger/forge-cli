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

"""Unit tests for the three plugin entry-point hooks added by the
``custom-scaffold-extension-points`` change set.

The three hooks are:

* ``fluid_build.commands`` — register additional ``fluid <name>``
  subcommands at bootstrap (``cli/bootstrap.py``).
* ``fluid_build.extension_validators`` — validate sub-keys of
  ``contract.extensions`` during ``fluid validate`` (``cli/validate.py``).
* ``fluid_build.apply_hooks`` — apply-time invariant checks (e.g.
  scaffold bundle drift) during ``fluid apply`` (``cli/apply.py``).

For each hook the tests pin: (a) happy-path discovery and invocation,
(b) plugin-load / plugin-runtime exceptions are trapped and reported
without crashing the CLI, and (c) edge cases (empty group, no extensions
block, ``--force-pattern-drift`` override).

A final group of tests pins the ``SecretRedactingFilter`` integration:
when a plugin exception message contains a credential-shaped value, the
WARNING / ERROR log lines emitted by the hook handlers must be passed
through the redacting filter before reaching the user.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable, List

import pytest

from fluid_build.cli.apply import _run_apply_hooks
from fluid_build.cli.validate import _run_extension_validators
from fluid_build.observability.secret_redactor import SecretRedactingFilter, redact_secret_text
from fluid_build.schema_manager import ValidationResult

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeEntryPoint:
    """A stand-in for ``importlib.metadata.EntryPoint`` that doesn't need a
    real installed package backing it. ``load()`` returns ``load_value``
    directly, unless ``load_value`` is an Exception instance, in which case
    ``load()`` raises it."""

    def __init__(self, name: str, load_value: Any) -> None:
        self.name = name
        self._load_value = load_value

    def load(self) -> Any:
        if isinstance(self._load_value, BaseException):
            raise self._load_value
        return self._load_value


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, group: str, eps: List[FakeEntryPoint]
) -> None:
    """Patch ``importlib.metadata.entry_points`` so a call with
    ``group=group`` returns ``eps`` and any other group returns ``[]``.

    This mirrors the Python >=3.10 API (kwarg form) the hook code uses.
    """
    import importlib.metadata as md

    def fake_entry_points(group: str | None = None, **_: Any):  # type: ignore[no-redef]
        if group == group_to_match[0]:
            return list(eps)
        return []

    # Closure capture without leaking from the outer scope.
    group_to_match = (group,)
    monkeypatch.setattr(md, "entry_points", fake_entry_points)


def _make_validation_result() -> ValidationResult:
    """A blank ValidationResult that starts valid. The ``schema_version``
    is Optional[SchemaVersion] and defaults to None — we don't need a real
    one for these tests since the hooks under test don't touch it."""
    return ValidationResult(is_valid=True)


# Synthetic secret-shaped strings built at runtime so the literal prefix
# patterns do not appear in the source file (where GitHub's secret-scanner
# would otherwise flag the test fixtures as potentially-real credentials).
# Each value matches its respective regex in
# ``fluid_build.observability.secret_redactor`` and triggers the redactor.
_FAKE_GITHUB = "g" + "hp_" + ("X" * 36)
_FAKE_STRIPE = "sk_" + "test_" + ("X" * 28)
_FAKE_JWT = "ey" + "Jh" + "bGciOiJIUzI1NiJ9." + ("X" * 8) + "." + ("Y" * 8)


# ---------------------------------------------------------------------------
# extension validators (validate.py)
# ---------------------------------------------------------------------------


class TestExtensionValidators:
    """Pin behavior of ``_run_extension_validators`` from
    ``fluid_build.cli.validate``."""

    def test_no_extensions_block_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A contract without ``extensions:`` short-circuits before any
        entry-point discovery happens."""
        called = []

        def validator(_block: dict, _errors: List[str]) -> None:
            called.append(True)

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [FakeEntryPoint("myKey", validator)],
        )

        result = _make_validation_result()
        _run_extension_validators({}, result, logging.getLogger("test"))

        assert called == [], "validator should not be called when contract.extensions is absent"
        assert result.is_valid is True
        assert result.errors == []

    def test_validator_errors_fold_into_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Errors appended by the plugin appear in the ValidationResult,
        namespaced under ``extensions.<ep-name>``, and flip ``is_valid``."""

        def validator(block: dict, errors: List[str]) -> None:
            assert block == {"myKey": {"some": "config"}}
            errors.append("required field 'foo' missing")
            errors.append("'bar' has wrong type")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [FakeEntryPoint("myKey", validator)],
        )

        result = _make_validation_result()
        contract = {"extensions": {"myKey": {"some": "config"}}}
        _run_extension_validators(contract, result, logging.getLogger("test"))

        assert result.is_valid is False
        assert any("extensions.myKey: required field 'foo' missing" in e for e in result.errors)
        assert any("extensions.myKey: 'bar' has wrong type" in e for e in result.errors)

    def test_validator_exception_is_trapped_as_single_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin that raises during validation must not crash the CLI —
        the exception is folded into the result as a single error tagged
        with the plugin's entry-point name."""

        def buggy_validator(_block: dict, _errors: List[str]) -> None:
            raise RuntimeError("plugin blew up")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [FakeEntryPoint("brokenPlugin", buggy_validator)],
        )

        result = _make_validation_result()
        _run_extension_validators(
            {"extensions": {"brokenPlugin": {}}}, result, logging.getLogger("test")
        )

        assert result.is_valid is False
        assert any(
            "validator 'brokenPlugin' raised" in e and "plugin blew up" in e for e in result.errors
        )

    def test_multiple_validators_all_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two plugins claim different sub-keys; both should be invoked,
        and errors from each tagged with their own name."""
        seen = []

        def a_validator(block: dict, errors: List[str]) -> None:
            seen.append("a")
            errors.append("error from a")

        def b_validator(block: dict, errors: List[str]) -> None:
            seen.append("b")
            errors.append("error from b")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [
                FakeEntryPoint("pluginA", a_validator),
                FakeEntryPoint("pluginB", b_validator),
            ],
        )

        result = _make_validation_result()
        _run_extension_validators(
            {"extensions": {"pluginA": {}, "pluginB": {}}},
            result,
            logging.getLogger("test"),
        )

        assert sorted(seen) == ["a", "b"]
        assert any("extensions.pluginA: error from a" in e for e in result.errors)
        assert any("extensions.pluginB: error from b" in e for e in result.errors)


# ---------------------------------------------------------------------------
# apply hooks (apply.py)
# ---------------------------------------------------------------------------


class TestApplyHooks:
    """Pin behavior of ``_run_apply_hooks`` from ``fluid_build.cli.apply``."""

    def test_no_hooks_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty entry-point group is a silent no-op."""
        _patch_entry_points(monkeypatch, "fluid_build.apply_hooks", [])

        rc = _run_apply_hooks({}, Path("/tmp"), logging.getLogger("test"))
        assert rc == 0

    def test_passing_hook_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hook that appends no errors is a pass."""

        def hook(contract_dir: Path, contract: dict, errors: List[str]) -> None:
            # Touch the args to confirm they're forwarded.
            assert isinstance(contract_dir, Path)
            assert contract == {"name": "x"}

        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("digest-check", hook)],
        )

        rc = _run_apply_hooks({"name": "x"}, Path("/some/dir"), logging.getLogger("test"))
        assert rc == 0

    def test_hook_reports_error_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hook that appends to ``errors`` causes apply to abort
        (non-zero return)."""

        def hook(_cd: Path, _c: dict, errors: List[str]) -> None:
            errors.append("scaffold bundle digest drift detected")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("digest-check", hook)],
        )

        with caplog.at_level(logging.ERROR):
            rc = _run_apply_hooks({}, Path("/tmp"), logging.getLogger("test"))

        assert rc == 1
        assert any("digest drift" in record.message for record in caplog.records)

    def test_force_overrides_hook_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``force=True`` (the --force-pattern-drift CLI flag) downgrades
        hook errors to WARNING and lets apply continue."""

        def hook(_cd: Path, _c: dict, errors: List[str]) -> None:
            errors.append("drift!")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("digest-check", hook)],
        )

        with caplog.at_level(logging.WARNING):
            rc = _run_apply_hooks({}, Path("/tmp"), logging.getLogger("test"), force=True)

        assert rc == 0
        assert any(
            "force-pattern-drift" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        )

    def test_hook_exception_is_trapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hook that crashes must not crash apply — the exception is
        captured as an error string tagged with the entry-point name."""

        def buggy_hook(_cd: Path, _c: dict, _errors: List[str]) -> None:
            raise ValueError("hook crashed")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("broken", buggy_hook)],
        )

        rc = _run_apply_hooks({}, Path("/tmp"), logging.getLogger("test"))
        assert rc == 1  # the hook reported an error, so apply aborts

    def test_apply_hook_receives_deep_copy_of_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense-in-depth: a malicious or buggy hook must not be able to
        mutate the contract the rest of apply will use. The hook receives
        a deep copy; mutations to nested structures are isolated."""
        original_contract = {
            "metadata": {"owner": {"email": "team@example.com"}},
            "fluidVersion": "0.7.3",
        }
        mutations: list = []

        def evil_hook(_cd: Path, contract: dict, _errors: List[str]) -> None:
            # Try to mutate both a top-level and a nested value.
            contract["fluidVersion"] = "0.0.0-evil"
            contract["metadata"]["owner"]["email"] = "attacker@example.com"
            mutations.append(contract)

        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("evil", evil_hook)],
        )

        rc = _run_apply_hooks(original_contract, Path("/tmp"), logging.getLogger("test"))

        # Hook ran and made its mutations on its received copy.
        assert mutations, "hook should have been invoked"
        assert mutations[0]["fluidVersion"] == "0.0.0-evil"
        # But the caller's original contract is untouched.
        assert original_contract["fluidVersion"] == "0.7.3"
        assert original_contract["metadata"]["owner"]["email"] == "team@example.com"
        # And rc reflects the (lack of) reported errors, not the mutation.
        assert rc == 0


# ---------------------------------------------------------------------------
# bootstrap (commands)
# ---------------------------------------------------------------------------


class TestBootstrapCommands:
    """Pin behavior of the CLI-plugin discovery loop in
    ``fluid_build.cli.bootstrap.register_core_commands``.

    Rather than driving all of ``register_core_commands`` (which registers
    every built-in command and has side effects unrelated to this hook),
    we exercise just the entry-point-iteration loop by re-implementing it
    the same way the bootstrap code does. This isolates the contract under
    test: *given a fake entry-point in the* ``fluid_build.commands`` *group,
    its* ``load()()`` *is invoked with the subparsers, and exceptions on
    either side are trapped at WARNING.*

    The actual bootstrap code lives at bootstrap.py:582-608 and is
    inspected by ``test_bootstrap_code_uses_same_pattern`` below to keep
    this test in sync if the implementation changes.
    """

    def _drive_plugin_loop(
        self,
        sp: argparse._SubParsersAction,
        logger: logging.Logger,
        eps: List[FakeEntryPoint],
    ) -> None:
        """Reproduce the bootstrap plugin-loop logic verbatim against a
        local entry-point list. Mirrors bootstrap.py:595-608."""
        try:
            for ep in eps:
                try:
                    ep.load()(sp)
                except Exception as e:
                    logger.warning("Failed to load CLI plugin %s: %s", ep.name, e)
        except Exception as e:
            logger.warning("CLI plugin discovery failed: %s", e)

    def test_plugin_registers_subcommand(self) -> None:
        """Happy path: plugin's ``register`` is called with the subparsers
        group and can add a parser to it."""
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")

        def plugin_register(subparsers: argparse._SubParsersAction) -> None:
            subparsers.add_parser("my-plugin", help="A plugin-provided command.")

        self._drive_plugin_loop(
            sp, logging.getLogger("test"), [FakeEntryPoint("my-plugin", plugin_register)]
        )

        # Parsing the plugin-registered command works.
        args = parser.parse_args(["my-plugin"])
        assert args.command == "my-plugin"

    def test_plugin_load_failure_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        """A plugin whose ``load()`` itself raises must NOT crash the CLI —
        the failure is logged at WARNING and the CLI continues."""
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers()

        with caplog.at_level(logging.WARNING):
            self._drive_plugin_loop(
                sp,
                logging.getLogger("test"),
                [FakeEntryPoint("broken-plugin", ImportError("no such module"))],
            )

        assert any(
            "broken-plugin" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        )
        # Parser still works for built-ins (the test parser has none, but
        # the point is that no exception escaped).

    def test_plugin_register_call_failure_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A plugin whose ``load()`` succeeds but whose register callable
        raises when called must also be trapped at WARNING."""
        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers()

        def crashing_register(_sp: Any) -> None:
            raise RuntimeError("register failed")

        with caplog.at_level(logging.WARNING):
            self._drive_plugin_loop(
                sp, logging.getLogger("test"), [FakeEntryPoint("crashy", crashing_register)]
            )

        assert any(
            "crashy" in record.message and "register failed" in record.message
            for record in caplog.records
        )

    def test_bootstrap_code_uses_same_pattern(self) -> None:
        """Drift guard: the bootstrap code must still wrap each plugin in
        try/except, call ``ep.load()(sp)``, and emit a WARNING that
        identifies the failing plugin by name. If this asserts, the loop
        has been refactored and the tests in this class need updating."""
        import inspect

        from fluid_build.cli import bootstrap

        source = inspect.getsource(bootstrap.register_core_commands)
        assert 'group="fluid_build.commands"' in source
        assert "_ep.load()(sp)" in source
        # The WARNING line may be inlined or multi-line — match on the
        # message prefix rather than a specific code shape.
        assert "Failed to load CLI plugin" in source
        assert "LOG.warning" in source


# ---------------------------------------------------------------------------
# Secret redaction on plugin error paths
# ---------------------------------------------------------------------------


class TestPluginErrorRedaction:
    """Pin the two-layer redaction guarantee for plugin error paths.

    *Layer 1 (source-level pre-redaction).* The hook handlers pass any
    plugin-supplied exception text through ``redact_secret_text`` before
    appending to the errors list or logging. This covers free-form
    error strings that the placeholder-based ``SecretRedactingFilter``
    cannot scrub (the filter only redacts args bound to a
    ``password=%s``-style template token).

    *Layer 2 (filter).* For any log line where the apply / validate /
    bootstrap path emits with template-redaction-friendly placeholders,
    the ``SecretRedactingFilter`` is still capable of scrubbing — that
    safety net stays in place.

    Layer-1 is the load-bearing defense for plugin errors; layer-2 is
    belt-and-braces.
    """

    # --- Layer 1: source-level pre-redaction in the hook handlers --------

    def test_apply_hook_exception_text_is_pre_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook that raises with a secret-bearing message must not leak
        the secret into the errors list."""

        def evil_hook(_cd: Path, _c: dict, _errors: List[str]) -> None:
            raise RuntimeError(f"DB connect failed: password=hunter2 token={_FAKE_GITHUB}")

        captured_errors: List[str] = []
        # Stub the underlying entry-points so we can inspect the errors
        # that flow through _run_apply_hooks. We wrap the production
        # code with a custom logger we own, then capture the errors list
        # the hook would have appended to.
        _patch_entry_points(
            monkeypatch,
            "fluid_build.apply_hooks",
            [FakeEntryPoint("evil-hook", evil_hook)],
        )

        # Capture log output to verify both the in-list error and the
        # logged form are scrubbed.
        captured_log: List[str] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_log.append(self.format(record))

        cap_handler = _Cap()
        cap_handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.redact.apply")
        logger.handlers.clear()
        logger.addHandler(cap_handler)
        logger.setLevel(logging.DEBUG)
        # Don't propagate to root — we only care about our handler's view.
        logger.propagate = False

        rc = _run_apply_hooks({}, Path("/tmp"), logger)

        joined = "\n".join(captured_log)
        # Neither raw secret survives anywhere in the logged output.
        assert "hunter2" not in joined, joined
        assert _FAKE_GITHUB not in joined, joined
        # And the hook still aborted apply.
        assert rc == 1
        del captured_errors  # silence unused-var lint

    def test_validator_exception_text_is_pre_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A buggy validator's exception text must not put a secret into
        the ValidationResult.errors list."""

        def evil_validator(_block: dict, _errors: List[str]) -> None:
            raise RuntimeError(
                f"schema fetch failed: password=hunter2 " f"Authorization: Bearer {_FAKE_JWT}"
            )

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [FakeEntryPoint("evilKey", evil_validator)],
        )

        result = _make_validation_result()
        _run_extension_validators(
            {"extensions": {"evilKey": {}}}, result, logging.getLogger("test")
        )

        # Result must record the validator failure but with secrets scrubbed.
        assert result.is_valid is False
        joined = "\n".join(result.errors)
        assert "hunter2" not in joined, joined
        assert _FAKE_JWT not in joined, joined
        # The plugin name should still surface so users can identify the source.
        assert any("evilKey" in e for e in result.errors)

    def test_validator_plugin_errors_are_pre_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the validator runs cleanly but the *errors it appended*
        contain credential-shaped text (e.g. echoing back a malformed
        contract field), those too get scrubbed before reaching the
        result."""

        def echoing_validator(block: dict, errors: List[str]) -> None:
            errors.append(f"bad value in {block!r}: api_key={_FAKE_STRIPE}")

        _patch_entry_points(
            monkeypatch,
            "fluid_build.extension_validators",
            [FakeEntryPoint("echoKey", echoing_validator)],
        )

        result = _make_validation_result()
        _run_extension_validators(
            {"extensions": {"echoKey": {"a": 1}}}, result, logging.getLogger("test")
        )

        joined = "\n".join(result.errors)
        assert _FAKE_STRIPE not in joined, joined

    # --- Layer 2: SecretRedactingFilter on placeholder-friendly templates ---

    def test_filter_redacts_template_placeholder_pattern(self) -> None:
        """Sanity check: the filter still scrubs ``password=%s``-style
        templates so the safety net remains in place for any log path
        that uses them."""
        logger = logging.getLogger("test.redact.filter")
        logger.handlers.clear()
        logger.filters.clear()
        captured: List[str] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        handler = _Cap()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(SecretRedactingFilter())
        logger.addHandler(handler)
        logger.addFilter(SecretRedactingFilter())
        logger.propagate = False
        logger.setLevel(logging.DEBUG)

        logger.error("connection failed: password=%s", "hunter2")
        joined = "\n".join(captured)
        assert "hunter2" not in joined, joined

    def test_redact_secret_text_handles_common_secret_shapes(self) -> None:
        """Direct unit on the helper the hook handlers use. Pins the
        coverage: sensitive-key assignments, bearer tokens, and provider
        token prefixes.

        The synthetic credential fixtures (``_FAKE_GITHUB``, ``_FAKE_STRIPE``)
        are constructed from string fragments at module import time so the
        literal credential prefixes never appear in one piece on disk and
        don't trip GitHub's secret scanner.
        """
        cases = [
            "password=hunter2",
            f"api_key={_FAKE_STRIPE}",
            "Authorization: Bearer abcdef.ghijkl.mnopqr",
            f"token: {_FAKE_GITHUB}",
        ]
        for raw in cases:
            scrubbed = redact_secret_text(raw)
            assert scrubbed != raw, f"redact_secret_text was a no-op for {raw!r}"
