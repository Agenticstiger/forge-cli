# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Filesystem helpers for the ODCS provider — read YAML/JSON, write YAML/JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Union


def read_input(path: Union[str, Path]) -> Dict[str, Any]:
    input_path = Path(path)
    with open(input_path) as f:
        if input_path.suffix in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(f)
        return json.load(f)


def write_output(data: Dict[str, Any], path: Union[Path, str], fmt: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        if fmt == "yaml":
            import yaml

            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(data, f, indent=2)
