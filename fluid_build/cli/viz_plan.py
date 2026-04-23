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

from __future__ import annotations

import argparse
import html
import json
import logging
import os

from ._common import CLIError, read_json
from ._logging import info

COMMAND = "viz-plan"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=argparse.SUPPRESS,  # hidden from help — deprecated
    )
    p.add_argument("plan", help="runtime/plan.json")
    p.add_argument("--out", default="runtime/plan.html", help="HTML path")
    p.set_defaults(cmd=COMMAND, func=run)


def _mermaid_node_id(action_id: str, idx: int) -> str:
    """Return a mermaid-safe node id.

    Mermaid flowchart node IDs must be alphanumeric + underscore; the
    action_id may contain dots and dashes, so we sanitize + append the
    positional index to avoid collisions when two actions share a
    slug (rare but possible in early-draft plans).
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", action_id or f"action_{idx}")
    return f"n{idx}_{cleaned}"


def _mermaid_label(action: dict, idx: int) -> str:
    """Render the per-node display label.

    Shape: ``<op><br/><id>`` so the graph shows both the operation
    (``provisionDataset``, ``grantAccess``, …) and the contract-
    specified action id. Both are useful at a glance — op tells you
    what; id tells you which.

    **SECURITY — XSS prevention:** the op + id values come from the
    contract via ``plan.json``. Contract YAML is schema-unconstrained
    on ``providerActions[].actionId`` / ``op`` — a malicious
    contributor (or compromised upstream reference) can land a string
    like ``"safe_id</pre><script>…</script>"`` in an action id.
    Without HTML-escaping, that payload breaks out of the surrounding
    ``<pre class="mermaid">`` element (HTML5 ``<pre>`` is ordinary
    flow content, NOT a raw-text element — child ``<script>`` tags
    parse + execute) and runs under the origin the plan.html is
    opened at. Mermaid's own ``securityLevel: 'strict'`` only
    sanitises WITHIN mermaid rendering; the browser's HTML
    tokenizer runs first, so a pre-mermaid escape is the only
    defence.

    Fix: route both op + id through ``html.escape(..., quote=True)``.
    This turns ``<`` / ``>`` / ``&`` / ``"`` / ``'`` into their
    character-reference forms before the mermaid parser or the HTML
    tokenizer sees them. Mermaid preserves HTML entity references in
    labels — ``&lt;br/&gt;`` renders as the literal string
    ``<br/>``, which is the desired display for a malicious id
    containing that substring. The explicit ``<br/>`` we insert
    between op and id is ASCII-literal (NOT entity-encoded) so
    mermaid still honours it as a line break.
    """
    op = action.get("op") or action.get("action_type") or "unknown"
    action_id = action.get("id") or action.get("action_id") or f"action_{idx}"
    # HTML-escape BEFORE the mermaid parser — this disarms both the
    # browser's HTML tokenizer (no ``</pre>`` escape) and the mermaid
    # label parser (no embedded ``"``). quote=True also covers ``'``
    # so a payload wrapped in single-quotes can't break out either.
    safe_op = html.escape(str(op), quote=True)
    safe_id = html.escape(str(action_id), quote=True)
    # The ``<br/>`` separator is our own safe literal — NOT operator-
    # controlled — so it stays as a mermaid line-break directive.
    return f'"{safe_op}<br/>{safe_id}"'


def _mermaid_class_for(action: dict) -> str:
    """Pick a CSS class based on the action's mode / status.

    The four canonical classes (``amend``, ``replace``, ``skipped``,
    ``unknown``) are styled via the classDef block at the top of the
    mermaid graph. Colour-coding makes destructive ``replace`` actions
    visually distinct from the happy-path ``amend`` — operators scan
    the graph before approving plan → apply.
    """
    mode = (action.get("mode") or "").lower()
    status = (action.get("status") or "").lower()
    if mode == "replace" or "replace" in str(action.get("op", "")).lower():
        return "replace"
    if status == "skipped":
        return "skipped"
    if mode in {"amend", "amend-and-build", "create-only"}:
        return "amend"
    return "unknown"


def _build_mermaid_graph(actions: list) -> str:
    """Build a mermaid ``graph TD`` body from the actions list.

    Each action becomes a node; ``depends_on`` entries become edges.
    No dependencies = sequential numbering fallback (n0 → n1 → n2 …)
    so the graph stays connected even for older plans without
    explicit dependency declarations.

    Returns the full ``graph TD`` block including classDef directives;
    the caller embeds this inside an HTML ``<pre class="mermaid">``.
    """
    if not actions:
        return 'graph TD\n    empty["(no actions)"]'

    lines = ["graph TD"]
    # CSS classes — amend=blue, replace=red, skipped=grey, unknown=
    # warm yellow. Tuned for dark background.
    lines.append("    classDef amend fill:#1e3a8a,stroke:#60a5fa,color:#dbeafe;")
    lines.append("    classDef replace fill:#7f1d1d,stroke:#f87171,color:#fecaca;")
    lines.append("    classDef skipped fill:#334155,stroke:#94a3b8,color:#cbd5e1;")
    lines.append("    classDef unknown fill:#7c2d12,stroke:#fb923c,color:#fed7aa;")

    # Emit a node declaration per action.
    node_ids: list[str] = []
    for i, action in enumerate(actions):
        nid = _mermaid_node_id(
            action.get("id") or action.get("action_id") or f"action_{i}",
            i,
        )
        node_ids.append(nid)
        label = _mermaid_label(action, i)
        cls = _mermaid_class_for(action)
        lines.append(f"    {nid}[{label}]:::{cls}")

    # Emit edges. When actions carry explicit depends_on, use those;
    # otherwise fall back to sequential chaining.
    emitted_edges = set()
    has_any_deps = False
    for i, action in enumerate(actions):
        deps = action.get("depends_on") or action.get("dependsOn") or []
        if not isinstance(deps, list):
            continue
        for dep in deps:
            # Find the dep action's node id. If the dep id doesn't
            # match any known action, skip silently (orphan refs
            # happen in partial plans).
            for j, other in enumerate(actions):
                other_id = other.get("id") or other.get("action_id") or f"action_{j}"
                if other_id == dep:
                    edge = (node_ids[j], node_ids[i])
                    if edge not in emitted_edges:
                        lines.append(f"    {node_ids[j]} --> {node_ids[i]}")
                        emitted_edges.add(edge)
                        has_any_deps = True
                    break
    # Fallback: sequential chain if no explicit deps anywhere.
    if not has_any_deps and len(node_ids) > 1:
        for i in range(len(node_ids) - 1):
            lines.append(f"    {node_ids[i]} --> {node_ids[i + 1]}")

    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<title>FLUID Plan</title>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{ startOnLoad: true, theme: 'dark', securityLevel: 'strict' }});
</script>
<style>
  body {{ font-family: Menlo, monospace; padding: 16px; background: #0b1020; color: #e5e7eb; }}
  .legend {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
  .legend span {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
  .graph-container {{
    border: 1px solid #374151; border-radius: 10px; padding: 16px;
    margin: 10px 0; background: #0f172a;
  }}
  .detail {{
    border: 1px solid #374151; border-radius: 10px; padding: 12px;
    margin: 10px 0; background: #0f172a;
  }}
  pre {{
    background: #0a0f1d; padding: 12px; border-radius: 8px;
    border: 1px solid #1f2937; overflow: auto; font-size: 11px;
  }}
  h1, h2 {{ color: #f1f5f9; }}
</style>
</head>
<body>
<h1>FLUID Plan</h1>
<p>{count} action(s). Colour-coded by mode:</p>
<div class="legend">
  <span style="background: #1e3a8a;">amend</span>
  <span style="background: #7f1d1d;">replace</span>
  <span style="background: #334155;">skipped</span>
  <span style="background: #7c2d12;">unknown</span>
</div>

<div class="graph-container">
<pre class="mermaid">
{mermaid_body}
</pre>
</div>

<h2>Action details</h2>
<div class="detail">
<pre>{actions_json}</pre>
</div>
</body>
</html>"""


