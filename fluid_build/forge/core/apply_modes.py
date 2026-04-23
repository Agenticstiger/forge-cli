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

"""Apply-mode matrix for ``fluid apply --mode``.

Six modes express every realistic deploy decision a team makes. Per the
plan (Part 1):

    ┌──────────────────────┬───────────────────────┬────────────┬───────────────────┐
    │ Mode                 │ DDL                   │ DML        │ Existing data     │
    ├──────────────────────┼───────────────────────┼────────────┼───────────────────┤
    │ dry-run              │ render only           │ —          │ untouched         │
    │ create-only          │ CREATE IF NOT EXISTS  │ —          │ untouched         │
    │                      │ + fail-if-exists      │            │                   │
    │ amend (default)      │ ALTER ADD COLUMN IF   │ —          │ preserved         │
    │                      │ NOT EXISTS; views     │            │ new cols NULL     │
    │                      │ CREATE OR REPLACE     │            │                   │
    │ amend-and-build      │ same as amend         │ dbt run    │ preserved         │
    │                      │                       │ + dbt test │ transforms fresh  │
    │ replace              │ auto-snapshot →       │ —          │ **dropped**       │
    │                      │ CREATE OR REPLACE     │            │ backup retained   │
    │ replace-and-build    │ same as replace       │ dbt run    │ **dropped**       │
    │                      │                       │ --full     │ rebuilt           │
    └──────────────────────┴───────────────────────┴────────────┴───────────────────┘

Destructive modes (``replace*``) require ``--allow-data-loss`` when the
environment is not dev OR the target has rows. ``create-only`` hard-fails
when the target already exists. ``dry-run`` renders DDL but makes zero
calls against the warehouse.

This module is pure — no provider imports — so mode-dispatch tests run
without any warehouse connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ApplyMode(str, Enum):
    """Six canonical apply modes. Order matches ``--mode`` flag's choice list
    (least-destructive → most-destructive, then build-augmented variants).
    """

    DRY_RUN = "dry-run"
    CREATE_ONLY = "create-only"
    AMEND = "amend"
    AMEND_AND_BUILD = "amend-and-build"
    REPLACE = "replace"
    REPLACE_AND_BUILD = "replace-and-build"

    @classmethod
    def default(cls) -> "ApplyMode":
        """Default mode: additive schema evolution, data preserved. Matches
        Terraform's default apply semantics in spirit: safe forward motion."""
        return cls.AMEND


# ``canonical_choices`` — the list in argparse's ``choices=`` arg. Separated
# from the enum so the CLI module doesn't need to know about the Enum class
# directly; keeps the test surface narrow.
CANONICAL_CHOICES = [m.value for m in ApplyMode]


DESTRUCTIVE_MODES = {ApplyMode.REPLACE, ApplyMode.REPLACE_AND_BUILD}
BUILD_MODES = {ApplyMode.AMEND_AND_BUILD, ApplyMode.REPLACE_AND_BUILD}


def is_destructive(mode: ApplyMode) -> bool:
    """True for modes that DROP + recreate the target (``replace*``)."""
    return mode in DESTRUCTIVE_MODES


def needs_build(mode: ApplyMode) -> bool:
    """True for modes that invoke dbt / the build engine after DDL
    (``amend-and-build`` and ``replace-and-build``)."""
    return mode in BUILD_MODES


def is_dry_run(mode: ApplyMode) -> bool:
    """Dry-run mode renders DDL but never executes it. Separate from the
    generic ``--dry-run`` flag so ``--mode dry-run`` is unambiguous about
    what's being previewed (DDL only, no DML/build)."""
    return mode is ApplyMode.DRY_RUN


def full_refresh_required(mode: ApplyMode) -> bool:
    """``replace-and-build`` must pass ``--full-refresh`` to dbt so the
    rebuilt table starts fresh instead of incremental-merging into a
    table that just got dropped."""
    return mode is ApplyMode.REPLACE_AND_BUILD


# ---------------------------------------------------------------------------
# Safety gate: --allow-data-loss required when destruction risk is real
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataLossGateResult:
    """Return shape for ``check_data_loss_gate``. ``blocked=True`` means the
    caller must hard-fail before any DDL runs; ``reason`` carries the
    operator-facing message with the row count + env context. ``blocked=False``
    means either the gate doesn't apply (non-destructive mode) or the
    operator has passed the explicit opt-in."""

    blocked: bool
    reason: Optional[str] = None


