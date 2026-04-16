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
            lines.append(f"  n{i-1} -> {nid};")
    lines.append("}")
    return "\n".join(lines)
