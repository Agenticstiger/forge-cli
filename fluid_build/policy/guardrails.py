# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0


def validate_no_overgrant(actions):
    for a in actions:
        if a.payload and isinstance(a.payload, dict):
            for r in a.payload.get("roles", []):
                if "owner" in r.lower():
                    raise ValueError(f"Over-broad role detected in {a.resource_id}: {r}")
    return True
