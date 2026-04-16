# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Module-level mtime-keyed cache for compiled skills (slice UX-J).

This follows the exact pattern established by slice UX-G for
``_SAMPLE_CACHE`` in ``forge_copilot_schema_inference.py`` and
``_CAPABILITY_MATRIX_CACHE`` in ``forge_copilot_runtime.py``:
module-scope dict + ``threading.Lock`` + ``(path, mtime_ns, size)``
cache key + ``clear_*()`` invalidation hook.

The compiled skills payload is a small JSON dict (~200-400 bytes)
holding the prompt-relevant fields extracted from the raw
``.fluid/skills.yaml`` by :func:`industry_skills.compile_skill`.
Caching it here avoids re-reading + re-compiling the YAML on every
``fluid forge`` invocation in the same Python process.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("fluid.cli.forge_copilot.skills_cache")

_COMPILED_SKILLS_CACHE: Optional[Dict[str, Any]] = None
_COMPILED_SKILLS_LOCK = threading.Lock()


def clear_compiled_skills_cache() -> None:
    """Drop the process-wide compiled skills cache.

    Tests that modify ``.fluid/skills.compiled.json`` mid-run should
    call this to force the next ``load_compiled_skills`` to re-read.
    """
    global _COMPILED_SKILLS_CACHE
    with _COMPILED_SKILLS_LOCK:
        _COMPILED_SKILLS_CACHE = None


def load_compiled_skills(workspace_root: Path) -> Optional[Dict[str, Any]]:
    """Load the compiled skills payload, or ``None`` when unavailable.

    Resolution order:

    1. ``.fluid/skills.compiled.json`` (preferred — produced by
       ``fluid skills compile``).  Memoized on ``(path, mtime_ns,
       size)`` so the file is only re-read when it changes.
    2. ``.fluid/skills.yaml`` (fallback).  If present, compile
       on-the-fly using :func:`industry_skills.compile_skill` and
       emit a one-line debug log suggesting ``fluid skills compile``.
    3. ``None`` if neither file exists (skills not installed).
    """
    global _COMPILED_SKILLS_CACHE

    from fluid_build.cli.artifact_paths import (
        workspace_skills_compiled_path,
        workspace_skills_path,
    )

    compiled_path = workspace_skills_compiled_path(workspace_root)

    # --- Prefer the precompiled JSON ---
    if compiled_path.is_file():
        try:
            stat = compiled_path.stat()
            key = f"{compiled_path}:{stat.st_mtime_ns}:{stat.st_size}"
            with _COMPILED_SKILLS_LOCK:
                cached = _COMPILED_SKILLS_CACHE
                if cached and cached.get("_key") == key:
                    return cached.get("payload")
            raw = json.loads(compiled_path.read_text(encoding="utf-8"))
            payload = raw.get("compiled") or raw
            with _COMPILED_SKILLS_LOCK:
                _COMPILED_SKILLS_CACHE = {"_key": key, "payload": payload}
            return payload
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Failed to read compiled skills: %s", exc)

    # --- Fallback: compile on-the-fly from the raw YAML ---
    raw_path = workspace_skills_path(workspace_root)
    if raw_path.is_file():
        try:
            import yaml

            from fluid_build.cli.industry_skills import compile_skill

            with raw_path.open() as f:
                merged = yaml.safe_load(f) or {}
            payload = compile_skill(merged)
            LOG.debug(
                "Compiled skills on-the-fly from %s — "
                "run 'fluid skills compile' to precompile for faster startup.",
                raw_path,
            )
            return payload if payload else None
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Failed to on-the-fly compile skills: %s", exc)

    return None


def write_compiled_skills(workspace_root: Path, compiled: Dict[str, Any]) -> Path:
    """Write the compiled skills payload to ``.fluid/skills.compiled.json``.

    Returns the path written.  Uses the standard FLUID envelope so
    ``fluid doctor`` can recognize the artifact.
    """
    from fluid_build.cli.artifact_envelope import dump_json_with_envelope
    from fluid_build.cli.artifact_paths import workspace_skills_compiled_path

    out_path = workspace_skills_compiled_path(workspace_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # noqa: BLE001
        tool_version = ""

    doc = {"compiled": compiled}
    json_str = dump_json_with_envelope(
        doc,
        kind="SkillsCompiled",
        command="fluid skills compile",
        tool_version=str(tool_version),
    )
    out_path.write_text(json_str, encoding="utf-8")
    # Invalidate the in-memory cache so the next load picks up the
    # fresh file.
    clear_compiled_skills_cache()
    return out_path
