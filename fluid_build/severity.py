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

"""Single source of truth for severity vocabulary across FLUID surfaces.

Two vocabularies meet in the validation/report layer and they are *not*
the same set of strings:

* **Contract vocabulary** — ``$defs.dqRule.severity`` in every 0.7.x
  schema is ``["info", "warn", "error", "critical"]``. Note ``warn``,
  not ``warning``, and note ``critical``.
* **Report vocabulary** — :class:`~fluid_build.providers.validation_provider.ValidationIssue`
  and the CLI renderers historically speak ``error`` / ``warning`` /
  ``info``.

Comparing a report severity with ``== "error"`` therefore silently
excludes ``critical`` (the *highest* contract severity), which made a
failing critical DQ rule exit 0. This module keeps the mapping in one
place so every gate — exit codes, ``--strict``, JUnit failures, the rich
table — classifies the same string the same way.

Deliberately dependency-free: it sits on the ``fluid --help`` cold path
via ``cli/test.py``.
"""

from __future__ import annotations

from typing import Optional

# Severities that must fail a run (non-zero exit, ``is_valid() == False``).
ERROR_SEVERITIES = frozenset({"error", "critical", "fatal"})

# Severities that are advisory but reportable, and that ``--strict``
# escalates into failures.
WARNING_SEVERITIES = frozenset({"warning", "warn"})

# Purely informational.
INFO_SEVERITIES = frozenset({"info", "debug", "notice"})

# Canonical report-vocabulary label for each accepted severity.
_CANONICAL = {
    **{s: "error" for s in ERROR_SEVERITIES},
    **{s: "warning" for s in WARNING_SEVERITIES},
    **{s: "info" for s in INFO_SEVERITIES},
}


def _key(severity: Optional[str]) -> str:
    return (severity or "").strip().lower()


def is_error(severity: Optional[str]) -> bool:
    """True when ``severity`` must fail the run.

    Covers the contract vocabulary's ``critical`` as well as ``error``.
    """
    return _key(severity) in ERROR_SEVERITIES


def is_warning(severity: Optional[str]) -> bool:
    """True when ``severity`` is advisory (``warning`` or the schema's ``warn``)."""
    return _key(severity) in WARNING_SEVERITIES


def is_info(severity: Optional[str]) -> bool:
    """True when ``severity`` is purely informational."""
    return _key(severity) in INFO_SEVERITIES


def canonical(severity: Optional[str]) -> str:
    """Map any accepted severity onto the report vocabulary.

    Unknown values fall back to ``"warning"`` — an unrecognised severity
    must never be silently downgraded to ``info`` and disappear from the
    report, and must never be promoted to a build-breaking ``error``.
    """
    return _CANONICAL.get(_key(severity), "warning")
