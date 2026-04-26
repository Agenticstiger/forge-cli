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

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fluid_build.cli.forge_copilot_memory import CopilotMemoryStore, CopilotProjectMemory
from fluid_build.cli.memory_cmd import run


def test_memory_cmd_save_and_status_use_configured_sqlite_backend(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("FLUID_STORE_PATH", str(tmp_path / "memory.sqlite3"))

    store = CopilotMemoryStore(tmp_path)
    store.save(
        CopilotProjectMemory(
            schema_version=1,
            saved_at="2026-04-23T00:00:00+00:00",
            project_profile={"template": "analytics", "provider": "local"},
            conventions={"build_engines": ["sql"]},
            recent_outcomes=[],
        )
    )

    logger = SimpleNamespace()
    result = run(SimpleNamespace(memory_action="save", scope="project"), logger)
    assert result == 0

    status = run(SimpleNamespace(memory_action="status"), logger)
    assert status == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("{") :])
    assert payload["backend"] == "SqliteBackend"
    assert payload["namespaces"]["memory/project"] == 1


def test_memory_cmd_search_reads_semantic_namespace_from_configured_store(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("FLUID_STORE_PATH", str(tmp_path / "semantic.sqlite3"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put(
        "memory/semantic",
        "orders",
        {"description": "customer orders revenue model"},
    )

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(
            memory_action="search",
            query="orders revenue",
            ns="memory/semantic",
            mode="keyword",
        ),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("[") :])
    assert payload[0]["description"] == "customer orders revenue model"


# ---------------------------------------------------------------------
# V2.2 — extended ``show`` scopes (episodic / semantic / history)
# ---------------------------------------------------------------------


def test_memory_cmd_show_episodic_lists_namespace_records(tmp_path: Path, monkeypatch, capsys):
    """``fluid memory show episodic`` lists records under the
    ``memory/episodic`` namespace with metadata + value previews. The
    plan promised episodic memory as a v1.2 deliverable; surfacing it
    via the same ``memory show <scope>`` UX keeps the CLI consistent
    with project / team / personal."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put(
        "memory/episodic",
        "2026-04-25T10:00:00",
        {"event": "forged retail dimensional", "score": 9},
        metadata={"technique": "dimensional"},
    )

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(memory_action="show", scope="episodic", limit=20),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("[") :])
    assert len(payload) == 1
    assert payload[0]["key"] == "2026-04-25T10:00:00"
    assert payload[0]["metadata"] == {"technique": "dimensional"}
    # Value preview must surface the top-level fields without
    # dumping the whole payload.
    preview = payload[0]["value_preview"]
    assert "event" in preview


def test_memory_cmd_show_semantic_lists_namespace_records(tmp_path: Path, monkeypatch, capsys):
    """Same as above for ``memory/semantic`` — the auto-write target
    when ``FLUID_COPILOT_SEMANTIC_MEMORY=1`` is set on a successful
    forge."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put(
        "memory/semantic",
        "customer_orders.abcd1234",
        {
            "name": "customer_orders",
            "technique": "dimensional",
            "description": "Order facts and customer dimensions",
        },
        metadata={"source_type": "intent"},
    )

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(memory_action="show", scope="semantic", limit=20),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("[") :])
    assert payload[0]["key"] == "customer_orders.abcd1234"
    assert payload[0]["metadata"]["source_type"] == "intent"


