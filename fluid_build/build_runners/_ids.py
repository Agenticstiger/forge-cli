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

# A permissive regex, identical to ``validate_ident`` in
# ``providers/_sql_safety``: alphanumerics + dots + dashes + underscores,
# starting with a letter or underscore, capped at a sane length to avoid
# DOS-via-filename attacks. Crucially this excludes ``/``, ``\``, ``..``
# (a leading dot is rejected) and any path separator, so a validated id
# can never traverse out of the directory it is joined into.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")


class IdentifierViolation(ValueError):
    """Raised when a contract identifier fails validation."""


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
            "attacker-controlled contract metadata."
        )
    return value
