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
import logging
import sys
import time
from typing import Any, Dict


def setup_logging(level: str = "INFO", file: str | None = None) -> logging.Logger:
    logger = logging.getLogger("fluid.cli")
    logger.handlers.clear()
    lvl = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logger.setLevel(lvl)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)

    if file:
        fh = logging.FileHandler(file)
        fh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(fh)
    return logger


#: The envelope keys every event carries. A payload key of the same name is
#: kept as ``extra_<key>`` instead of replacing the envelope's (the event
#: name is ``message``; a provider result with its own ``message`` would
#: otherwise have renamed the event).
_ENVELOPE_KEYS = frozenset({"time", "level", "name", "message"})


def _event(level: str, name: str, payload: Dict[str, Any]) -> str:
    # Renamed, not dropped: the pattern of WebbPulse/webbpulse-python#129
    # (``extra_<key>`` for an ``extra`` key that collides with a LogRecord
    # attribute). structlog's hynek/structlog#842 drops such keys instead,
    # which would lose the provider's own explanation here.
    fields = {(f"extra_{k}" if k in _ENVELOPE_KEYS else k): v for k, v in payload.items()}
    return json.dumps(
        {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "name": "fluid.cli",
            "message": name,
            **fields,
        }
    )


# ``logger`` and ``message`` are positional-only (PEP 570), so a payload may
# carry keys of those names: ``info(logger, "policy_apply_result", **res)``
# raised ``TypeError: info() got multiple values for argument 'message'``
# for every provider whose result has a ``message`` (GCP's policy applier
# does), and failed stage 8 of every generated gcp pipeline.
def info(logger: logging.Logger, message: str, /, **payload: Any) -> None:
    """Emit a structured INFO event to the log sink.

    Routed at DEBUG level for the human-facing console handler so the
    ``{"time":"...","message":"plan_success",...}`` JSON line stops
    bleeding onto user terminals next to the user-facing
    ``cprint("✅ Plan saved to: ...")`` line. Operators can surface
    these structured events with ``--debug`` / ``FLUID_LOG_LEVEL=DEBUG``
    or by adding a ``--log-file`` JSON sink.

    UX hardening pass — the legacy ``logger.info`` emission was the
    single biggest source of "what is this JSON line on my terminal?"
    feedback from users.
    """
    logger.debug(_event("INFO", message, payload))


def warn(logger: logging.Logger, message: str, /, **payload: Any) -> None:
    """Emit a structured WARNING event — stays at WARNING level.

    Warnings are user-relevant ("we didn't break, but you should know
    about this") so they continue to surface on the console handler.
    """
    logger.warning(_event("WARNING", message, payload))


def error(logger: logging.Logger, message: str, /, **payload: Any) -> None:
    logger.error(_event("ERROR", message, payload))