def test_memory_cmd_show_history_lists_versions(tmp_path: Path, monkeypatch, capsys):
    """``history`` namespace stores per-artifact versioned snapshots
    written by ``archive_snapshot``. Listing them via ``memory show
    history`` lets a user audit "what did this contract look like 3
    versions ago" without grepping the store directory by hand."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put(
        "history",
        "contract_abc.v1",
        {"contract": {"id": "x"}, "version": 1},
    )
    staged_store.put(
        "history",
        "contract_abc.v2",
        {"contract": {"id": "x"}, "version": 2},
    )

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(memory_action="show", scope="history", limit=20),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("[") :])
    assert len(payload) == 2
    assert {entry["key"] for entry in payload} == {
        "contract_abc.v1",
        "contract_abc.v2",
    }


def test_memory_cmd_show_respects_limit(tmp_path: Path, monkeypatch, capsys):
    """``--limit N`` caps the number of records returned. Without
    this, the listing would be unbounded — fine for ten records,
    catastrophic for ten million."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    for i in range(10):
        staged_store.put("memory/semantic", f"key_{i:02d}", {"description": f"record {i}"})

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(memory_action="show", scope="semantic", limit=3),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("[") :])
    assert len(payload) == 3


# ---------------------------------------------------------------------
# V2.2 — ``clear --older-than`` TTL pruning
# ---------------------------------------------------------------------


def test_memory_cmd_clear_older_than_removes_only_old_files(tmp_path: Path, monkeypatch, capsys):
    """``--older-than 30d`` must remove only files older than the
    threshold. Files newer than the threshold survive. Defends
    against a careless ``fluid memory clear`` accidentally wiping
    the active project's memory."""
    import os
    import time

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put("memory/episodic", "old", {"event": "old"})
    staged_store.put("memory/episodic", "new", {"event": "new"})

    # Backdate the "old" record by 60 days so a 30d threshold
    # catches it but not the "new" one.
    old_path = next(staged_store.root.rglob("old.json"))
    sixty_days_ago = time.time() - 60 * 86400
    os.utime(old_path, (sixty_days_ago, sixty_days_ago))

    logger = SimpleNamespace()
    from datetime import timedelta

    result = run(
        SimpleNamespace(
            memory_action="clear",
            ns="memory/episodic",
            older_than=timedelta(days=30),
        ),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    assert "1 record" in output

    # "new" must survive; "old" must be gone.
    survivors = list(staged_store.root.rglob("*.json"))
    survivor_names = {p.stem for p in survivors}
    assert "new" in survivor_names
    assert "old" not in survivor_names


def test_memory_cmd_clear_without_older_than_clears_all(tmp_path: Path, monkeypatch, capsys):
    """The default behaviour (no ``--older-than``) preserves the v1.0
    contract: clear every record in the namespace. The TTL flag is
    purely additive."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))

    from fluid_build.copilot.store.factory import resolve_store

    staged_store = resolve_store(workspace_root=tmp_path)
    staged_store.put("memory/episodic", "a", {})
    staged_store.put("memory/episodic", "b", {})
    staged_store.put("memory/episodic", "c", {})

    logger = SimpleNamespace()
    result = run(
        SimpleNamespace(memory_action="clear", ns="memory/episodic", older_than=None),
        logger,
    )
    assert result == 0
    output = capsys.readouterr().out
    assert "3 record" in output

    # Namespace is empty.
    survivors = list(staged_store.root.rglob("memory/episodic/*.json"))
    assert survivors == []


def test_parse_duration_accepts_documented_units():
    """The ``--older-than`` parser must accept all five unit
    suffixes (s/m/h/d/w) so a CI script using any of them isn't
    caught by surprise. Pin the contract."""
    from datetime import timedelta

    from fluid_build.cli.memory_cmd import _parse_duration

    assert _parse_duration("30s") == timedelta(seconds=30)
    assert _parse_duration("5m") == timedelta(minutes=5)
    assert _parse_duration("12h") == timedelta(hours=12)
    assert _parse_duration("30d") == timedelta(days=30)
    assert _parse_duration("2w") == timedelta(weeks=2)


def test_parse_duration_rejects_garbage():
    """Bad input must surface a clean argparse error rather than
    silently parsing as zero (which would no-op the clear and
    leave the user thinking everything was deleted)."""
    import argparse

    import pytest

    from fluid_build.cli.memory_cmd import _parse_duration

    with pytest.raises(argparse.ArgumentTypeError, match="--older-than"):
        _parse_duration("forever")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duration("30")  # missing unit
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duration("30y")  # unsupported unit (years)
