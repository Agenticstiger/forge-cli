# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""One failure condition ⇒ one stable slug, and no raw stack traces.

Two contract promises that were unmet:

* PRs #302 / #310 / #326 promised a stable error slug per failure
  condition. A missing contract file produced four different behaviours:
  ``contract_file_not_found`` (validate), ``file_not_found``
  (plan / apply — a different slug for the identical condition),
  no slug at all (test), a raw internal token (policy-check) and a bare
  log line (publish).
* ``fluid_build/_errors.py`` states "No raw stack traces in user-facing
  output unless ``--debug`` is set." ``fluid test`` and ``fluid
  policy-check`` printed one on a malformed contract either way, because
  they used ``LOG.exception`` (which emits at ERROR regardless of
  verbosity) rather than ``LOG.debug(..., exc_info=True)``.
"""

from __future__ import annotations

import argparse
import logging

import pytest

from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

_LOGGER = logging.getLogger("test-error-surface")


def _broken_contract(tmp_path):
    path = tmp_path / "broken.fluid.yaml"
    path.write_text('fluidVersion: "0.7.1"\nkind: DataProduct\nid: [unclosed\n', encoding="utf-8")
    return path


# ── one slug per condition ────────────────────────────────────────────


class TestMissingContractSlugIsShared:
    """Every command must name the same condition the same way."""

    def _slug_for_missing_contract(self, command: str, contract: str, capsys):
        """Return the stable slug the command surfaces for a missing file.

        Most commands raise ``CLIError`` and let the entry point render it;
        ``fluid validate`` renders the same typed panel itself and returns an
        exit code. Both are acceptable — the slug must be identical.
        """
        from fluid_build.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([command, contract])
        try:
            code = args.func(args, _LOGGER)
        except CLIError as exc:
            assert exc.suggestions, f"{exc.event!r} carries no catalog suggestions"
            return exc.event
        assert code != 0, f"`fluid {command}` exited 0 for a missing contract"
        rendered = capsys.readouterr()
        combined = rendered.out + rendered.err
        assert "ERR_CONTRACT_FILE_NOT_FOUND" in combined, combined
        return "contract_file_not_found"

    @pytest.mark.parametrize(
        "command", ["validate", "plan", "apply", "test", "policy-check", "verify", "publish"]
    )
    def test_missing_contract_uses_one_shared_slug(self, command, tmp_path, capsys):
        missing = str(tmp_path / "nope.fluid.yaml")
        slug = self._slug_for_missing_contract(command, missing, capsys)
        assert slug == "contract_file_not_found", (
            f"`fluid {command}` names a missing contract {slug!r}; "
            "every command must use one slug"
        )

    def test_the_slug_is_in_the_error_catalog(self):
        from fluid_build._error_catalog import enrich, slug_for

        assert slug_for("contract_file_not_found") == "ERR_CONTRACT_FILE_NOT_FOUND"
        suggestions, _docs = enrich("contract_file_not_found", None, None)
        assert suggestions


def test_not_found_event_specialises_contracts_only():
    from fluid_build.cli.security import _not_found_event

    assert _not_found_event("contract") == "contract_file_not_found"
    assert _not_found_event("report") == "file_not_found"
    assert _not_found_event("bundle") == "file_not_found"


# ── no raw stack traces ───────────────────────────────────────────────


class TestNoTracebackOnAMalformedContract:
    """A malformed contract is an ordinary user error, not a crash."""

    def test_fluid_test_renders_a_typed_error(self, tmp_path, caplog):
        from fluid_build.cli.test import run as test_run

        args = argparse.Namespace(
            contract=str(_broken_contract(tmp_path)),
            env=None,
            provider=None,
            project=None,
            region=None,
            strict=False,
            no_data=True,
            engine="native",
            output="text",
            output_file=None,
            cache=False,
            cache_ttl=0,
            cache_clear=False,
            check_drift=False,
            server=None,
            publish=None,
        )
        with caplog.at_level(logging.DEBUG, logger="fluid.cli.test"):
            with pytest.raises(CLIError) as exc_info:
                test_run(args, _LOGGER)
        assert exc_info.value.event == "contract_load_failed"
        # The trace is still available — at DEBUG, which is what --debug sets.
        assert any(r.exc_info for r in caplog.records), "the traceback must survive at DEBUG"
        # ...and must NOT have been emitted at ERROR/WARNING.
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]

    def test_fluid_policy_check_renders_a_typed_error(self, tmp_path, caplog):
        from fluid_build.cli.policy_check import run as policy_run

        args = argparse.Namespace(
            contract=str(_broken_contract(tmp_path)),
            env=None,
            category=None,
            format="json",
            output=None,
            strict=False,
            show_passed=False,
            fix=False,
        )
        logger = logging.getLogger("test-policy-check")
        with caplog.at_level(logging.DEBUG, logger="test-policy-check"):
            with pytest.raises(CLIError):
                policy_run(args, logger)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]
