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

"""The destructive gate's confirm primitive — fail closed, always.

``cli/_preview_panel.confirm`` is fail-**open** on a non-TTY (it returns
True so CI doesn't hang on a cost preview). That posture is right for
"here is what this will cost"; it is unacceptable for "this edit deletes
three columns". :func:`confirm_fail_closed` is the inverted primitive
(RFC-deep-agents.md, "Gate mechanics"):

- non-TTY / unavailable stdin → **reject**, and emit the structured
  ``mission_destructive_gate_rejected`` WARNING (audit-trail posture,
  same as the OpenTofu ``--allow-data-loss`` override event);
- only an explicit affirmative answer approves;
- there is **no** ``auto_yes`` parameter and **no**
  ``FLUID_MISSION_AUTO_APPROVE`` env var. ``--yes`` cannot reach this
  function, so "``--yes`` never approves a destructive diff" is
  structural rather than a rule someone has to remember. The
  alternative — silent destructive approval in CI — is worse than the
  "why is it prompting?" reports this will generate.

Borrowed posture: Terraform states this rule canonically and keeps the
two concerns on **two separate flags** — ``-input=false`` means "no
interactive user, so conservatively assume you do not wish to apply",
while ``-auto-approve`` is a distinct, explicit opt-in. Non-TTY never
implies yes. We ship only the first half of that pair on purpose;
there is no mission-level ``-auto-approve``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional, Sequence

LOG = logging.getLogger("fluid.copilot.missions.gate")

#: Emitted whenever a destructive diff is refused. Stable event tag for
#: CI log parsers (same contract as ``mission_untrusted_spec_refused``).
GATE_REJECTED_EVENT = "mission_destructive_gate_rejected"
GATE_APPROVED_EVENT = "mission_destructive_gate_approved"

_AFFIRMATIVE = frozenset({"y", "yes"})


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 — a stdin that can't be probed is not a TTY
        return False


def confirm_fail_closed(
    summary_lines: Sequence[str],
    *,
    mission: str = "",
    step: str = "",
    input_fn: Optional[Callable[[str], str]] = None,
    printer: Optional[Callable[[str], Any]] = None,
) -> bool:
    """Ask the operator to approve a destructive diff. Default: NO.

    Returns True only when an interactive operator explicitly answers
    yes. Every other path — no TTY, EOF, Ctrl-C, OSError on stdin, an
    empty answer, anything that isn't ``y``/``yes`` — returns False and
    logs :data:`GATE_REJECTED_EVENT`.

    ``input_fn`` is the test seam (supplying it bypasses the TTY probe,
    exactly like an interactive session would behave).
    """
    emit = printer or print
    interactive = input_fn is not None or _stdin_is_tty()

    if not interactive:
        LOG.warning(
            GATE_REJECTED_EVENT,
            extra={
                "mission": mission,
                "step": step,
                "reason": "non_interactive",
                "findings": list(summary_lines)[:20],
            },
        )
        return False

    try:
        emit("")
        emit("DESTRUCTIVE CHANGE — this step removes or loosens contract content:")
        for line in summary_lines:
            emit(f"  - {line}")
        emit("")
        emit("Missions verify metadata; they cannot judge whether losing this is intended.")
        fn = input_fn or input
        answer = fn("Apply this destructive change? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError, OSError):
        LOG.warning(
            GATE_REJECTED_EVENT,
            extra={
                "mission": mission,
                "step": step,
                "reason": "prompt_unavailable",
                "findings": list(summary_lines)[:20],
            },
        )
        return False

    if answer in _AFFIRMATIVE:
        LOG.warning(
            GATE_APPROVED_EVENT,
            extra={
                "mission": mission,
                "step": step,
                "findings": list(summary_lines)[:20],
            },
        )
        return True

    LOG.warning(
        GATE_REJECTED_EVENT,
        extra={
            "mission": mission,
            "step": step,
            "reason": "declined",
            "findings": list(summary_lines)[:20],
        },
    )
    return False


def reject_destructive(
    summary_lines: Sequence[str],
    *,
    mission: str = "",
    step: str = "",
    reason: str = "gates_destructive_deny",
) -> bool:
    """Refuse outright (``gates.destructive: deny``) with the audit event.

    Always returns False — it exists so the ``deny`` mode emits the same
    structured event shape as the interactive refusal path.
    """
    LOG.warning(
        GATE_REJECTED_EVENT,
        extra={
            "mission": mission,
            "step": step,
            "reason": reason,
            "findings": list(summary_lines)[:20],
        },
    )
    return False


__all__ = [
    "GATE_APPROVED_EVENT",
    "GATE_REJECTED_EVENT",
    "confirm_fail_closed",
    "reject_destructive",
]