def check_data_loss_gate(
    mode: ApplyMode,
    *,
    env: Optional[str],
    target_row_count: Optional[int],
    allow_data_loss: bool,
) -> DataLossGateResult:
    """Decide whether a destructive mode may proceed.

    Rules (in order of evaluation):

    1. Non-destructive modes (``dry-run``, ``create-only``, ``amend*``)
       always pass. The gate only applies to ``replace*``.

    2. ``allow_data_loss=True`` (operator opt-in) always passes. The gate's
       job is to force an explicit opt-in, not to veto destruction outright.

    3. ``env == "dev"`` AND target has 0 rows (or row count unknown-but-
       empirically-empty) passes — dev scratch products routinely recreate
       themselves, forcing ``--allow-data-loss`` in that workflow creates
       pointless friction.

    4. Any destructive case not covered above: **blocked**. Message includes
       row count + env so the operator knows what they'd be dropping.

    ``target_row_count=None`` means "unknown" (e.g., the provider couldn't
    cheaply count rows, or the table doesn't exist yet). Treated as non-empty
    to err on the side of safety — better a false-positive block than a
    silent drop.
    """
    if not is_destructive(mode):
        return DataLossGateResult(blocked=False)

    if allow_data_loss:
        return DataLossGateResult(blocked=False)

    env_normalized = (env or "").strip().lower()
    is_dev = env_normalized in ("dev", "development")

    if is_dev and target_row_count == 0:
        return DataLossGateResult(blocked=False)

    # Compose an operator-facing message. The message has to be specific
    # enough that a human looking at a CI log can immediately see what's at
    # risk AND know the exact incantation to proceed.
    env_part = f"env={env!r}" if env else "env not set"
    if target_row_count is None:
        row_part = "target row count unknown (treating as populated)"
    elif target_row_count == 0:
        row_part = "target is empty"
    else:
        row_part = f"target has {target_row_count:,} row(s)"

    reason = (
        f"--mode {mode.value} is destructive ({env_part}; {row_part}). "
        f"Pass --allow-data-loss to confirm the drop. The pre-replace table "
        f"will be snapshotted to <target>__backup_<ts> so `fluid rollback` "
        f"can restore it."
    )
    return DataLossGateResult(blocked=True, reason=reason)


# ---------------------------------------------------------------------------
# --build deprecation alias
# ---------------------------------------------------------------------------


def resolve_mode_with_build_alias(
    mode_arg: Optional[str],
    build_id: Optional[str],
) -> tuple[ApplyMode, Optional[str]]:
    """Coerce the user's ``--mode`` + ``--build`` inputs into a canonical
    ``(ApplyMode, build_id_or_None)`` pair.

    Rules:

    * Neither flag set → default ``ApplyMode.AMEND``.
    * ``--mode X`` set, no ``--build`` → ``(X, None)``.
    * No ``--mode``, ``--build Y`` set → legacy invocation. Auto-upgrade to
      ``amend-and-build`` and log a deprecation warning. Keep ``build_id=Y``
      so the build engine still picks the right job.
    * BOTH ``--mode`` and ``--build`` set — only valid when ``--mode`` is
      a build-augmented variant (amend-and-build or replace-and-build).
      Otherwise raise ``ValueError`` (CLI converts to user-facing error).

    Defensive input handling: ``mode_arg`` and ``build_id`` are normalized
    to ``None`` when not strings. This is the gate that lets tests pass a
    ``MagicMock`` for ``args`` without explicitly stubbing these attributes —
    Mock's auto-attribute creation would otherwise trigger the unknown-mode
    error path. Real argparse always sends strings or None.

    The ``ValueError`` carries a specific message so ``apply.py`` can map
    it to a CLIError with a stable ``event`` field.
    """
    # Normalize non-string inputs (MagicMock, missing attrs) to None so the
    # downstream logic doesn't have to special-case them.
    if not isinstance(mode_arg, str):
        mode_arg = None
    if not isinstance(build_id, str):
        build_id = None

    if mode_arg is None:
        if build_id:
            # Legacy path — caller will emit the deprecation log.
            return ApplyMode.AMEND_AND_BUILD, build_id
        return ApplyMode.default(), None

    try:
        mode = ApplyMode(mode_arg)
    except ValueError:
        raise ValueError(
            f"unknown --mode value: {mode_arg!r}. " f"Valid: {', '.join(CANONICAL_CHOICES)}"
        ) from None

    if build_id and not needs_build(mode):
        raise ValueError(
            f"--build is only valid with build-augmented modes "
            f"(amend-and-build, replace-and-build); got --mode {mode.value}. "
            f"Either drop --build or use --mode amend-and-build."
        )

    return mode, build_id


__all__ = [
    "ApplyMode",
    "BUILD_MODES",
    "CANONICAL_CHOICES",
    "DESTRUCTIVE_MODES",
    "DataLossGateResult",
    "check_data_loss_gate",
    "full_refresh_required",
    "is_destructive",
    "is_dry_run",
    "needs_build",
    "resolve_mode_with_build_alias",
]
