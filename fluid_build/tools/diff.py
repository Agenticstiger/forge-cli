# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import difflib
import json


def plan_diff(plan_a_path: str, plan_b_path: str) -> str:
    with open(plan_a_path, encoding="utf-8") as plan_a_file:
        a = json.dumps(json.load(plan_a_file), indent=2).splitlines()
    with open(plan_b_path, encoding="utf-8") as plan_b_file:
        b = json.dumps(json.load(plan_b_file), indent=2).splitlines()
    return "\n".join(
        difflib.unified_diff(a, b, fromfile=plan_a_path, tofile=plan_b_path, lineterm="")
    )
