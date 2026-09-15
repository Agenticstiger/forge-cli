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

"""Typed error catalog for user-facing errors (tier-0 shared leaf).

Every error is a class with five fields:

  - ``what``  — one-line summary of what happened
  - ``where`` — file:line:col when applicable, else None
  - ``why``   — underlying cause
  - ``fix``   — concrete remediation
  - ``doc``   — stable doc URL

Rendered via ``rich`` for TTY output; emitted as a stable JSON shape under
``--json`` so tooling (CI, IDEs) can act on the same fields without scraping
text. No raw stack traces in user-facing output unless ``--debug`` is set.

This module is a **tier-0 shared leaf** — stdlib-only (``rich`` is imported
lazily inside :meth:`FluidUserError.render`), with no ``fluid_build.*``
upstreams. It lives at the bottom of the import graph so both ``cli`` and
``build_runners`` (and ``forge`` and the providers) can import the runtime
error catalog without inducing the ``build_runners → cli`` edge that the
``[tool.importlinter]`` contracts forbid. The public home for these names
used to be ``fluid_build.cli._errors``; that module is now a backwards-compat
re-export shim so external imports stay stable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_DOC_SITE = "https://agenticstiger.github.io/forge_docs"
# Re-exported by ``_error_catalog`` and asserted as a prefix by its tests.
_DOC_BASE = _DOC_SITE

# Topic -> a route the docs site ACTUALLY SERVES.
#
# What this replaces was `f"{base}/{topic}"`, and both halves were wrong. The
# base was `forge.fluid.dev`, which does not resolve — so all 54 catalogued
# events and all 15 typed errors below printed a link to nothing. And composing
# the path from a topic word manufactured a plausible URL whether or not anyone
# had written that page, which is why swapping the host alone would only have
# traded a dead domain for 404s: eleven of the sixteen topics had no page.
#
# `test_docs_urls_are_well_formed_under_doc_base` passed throughout, because
# "well formed" meant "starts with the base the function had just prefixed".
# Composition is what made a missing page indistinguishable from a real one, so
# the fix is a map: a topic with no entry goes to the troubleshooting page
# rather than to a URL invented from its own name.
_DOC_ROUTES = {
    # dedicated pages, where one exists
    "capabilities": "advanced/capability-warnings.html",
    "cost": "advanced/cost-tracking.html",
    "installation": "getting-started/",
    "providers": "cli/providers.html",
    "secrets": "cli/secrets.html",
    "sovereignty": "concepts/sovereignty.html",
    "sovereignty#residency": "concepts/sovereignty.html",
    "supply-chain": "cli/verify-signature.html",
    # otherwise the reference page that documents these fifteen errors by name,
    # anchored to the section the error is listed under
    "acquisition": "advanced/typed-cli-errors.html#validation-schema",
    "schema-evolution": "advanced/typed-cli-errors.html#validation-schema",
    "installation#extras": "advanced/typed-cli-errors.html#capability-negotiation",
    "troubleshooting#connectivity": "advanced/typed-cli-errors.html#connectivity-secrets",
    "concurrency": "advanced/typed-cli-errors.html#pipeline-operations",
    "dlq": "advanced/typed-cli-errors.html#pipeline-operations",
    "replay": "advanced/typed-cli-errors.html#pipeline-operations",
    "infra#drift": "advanced/typed-cli-errors.html#governance",
}
_DOC_FALLBACK = "advanced/production-troubleshooting.html"


def doc_url(topic: Optional[str] = None) -> str:
    """Absolute docs URL for ``topic``; the troubleshooting page when unmapped.

    Never composes a path out of an unknown topic: an unmapped topic is a page
    nobody has written, and guessing its URL is how a link rots invisibly.
    """
    return f"{_DOC_SITE}/{_DOC_ROUTES.get(topic or '', _DOC_FALLBACK)}"


@dataclass
class FluidUserError(Exception):
    """Base class for typed user-facing errors. Emit via ``render`` / ``as_json``."""

    what: str
    why: str
    fix: str
    doc: str
    where: Optional[str] = None
    code: str = "FluidUserError"
    extras: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.what)

    def as_json(self) -> str:
        return json.dumps(
            {
                "code": self.code,
                "what": self.what,
                "where": self.where,
                "why": self.why,
                "fix": self.fix,
                "doc": self.doc,
                "extras": self.extras,
            },
            sort_keys=True,
        )

    def render(self, *, color: bool = True) -> str:
        """Render to a string suitable for human consumption.

        ``rich`` is used when available; otherwise plain text. The renderer
        intentionally avoids any cross-cutting state — it returns a string so
        the caller can route it to stderr, a logger, or a buffer.
        """
        try:
            from rich.console import Console
            from rich.panel import Panel
            from rich.text import Text
        except Exception:
            return self._render_plain()

        from io import StringIO

        buf = StringIO()
        console = Console(file=buf, force_terminal=color, no_color=not color, width=100)
        body = Text()
        if self.where:
            body.append("in   ", style="dim")
            body.append(f"{self.where}\n")
        body.append("why  ", style="dim")
        body.append(f"{self.why}\n")
        body.append("fix  ", style="dim")
        body.append(f"{self.fix}\n")
        body.append("doc  ", style="dim")
        body.append(f"{self.doc}")
        console.print(Panel(body, title=f"✗ {self.what}", border_style="red"))
        return buf.getvalue()

    def _render_plain(self) -> str:
        out = [f"✗ {self.what}"]
        if self.where:
            out.append(f"  in   {self.where}")
        out.append(f"  why  {self.why}")
        out.append(f"  fix  {self.fix}")
        out.append(f"  doc  {self.doc}")
        return "\n".join(out)


# ── Concrete error classes ──────────────────────────────────────────────


@dataclass
class SchemaValidationError(FluidUserError):
    code: str = "SchemaValidationError"

    @classmethod
    def for_field(
        cls,
        *,
        contract_path: str,
        line: Optional[int],
        col: Optional[int],
        field_path: str,
        message: str,
    ) -> "SchemaValidationError":
        where = contract_path
        if line is not None:
            where += f":{line}"
            if col is not None:
                where += f":{col}"
        return cls(
            what=f"{field_path}: {message}",
            where=where,
            why=f"The contract field {field_path} did not satisfy the v0.7.3 schema.",
            fix=f"Adjust {field_path} to match the schema. Run `fluid validate <contract>` for the full error list.",
            doc=doc_url("acquisition"),
        )


@dataclass
class CapabilityMismatchError(FluidUserError):
    code: str = "CapabilityMismatchError"

    @classmethod
    def for_runner(
        cls,
        *,
        runner_name: str,
        asked: list[str],
        declared: list[str],
        suggestion: Optional[str] = None,
    ) -> "CapabilityMismatchError":
        missing = sorted(set(asked) - set(declared))
        return cls(
            what=f"runner `{runner_name}` does not support capability {missing}",
            why=(
                f"build asks for capabilities={sorted(asked)}; runner declares {sorted(declared)}."
            ),
            fix=(
                suggestion
                or f"Switch engine to one declaring {missing}, or remove from build.capabilities."
            ),
            doc=doc_url("capabilities"),
        )


@dataclass
class SecretResolutionError(FluidUserError):
    code: str = "SecretResolutionError"

    @classmethod
    def for_ref(
        cls, *, ref: str, reason: str, fix: Optional[str] = None
    ) -> "SecretResolutionError":
        return cls(
            what=f"failed to resolve secret `{ref}`",
            why=reason,
            fix=fix or "Configure the secret backend (vault, aws, gcp, azure, env) and retry.",
            doc=doc_url("secrets"),
        )


@dataclass
class SovereigntyViolationError(FluidUserError):
    code: str = "SovereigntyViolationError"

    @classmethod
    def for_connector(cls, *, connector: str, jurisdiction: str) -> "SovereigntyViolationError":
        return cls(
            what=f"connector `{connector}` is not allowed in jurisdiction {jurisdiction}",
            why=(
                f"contract.sovereignty.jurisdiction = {jurisdiction!r}; connector is not on the "
                f"allow-list."
            ),
            fix=(
                f"Use a connector image approved for {jurisdiction}, or update sovereignty.jurisdiction."
            ),
            doc=doc_url("sovereignty"),
        )


@dataclass
class ConnectivityProbeError(FluidUserError):
    code: str = "ConnectivityProbeError"

    @classmethod
    def for_target(cls, *, target: str, reason: str) -> "ConnectivityProbeError":
        return cls(
            what=f"connectivity probe failed: {target}",
            why=reason,
            fix="Check VPN / network policy / credentials. Run `fluid doctor --scope ingestion`.",
            doc=doc_url("troubleshooting#connectivity"),
        )


@dataclass
class PartialFailureError(FluidUserError):
    code: str = "PartialFailureError"

    @classmethod
    def for_streams(cls, *, succeeded: list[str], failed: list[str]) -> "PartialFailureError":
        return cls(
            what="partial success: some streams failed",
            why=f"succeeded={succeeded}; failed={failed}.",
            fix="Inspect logs for failed streams (`fluid logs <product> --component build`) and replay (`fluid apply --replay --run-id last-failure`).",
            doc=doc_url("replay"),
        )


@dataclass
class DLQOverflowError(FluidUserError):
    code: str = "DLQOverflowError"

    @classmethod
    def for_run(cls, *, count: int, cap: int, alerts: list[str]) -> "DLQOverflowError":
        return cls(
            what="DLQ overflow",
            why=f"{count} records exceeded maxRecordsBeforeAbort={cap}; alerts={alerts}.",
            fix=(
                "Inspect DLQ contents (`fluid logs <product> --component dlq`); fix upstream "
                "data quality or raise the cap; rerun via `fluid apply --replay --include-dlq`."
            ),
            doc=doc_url("dlq"),
        )


@dataclass
class SchemaDriftError(FluidUserError):
    code: str = "SchemaDriftError"

    @classmethod
    def for_diff(
        cls, *, baseline_digest: str, current_digest: str, summary: str
    ) -> "SchemaDriftError":
        return cls(
            what="source schema drift detected",
            why=f"baseline={baseline_digest} current={current_digest}; {summary}",
            fix=(
                "Review contract.exposes[].contract.schemaPolicy; if policy=evolve_safe, this is "
                "expected. If strict/discover_and_freeze, update the contract or fix the source."
            ),
            doc=doc_url("schema-evolution"),
        )


@dataclass
class BudgetExceededError(FluidUserError):
    code: str = "BudgetExceededError"

    @classmethod
    def for_cap(cls, *, dimension: str, used: int, cap: int) -> "BudgetExceededError":
        return cls(
            what=f"monthly budget exceeded: {dimension}",
            why=f"used={used}, cap={cap}.",
            fix=(
                "Raise properties.cost.budget.monthly cap, or set onExceed=warn to allow runs to "
                "proceed with an alert."
            ),
            doc=doc_url("cost"),
        )


@dataclass
class LockHeldError(FluidUserError):
    code: str = "LockHeldError"

    @classmethod
    def for_resource(cls, *, holder: str, scope: str, resource_id: str) -> "LockHeldError":
        return cls(
            what=f"lock held: {scope}:{resource_id}",
            why=f"another run (holder={holder}) holds the single-flight lock.",
            fix=(
                "Wait for the holder to finish, change concurrency.lock.onContended to 'queue', or "
                "release manually if the holder is gone."
            ),
            doc=doc_url("concurrency"),
        )


@dataclass
class StaleReplayError(FluidUserError):
    code: str = "StaleReplayError"

    @classmethod
    def for_run(cls, *, run_id: str, retention_horizon: str) -> "StaleReplayError":
        return cls(
            what=f"replay target {run_id} is past retention horizon",
            why=f"retention.runState={retention_horizon} elapsed; manifest no longer available.",
            fix="Pick a more recent run, or extend retention.runState.",
            doc=doc_url("replay"),
        )


@dataclass
class MissingExtraError(FluidUserError):
    code: str = "MissingExtraError"

    @classmethod
    def for_extra(cls, *, extra: str, install_hint: str) -> "MissingExtraError":
        return cls(
            what=f"optional extra '{extra}' is not installed",
            why=f"the requested engine requires the '{extra}' extra; it isn't on the import path.",
            fix=install_hint,
            doc=doc_url("installation#extras"),
        )


@dataclass
class InfraDriftError(FluidUserError):
    code: str = "InfraDriftError"

    @classmethod
    def for_chart(cls, *, chart: str, declared: str, live: str) -> "InfraDriftError":
        return cls(
            what=f"infrastructure drift: {chart}",
            why=f"declared chart version={declared}; live cluster version={live}.",
            fix="Run `fluid plan <contract>` and review the Infrastructure section, then `fluid apply`.",
            doc=doc_url("infra#drift"),
        )


@dataclass
class ResidencyViolationError(FluidUserError):
    code: str = "ResidencyViolationError"

    @classmethod
    def for_transfer(
        cls, *, from_region: str, to_region: str, jurisdiction: str
    ) -> "ResidencyViolationError":
        return cls(
            what=f"data residency violation: {from_region} → {to_region}",
            why=f"sovereignty.dataResidency.prohibitTransferTo includes {to_region} for {jurisdiction}.",
            fix=(
                "Use a destination in an allowed region, update sovereignty.dataResidency, or "
                "obtain compliance approval before changing."
            ),
            doc=doc_url("sovereignty#residency"),
        )


@dataclass
class SupplyChainViolationError(FluidUserError):
    code: str = "SupplyChainViolationError"

    @classmethod
    def for_image(cls, *, image_ref: str, reason: str) -> "SupplyChainViolationError":
        return cls(
            what=f"image signature verification failed: {image_ref}",
            why=reason,
            fix=(
                "Pin a Cosign-signed image with the configured public key, or update "
                "sovereignty.allowedSigners."
            ),
            doc=doc_url("supply-chain"),
        )
