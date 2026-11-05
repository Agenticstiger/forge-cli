# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import datetime
import json
import os
import pathlib
import shutil
import sys
from typing import Any, Dict, Tuple


def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def safe_rel(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(pathlib.Path.cwd()))
    except Exception:
        return str(path)


def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_yaml(path: pathlib.Path) -> Tuple[dict, str]:
    """Load YAML (requires PyYAML). If not available, raise with guidance."""
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError("PyYAML is required to edit contracts. Run: pip install pyyaml") from e
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at root of {path}")
    return data, "yaml"


def dump_yaml(obj: dict, path: pathlib.Path) -> None:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError("PyYAML is required to edit contracts. Run: pip install pyyaml") from e
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False)


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(content)


def copytree(src: pathlib.Path, dst: pathlib.Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
