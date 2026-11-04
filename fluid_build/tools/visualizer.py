# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0


def to_dot(plan: list) -> str:
    lines = [
        "digraph PLAN {",
        "  rankdir=LR;",
        '  node [shape=box, style="rounded,filled", color="#334155", fillcolor="#0b1220", fontcolor="#e5e7eb"];',
    ]
    for i, a in enumerate(plan):
        nid = f"n{i}"
        label = f"{a['op']}\\n{a['resource_type']}\\n{a['resource_id']}"
        lines.append(f'  {nid} [label="{label}"];')
        if i > 0:
            lines.append(f"  n{i - 1} -> {nid};")
    lines.append("}")
    return "\n".join(lines)
