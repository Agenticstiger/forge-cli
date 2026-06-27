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

"""Central error catalog: stable slugs + curated suggestions + docs URLs.

Every ``CLIError`` (and its ``FluidCLIError`` / ``CopilotGenerationError``
subclasses) is auto-enriched from this catalog at construction time, so a raise
site gets an actionable ``.suggestions`` list and a ``.docs_url`` *for free* —
without editing each of the ~150 raise sites across ``fluid_build/cli``. A
caller may always pass its own ``suggestions`` / ``docs_url`` and those win.

Two things are universal regardless of whether an event is catalogued:

* ``error_slug`` — a stable, uppercase ``ERR_<EVENT>`` code derived from the
  snake_case ``event`` key (the historical stable identity). It is rendered on
  every failure and logged in the structured ``extra``, so CI log parsers and
  dashboards can route on a code that survives across releases.

Design borrowed (adapt) from prior art surveyed in borrow-before-build:

* rustc's stable ``E####`` error-code registry + ``--explain`` (one index of
  codes, each with a doc anchor) — hence the single ``troubleshooting#<slug>``
  doc anchor convention below.
* dbt's "Docs at <URL>" on every failure — hence ``docs_url``.
* the GitHub CLI's actionable next-command hints (``Run `gh auth login` ...``)
  — hence concrete ``fluid <verb>`` suggestions rather than prose.

This module is a **stdlib-only leaf** (its only intra-package import is the
sibling stdlib-only ``_errors`` doc base) so importing it never lands a heavy
dependency on the cold ``fluid --help`` path.

Adding a new error code: see the "Error codes" checklist in CONTRIBUTING.md.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ._errors import _DOC_BASE as DOC_BASE  # single source of truth for the docs base


def slug_for(event: str) -> str:
    """Return the stable ``ERR_<EVENT>`` code for an event key.

    Deterministic and release-stable: ``provider_not_specified`` ->
    ``ERR_PROVIDER_NOT_SPECIFIED``. Non-alphanumeric runs collapse to ``_`` so
    the result is always a safe identifier for log routing.
    """
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in (event or "").strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)  # squeeze repeats/edges
    return "ERR_" + (cleaned.upper() or "UNKNOWN")


# event key -> (suggestions, optional explicit docs topic).
# When the docs topic is ``None`` the URL defaults to a per-slug anchor on the
# canonical troubleshooting page (``troubleshooting#err_<event>``), which is a
# page that exists today; explicit topics point at other real doc sections.
_GUIDANCE: Dict[str, Tuple[List[str], Optional[str]]] = {
    # ── providers ──────────────────────────────────────────────────────────
    "provider_not_specified": (
        [
            "Pass a provider: --provider local|gcp|snowflake|aws|azure",
            "Or set the FLUID_PROVIDER environment variable",
            "Run 'fluid providers' to list the available providers",
        ],
        "providers",
    ),
    "provider_not_found": (
        [
            "Run 'fluid providers' to see the available providers",
            "Check the provider name spelling",
            "Install the provider extra, e.g. pip install 'data-product-forge[gcp]'",
        ],
        "providers",
    ),
    "provider_unknown": (
        [
            "Run 'fluid providers' to see the available providers",
            "Valid built-ins: local, gcp, snowflake, aws, azure",
        ],
        "providers",
    ),
    "reserved_provider_name": (
        ["Choose a provider name that is not a reserved built-in"],
        "providers",
    ),
    # ── contracts ──────────────────────────────────────────────────────────
    "contract_required": (
        [
            "Pass the contract path: fluid <command> path/to/contract.fluid.yaml",
            "Or run from a directory that contains a single contract.fluid.yaml",
            "Scaffold one with 'fluid init' or 'fluid forge'",
        ],
        None,
    ),
    "contract_not_found": (
        [
            "Check that the contract file path is correct",
            "Ensure the file has a .yaml, .yml, or .json extension",
            "Scaffold one with 'fluid init' or 'fluid forge'",
        ],
        None,
    ),
    "contract_file_not_found": (
        [
            "Check that the contract file path is correct",
            "Run 'ls *.fluid.yaml' to see contracts in the current directory",
        ],
        None,
    ),
    "contract_load_failed": (
        [
            "Verify the YAML/JSON syntax is valid",
            "Check file permissions and that the encoding is UTF-8",
            "Run 'fluid validate <contract>' for a detailed report",
        ],
        None,
    ),
    # ── schema / version ───────────────────────────────────────────────────
    "invalid_schema_version": (
        [
            "Set a supported fluidVersion (current line: 0.7.x)",
            "Run 'fluid validate <contract>' to see the supported versions",
        ],
        "schema-evolution",
    ),
    "contract_version_unsupported": (
        [
            "Migrate the contract to the current 0.7.x schema",
            "Pre-0.7 contracts (0.4.0 / 0.5.x) are no longer supported",
        ],
        "schema-evolution",
    ),
    "invalid_min_version": (
        ["Use a valid PEP 440 / semver version string for the minimum bound"],
        "schema-evolution",
    ),
    "invalid_max_version": (
        ["Use a valid PEP 440 / semver version string for the maximum bound"],
        "schema-evolution",
    ),
    # ── validation ─────────────────────────────────────────────────────────
    "validation_failed": (
        [
            "Run 'fluid validate <contract> --verbose' for the full error list",
            "Check required fields and the schema reference for your fluidVersion",
        ],
        None,
    ),
    "validation_error": (
        ["Run 'fluid validate <contract> --verbose' for details"],
        None,
    ),
    # ── bundle ─────────────────────────────────────────────────────────────
    "bundle_not_found": (
        [
            "Run 'fluid bundle <contract> --format tgz' to produce a bundle first",
            "Check the bundle path you passed",
        ],
        None,
    ),
    "bundle_manifest_invalid": (
        ["Re-create the bundle with 'fluid bundle' — the manifest is malformed"],
        None,
    ),
    "bundle_missing_contract": (
        ["Re-create the bundle with 'fluid bundle' — it has no contract entry"],
        None,
    ),
    # ── plan / apply ───────────────────────────────────────────────────────
    "planner_failed": (
        ["Run 'fluid validate <contract>' first to rule out a contract problem"],
        None,
    ),
    "output_write_failed": (
        ["Check that the --out path is writable and the parent directory exists"],
        None,
    ),
    "opentofu_engine_install_failed": (
        [
            "Install OpenTofu >= 1.6.0 and ensure 'tofu' is on PATH",
            "See https://opentofu.org/docs/intro/install/",
        ],
        "installation",
    ),
    "opentofu_init_failed": (
        ["Check provider credentials and network access, then retry"],
        "troubleshooting#connectivity",
    ),
    "opentofu_plan_failed": (
        ["Run 'fluid plan <contract>' and inspect the emitted plan for the failing action"],
        None,
    ),
    # ── AI / copilot ───────────────────────────────────────────────────────
    "copilot_missing_llm_api_key": (
        [
            "Run 'fluid ai setup' to configure a provider interactively",
            "Or set a provider key: OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY",
            "For local models: --llm-provider ollama (no key required)",
            "For keyless authoring: run forge from your IDE (mcp-sampling) or "
            "--llm-provider claude-code",
        ],
        "secrets",
    ),
    "copilot_missing_llm_model": (
        [
            "Set FLUID_LLM_MODEL or pass --llm-model",
            "Run 'fluid ai setup' to pick a default model",
        ],
        None,
    ),
    "copilot_llm_model_preflight_failed": (
        [
            "Check the provider API key and network connectivity",
            "For Ollama, start the local server and pull the requested model",
            "Pass --llm-model with a known-available model",
        ],
        "troubleshooting#connectivity",
    ),
    "model_not_found": (
        [
            "Run 'fluid ai setup' to pick a model the provider actually serves",
            "List the provider's models, then pass --llm-model <name>",
        ],
        None,
    ),
    # ── contract / loader ──────────────────────────────────────────────────
    "missing_contract": (
        [
            "Pass the contract path: fluid <command> path/to/contract.fluid.yaml",
            "Or run from a directory that contains a single contract.fluid.yaml",
        ],
        None,
    ),
    "loader_import_failed": (
        [
            "Check that the loader module path is importable (on PYTHONPATH)",
            "Verify the module has no syntax/import errors: python -c 'import <module>'",
        ],
        None,
    ),
    "loader_missing_functions": (
        ["The loader module must define the required entry-point functions — check its API"],
        None,
    ),
    # ── plan / apply / generate ────────────────────────────────────────────
    "generate_iac_failed": (
        [
            "Run 'fluid validate <contract>' first to rule out a contract problem",
            "Re-run with --provider explicitly set if auto-detect picked the wrong cloud",
        ],
        None,
    ),
    "generate_ci_failed": (
        ["Check the --system value is a supported CI provider and the contract validates"],
        None,
    ),
    "no_builds": (
        [
            "No build runners matched — check the contract's build/transform config",
            "Run 'fluid apply <contract> --mode amend-and-build' only when builds are defined",
        ],
        None,
    ),
    "verify_failed": (
        ["Run 'fluid verify <contract> --verbose' to see which reconciliation check failed"],
        None,
    ),
    # ── policy ─────────────────────────────────────────────────────────────
    "policy_compile_failed": (
        ["Check the agent-policy block in the contract; run 'fluid policy check <contract>'"],
        "sovereignty",
    ),
    "policy_apply_failed": (
        ["Run 'fluid policy check <contract>' to surface the offending rule before apply"],
        "sovereignty",
    ),
    # ── product authoring ──────────────────────────────────────────────────
    "product_new_failed": (
        ["Check the target directory is writable and the productType is valid (SDP/ADP/CDP)"],
        None,
    ),
    "product_add_failed": (
        ["Run 'fluid validate' on the contract first; --type must be source|exposure|dq"],
        None,
    ),
    "product_add_expose_not_found": (
        [
            "The --expose target does not exist in the contract — list exposes with 'fluid status'",
            "Add the expose first, or target an existing exposeId",
        ],
        None,
    ),
    # ── bundle / market ────────────────────────────────────────────────────
    "bundle_source_missing": (
        ["Re-create the bundle with 'fluid bundle' — a referenced source file is absent"],
        None,
    ),
    "bundle_load_failed": (
        ["The bundle is corrupt or truncated — re-create it with 'fluid bundle <contract>'"],
        None,
    ),
    "market_discovery_failed": (
        [
            "Check network access to the marketplace endpoint (FLUID_MARKET_URL)",
            "Bundled blueprints work offline: 'fluid market --blueprints'",
        ],
        "troubleshooting#connectivity",
    ),
    "missing_blueprint_parameter": (
        ["The blueprint requires a parameter — pass it with --param key=value"],
        None,
    ),
    # ── signing / supply-chain ─────────────────────────────────────────────
    "signing_bundle_missing": (
        ["Produce the bundle first ('fluid bundle <contract> --format tgz'), then sign it"],
        "supply-chain",
    ),
    "signing_bundle_not_file": (
        ["The signing target must be a bundle file, not a directory"],
        "supply-chain",
    ),
    "signing_key_ref_empty": (
        [
            "Provide a signing key reference (e.g. a cosign key or KMS key URI)",
            "See the signing setup in the supply-chain docs",
        ],
        "supply-chain",
    ),
    # ── verify / validate-artifacts ────────────────────────────────────────
    "validate_artifacts_input_missing": (
        ["Pass the generated-artifacts path produced by 'fluid generate artifacts'"],
        None,
    ),
    # ── schedule-sync (Airflow handoff) ────────────────────────────────────
    "schedule_sync_dags_dir_missing": (
        ["Pass --dags-dir pointing at your Airflow DAGs directory"],
        None,
    ),
    "schedule_sync_dags_dir_not_directory": (
        ["The --dags-dir value must be an existing directory"],
        None,
    ),
    "schedule_sync_unhandled_scheme": (
        ["Use a supported destination scheme (file / scp / git+ssh) for --to"],
        None,
    ),
    # ── rollback ───────────────────────────────────────────────────────────
    "rollback_product_id_empty": (
        ["Pass the product id to roll back: fluid rollback <product-id>"],
        None,
    ),
}


def _docs_url(event: str, topic: Optional[str]) -> str:
    """Build the docs URL for a catalogued event.

    An explicit ``topic`` points at a real doc section; otherwise the URL is a
    per-slug anchor on the canonical troubleshooting page.
    """
    if topic:
        return f"{DOC_BASE}/{topic}"
    return f"{DOC_BASE}/troubleshooting#{slug_for(event).lower()}"


def suggestions_for(event: str) -> List[str]:
    """Curated suggestions for ``event`` ([] when the event is not catalogued)."""
    entry = _GUIDANCE.get(event)
    return list(entry[0]) if entry else []


def docs_url_for(event: str) -> Optional[str]:
    """Docs URL for ``event`` (``None`` when the event is not catalogued)."""
    entry = _GUIDANCE.get(event)
    return _docs_url(event, entry[1]) if entry else None


def enrich(
    event: str,
    suggestions: Optional[List[str]],
    docs_url: Optional[str],
) -> Tuple[List[str], Optional[str]]:
    """Fill blank ``suggestions`` / ``docs_url`` from the catalog.

    Caller-provided values always win — only empty/absent fields are filled.
    Returns the (possibly enriched) ``(suggestions, docs_url)`` pair.
    """
    if not suggestions:
        suggestions = suggestions_for(event)
    if not docs_url:
        docs_url = docs_url_for(event)
    return list(suggestions or []), docs_url


def catalogued_events() -> List[str]:
    """All event keys with curated guidance (used by the catalog tests)."""
    return list(_GUIDANCE.keys())
