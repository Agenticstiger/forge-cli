# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import json
import pathlib
from typing import Any, Dict

import yaml


def load_contract(path: str) -> Dict[str, Any]:
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Contract not found: {path}")
    text = p.read_text(encoding="utf-8")
    if path.endswith((".yml", ".yaml")):
        return yaml.safe_load(text)
    return json.loads(text)


def dump_json(path: str, obj):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
