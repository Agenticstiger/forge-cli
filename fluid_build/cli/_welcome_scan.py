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

# ruff: noqa: T201 — this helper module owns CLI prompt output (print) by design;
# user-facing output flows through console.cprint elsewhere.
"""Detect-first welcome scan (Phase 0.2).

Runs in parallel before any interview prompt to answer:

* **Workspace state** — fluid.workspace.yaml present, products already exist?
* **AI configured** — env var or stored credential present?
* **CLIs installed** — dbt, duckdb, gcloud, snowflake-cli, kubectl, ...
* **Sample data** — CSV / Parquet / JSON in cwd or under ``data/``?
* **Cloud creds** — GCP / AWS / Azure / Snowflake env vars set?
* **Return-user state** — ``~/.fluid/usage.json`` carries forge_count.

The user sees a panel **populated with what was found**, asking only the
un-inferred bits. Recurring users (≥5 forges) skip the welcome entirely
and land straight in the interview.

50ms scan budget — every probe is best-effort, never blocking.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# Probe budgets — every individual probe must finish under this.
_PROBE_TIMEOUT_S = 0.4
# Aggregate scan budget — shut down probes that overrun.
_TOTAL_TIMEOUT_S = 1.0
# Repeat-user threshold — at this many forges we stop showing the panel.
_REPEAT_USER_THRESHOLD = 5
# File extensions we recognise as candidate sample data.
_SAMPLE_EXTENSIONS = (".csv", ".parquet", ".json", ".jsonl", ".tsv", ".duckdb")
# CLI binaries we report on.
_CLI_BINARIES = (
    "dbt",
    "duckdb",
    "gcloud",
    "aws",
    "az",
    "snowflake",
    "kubectl",
    "terraform",
    "git",
    "uv",
    "ollama",
)
# Cloud-credential environment markers.
_CLOUD_ENV = {
    "gcp": ("GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_PROJECT", "FLUID_GCP_PROJECT"),
    "aws": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_DEFAULT_REGION"),
    "azure": ("AZURE_SUBSCRIPTION_ID", "AZURE_CLIENT_ID"),
    "snowflake": ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"),
}
# AI-credential environment markers.
_AI_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)
# AI coding-agent CLIs forge can delegate to, mapped binary -> canonical
# provider name. Detected on PATH so the welcome panel can offer keyless
# authoring even with no API key configured. Claude Code is keyless via the
# user's subscription; the others reuse their own key (validated inside the
# provider at call time). Order encodes keyless preference (claude-code first).
_CODING_AGENT_BINARIES = (
    ("claude", "claude-code"),
    ("codex", "codex"),
    ("cursor-agent", "cursor"),
    ("kiro-cli", "kiro"),
)


@dataclass
class WelcomeFindings:
    """What the parallel scan found in the user's environment.

    Every field defaults to a "nothing detected" value so a partial scan
    (timeout, exception) still returns a sensible shape — invariant the
    welcome panel relies on.
    """

    in_workspace: bool = False
    workspace_root: Optional[str] = None
    workspace_lock: str = ""
    existing_products: int = 0
    has_contract_in_cwd: bool = False
    ai_configured: bool = False
    ai_provider_hint: str = ""
    installed_clis: List[str] = field(default_factory=list)
    coding_agents_available: List[str] = field(default_factory=list)
    sample_data_candidates: List[str] = field(default_factory=list)
    cloud_credentials: List[str] = field(default_factory=list)
    return_user: bool = False
    forge_count: int = 0
    suggested_data_product_type: str = ""
    scan_duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def suggested_keyless_provider(self) -> str:
        """Best keyless LLM provider given what was detected, else "".

        Returns the most key-free coding agent on PATH — Claude Code first
        because it's the only *truly* keyless one (subscription OAuth); the
        others reuse their own key but still spare the user forge's separate
        key setup. ``_CODING_AGENT_BINARIES`` order encodes the preference.
        """
        return self.coding_agents_available[0] if self.coding_agents_available else ""


# ---------------------------------------------------------------------------
# Individual probes — each is best-effort, never raises, returns partial state.
# ---------------------------------------------------------------------------


def _probe_workspace(start: Path) -> Dict[str, Any]:
    """Detect ``fluid.workspace.yaml`` and counted products."""
    try:
        from fluid_build.cli.workspace_config import (
            discover_workspace_products,
            find_workspace_root,
            load_workspace_config,
        )

        ws_root = find_workspace_root(start)
        if ws_root is None:
            return {
                "in_workspace": False,
                "has_contract_in_cwd": (start / "contract.fluid.yaml").is_file(),
            }
        cfg = load_workspace_config(ws_root)
        products = discover_workspace_products(ws_root)
        return {
            "in_workspace": True,
            "workspace_root": str(ws_root),
            "workspace_lock": cfg.data_product_type_lock or "",
            "existing_products": len(products),
            "has_contract_in_cwd": (start / "contract.fluid.yaml").is_file(),
        }
    except Exception as exc:  # noqa: BLE001 — best-effort
        LOG.debug("welcome_scan_workspace_failed: %s", exc)
        return {}


def _provider_hint_from_env_var(var: str) -> str:
    """Map an AI-credential env-var name to a canonical provider name."""
    if "OPENAI" in var:
        return "openai"
    if "ANTHROPIC" in var or "CLAUDE" in var:
        return "anthropic"
    if "GEMINI" in var or "GOOGLE" in var:
        return "gemini"
    return ""


def _probe_ai_credentials() -> Dict[str, Any]:
    """Detect the configured AI provider.

    Authoritative-signal-first ladder (mirrors
    ``check_llm_readiness``'s ordering so the welcome panel never
    disagrees with the rest of the run):

    1. ``FLUID_LLM_PROVIDER`` env var — explicit selector.
    2. Saved ``~/.fluid/ai_config.json`` — the user's persisted choice.
    3. AI-credential env-var presence (``OPENAI_API_KEY`` etc.) — last
       resort, otherwise a stray ``OPENAI_API_KEY`` in the shell
       silently overrides a Gemini run.
    """
    # 1. Explicit selector wins.
    explicit = (os.environ.get("FLUID_LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return {"ai_configured": True, "ai_provider_hint": explicit}

    # 2. Persisted choice (``fluid ai setup``).
    try:
        from fluid_build.cli.ai_setup import _load_ai_config

        saved = _load_ai_config() or {}
        provider = (saved.get("provider") or "").strip().lower()
        if provider:
            return {"ai_configured": True, "ai_provider_hint": provider}
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks the scan
        LOG.debug("welcome_scan_ai_config_failed: %s", exc)

    # 3. Env-var inference.
    for var in _AI_ENV:
        if os.environ.get(var):
            return {
                "ai_configured": True,
                "ai_provider_hint": _provider_hint_from_env_var(var),
            }
    return {"ai_configured": False}


def _probe_clis() -> Dict[str, Any]:
    """Find which dev CLIs are on PATH."""
    found: List[str] = []
    for binary in _CLI_BINARIES:
        if shutil.which(binary):
            found.append(binary)
    return {"installed_clis": found}


def _probe_coding_agents() -> Dict[str, Any]:
    """Find AI coding-agent CLIs on PATH (claude/codex/cursor-agent/kiro).

    These enable keyless authoring without an LLM API key — Claude Code via
    the user's subscription, the others by reusing the agent's own key. We
    report canonical provider names (e.g. ``claude-code``) so the welcome
    panel can suggest ``--llm-provider <name>`` directly.
    """
    found: List[str] = []
    for binary, canonical in _CODING_AGENT_BINARIES:
        if shutil.which(binary):
            found.append(canonical)
    return {"coding_agents_available": found}


def _probe_sample_data(start: Path) -> Dict[str, Any]:
    """Look for sample data files in cwd and ``./data``.

    Capped at 6 results so the panel doesn't drown the user in noise.
    """
    samples: List[str] = []
    for base in (start, start / "data"):
        if not base.is_dir():
            continue
        try:
            for entry in sorted(base.iterdir())[:50]:
                if entry.is_file() and entry.suffix.lower() in _SAMPLE_EXTENSIONS:
                    rel = entry.relative_to(start) if entry.is_relative_to(start) else entry
                    samples.append(str(rel))
                    if len(samples) >= 6:
                        break
        except (PermissionError, OSError):
            continue
        if len(samples) >= 6:
            break
    return {"sample_data_candidates": samples}


def _probe_cloud_creds() -> Dict[str, Any]:
    """Detect cloud-provider credentials via env vars."""
    found: List[str] = []
    for cloud, vars_ in _CLOUD_ENV.items():
        for var in vars_:
            if os.environ.get(var):
                found.append(cloud)
                break
    return {"cloud_credentials": found}


def _probe_return_user() -> Dict[str, Any]:
    """Read ``~/.fluid/usage.json`` to detect a return user."""
    try:
        path = Path.home() / ".fluid" / "usage.json"
        if not path.is_file():
            return {"return_user": False, "forge_count": 0}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        count = int(data.get("forge_count", 0) or 0)
        return {
            "return_user": count >= _REPEAT_USER_THRESHOLD,
            "forge_count": count,
        }
    except Exception as exc:  # noqa: BLE001
        LOG.debug("welcome_scan_usage_failed: %s", exc)
        return {"return_user": False, "forge_count": 0}


def _probe_specialization_suggestion(start: Path) -> Dict[str, Any]:
    """Suggest a productType based on recent forge history.

    Reads the last 5 ``.fluid/forge-receipt.json`` entries under the
    workspace and proposes the dominant type. Only fires when ≥4/5
    are the same type.
    """
    try:
        # Cheapest possible: glob for nearby contracts and tally their
        # productType. Limited to a small radius so the scan stays
        # under budget.
        contracts = sorted(start.rglob("contract.fluid.yaml"))[:8]
        if not contracts:
            return {}
        import yaml as _yaml

        types: List[str] = []
        for c in contracts:
            try:
                doc = _yaml.safe_load(c.read_text(encoding="utf-8")) or {}
                pt = (doc.get("metadata") or {}).get("productType")
                if pt:
                    types.append(pt)
            except Exception:  # noqa: BLE001
                continue
        if not types:
            return {}
        from collections import Counter

        most_common, count = Counter(types).most_common(1)[0]
        if count >= 4 and len(types) >= 5:
            return {"suggested_data_product_type": most_common}
        return {}
    except Exception as exc:  # noqa: BLE001
        LOG.debug("welcome_scan_specialization_failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_welcome_scan(*, start: Optional[Path] = None) -> WelcomeFindings:
    """Run every probe in parallel and return a :class:`WelcomeFindings`.

    Total wall-clock <= ``_TOTAL_TIMEOUT_S``. Each individual probe runs
    under ``_PROBE_TIMEOUT_S``. A probe that times out is silently
    dropped — its fields stay at their defaults.
    """
    target = (start or Path.cwd()).resolve()
    findings = WelcomeFindings()
    started = time.time()

    probes = {
        "workspace": (_probe_workspace, (target,)),
        "ai": (_probe_ai_credentials, ()),
        "clis": (_probe_clis, ()),
        "coding_agents": (_probe_coding_agents, ()),
        "samples": (_probe_sample_data, (target,)),
        "cloud": (_probe_cloud_creds, ()),
        "return_user": (_probe_return_user, ()),
        "specialization": (_probe_specialization_suggestion, (target,)),
    }

    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {name: pool.submit(fn, *args) for name, (fn, args) in probes.items()}
        deadline = started + _TOTAL_TIMEOUT_S
        for name, fut in futures.items():
            remaining = max(0.0, deadline - time.time())
            try:
                result = fut.result(timeout=min(_PROBE_TIMEOUT_S, remaining))
            except Exception as exc:  # noqa: BLE001 — concurrent.futures.TimeoutError + others
                LOG.debug("welcome_scan_probe_failed: %s — %s", name, exc)
                continue
            for key, value in (result or {}).items():
                if hasattr(findings, key):
                    setattr(findings, key, value)

    findings.scan_duration_ms = int((time.time() - started) * 1000)
    return findings


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_welcome(findings: WelcomeFindings, *, console: Optional[Any] = None) -> None:
    """Render a populated welcome panel based on what we found.

    Suppresses entirely for return users (forge_count >= threshold)
    so the prompt-fatigue tax is paid once, not every run.
    """
    if findings.return_user:
        return
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except Exception:  # noqa: BLE001
        _render_welcome_plain(findings)
        return

    out = console or Console()
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="dim")
    table.add_column()
    table.add_row(
        "Workspace:",
        f"{findings.workspace_root or 'not in a workspace yet'}"
        + (f"  · locked to {findings.workspace_lock}" if findings.workspace_lock else "")
        + (
            f"  · {findings.existing_products} existing products"
            if findings.existing_products
            else ""
        ),
    )
    if findings.ai_configured:
        ai_status = "[green]✓ configured[/green]" + (
            f" ({findings.ai_provider_hint})" if findings.ai_provider_hint else ""
        )
    elif findings.coding_agents_available:
        ai_status = "[yellow]no API key set — keyless coding agent available ↓[/yellow]"
    else:
        ai_status = "[yellow]not configured — run `fluid ai setup`[/yellow]"
    table.add_row("AI:", ai_status)
    if findings.coding_agents_available:
        agents = ", ".join(findings.coding_agents_available)
        suggested = findings.suggested_keyless_provider()
        table.add_row(
            "Keyless:",
            f"[green]✓ {agents}[/green] — no API key needed, "
            f"e.g. [bold]--llm-provider {suggested}[/bold]",
        )
    if findings.installed_clis:
        table.add_row("CLIs:", ", ".join(findings.installed_clis[:6]))
    if findings.cloud_credentials:
        table.add_row("Cloud:", ", ".join(findings.cloud_credentials))
    if findings.sample_data_candidates:
        table.add_row(
            "Sample data:",
            ", ".join(findings.sample_data_candidates[:4]),
        )
    if findings.suggested_data_product_type:
        table.add_row(
            "Suggested:",
            f"--data-product-type {findings.suggested_data_product_type} "
            "(based on your recent forge history)",
        )
    out.print(
        Panel(
            table,
            title="[bold]🌊 fluid forge[/bold]",
            subtitle=f"[dim]scanned in {findings.scan_duration_ms}ms[/dim]",
            border_style="cyan",
        )
    )


def _render_welcome_plain(findings: WelcomeFindings) -> None:
    print(f"\n=== fluid forge ===  (scanned in {findings.scan_duration_ms}ms)")
    if findings.workspace_root:
        line = f"  Workspace: {findings.workspace_root}"
        if findings.workspace_lock:
            line += f"  · locked to {findings.workspace_lock}"
        if findings.existing_products:
            line += f"  · {findings.existing_products} existing products"
        print(line)
    if findings.ai_configured:
        hint = f" ({findings.ai_provider_hint})" if findings.ai_provider_hint else ""
        print(f"  AI: configured{hint}")
    elif findings.coding_agents_available:
        print("  AI: no API key set — keyless coding agent available below")
    else:
        print("  AI: not configured")
    if findings.coding_agents_available:
        suggested = findings.suggested_keyless_provider()
        print(
            f"  Keyless: {', '.join(findings.coding_agents_available)} "
            f"— no API key needed (--llm-provider {suggested})"
        )
    if findings.installed_clis:
        print(f"  CLIs: {', '.join(findings.installed_clis[:6])}")
    if findings.cloud_credentials:
        print(f"  Cloud: {', '.join(findings.cloud_credentials)}")
    if findings.sample_data_candidates:
        print(f"  Sample data: {', '.join(findings.sample_data_candidates[:4])}")
    if findings.suggested_data_product_type:
        print(f"  Suggested: --data-product-type {findings.suggested_data_product_type}")


# ---------------------------------------------------------------------------
# Usage tracking — bumps forge_count for return-user detection
# ---------------------------------------------------------------------------


def bump_forge_count() -> int:
    """Increment ``~/.fluid/usage.json:forge_count`` and return the new value.

    Best-effort: a write failure (e.g. read-only home) returns ``0``
    silently rather than crashing the run.
    """
    try:
        path = Path.home() / ".fluid" / "usage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8") or "{}") or {}
        count = int(data.get("forge_count", 0) or 0) + 1
        data["forge_count"] = count
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return count
    except Exception as exc:  # noqa: BLE001
        LOG.debug("bump_forge_count_failed: %s", exc)
        return 0


__all__ = [
    "WelcomeFindings",
    "bump_forge_count",
    "render_welcome",
    "run_welcome_scan",
]
