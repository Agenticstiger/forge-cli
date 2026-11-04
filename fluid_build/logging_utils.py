# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import sys
import traceback
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # timezone-aware now(): datetime.utcnow() raises DeprecationWarning on
        # 3.12+, and a formatter that warns while formatting re-enters the
        # warning machinery of anything upstream that records warnings during
        # logging (the mcp 1.x server does) — see tests/test_logging_formatters_no_warnings.py.
        base = {
            "time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
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
