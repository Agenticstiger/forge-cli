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

import json
import logging
import sys
import traceback
from datetime import datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "time": datetime.utcnow().isoformat(timespec="seconds"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            base.update(record.extra)
        if record.exc_info:
            base["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        return json.dumps(base)


def setup_logger(level="INFO"):
    logger = logging.getLogger()
    logger.setLevel(level.upper())
    # Clear old handlers
    for h in list(logger.handlers):
        logger.removeHandler(h)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    # attach a run id for correlation
    logger = logging.getLogger("fluid")
    logger.propagate = True
    logger.debug(json.dumps({"message": "logger_initialized"}))
    return logger


def log_json(name, level, message, **kwargs):
    logging.getLogger(name).log(
        getattr(logging, level.upper(), logging.INFO),
        message,
        extra={"extra": kwargs} if kwargs else None,
    )
