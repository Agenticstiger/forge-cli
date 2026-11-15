# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The run a secret is resolved for: its stage, its cache, and what to undo at its end.

Every pipeline stage runs inside :func:`secret_run` (``observability.tracing``'s
``traced_stage`` enters it), which gives resolution three things:

* the stage name, which the policy's stage gate reads (only ``apply`` fetches a
  value by default; ``validate``, ``plan``, ``bundle`` and ``diff`` never do);
* a cache, so one reference is fetched once per run and the value lives no
  longer than the run;
* cleanups, run when the outermost stage ends: a token forge minted (a Vault
  JWT login, an unwrapped token) is revoked then, so a credential outlives the
  run only by the store's own TTL if the revoke fails.

The state is process-global behind a lock, not a ``contextvars.ContextVar``:
``fluid apply`` executes its plan on a worker thread when an event loop is
already running, and a new thread starts with an empty context, so a context
variable would make every resolution there look stage-less and be refused.
Process-global is correct because a process runs one stage at a time: the CLI
runs one command, and ``fluid_build.engine`` serialises stages behind its
``_STAGE_LOCK``.

Stdlib only at import: ``traced_stage`` wraps every stage entry point, and the
startup budget (``tests/perf/test_startup_budget.py``) counts what that costs.
"""

from __future__ import annotations

import atexit
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

LOG = logging.getLogger("fluid.secrets")

_LOCK = threading.RLock()
_STAGES: List[str] = []
_STATE: Optional["RunState"] = None
#: Lookups made outside every stage (the credential chain in a command that is
#: not a pipeline stage) share this state until the process exits.
_ORPHAN: Optional["RunState"] = None


@dataclass
class RunState:
    """What one run has resolved and what it must undo. Never persisted."""

    #: canonical reference -> pydantic ``SecretStr``
    cache: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: resolver-private state (a minted token per store address, clients);
    #: kept out of the repr, since it holds tokens
    scratch: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: the run-secrets file, once read: canonical reference -> ``SecretStr``
    handed: Optional[Dict[str, Any]] = field(default=None, repr=False)
    #: False for the state lookups share outside every stage: it keeps what a
    #: store login minted (so a wrapped token is unwrapped once per process,
    #: and revoked at exit) but never holds a value past its lookup.
    keep_values: bool = True
    _cleanups: List[Callable[[], None]] = field(default_factory=list)
    _closed: bool = False

    def on_close(self, cleanup: Callable[[], None]) -> None:
        """Run ``cleanup`` when the run ends (newest first). Never fatal."""
        with _LOCK:
            self._cleanups.append(cleanup)

    def close(self) -> None:
        """Undo what the run did: cleanups first, then forget every value."""
        with _LOCK:
            if self._closed:
                return
            self._closed = True
            cleanups = list(reversed(self._cleanups))
            self._cleanups.clear()
        for cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:  # a failed revoke must not fail the run
                LOG.warning("secret cleanup at run end failed: %s", type(exc).__name__)
        with _LOCK:
            self.cache.clear()
            self.scratch.clear()
            self.handed = None


def _normalize(stage: str) -> str:
    return stage.strip().lower().replace("_", "-")


@contextmanager
def secret_run(stage: str) -> Iterator[RunState]:
    """Run the body as ``stage``; the outermost exit closes the run's state.

    Nested stages (a stage that calls another) share the outer run's cache and
    cleanups; the innermost stage name is the one the gate reads.
    """
    global _STATE
    with _LOCK:
        _STAGES.append(_normalize(stage))
        if _STATE is None:
            _STATE = RunState()
        state = _STATE
    try:
        yield state
    finally:
        closing: Optional[RunState] = None
        with _LOCK:
            _STAGES.pop()
            if not _STAGES:
                closing, _STATE = _STATE, None
        if closing is not None:
            closing.close()


def current_stage() -> Optional[str]:
    """The innermost running stage, or ``None`` outside every stage."""
    with _LOCK:
        return _STAGES[-1] if _STAGES else None


def active_state() -> Optional[RunState]:
    """The running stage's state, or ``None`` outside every stage."""
    with _LOCK:
        return _STATE


def _close_orphan() -> None:
    global _ORPHAN
    with _LOCK:
        orphan, _ORPHAN = _ORPHAN, None
    if orphan is not None:
        orphan.close()


@contextmanager
def state_for_lookup() -> Iterator[RunState]:
    """The running stage's state, else the process's state for lookups outside a stage.

    Outside every stage, one state serves the process: a token a store login
    minted is reused rather than minted (or a single-use wrapped token
    unwrapped) per lookup, and is revoked when the process exits. It keeps no
    value (``keep_values`` is false).
    """
    global _ORPHAN
    with _LOCK:
        state = _STATE
        if state is None:
            if _ORPHAN is None:
                _ORPHAN = RunState(keep_values=False)
                atexit.register(_close_orphan)
            state = _ORPHAN
    yield state
