# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""`fluid market --blueprints --format json` must emit PURE machine-parseable JSON.

Regression test for the gap where the blueprint-listing path ignored `--format
json` and printed the rich human table (plus a deprecation banner and registry
status lines) to stdout, contaminating any script that piped the output. Only the
catalog path honoured `--format json`.

These drive the real `market.run` entry point IN-PROCESS with stdout captured via
``redirect_stdout``. The check is just as strong as a subprocess one — the JSON is
written to ``sys.stdout`` (the redirect target), and ANY decorative print that
isn't suppressed in json mode lands in the same buffer and breaks the parse — but
it's deterministic (a subprocess variant proved flaky under CI's captured-stream /
litellm-atexit environment, where the child's stdout came back empty).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging

import pytest

from fluid_build.cli import market

pytestmark = [pytest.mark.unit]

_EXPECTED_KEYS = {"id", "name", "category", "maturity", "source", "version", "description"}


def _market_blueprints_json(search: str | None = None) -> tuple[int, str]:
    """Run `fluid market --blueprints --format json` in-process; return (rc, stdout)."""
    args = argparse.Namespace(
        blueprints=True,
        blueprint_id=None,
        instantiate=False,
        format="json",
        search=search,
        limit=20,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = market.run(args, logging.getLogger("test-market-json"))
    return rc, buf.getvalue()


def test_blueprints_json_stdout_is_pure_parseable_json() -> None:
    rc, out = _market_blueprints_json()
    assert rc == 0
    # The WHOLE captured stdout must parse — no banner / table / status contamination.
    data = json.loads(out)
    assert isinstance(data, list)
    assert data, "expected at least the bundled blueprints"
    for item in data:
        assert _EXPECTED_KEYS <= set(item), f"missing keys: {_EXPECTED_KEYS - set(item)}"


def test_blueprints_json_has_no_human_contamination() -> None:
    rc, out = _market_blueprints_json()
    assert rc == 0
    for noise in ("Searching marketplace", "deprecated", "Blueprint Marketplace (", "No registry"):
        assert noise not in out, f"human noise leaked into JSON stdout: {noise!r}"


def test_blueprints_json_respects_search_filter() -> None:
    rc, out = _market_blueprints_json(search="analytics")
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)  # may be empty, but must be a valid JSON array
