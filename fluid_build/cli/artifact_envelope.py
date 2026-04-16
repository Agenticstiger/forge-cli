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

"""Common envelope for every artifact the CLI writes.

Every file produced by init/forge/compile carries a small envelope at the
top level that answers "what is this file, who generated it, and when?"
Having a single shape across YAML contracts, JSON state files, and CI
headers means:

* PR reviewers can identify a generated file at a glance.
* Tooling can trust a ``schema_version`` and evolve the payload safely.
* ``fluid doctor`` and future migration logic have one field to inspect.

The envelope is intentionally small — four fields that never change:
``schema_version``, ``kind``, ``generated_at``, ``generated_by``.  Anything
artifact-specific (contract metadata, receipt entries, ci-state, etc.)
sits alongside the envelope, not inside it.
"""

from __future__ import annotations

__all__ = [
    "EnvelopeKind",
    "build_envelope",
    "dump_yaml_with_envelope",
    "dump_json_with_envelope",
    "utc_now_iso",
]

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover — YAML is required by other modules
    yaml = None  # type: ignore[assignment]

from fluid_build.cli.artifact_paths import ENVELOPE_SCHEMA_VERSION

LOG = logging.getLogger("fluid.cli.artifact_envelope")

#: Every artifact kind the CLI writes.  Adding a new kind is intentional —
#: keeping the set closed prevents ad-hoc kinds slipping into the receipt
#: and makes the receipt schema reviewable in one place.
EnvelopeKind = Literal[
    "WorkspaceConfig",
    "ContractMetadata",
    "PersonalMemory",
    "ProjectMemory",
    "InitReceipt",
    "ForgeReceipt",
    "CIState",
    "ContractLockfile",
    "ContractBundle",
    "SkillsCompiled",      # slice UX-J
    "DiscoveryCache",      # slice UX-J
]


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string ending in ``Z``.

    Used as the default ``generated_at`` when callers don't override it
    (e.g. in tests that want a deterministic timestamp).
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_envelope(
    *,
    kind: EnvelopeKind,
    command: str,
    tool_version: str,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the 4-field envelope every artifact carries.

    Parameters
    ----------
    kind:
        Discriminator naming the artifact (:data:`EnvelopeKind`).  The
        receipt and any future ``fluid doctor`` checks use this field to
        decide how to parse the surrounding payload.
    command:
        Human-readable command string, e.g. ``"fluid init --blank"``.  The
        caller is responsible for formatting this; the envelope never
        second-guesses or redacts it.
    tool_version:
        The ``fluid_build`` version string.  Callers should pass the value
        from ``fluid_build.__version__`` (or an empty string if the version
        is unknown — receipts still work, they just carry an empty tag).
    generated_at:
        Optional ISO 8601 UTC timestamp.  Defaults to :func:`utc_now_iso`
        so tests can inject a deterministic value.
    """
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "kind": kind,
        "generated_at": generated_at or utc_now_iso(),
        "generated_by": {
            "tool": "fluid-cli",
            "version": tool_version,
            "command": command,
        },
    }


def dump_yaml_with_envelope(
    payload: Mapping[str, Any],
    *,
    kind: EnvelopeKind,
    command: str,
    tool_version: str,
    header_comment: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Serialize *payload* as YAML with the envelope fields at the top.

    The envelope keys come first in the output so a reader hitting ``head``
    on the file instantly sees what it is.  Payload keys follow in their
    original order.

    An optional ``header_comment`` is prepended verbatim (caller is
    responsible for the ``#`` prefix and trailing newline).
    """
    if yaml is None:  # pragma: no cover — YAML is required by other modules
        raise RuntimeError(
            "PyYAML is required to serialise envelope-wrapped YAML artifacts."
        )
    envelope = build_envelope(
        kind=kind,
        command=command,
        tool_version=tool_version,
        generated_at=generated_at,
    )
    merged: Dict[str, Any] = {**envelope, **dict(payload)}
    body = yaml.dump(
        merged,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if header_comment:
        return header_comment + body
    return body


def dump_json_with_envelope(
    payload: Mapping[str, Any],
    *,
    kind: EnvelopeKind,
    command: str,
    tool_version: str,
    generated_at: Optional[str] = None,
    indent: int = 2,
    sort_keys: bool = False,
) -> str:
    """Serialize *payload* as JSON with the envelope at the top level.

    The returned string ends with a trailing newline so files round-trip
    cleanly through git and editors.
    """
    envelope = build_envelope(
        kind=kind,
        command=command,
        tool_version=tool_version,
        generated_at=generated_at,
    )
    merged: Dict[str, Any] = {**envelope, **dict(payload)}
    return json.dumps(merged, indent=indent, sort_keys=sort_keys) + "\n"