def render_plan_html(plan_path: str, out_html: str, logger: logging.Logger) -> None:
    """Render ``plan.json`` as an HTML page with a mermaid.js DAG +
    colour-coded action-mode legend + raw JSON drill-down.

    mermaid.js is loaded from the jsdelivr CDN via ES module import.
    That means opening the file in a browser works out-of-the-box
    (no local build step) but requires network access on first view
    — acceptable for a diagnostics view, not something that ships
    into production data planes.

    ``securityLevel: 'strict'`` in the init call is defence-in-depth
    WITHIN mermaid rendering (disables click bindings + HTML in node
    labels). The primary XSS defence is pre-mermaid HTML-escaping in
    :func:`_mermaid_label` + the JSON-in-HTML escape applied to the
    raw-JSON drill-down block below. Without both, a contract with a
    malicious ``actionId`` like ``"x</pre><script>…</script>"`` would
    break out of the ``<pre>`` container and execute JS — because
    HTML5 ``<pre>`` is ordinary flow content, NOT a raw-text element,
    so child ``<script>`` tags are parsed + executed.
    """
    data = read_json(plan_path)
    actions = data.get("actions", [])
    mermaid_body = _build_mermaid_graph(actions)
    # JSON-in-HTML escape: ``json.dumps`` does not by default escape
    # HTML-relevant characters — ``</pre>`` / ``</script>`` pass
    # through verbatim. When that JSON is embedded inside ``<pre>``,
    # a ``</pre>`` substring closes the block and any subsequent
    # ``<script>`` runs. Replacing ``<`` / ``>`` / ``&`` with their
    # \uXXXX escapes produces valid JSON that still parses identically
    # (JSON allows \u-escapes of any ASCII char) but is inert against
    # the HTML tokenizer. Standard OWASP-recommended JSON-in-HTML
    # pattern; see https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html#output-encoding-for-html-contexts
    raw_json = json.dumps(actions, indent=2)
    actions_json_safe = (
        raw_json.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    rendered = _HTML_TEMPLATE.format(
        count=len(actions),
        mermaid_body=mermaid_body,
        actions_json=actions_json_safe,
    )
    out_dir = os.path.dirname(out_html)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(rendered)
    info(logger, "viz_plan_ok", out=out_html, actions=len(actions))


def run(args, logger: logging.Logger) -> int:
    from fluid_build.cli.console import cprint

    cprint("Note: 'fluid viz-plan' is deprecated. Use 'fluid plan --html' instead.\n")
    try:
        render_plan_html(args.plan, args.out, logger)
        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "viz_plan_failed", {"error": str(e)})
