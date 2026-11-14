# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

This module is a **tier-0 shared leaf**: its only intra-package import is the
sibling stdlib-only ``fluid_build._errors`` doc base, so importing it never
lands a heavy dependency on the cold ``fluid --help`` path, and neither ``cli``
nor ``build_runners`` pulls the other in through it. The public home used to be
``fluid_build.cli._error_catalog``; that module is now a backwards-compat
re-export shim.

Adding a new error code: see the "Error codes" checklist in CONTRIBUTING.md.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from fluid_build._errors import _DOC_BASE as DOC_BASE  # single source of truth for the docs base
from fluid_build._errors import doc_url


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
            "Pass a provider: --provider aws|databricks|gcp|local|snowflake",
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
            "Valid built-ins: aws, databricks, gcp, local, snowflake",
        ],
        "providers",
    ),
    "provider_load_failed": (
        [
            "The provider is declared but importing it failed (see the WARNING above)",
            "Reinstall the distribution named in the error, or its extra",
            "Run 'fluid providers --debug' for the discovery errors",
        ],
        "providers",
    ),
    "provider_retired": (
        [
            "Run 'fluid providers' to see the available providers",
            "CHANGELOG.md lists it under Removed, with its replacement",
        ],
        "providers",
    ),
    "apply_platform_unsupported": (
        [
            (
                "If a job outside forge-cli (a Flink job, say) writes that expose or is that "
                "build, declare the build with engine: flink (or properties.language: "
                "flink_sql) and outputs naming what it writes: forge-cli then leaves both to "
                "the job"
            ),
            (
                "--provider <name> (or FLUID_PROVIDER) applies the whole contract with the "
                "provider you name; the local provider writes a binding it does not serve as "
                "a placeholder file"
            ),
        ],
        "providers",
    ),
    "apply_plan_runs_outside_forge": (
        [
            (
                "In the default mode, apply the contract file instead (fluid apply "
                "contract.fluid.yaml): the local planner plans it, and leaves what a job "
                "outside forge-cli writes to that job"
            ),
        ],
        None,
    ),
    "provider_blocked_by_policy": (
        [
            "This provider is named in FLUID_PLUGINS_BLOCKLIST, or an "
            "FLUID_PLUGINS_ALLOWLIST is set and does not name it",
            "Run 'fluid plugins --role provider' to see the effective policy",
            "Unset or amend the env var to re-enable it",
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
            "Use a supported version (current line: 0.7.x) in fluidVersion or --schema-version",
            "Run 'fluid validate --list-versions' to see the supported versions",
        ],
        "schema-evolution",
    ),
    "schema_version_required": (
        [
            "Name the version: fluid validate --fragment KIND <file> --schema-version <version>",
            "Run 'fluid validate --list-versions' to see the supported versions",
        ],
        "schema-evolution",
    ),
    # ── validate --fragment ────────────────────────────────────────────────
    "fragment_file_required": (
        ["Give the kind, then the file: fluid validate --fragment expose <file>"],
        None,
    ),
    "fragment_bundle_unsupported": (
        ["Validate the bundle without --fragment: fluid validate <bundle>.tgz"],
        None,
    ),
    "fragment_kind_unknown": (
        [
            "Use one of the kinds the error lists, for example: fluid validate --fragment expose <file>"
        ],
        None,
    ),
    "fragment_env_unsupported": (
        [
            "Validate the whole contract with the overlay: fluid validate contract.fluid.yaml --env <env>"
        ],
        None,
    ),
    "fragment_has_pointers": (
        ["Validate the contract that uses the fragment: fluid validate contract.fluid.yaml"],
        None,
    ),
    "schema_not_available": (
        ["Run 'fluid validate --list-versions' to see the versions this CLI has"],
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
        ["Give --min-version a version constraint, for example --min-version '>=0.7.0'"],
        "schema-evolution",
    ),
    "invalid_max_version": (
        ["Give --max-version a version constraint, for example --max-version '<0.8.0'"],
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
    "apply_build_identifier_invalid": (
        [
            "Rename the id to fit, for example orders_v1 instead of 1orders, and apply again",
            (
                "A renamed id starts over: its builds get new cursors and watermarks under "
                ".fluid/runs/, and a renamed contract.id gets a new OpenTofu workdir under "
                ".fluid/iac/<provider>/"
            ),
        ],
        None,
    ),
    "apply_build_id_not_found": (
        [
            "Pass one of the available build ids as --build-id: ids match exactly, case included",
            "Run 'fluid apply <contract> --mode amend-and-build' without --build-id to run "
            + "every build",
        ],
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
    "opentofu_apply_failed": (
        [
            "OpenTofu records state as it goes, so resources created before the failure are "
            + "normally tracked",
            "Fix the cause named above, then re-run 'fluid apply' to continue",
            "Re-run with --debug to see the raw tofu output",
        ],
        None,
    ),
    "opentofu_adoption_failed": (
        [
            "Check the provider credentials and network access, then re-run 'fluid apply'",
            "The apply stopped before planning: it could not tell whether the object "
            + "exists, and creating over it would replace it",
        ],
        "troubleshooting#connectivity",
    ),
    "opentofu_region_moved": (
        [
            "If the resources should stay where they are, set the binding's location.region "
            + "to the region the error names",
            "If they should move, empty and remove them there first (tofu destroy in the "
            + "state directory the error names, with AWS_REGION set to the old region), "
            + "then apply again",
        ],
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
    "copilot_no_llm_provider": (
        [
            "Pass --llm-provider, for example anthropic, openai, gemini or ollama",
            "Or set FLUID_LLM_PROVIDER, or the API key of exactly one provider",
            "Or run 'fluid ai setup' to choose and save a provider",
            "Run 'fluid doctor' to see what forge would select",
        ],
        None,
    ),
    # Optional-dependency gate (fluid_build/_optional.py). The message already
    # carries the exact `pip install` line; these only give the stable slug a
    # curated follow-up and a docs anchor.
    "copilot_litellm_unavailable": (
        ["Run 'fluid doctor' to see which optional packages are installed"],
        None,
    ),
    "mcp_sdk_unavailable": (
        ["Run 'fluid doctor' to see which optional packages are installed"],
        None,
    ),
    # ``fluid scaffold-ide`` refusals. All but the last are raised before the command writes
    # a file; ``refusing_to_overwrite…`` is raised at the first existing file it would write.
    "claude_settings_not_valid_json": (
        [
            "Fix the JSON syntax in the named .claude/settings.json (or settings.local.json)",
            "Re-run: fluid scaffold-ide --target claude-code",
        ],
        None,
    ),
    "claude_settings_unexpected_shape": (
        [
            (
                "The named settings file must be a JSON object; if it has 'hooks', that must be "
                "an object, and 'hooks.PostToolUse' a list of objects"
            ),
            "Fix the file, then re-run: fluid scaffold-ide --target claude-code",
        ],
        None,
    ),
    "claude_settings_links_outside_project": (
        [
            (
                "The named Claude Code settings file is a symlink to a file outside this "
                "project; scaffold-ide would copy that file's content into the project"
            ),
            (
                "Replace the link with a regular file or delete it, then re-run: "
                "fluid scaffold-ide --target claude-code"
            ),
        ],
        None,
    ),
    "scaffold_ide_file_not_utf8": (
        [
            "Re-save the named CLAUDE.md or AGENTS.md as UTF-8",
            "Then re-run: fluid scaffold-ide --target <target>",
        ],
        None,
    ),
    "scaffold_ide_block_markers_malformed": (
        [
            (
                "The named CLAUDE.md or AGENTS.md must hold either no scaffold-ide markers or "
                "exactly one '<!-- BEGIN forge-cli scaffold-ide block -->' followed by one "
                "matching END marker"
            ),
            (
                "Delete the stray or duplicate marker lines (the file was left untouched), "
                "then re-run: fluid scaffold-ide --target <target>"
            ),
        ],
        None,
    ),
    "refusing_to_overwrite_existing_file_pass_force_to_override": (
        [
            "Re-run with --force to replace the file: fluid scaffold-ide --target <target> --force",
            "--force keeps the text of CLAUDE.md and AGENTS.md outside the scaffold-ide block",
            "Upgrading? --force also removes the steering files an earlier scaffold-ide wrote",
        ],
        None,
    ),
    "optional_dependency_missing": (
        ["Run 'fluid doctor' to see which optional packages are installed"],
        None,
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
            "Scaffold one with 'fluid init' or 'fluid forge'",
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
        [
            "The loader module must define the required entry-point functions — check its API",
            "Define the expected functions (e.g. load/run) — see the custom-loader docs",
        ],
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
    "generate_iac_provider_mismatch": (
        [
            "Drop --provider and let the cloud auto-detect from the contract's binding",
            "Or change the contract's exposes[].binding.platform to the cloud you meant "
            "to target — that is how a data product is retargeted, not --provider",
        ],
        None,
    ),
    "generate_iac_empty_module": (
        [
            "Check the contract's exposes[].binding carries the location fields "
            "the target cloud's emitter needs (bucket/dataset/database)",
            "Run 'fluid validate <contract>' — the Iceberg and Confluent binding "
            "gates name the specific field a skipped resource was missing",
            "Pass --allow-empty only if a resource-free module is genuinely intended",
        ],
        None,
    ),
    "generate_iac_aws_account_required": (
        [
            (
                "Set AWS_ACCOUNT_ID to the account the module will be applied in, then re-run "
                "'fluid generate iac' (it never looks the account up in AWS)"
            ),
            "'fluid apply' resolves the account itself and needs no AWS_ACCOUNT_ID",
        ],
        None,
    ),
    "generate_ci_failed": (
        [
            "Check the --system value is a supported CI provider and the contract validates",
            "Run 'fluid validate <contract>', then re-run with a supported "
            "--system (github_actions|gitlab|jenkins|azure_devops|...)",
        ],
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
    "policy_compiler_crashed": (
        [
            (
                "Run 'fluid validate <contract>': policy compile reads accessPolicy and "
                + "exposes without validating them against the contract schema"
            ),
            (
                "If the contract validates, re-run with 'fluid --log-level DEBUG policy compile "
                + "<contract>' to see the compiler's traceback"
            ),
        ],
        "policy-compile#errors",
    ),
    "policy_apply_failed": (
        ["Run 'fluid policy check <contract>' to surface the offending rule before apply"],
        "sovereignty",
    ),
    "sovereignty_violation": (
        [
            "Move the offending binding into an allowed region, or widen "
            "'sovereignty.allowedRegions' in the contract",
            "Run 'fluid validate <contract>' to see the same findings with full detail",
            (
                "Set 'sovereignty.enforcementMode: advisory' to report a region outside "
                + "'allowedRegions' without blocking; a region in 'deniedRegions' blocks in "
                + "every mode"
            ),
            (
                "Pass --no-check-sovereignty to write the plan anyway (logged at WARNING; "
                + "fluid apply keeps its own checks)"
            ),
        ],
        "sovereignty",
    ),
    # ── product authoring ──────────────────────────────────────────────────
    "product_new_failed": (
        [
            "Check the target directory is writable and the productType is valid (SDP/ADP/CDP)",
            "Pick an empty/new target dir and pass --data-product-type SDP|ADP|CDP explicitly",
        ],
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
        [
            "The signing target must be a bundle file, not a directory",
            "Produce one with 'fluid bundle <contract> --format tgz', then sign that .tgz",
        ],
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
        [
            "The --dags-dir value must be an existing directory",
            "Create it (mkdir -p) or point --dags-dir at your Airflow dags/ folder",
        ],
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
    # ── fluid test results table / dashboard (Databricks) ──────────────────
    "test_results_table_invalid": (
        [
            "Name the table as catalog.schema.table, e.g. --results-table ops.dq.fluid_test_results",
            "Each part may hold letters, digits and underscores only",
        ],
        None,
    ),
    "test_results_table_in_product_schema": (
        [
            "Put the results table in a schema of its own: every product writes to it, "
            + "and fluid apply owns each product's schema",
            "Create one with CREATE SCHEMA <catalog>.dq, then pass "
            + "--results-table <catalog>.dq.fluid_test_results",
        ],
        None,
    ),
    "test_results_table_mismatch": (
        [
            "The table exists without the fluid test results columns, so nothing was written",
            "Point --results-table at a new table name; forge creates it with the right columns",
        ],
        None,
    ),
    "test_results_write_failed": (
        [
            "Check DATABRICKS_HOST, the token or OAuth client, and DATABRICKS_WAREHOUSE_ID",
            "The schema must exist and this identity needs USE CATALOG, USE SCHEMA, "
            + "CREATE TABLE and MODIFY on it",
            "The test report above is unaffected: rerun fluid test once the write can succeed",
        ],
        None,
    ),
    "test_results_dashboard_needs_table": (
        [
            "--results-dashboard draws the --results-table table: pass both",
        ],
        None,
    ),
    "test_results_table_needs_native_engine": (
        [
            "--results-table records the native engine's per-rule results; drop --engine soda",
            "Or keep --engine soda and its JUnit or JSON output, without --results-table",
        ],
        None,
    ),
    "test_results_table_needs_live_checks": (
        [
            "--results-table records the live checks of a run, and --no-data runs none",
            "Drop --no-data to test and record, or drop --results-table",
        ],
        None,
    ),
    "test_results_dashboard_invalid": (
        [
            "Give a workspace path whose last part is the name, e.g. "
            + "--results-dashboard '/Workspace/Shared/fluid/Data quality'",
            "The path must start with / and have no empty, . or .. parts",
        ],
        None,
    ),
    "test_results_dashboard_path_taken": (
        [
            "Something other than a dashboard already sits at that workspace path",
            "Choose another --results-dashboard path, or move that object away",
        ],
        None,
    ),
    "test_results_dashboard_failed": (
        [
            "The results were written; only the dashboard step failed",
            "This identity needs CAN MANAGE on the dashboard (or write access to its folder) "
            + "and CAN USE on DATABRICKS_WAREHOUSE_ID",
        ],
        None,
    ),
    # ── apply safety gates ─────────────────────────────────────────────────
    "iceberg_catalog_move_blocked": (
        [
            (
                "Run the printed `tofu state rm` commands: they release the resources from "
                + "this contract's OpenTofu state and touch nothing in the cloud"
            ),
            "Then re-run fluid apply",
            (
                "The released Glue database/table or Snowflake EXTERNAL VOLUME stays in place; "
                + "delete it by hand only if nothing else uses it"
            ),
        ],
        "iceberg-catalog-move",
    ),
}


# ── contract validation findings ───────────────────────────────────────────
# Not CLIError events: each names the check of ``fluid validate`` that found a
# problem in a contract. ``fluid validate --format json`` lists every finding in
# ``issues[]`` with ``code`` = ``slug_for(event)``, ``fix`` = these suggestions and
# ``example`` = ``_EXAMPLES[event]`` (``fluid_build._validation_issues``). Kept in
# their own map, merged below, so the CLIError entries above stay one block.
_VALIDATION_GUIDANCE: Dict[str, Tuple[List[str], Optional[str]]] = {
    # authoring lint: what an unedited template, or a guess, leaves in a contract
    "contract_placeholder_email": (
        [
            "Use the owning team's real address, never a guess; if there is none, delete "
            + "the key (a contract needs no email: metadata.owner.email is optional)",
            "Never make an address up from a team or product name: an example.com, "
            + "company.com, test@ or noreply@ address reaches nobody",
        ],
        "validate",
    ),
    "contract_template_residue": (
        [
            "Replace the starter value with the product's own: an id of <domain>.<name>, "
            + "its owning team, its real build and its real output",
            "Delete what the product does not need, such as the starter's builds or "
            + "semantics, rather than keep the starter's",
        ],
        "validate",
    ),
    "contract_placeholder_value": (
        [
            "Replace the placeholder (TODO, FIXME, TBD, CHANGEME or <...>) with the real "
            + "value, or delete the key when it is optional",
            "When no value is known, choose the smallest valid one and note the "
            + "assumption instead of leaving a placeholder",
        ],
        "validate",
    ),
    "contract_sdp_joins": (
        [
            "An SDP (Bronze) reads one source as it is: if the product combines sources, set "
            + "metadata.productType to ADP and metadata.layer to Silver",
            "Otherwise read the one source without the join",
        ],
        "validate",
    ),
    "contract_sdp_consumes_products": (
        [
            "An SDP (Bronze) reads source systems, never other data products: if this "
            + "product builds on the product consumes[] names, set metadata.productType to "
            + "ADP and metadata.layer to Silver",
            "Otherwise delete the consumes[] entry and read the source system in the build",
        ],
        "validate",
    ),
    # JSON-Schema findings, by the keyword that failed
    "contract_schema_required": (
        [
            "Add the property the message names to the object at the pointer: the schema "
            + "requires it there",
            "fluid validate <contract> --show-schema prints the schema, with the required "
            + "properties of each object",
        ],
        "validate",
    ),
    "contract_schema_unknown_field": (
        [
            "Delete or rename the property the message names: the object at the pointer "
            + "does not take it",
            "Check its spelling and nesting against the schema: fluid validate <contract> "
            + "--show-schema",
        ],
        "validate",
    ),
    "contract_schema_type": (
        [
            "Change the value at the pointer to the type the message names: a string, a "
            + "number, true or false, a list or a mapping",
            "Quote a value YAML would read as another type (yes, no, on, off, 1.0, 02:00)",
        ],
        "validate",
    ),
    "contract_schema_enum": (
        [
            "Use one of the values the message lists, spelled exactly: case matters "
            + "(Bronze, not bronze)",
        ],
        "validate",
    ),
    "contract_schema_pattern": (
        [
            "Rewrite the value at the pointer so it matches the pattern the message shows",
            "A value that ends in a line break fails too: a YAML | block keeps one, |- drops it",
        ],
        "validate",
    ),
    "contract_schema_format": (
        [
            "Write the value in the format the message names (an email address, a date-time "
            + "such as 2026-11-13T02:00:00Z, a URI), or delete the key when it is optional",
        ],
        "validate",
    ),
    "contract_schema_no_variant": (
        [
            "The value at the pointer matches none of the shapes allowed there: compare it "
            + "with the schema (fluid validate <contract> --show-schema) and fix the field "
            + "that differs",
            "For a binding, the platform decides the format and the location fields it needs",
        ],
        "validate",
    ),
    "contract_schema_not_allowed": (
        [
            "Delete the property the message names: this object does not take it, often "
            + "because another property chose a variant without it",
        ],
        "validate",
    ),
    "contract_schema_bounds": (
        [
            "Bring the value at the pointer inside the limit the message names: a minimum "
            + "or maximum, a length, a number of items, or items that must be unique",
        ],
        "validate",
    ),
    "contract_schema_invalid": (
        [
            "Fix the value at the pointer as the message says; fluid validate <contract> "
            + "--show-schema prints the rule it broke",
        ],
        "validate",
    ),
    # streaming types and the managed Flink lane (schema_manager's whole-contract checks)
    "contract_streaming_types": (
        [
            "Give the nested column the type the message asks for: a struct its fields, an "
            + "array its element, a map its key and value, each with a type of its own",
        ],
        "validate",
    ),
    "contract_managed_flink": (
        [
            "Change the build or the expose the message names so the managed Flink lane "
            + "can run it; the message names the field and the value it needs",
        ],
        "validate",
    ),
    # contract rules (fluid validate's checks after JSON Schema)
    "contract_sovereignty": (
        [
            "Move the binding into a region that sovereignty.allowedRegions lists, or widen "
            + "allowedRegions when the data's residency rules allow it",
            "A region in sovereignty.deniedRegions is refused in every enforcement mode",
        ],
        "sovereignty",
    ),
    "contract_agent_policy": (
        [
            "Change exposes[].policy.agentPolicy as the message says: the models and use "
            + "cases it allows or denies must not contradict each other",
        ],
        "validate",
    ),
    "contract_product_type_inconsistent": (
        [
            "Make metadata.productType and metadata.layer agree: SDP with Bronze, ADP with "
            + "Silver, CDP with Gold (or set only one of them)",
        ],
        "validate",
    ),
    "contract_duplicate_id": (
        [
            "Give each output port its own contract id: change or delete the "
            + "exposes[].contract.id the message names",
        ],
        "validate",
    ),
    "contract_composition_violation": (
        [
            "Consume only products of a type this product accepts: SDP takes none, ADP "
            + "takes SDP and ADP, CDP takes SDP, ADP and CDP",
            "Or correct this product's metadata.productType if it was set wrong",
        ],
        "validate",
    ),
    "contract_iceberg_sink": (
        [
            "Fix the streaming Iceberg sink field the message names; the message gives the "
            + "value or the variable it needs",
        ],
        "validate",
    ),
    "contract_confluent_binding": (
        [
            "Fix the Confluent (Tableflow) binding field the message names; the message gives "
            + "the value it needs",
        ],
        "validate",
    ),
    "contract_databricks_binding": (
        [
            "Fix the Databricks binding or setting the message names (a SQL warehouse in "
            + "DATABRICKS_WAREHOUSE_ID, no workspace host in the contract)",
        ],
        "validate",
    ),
    "contract_iceberg_binding": (
        [
            "Fix the Iceberg binding field the message names: a catalog kind FLUID knows, "
            + "and one storage location per external volume",
        ],
        "validate",
    ),
    "contract_gcp_binding": (
        [
            "Fix the GCP binding field the message names; its remediation is part of the "
            + "message",
        ],
        "validate",
    ),
    "contract_governance": (
        [
            "Change the governance or access-policy field the message names, or bind the "
            + "expose to a platform that enforces it",
        ],
        "validate",
    ),
    "contract_vector_binding": (
        [
            "Fix the pgvector binding field the message names (binding.vectorConfig and the "
            + "columns it reads)",
        ],
        "validate",
    ),
    "contract_continuous_trigger": (
        [
            "Give a batch build a schedule trigger (type: schedule and a cron), or use an "
            + "engine that runs continuously",
        ],
        "validate",
    ),
    "contract_build_id": (
        [
            "Rename the id the message names: a letter or _ first, then at most 127 letters, "
            + "digits, _, - or .",
        ],
        "validate",
    ),
    "contract_packaging": (
        [
            "Fix the packaging block as the message says; plan, generate iac and apply decide "
            + "from the same block",
        ],
        "validate",
    ),
    "contract_semantics": (
        [
            "Write a measure's expr as the column it aggregates (agg: sum, expr: amount), "
            + "never as an aggregate (SUM(amount)), which aggregates twice",
        ],
        "validate",
    ),
    "contract_extension": (
        [
            "Fix the extensions block the message names: the plugin that owns that key "
            + "wrote the message",
        ],
        "validate",
    ),
    "contract_validator_finding": (
        [
            "The validator plugin named in brackets wrote this finding: do what it says, or "
            + "ask that plugin's owner",
        ],
        "validate",
    ),
    "contract_overlay": (
        [
            "Key the overlay's list entries by id (exposeId, id) instead of position, so "
            + "each patch lands on the entry it means",
        ],
        "validate",
    ),
    "contract_check_failed": (
        [
            "A check crashed, so the contract was NOT checked for what it covers: run "
            + "fluid validate <contract> --verbose and report the error with the contract",
        ],
        "validate",
    ),
    "contract_finding": (
        [
            "Do what the message says at the place it names, then run fluid validate again",
        ],
        "validate",
    ),
}
_GUIDANCE.update(_VALIDATION_GUIDANCE)

#: A minimal example of the fix for an event: the YAML to write (its first line names
#: where it goes when that is not the document root), or the command to run.
#: ``fluid validate --format json`` gives it as an issue's ``example``; every one is
#: valid FLUID and passes the authoring lint (``tests/cli/test_validate_issues.py``).
_OWNER = "metadata:\n  owner:\n    team: sales-data\n"
_CLASSIFIED = "metadata:\n  productType: SDP\n  layer: Bronze\n"
_LOCAL_BINDING = (
    "# exposes[].binding\nplatform: local\nformat: parquet\nlocation:\n"
    "  path: runtime/out/orders_raw.parquet\n"
)
_METADATA = "metadata:\n  productType: SDP\n  layer: Bronze\n  owner:\n    team: sales-data\n"
_EXPOSE = (
    "exposes:\n- exposeId: orders_raw\n  kind: table\n  binding:\n    platform: local\n"
    "    format: parquet\n    location:\n      path: runtime/out/orders_raw.parquet\n"
    "  contract:\n    schema:\n    - name: order_id\n      type: string\n"
)
_CONTRACT = (
    "fluidVersion: {FLUID_VERSION}\nkind: DataProduct\nid: sales.orders_raw\nname: orders_raw\n"
    + _OWNER
    + _EXPOSE
)
_COLUMN = "# exposes[].contract.schema[]\n- name: amount\n  type: decimal(12,2)\n  required: true\n"
_DQ = (
    "# exposes[].contract.dq\nrules:\n- id: order_id_unique\n  type: uniqueness\n"
    "  selector: order_id\n  threshold: 1.0\n  operator: '>='\n  severity: error\n"
)
_BUILD = (
    "# builds[]\n- id: ingest_orders\n  pattern: embedded-logic\n  engine: sql\n"
    "  properties:\n    sql: SELECT * FROM read_csv_auto('data/orders.csv', header=true)\n"
    "  execution:\n    trigger:\n      type: schedule\n      cron: 0 2 * * *\n"
    "    runtime:\n      platform: local\n"
)
_VERBOSE = "fluid validate contract.fluid.yaml --offline --verbose"
_SHOW_SCHEMA = "fluid validate contract.fluid.yaml --offline --show-schema"
#: A schema error's example is the block its pointer is in, written right (first match
#: wins); the event's own example when it is in none of them.
_BLOCK_EXAMPLES: Tuple[Tuple[str, str], ...] = (
    (r"/exposes/\d+/binding(/|\Z)", _LOCAL_BINDING),
    (r"/exposes/\d+/contract/schema(/|\Z)", _COLUMN),
    (r"/exposes/\d+/contract/dq(/|\Z)", _DQ),
    (r"/exposes(/|\Z)", _EXPOSE),
    (r"/builds(/|\Z)", _BUILD),
    (r"/metadata(/|\Z)", _METADATA),
    (r"(/[^/]+)?\Z", _CONTRACT),
)
_EXAMPLES: Dict[str, str] = {
    "contract_placeholder_email": _OWNER,
    "contract_template_residue": (
        "id: sales.orders_raw\nname: orders_raw\ndomain: sales\n" + _OWNER
    ),
    "contract_placeholder_value": (
        "description: Raw orders from the shop's daily CSV export, with types and checks.\n"
    ),
    "contract_sdp_joins": "metadata:\n  productType: ADP\n  layer: Silver\n",
    "contract_sdp_consumes_products": (
        "metadata:\n  productType: ADP\n  layer: Silver\n"
        "consumes:\n- productId: sales.orders_raw\n  exposeId: orders_raw\n"
    ),
    "contract_schema_required": _CONTRACT,
    "contract_schema_unknown_field": _CONTRACT,
    "contract_schema_type": _COLUMN,
    "contract_schema_enum": _CLASSIFIED,
    "contract_schema_pattern": "id: sales.orders_raw\n",
    "contract_schema_format": _OWNER,
    "contract_schema_no_variant": _LOCAL_BINDING,
    "contract_schema_not_allowed": _LOCAL_BINDING,
    "contract_schema_bounds": _DQ,
    "contract_schema_invalid": _SHOW_SCHEMA,
    "contract_streaming_types": _SHOW_SCHEMA,
    "contract_managed_flink": _VERBOSE,
    "contract_sovereignty": "sovereignty:\n  jurisdiction: EU\n  allowedRegions:\n  - europe-west1\n",
    "contract_agent_policy": (
        "exposes:\n- exposeId: orders_raw\n  policy:\n    agentPolicy:\n"
        "      allowedUseCases:\n      - analysis\n      deniedUseCases:\n      - training\n"
    ),
    "contract_product_type_inconsistent": _CLASSIFIED,
    "contract_duplicate_id": (
        "exposes:\n- exposeId: orders_raw\n  contract:\n    id: sales.orders_raw.orders_raw\n"
    ),
    "contract_composition_violation": (
        "metadata:\n  productType: ADP\n  layer: Silver\n"
        "consumes:\n- productId: sales.orders_raw\n  exposeId: orders_raw\n"
    ),
    "contract_iceberg_sink": _VERBOSE,
    "contract_confluent_binding": _VERBOSE,
    "contract_databricks_binding": (
        "# exposes[].binding\nplatform: databricks\nformat: delta_table\nlocation:\n"
        "  database: main\n  schema: sales\n  table: orders\n"
    ),
    "contract_iceberg_binding": _VERBOSE,
    "contract_gcp_binding": (
        "# exposes[].binding\nplatform: gcp\nformat: bigquery_table\nlocation:\n"
        "  project: acme-analytics\n  dataset: sales_raw\n  table: orders\n"
    ),
    "contract_governance": _VERBOSE,
    "contract_vector_binding": _VERBOSE,
    "contract_continuous_trigger": _BUILD,
    "contract_build_id": "id: sales.orders_raw\n",
    "contract_packaging": _VERBOSE,
    "contract_semantics": _VERBOSE,
    "contract_extension": _VERBOSE,
    "contract_validator_finding": _VERBOSE,
    "contract_overlay": "fluid validate contract.fluid.yaml --offline --env prod",
    "contract_check_failed": _VERBOSE,
    "contract_finding": "fluid validate contract.fluid.yaml --offline",
    # CLIError events that fluid validate also reports as findings
    "invalid_schema_version": "fluidVersion: {FLUID_VERSION}\n",
    "schema_not_available": "fluidVersion: {FLUID_VERSION}\n",
    "contract_version_unsupported": "fluidVersion: {FLUID_VERSION}\n",
    "validation_error": _VERBOSE,
    "optional_dependency_missing": "fluid doctor",
    "contract_file_not_found": "fluid validate contract.fluid.yaml --offline",
    "contract_load_failed": "fluid validate contract.fluid.yaml --offline",
}


def _docs_url(event: str, topic: Optional[str]) -> str:
    """Build the docs URL for a catalogued event.

    An explicit ``topic`` points at a real doc section; otherwise the URL is a
    per-slug anchor on the canonical troubleshooting page.
    """
    return doc_url(topic)


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


def is_catalogued(event: str) -> bool:
    """Whether ``event`` has curated guidance."""
    return event in _GUIDANCE


def fix_for(event: str) -> str:
    """The suggestions for ``event`` as one instruction, each ending in a period."""
    return " ".join(s.rstrip(".") + "." for s in suggestions_for(event))


def _with_version(example: str) -> str:
    """``example`` with the newest stable schema version in it: one typed here goes stale."""
    if "{FLUID_VERSION}" not in example:
        return example
    from fluid_build.schema_manager import FluidSchemaManager  # loaded by validate already

    return example.replace("{FLUID_VERSION}", FluidSchemaManager.latest_bundled_version())


def example_for(event: str, pointer: Optional[str] = None) -> Optional[str]:
    """A minimal example of the fix for ``event``: YAML to write, or a command.

    For a JSON-Schema finding at ``pointer``, the example of the block it is in (a
    binding, a column, a build, ...): the event alone does not say which block it is.
    """
    example = _EXAMPLES.get(event)
    if pointer is not None and event.startswith("contract_schema_"):
        example = next((e for p, e in _BLOCK_EXAMPLES if re.match(p, pointer)), example)
    return None if example is None else _with_version(example)


def block_examples() -> List[str]:
    """The examples a JSON-Schema finding can get by its place (for the catalog tests)."""
    return [_with_version(example) for _pattern, example in _BLOCK_EXAMPLES]


def validation_events() -> List[str]:
    """The events ``fluid validate`` reports a contract finding under (its issues' codes)."""
    return list(_VALIDATION_GUIDANCE.keys())
