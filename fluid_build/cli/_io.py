# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import tempfile


def atomic_write(path: str, data: str, encoding: str = "utf-8") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def dump_json(path: str, obj):
    atomic_write(path, json.dumps(obj, indent=2))
