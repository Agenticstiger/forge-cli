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

"""Direnv-style trust pinning for workspace mission specs.

A mission spec configures autonomous execution (tool allowlist, gate
mode, budgets, planner goal text), so a cloned repo shipping
``.fluid/missions/`` must not silently control any of that. The rule
(RFC-deep-agents.md, "Security & governance"):

- **Built-ins** (shipped package data) and **user-global** specs
  (``~/.fluid/missions/`` — user-authored, outside any repo) are
  trusted implicitly.
- **Everything else** — workspace ``.fluid/missions/`` and arbitrary
  paths — requires an explicit one-time approval, pinned by the
  sha256 of the file's bytes in ``~/.fluid/mission_trust.json``
  (direnv's ``allow`` model: a changed file requires re-approval).
- **Fail closed.** An unpinned or changed spec refuses with a typed
  :class:`MissionTrustError` and a structured
  ``mission_untrusted_spec_refused`` WARNING (audit-trail posture,
  same as the OpenTofu ``--allow-data-loss`` override event). There
  is no bypass env var; ``fluid mission trust <spec>`` is the only
  way to approve.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.copilot.missions.spec import (
    BUILTIN_MISSIONS_DIR,
    USER_MISSIONS_DIR_NAME,
    MissionSpec,
)

LOG = logging.getLogger("fluid.copilot.missions.trust")

TRUST_FILE_NAME = "mission_trust.json"
_TRUST_DB_VERSION = 1

#: Trust statuses. ``builtin`` / ``user_global`` / ``pinned`` are runnable;
#: ``untrusted`` (never approved) and ``changed`` (approved bytes differ)
#: are refused.
TRUSTED_STATUSES = frozenset({"builtin", "user_global", "pinned"})


class MissionTrustError(RuntimeError):
    """Raised (fail-closed) when a mission spec is not trusted.

    Carries ``status`` (``untrusted`` | ``changed``) and ``spec_path``
    so callers can render a precise remediation message.
    """

    def __init__(self, message: str, *, status: str, spec_path: Optional[Path]) -> None:
        super().__init__(message)
        self.status = status
        self.spec_path = spec_path


def trust_file_path() -> Path:
    """Location of the trust database (``<user-home>/mission_trust.json``).

    Anchored on :func:`fluid_build.paths.user_home` so ``$FLUID_USER_HOME``
    isolation (containers, tests) works with zero extra wiring.
    """
    from fluid_build.paths import user_home

    return user_home() / TRUST_FILE_NAME


def _load_trust_db(path: Optional[Path] = None) -> Dict[str, Any]:
    db_path = path or trust_file_path()
    if not db_path.is_file():
        return {"version": _TRUST_DB_VERSION, "trusted": {}}
    try:
        payload = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Fail closed: an unreadable trust db trusts nothing, but must not
        # crash `fluid mission list`. The refusal path surfaces the problem.
        LOG.warning(
            "mission_trust_db_unreadable",
            extra={"path": str(db_path), "error": type(exc).__name__},
        )
        return {"version": _TRUST_DB_VERSION, "trusted": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("trusted"), dict):
        LOG.warning("mission_trust_db_malformed", extra={"path": str(db_path)})
        return {"version": _TRUST_DB_VERSION, "trusted": {}}
    return payload


def _save_trust_db(db: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    db_path = path or trust_file_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:  # best-effort private perms, same posture as credentials/encrypted_store
        os.chmod(db_path.parent, 0o700)
    except OSError:  # pragma: no cover — non-POSIX / read-only parent
        pass
    # Atomic replace so a crash mid-write can never truncate the db.
    fd, tmp_name = tempfile.mkstemp(dir=str(db_path.parent), prefix=".mission_trust-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(db, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, db_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover — already gone
            pass
        raise
    try:
        os.chmod(db_path, 0o600)
    except OSError:  # pragma: no cover — non-POSIX
        pass


def _is_user_global(spec_path: Path) -> bool:
    from fluid_build.paths import user_home

    try:
        spec_path.relative_to(user_home() / USER_MISSIONS_DIR_NAME)
        return True
    except ValueError:
        return False


def spec_trust_status(spec: MissionSpec) -> str:
    """Classify *spec*: ``builtin`` | ``user_global`` | ``pinned`` |
    ``untrusted`` | ``changed``."""
    if spec.builtin:
        return "builtin"
    spec_path = spec.source_path
    if spec_path is None:
        # Cannot pin what has no file identity — fail closed.
        return "untrusted"
    if spec_path.parent == BUILTIN_MISSIONS_DIR.resolve():
        return "builtin"
    if _is_user_global(spec_path):
        return "user_global"

    entry = _load_trust_db().get("trusted", {}).get(str(spec_path))
    if not isinstance(entry, dict):
        return "untrusted"
    return "pinned" if entry.get("sha256") == spec.content_sha256 else "changed"


def is_trusted(spec: MissionSpec) -> bool:
    """True when *spec* may configure autonomous behavior."""
    return spec_trust_status(spec) in TRUSTED_STATUSES


def trust_spec(spec: MissionSpec) -> Dict[str, Any]:
    """Record (or refresh) the content-hash pin for *spec*.

    Implicitly-trusted specs (built-in / user-global) are a no-op —
    the returned record says so. Returns the stored record shape:
    ``{"status", "path", "sha256", "trusted_at"}``.
    """
    status = spec_trust_status(spec)
    if status in ("builtin", "user_global"):
        return {
            "status": status,
            "path": str(spec.source_path) if spec.source_path else "",
            "sha256": spec.content_sha256,
            "trusted_at": None,
        }
    if spec.source_path is None:  # pragma: no cover — loader always sets it
        raise MissionTrustError(
            "Cannot trust a mission spec with no source file.",
            status="untrusted",
            spec_path=None,
        )

    db = _load_trust_db()
    record = {
        "sha256": spec.content_sha256,
        "name": spec.name,
        "trusted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    db.setdefault("trusted", {})[str(spec.source_path)] = record
    db["version"] = _TRUST_DB_VERSION
    _save_trust_db(db)
    LOG.info(
        "mission_spec_trusted",
        extra={"spec": spec.name, "path": str(spec.source_path), "sha256": spec.content_sha256},
    )
    return {"status": "pinned", "path": str(spec.source_path), **record}


def require_trusted(spec: MissionSpec) -> str:
    """Gate: return the trust status, or raise :class:`MissionTrustError`.

    Every mission entry point that lets a spec configure behavior calls
    this first. Refusals emit the structured
    ``mission_untrusted_spec_refused`` WARNING so audit trails catch
    attempts to run unapproved specs (e.g. in CI).
    """
    status = spec_trust_status(spec)
    if status in TRUSTED_STATUSES:
        return status

    path_text = str(spec.source_path) if spec.source_path else "(no file)"
    LOG.warning(
        "mission_untrusted_spec_refused",
        extra={
            "spec": spec.name,
            "path": path_text,
            "trust_status": status,
            "sha256": spec.content_sha256,
        },
    )
    if status == "changed":
        message = (
            f"Mission spec '{spec.name}' at {path_text} has CHANGED since it was "
            "trusted (content hash differs). Review the file, then re-approve with: "
            f"fluid mission trust {path_text}"
        )
    else:
        message = (
            f"Mission spec '{spec.name}' at {path_text} is not trusted. Workspace "
            "mission specs configure autonomous behavior, so they require one-time "
            "approval (direnv-style). Review the file, then run: "
            f"fluid mission trust {path_text}"
        )
    raise MissionTrustError(message, status=status, spec_path=spec.source_path)


__all__ = [
    "MissionTrustError",
    "TRUSTED_STATUSES",
    "TRUST_FILE_NAME",
    "is_trusted",
    "require_trusted",
    "spec_trust_status",
    "trust_file_path",
    "trust_spec",
]
