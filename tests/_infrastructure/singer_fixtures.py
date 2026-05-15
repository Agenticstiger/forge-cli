# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Synthetic Singer tap for tests.

Real Singer taps (``tap-postgres``, ``tap-stripe``, …) require pipx install
and external services. For unit + matrix testing, this fixture builds a tiny
``tap-fluid-fake`` Python script on disk that emits SCHEMA / RECORD / STATE
messages exactly per the Singer protocol.

The fixture writes the tap to a tmp dir and prepends it to PATH for the test,
so ``shutil.which("tap-fluid-fake")`` finds it.
"""

from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

_TAP_FAKE_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    \"\"\"tap-fluid-fake — emits a fixed set of Singer messages for tests.\"\"\"
    import argparse, json, sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=False)
    parser.add_argument("--catalog", required=False)
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    n_records = int(config.get("n_records", 3))
    fail = bool(config.get("fail", False))
    if fail:
        sys.stderr.write("simulated tap failure\\n")
        sys.exit(2)

    streams = config.get("streams") or ["orders"]

    state = {}
    if args.state:
        try:
            with open(args.state) as f:
                state = json.load(f)
        except Exception:
            state = {}

    for stream in streams:
        sys.stdout.write(json.dumps({
            "type": "SCHEMA",
            "stream": stream,
            "schema": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "label": {"type": "string"},
            }},
            "key_properties": ["id"],
        }) + "\\n")
        for i in range(1, n_records + 1):
            sys.stdout.write(json.dumps({
                "type": "RECORD",
                "stream": stream,
                "record": {"id": i, "label": f"{stream}-{i}"},
            }) + "\\n")

    new_state = dict(state)
    new_state.setdefault("bookmarks", {})
    for stream in streams:
        new_state["bookmarks"][stream] = {"last_id": n_records}
    sys.stdout.write(json.dumps({"type": "STATE", "value": new_state}) + "\\n")
    sys.exit(0)
    """
)


@pytest.fixture
def fake_singer_tap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, Any]]:
    """Install a synthetic ``tap-fluid-fake`` on PATH.

    Yields a dict with ``binary`` (absolute path) and ``name`` (``tap-fluid-fake``).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tap_path = bin_dir / "tap-fluid-fake"
    tap_path.write_text(_TAP_FAKE_SCRIPT, encoding="utf-8")
    tap_path.chmod(tap_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield {"binary": str(tap_path), "name": "tap-fluid-fake"}
