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

"""Shared identifier validation for build_runners.

``contract.id`` and ``build.id`` flow into filesystem paths
(``.fluid/runs/<product-id>/<build-id>/`` in
:mod:`fluid_build.build_runners._state`,
``.fluid/artifacts/<id>/`` and ``.fluid/policies/<id>/`` in
:mod:`fluid_build.cli._acquisition_stage_ext`) and into inline f-string
interpolation into Airflow / Dagster / Prefect Python + cron entries.
A single permissive-but-safe identifier grammar is enforced at every one
of those boundaries so attacker-controlled contract metadata cannot
escape the workspace via ``..`` / absolute paths or inject code/shell
into a generated artifact.

This module is a **tier-neutral leaf** under ``build_runners``: it has no
``fluid_build`` upstreams beyond the stdlib so both the runtime
chokepoint (``build_runners.base``) and the pipeline-stage extension
(``cli._acquisition_stage_ext``) can import it without creating a
``cli`` ↔ ``build_runners`` reverse edge. ``cli`` → ``build_runners`` is
an existing allowed edge (``cli/apply.py`` already imports
``build_runners.run_builds_from_args``).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# A permissive regex, identical to ``validate_ident`` in
# ``providers/_sql_safety``: alphanumerics + dots + dashes + underscores,
# starting with a letter or underscore, capped at a sane length to avoid
# DOS-via-filename attacks. Crucially this excludes ``/``, ``\``, ``..``
# (a leading dot is rejected) and any path separator, so a validated id
# can never traverse out of the directory it is joined into.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}\Z")

# :data:`_IDENT_RE` in words, for a message that tells an author what to change.
IDENT_RULE = (
    "start with a letter or underscore, use only letters, digits, '_', '.' and '-', "
    "and be at most 128 characters"
)


class IdentifierViolation(ValueError):
    """Raised when a contract identifier fails validation.

    ``kind`` and ``value`` name the id that failed when the raiser knows it
    (:func:`validate_identifier` always does), so a caller can report it
    without parsing the message.
    """

    def __init__(self, message: str, *, kind: Optional[str] = None, value: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.value = value


def validate_identifier(value: str, *, kind: str) -> str:
    """Validate a contract/build identifier; return it unchanged on success.

    Raises :class:`IdentifierViolation` (a ``ValueError`` subclass) when
    ``value`` is not a string or does not match :data:`_IDENT_RE`. The
    guard blocks template-injection / path-traversal attacks via
    attacker-controlled contract metadata (e.g. an ``id`` like
    ``../../../../tmp/escape`` or an absolute path).
    """
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise IdentifierViolation(
            f"{kind} {value!r} is not a valid identifier "
            "(must match ^[A-Za-z_][A-Za-z0-9_.\\-]{0,127}$). This guard "
            "blocks template-injection / path-traversal attacks via "
            "attacker-controlled contract metadata.",
            kind=kind,
            value=value,
        )
    return value


def validate_build_ids(contract: Mapping[str, Any]) -> None:
    """Validate the ids a contract's build phase uses: ``contract.id`` and each ``builds[].id``.

    The build runner (``base.run_builds_from_args``) runs this before any
    build, and ``fluid apply`` runs it before it applies anything, so the two
    refuse the same ids. A contract with no builds has no build phase and is
    not checked. An empty or absent id is skipped: it cannot traverse, and the
    state store falls back to a safe default. A ``builds`` entry that is not a
    mapping is left to the schema gate.

    Raises :class:`IdentifierViolation` for the first id that fails.
    """
    builds = contract.get("builds") or []
    if not builds:
        return
    if contract.get("id"):
        validate_identifier(contract["id"], kind="contract.id")
    for build in builds:
        if isinstance(build, Mapping) and build.get("id"):
            validate_identifier(build["id"], kind="build.id")


def build_id_warning(contract: Mapping[str, Any]) -> Optional[str]:
    """The warning ``fluid validate`` and ``fluid plan`` give for an id the build phase refuses.

    The schema's ``$defs.identifier`` admits more than :data:`_IDENT_RE` (a
    leading digit, any length), and the two stay different by decision. So
    validate and plan accept such an id and warn, and ``fluid apply`` in a
    build mode refuses it. The check is :func:`validate_build_ids`, so the
    warning names the id apply would name. Returns ``None`` when it passes.

    The text does not say the schema accepted the id: validate also runs this
    on a contract the schema refused, such as one whose ``id`` is ``123``.
    """
    try:
        validate_build_ids(contract)
    except IdentifierViolation as exc:
        return (
            f"{exc.kind} {exc.value!r} cannot be used by the build phase, which puts it in "
            f"directory names and generated code, so it must {IDENT_RULE}. fluid apply in a "
            "build mode (amend-and-build, replace-and-build) refuses this id before it "
            "applies anything. Rename it to fit."
        )
    return None
