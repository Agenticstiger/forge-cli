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


def _event(level: str, name: str, payload: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "name": "fluid.cli",
            "message": name,
            **payload,
        }
    )


def info(logger: logging.Logger, message: str, **payload: Any) -> None:
    logger.info(_event("INFO", message, payload))


def warn(logger: logging.Logger, message: str, **payload: Any) -> None:
    logger.warning(_event("WARNING", message, payload))


def error(logger: logging.Logger, message: str, **payload: Any) -> None:
    logger.error(_event("ERROR", message, payload))
