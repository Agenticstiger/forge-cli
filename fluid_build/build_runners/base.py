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

"""Shared base for build_runners: constants, helpers, and the top-level
``run_builds_from_args`` dispatcher.

This module is the runtime-execution counterpart of
``fluid_build.engines.*`` (which generates dbt/SQL project files at
``fluid generate speed-transformation`` time). See
``fluid_build/engines/__init__.py`` for the generation-side framework.

``cli/apply.py`` calls :func:`run_builds_from_args` directly under
``--mode amend-and-build``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

from fluid_build._console import cprint, success
from fluid_build._console import error as console_error
from fluid_build._contract_loader import CLIError, load_contract_with_overlay

LOG = logging.getLogger("fluid.build_runners")

# ``{{ env.NAME }}`` placeholder substitution — resolved against
# ``os.environ`` at run time. Used by both the dbt profile generator and
# the dbt command builder to expand author-supplied vars/resources
# references.
ENV_PLACEHOLDER_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Used by the dbt command-log renderer to decide whether the value of an
# ``-e KEY=VALUE`` pair should be redacted. A structural hack: if the KEY
# looks sensitive (password/token/etc.), the value is replaced with
# ``<redacted>`` before being printed. Keep this in sync with the module
# that owns the renderer (``build_runners.dbt.runner._render_command_for_log``).
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?i)(password|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|auth)"
)


def _resolve_env_placeholders(value: Any) -> Any:
    """Resolve ``{{ env.NAME }}`` placeholders against the current environment.

    Recurses into lists and dicts. Non-string/list/dict values are returned
    unchanged. A missing env var resolves to the empty string (not ``None``)
    so downstream YAML dumps and command-line args don't carry a literal
    ``None`` token.
    """
    if isinstance(value, str):
        return ENV_PLACEHOLDER_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_placeholders(item) for key, item in value.items()}
    return value


def is_dbt_build(build: Dict[str, Any]) -> bool:
    """Return True when a build should execute as a dbt project.

    Accepts ``dbt`` plus any ``dbt-<adapter>`` variant (``dbt-bigquery``,
    ``dbt-snowflake``, ``dbt-redshift``, ``dbt-postgres``, ``dbt-duckdb``, …).
    The adapter is selected by dbt itself from ``profiles.yml``; the engine
    name just routes the build into the dbt execute path here.
    """
    engine = (build.get("engine") or "").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


def is_embedded_sql_build(build: Dict[str, Any]) -> bool:
    """Return True for builds with inline SQL (``engine: sql`` or
    ``pattern: embedded-logic`` + inline ``properties.sql``).

    These builds carry their SQL directly in the contract and are executed
    by the local provider's DuckDB engine.  They do NOT require an external
    Python script, so the python-runner's ``resolve_script_path`` lookup
    is the wrong dispatch — it always returns ``None`` and emits the
    confusing "Script not found: ingest.py" warning.

    Bug A4-A: ``--mode amend-and-build`` delegated to ``run_builds_from_args``
    which fell through to the Python-runner branch for embedded-SQL builds.
    """
    engine = (build.get("engine") or "").strip().lower()
    pattern = (build.get("pattern") or "").strip().lower()
    if engine == "sql":
        return True
    if pattern == "embedded-logic":
        props = build.get("properties") or {}
        return bool(props.get("sql"))
    return False


# Engines that ship as acquisition runners under build_runners/<engine>/.
ACQUISITION_ENGINES = frozenset(
    {"duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"}
)


def _declared_capabilities_for_engine(engine: str):
    """Return the set of capabilities the named engine declares.

    Returns None when we can't import the runner (so the caller skips the
    capability gate rather than failing loudly on an unrelated import
    issue). The lazy import keeps fluid CLI startup fast.
    """
    runner_modpaths = {
        "duckdb": ("fluid_build.build_runners.duckdb.runner", "DuckdbRunner"),
        "dlt": ("fluid_build.build_runners.dlt.runner", "DltRunner"),
        "meltano": ("fluid_build.build_runners.meltano.runner", "MeltanoRunner"),
        "airbyte": ("fluid_build.build_runners.airbyte.runner", "AirbyteRunner"),
        "kafka-connect": (
            "fluid_build.build_runners.kafka_connect.runner",
            "KafkaConnectRunner",
        ),
        "debezium": ("fluid_build.build_runners.debezium.runner", "DebeziumRunner"),
    }
    target = runner_modpaths.get(engine)
    if not target:
        return None
    try:
        import importlib

        mod = importlib.import_module(target[0])
        cls = getattr(mod, target[1], None)
        if cls is None:
            return None
        decl = getattr(cls, "declared_capabilities", None)
        if decl is None:
            return None
        return [str(c.value if hasattr(c, "value") else c) for c in decl]
    except Exception:  # noqa: BLE001
        return None


def is_acquisition_build(build: Dict[str, Any]) -> bool:
    """Return True for builds with ``pattern: acquisition`` AND a known runner."""
    if (build.get("pattern") or "").strip().lower() != "acquisition":
        return False
    engine = (build.get("engine") or "").strip().lower()
    return engine in ACQUISITION_ENGINES


# Acquisition-engine registry: ``engine name → (module path, callable name)``.
# Adding a new engine is one entry here + a runner module — no edits to
# the dispatcher's switch chain. Modules are imported lazily so a missing
# optional extra (e.g. ``dlt`` not installed) doesn't abort the import
# of this whole module.
_ACQUISITION_RUNNER_REGISTRY: Dict[str, tuple] = {
    "duckdb": (".duckdb.runner", "execute_duckdb_build"),
    "dlt": (".dlt.runner", "execute_dlt_build"),
    "meltano": (".meltano.runner", "execute_meltano_build"),
    "airbyte": (".airbyte.runner", "execute_airbyte_build"),
    "kafka-connect": (".kafka_connect.runner", "execute_kafka_connect_build"),
    "debezium": (".debezium.runner", "execute_debezium_build"),
}


def _execute_acquisition_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool,
    sample_rows: Any = None,
) -> int:
    """Dispatch an acquisition build to its runner.

    Looks up the engine in ``_ACQUISITION_RUNNER_REGISTRY`` and invokes
    the registered callable. Capability negotiation runs first so a
    contract asking for an unsupported capability fails with the rich
    ``CapabilityMismatchError`` instead of running the wrong shape.
    """
    engine = (build.get("engine") or "").strip().lower()

    # Capability negotiation: each runner declares the capabilities it
    # supports via ``declared_capabilities``. If the build asks for one
    # the runner doesn't declare, raise the typed catalog error so the
    # user sees the five-field Panel pointing to a runner that does.
    asked = [str(c) for c in (build.get("capabilities") or [])]
    if asked:
        declared = _declared_capabilities_for_engine(engine)
        if declared is not None and not set(asked).issubset(set(declared)):
            from fluid_build._errors import CapabilityMismatchError

            raise CapabilityMismatchError.for_runner(
                runner_name=engine,
                asked=asked,
                declared=list(declared),
            )

    entry = _ACQUISITION_RUNNER_REGISTRY.get(engine)
    if entry is None:
        LOG.error(
            "acquisition.engine_not_implemented engine=%s build=%s",
            engine,
            build.get("id"),
        )
        return 1

    # Resolve ``{{ env.X }}`` placeholders across the ENTIRE build + contract
    # before the runner sees them. Some runners do their own per-slice
    # resolution (e.g. dlt source_dict at runner.py:480) — those calls are
    # left in place as a defence-in-depth fallback for runners invoked
    # outside this dispatcher. Doing it once HERE guarantees every runner
    # gets resolved values for every nested field (source.connection.*,
    # properties.<engine>.*, sink.*, delivery.*, schemaEvolution.*, …) so
    # operators don't need to memorise which fields support templating.
    #
    # Secrets policy: this is the runtime path; values stay in process
    # memory and never get serialised to a remote catalog, so we resolve
    # everything (including secret-shaped vars). Catalog-export paths use
    # ``cli/_common.py::resolve_contract_env_templates`` instead, which
    # leaves sensitive placeholders literal.
    build = _resolve_env_placeholders(build)
    contract = _resolve_env_placeholders(contract)

    module_path, function_name = entry
    import importlib

    module = importlib.import_module(module_path, package="fluid_build.build_runners")
    runner_fn = getattr(module, function_name)
    return runner_fn(
        build,
        contract,
        contract_dir,
        dry_run=dry_run,
        sample_rows=sample_rows,
    )


def _execute_embedded_sql_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    dry_run: bool = False,
) -> int:
    """Execute an embedded-SQL build via the local provider's DuckDB engine.

    Embedded-SQL builds (``engine: sql`` / ``pattern: embedded-logic`` +
    ``properties.sql``) carry their transformation inline and do not ship a
    Python driver script.  They are materialised directly by the local
    provider, not by the python runner.

    Returns 0 on success, 1 on failure.
    """
    import time

    build_id = build.get("id", "unknown")
    cprint(f"\n{'─' * 60}")
    cprint(f"🔷 Build '{build_id}' (embedded-SQL / local DuckDB)")

    if dry_run:
        props = build.get("properties") or {}
        sql_preview = (props.get("sql") or "").strip()[:200]
        cprint(f"   [DRY RUN] Would execute SQL:\n{sql_preview}")
        return 0

    try:
        from fluid_build.providers.local.local import LocalProvider

        provider = LocalProvider(project="local", region="local")
        # Derive actions from the single build; wrap the build as a
        # mini-contract so _derive_actions_from_contract can find inputs
        # and outputs.
        mini_contract = {
            "id": contract.get("id", "product"),
            "builds": [build],
            "consumes": contract.get("consumes", []),
            "exposes": contract.get("exposes", []),
        }
        actions = provider._derive_actions_from_contract(mini_contract)
        t0 = time.time()
        result = provider.apply(actions=actions, plan={"contract": mini_contract})
        elapsed = round(time.time() - t0, 2)

        applied = result.get("applied", 0)
        failed = result.get("failed", 0)
        if failed == 0:
            cprint(f"   ✅ Completed in {elapsed}s — {applied} action(s) executed")
            written_files = []
            for r in result.get("results") or []:
                if r.get("status") == "ok":
                    written_files.extend(r.get("written", []))
            for p in written_files:
                cprint(f"   📁 {p}")
            return 0
        else:
            cprint(f"   ❌ Failed: {failed} action(s) failed")
            return 1
    except Exception as exc:
        cprint(f"   ❌ Embedded-SQL build '{build_id}' error: {exc}")
        LOG.exception("embedded_sql_build_error build_id=%s", build_id)
        return 1


def run_builds_from_args(
    args: argparse.Namespace,
    logger: logging.Logger,
    *,
    force_run: bool = False,
) -> int:
    """Execute builds from a FLUID contract.

    Loads the contract, filters by ``args.build_id`` if present, and
    dispatches each build to the dbt or python engine via
    :func:`is_dbt_build`. Returns 0 if no build failed, 1 otherwise.

    ``force_run=True`` (the default when called from ``fluid apply --build``)
    forces scheduled builds to run once — normally the scheduler owns the
    run, but apply may legitimately kick off a one-shot refresh.
    """
    # Deferred imports to avoid circular import at module-load time:
    # base.py -> python.runner -> base.py (for _resolve_env_placeholders).
    from .dbt.runner import execute_dbt_build, resolve_dbt_project_path
    from .python.runner import execute_build, resolve_script_path

    global LOG
    LOG = logger

    contract_path = Path(args.contract)

    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    # Two input shapes are supported:
    #   1. ``<contract>.yaml`` — load via the standard FLUID loader
    #      (handles overlays, $ref bundling, alias normalization).
    #   2. ``<plan>.json`` — the build runner is being invoked from
    #      ``fluid apply <plan>.json --mode amend-and-build``. The plan
    #      embeds the FULL FLUID contract under ``plan["contract"]``
    #      (with ``builds[]`` intact) so we extract it directly. Without
    #      this branch the standard "load contract from path" path would
    #      treat plan.json as a contract, find no ``builds`` key, and
    #      log "No builds defined in contract" — leaving acquisition /
    #      hybrid-reference dbt builds as silent no-ops on the canonical
    #      stage-7 path the lab Taskfile uses.
    if str(contract_path).endswith(".json"):
        LOG.info(f"Loading contract from execution plan: {contract_path}")
        try:
            plan_data = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIError(
                1, "contract_load_failed", {"path": str(contract_path), "error": str(exc)}
            )
        contract = plan_data.get("contract") or {}
        if not contract:
            raise CLIError(
                1,
                "plan_missing_contract",
                {
                    "path": str(contract_path),
                    "hint": (
                        "plan.json has no embedded ``contract`` key. "
                        "Re-run ``fluid plan <contract>.yaml --out <plan>.json`` "
                        "with a current forge-cli; older plan generators omit it."
                    ),
                },
            )
        # Re-anchor ``contract_path`` at the original source file so build
        # runners can resolve contract-relative paths (e.g.
        # ``repository: ../../reference-assets/dbt_dv2_subscriber360``).
        # Without this, ``contract_path.parent`` below would point at
        # ``runtime/`` (where plan.json lives) and dbt project lookups
        # would resolve wrong. The source path is recorded by ``fluid
        # plan`` in ``contract_metadata.source_path``.
        source_path_str = (plan_data.get("contract_metadata") or {}).get("source_path")
        if source_path_str:
            source_path = Path(source_path_str)
            if source_path.exists():
                LOG.info(f"Anchoring builds at source contract dir: {source_path.parent}")
                contract_path = source_path
            else:
                LOG.warning(
                    "plan source_path %s no longer exists; anchoring at plan dir %s "
                    "(relative paths in builds may not resolve)",
                    source_path,
                    contract_path.parent,
                )
        else:
            LOG.warning(
                "plan.json has no contract_metadata.source_path; anchoring at plan "
                "dir %s. Relative paths in builds (e.g. dbt repository) may not "
                "resolve. Re-run ``fluid plan`` with a current forge-cli to embed "
                "the source path.",
                contract_path.parent,
            )
        # Mirror the YAML loader's env-template + alias normalization that
        # ``load_contract_with_overlay`` would have done. Plan-embedded
        # contracts are still authored with ``{{ env.X }}`` placeholders;
        # the build runner needs them resolved before passing to engine
        # SDKs (dlt destination spec, dbt vars, target-snowflake config).
        # Apply the same secret-aware resolver the publish path uses so we
        # don't accidentally exfiltrate secret-named placeholders.
        try:
            from fluid_build._contract_loader import resolve_contract_env_templates

            contract = resolve_contract_env_templates(contract)
        except Exception as exc:  # noqa: BLE001 — defensive
            LOG.debug("env template resolution failed (non-fatal): %s", exc)
    else:
        # Standard YAML contract path.
        LOG.info(f"Loading contract: {contract_path}")
        try:
            contract = load_contract_with_overlay(
                str(contract_path), getattr(args, "env", None), logger
            )
        except CLIError:
            raise
        except Exception as e:
            raise CLIError(1, "contract_load_failed", {"path": str(contract_path), "error": str(e)})

    builds = contract.get("builds", [])

    if not builds:
        LOG.warning("No builds defined in contract")
        return 0

    # ── Identifier guard (fail CLOSED) ───────────────────────────────────
    # This is the single runtime chokepoint for ``fluid apply --mode
    # amend-and-build``, which loads a contract WITHOUT jsonschema
    # validation. ``contract['id']`` and each ``build['id']`` flow into
    # ``RunContext.product_id`` / ``build_id`` (``_acquisition_common``)
    # and then into ``FileStateStore._build_dir`` (``_state``), which
    # joins them as ``<root>/runs/<product_id>/<build_id>`` with
    # ``parents=True`` and (historically) no sanitisation — so an
    # ``id`` like ``../../../../tmp/escape`` would write JSON OUTSIDE the
    # workspace. Validate every id here, BEFORE any runner runs and BEFORE
    # any state-store path is created, so a malicious contract is rejected
    # rather than traversing the filesystem. The parallel pipeline
    # (``cli/_acquisition_stage_ext``) already validates the same fields;
    # this closes the one-sided-guard gap on the runtime path.
    from ._ids import validate_identifier

    # Only non-empty ids are checked: an absent/empty id cannot traverse
    # (``_build_dir`` falls back to a safe "product" default and
    # ``FileStateStore._confine()`` is the backstop), while a non-empty id
    # like ``../../../../tmp/escape`` is rejected here before any path is made.
    if contract.get("id"):
        validate_identifier(contract["id"], kind="contract.id")
    for _b in builds:
        if _b.get("id"):
            validate_identifier(_b["id"], kind="build.id")

    # Filter builds if specific ID requested
    if args.build_id:
        builds = [b for b in builds if b.get("id") == args.build_id]
        if not builds:
            LOG.error(f"Build not found: {args.build_id}")
            return 1

    cprint(f"\n{'=' * 80}")
    cprint("🚀 FLUID Build Runner")
    cprint(f"{'=' * 80}")
    cprint(f"Contract: {contract_path}")
    cprint(f"Builds: {len(builds)}")
    if args.dry_run:
        cprint("Mode: DRY RUN")
    cprint(f"{'=' * 80}")

    total_executed = 0
    total_failed = 0
    total_skipped = 0

    for build in builds:
        build_id = build.get("id", "unknown")

        if is_acquisition_build(build):
            sample_rows = getattr(args, "sample_rows", None)
            result = _execute_acquisition_build(
                build,
                contract,
                contract_path.parent,
                dry_run=args.dry_run,
                sample_rows=sample_rows,
            )
            if result == 0:
                total_executed += 1
            else:
                total_failed += 1
                if args.fail_fast:
                    break
            continue

        if is_embedded_sql_build(build):
            # An inline-SQL build that ALSO declares a dbt(-adapter) engine
            # still runs via the local DuckDB engine below — NOT dbt. Surface
            # that explicitly so ``engine: dbt`` is never silently ignored.
            if is_dbt_build(build):
                cprint(
                    f"\n⚠️  Build '{build_id}' sets engine "
                    f"'{build.get('engine')}' but carries inline SQL "
                    f"('properties.sql'); running it via the local DuckDB "
                    f"engine, not dbt. To run dbt, point the build at a dbt "
                    f"project (repository + dbt_project.yml) instead of inline "
                    f"SQL."
                )
            # Bug A4-A fix: embedded-SQL builds (engine: sql / pattern:
            # embedded-logic + properties.sql) carry their SQL inline and do
            # NOT have an external Python script.  They are executed via the
            # local provider's DuckDB engine, not the python runner.
            # Previously they fell through to the python-runner branch and
            # emitted the confusing "Script not found: ingest.py" warning.
            result = _execute_embedded_sql_build(
                build,
                contract,
                contract_path.parent,
                dry_run=args.dry_run,
            )
        elif is_dbt_build(build):
            project_dir = resolve_dbt_project_path(contract_path, build)
            if not project_dir:
                repository = build.get("repository", "./")
                expected = (contract_path.parent / repository / "dbt_project.yml").resolve()
                cprint(f"\n⚠️  Build '{build_id}' - dbt project not found: {expected}")
                total_skipped += 1
                continue

            result = execute_dbt_build(
                build,
                project_dir,
                contract_path.parent,
                dry_run=args.dry_run,
                delay=args.delay,
                no_output=args.no_output,
                fail_fast=args.fail_fast,
                force_run=force_run,
                # Forward the apply mode so destructive modes (replace
                # / replace-and-build) append ``--full-refresh`` to dbt.
                apply_mode=getattr(args, "mode", None),
            )
        else:
            # Resolve script path
            script_path = resolve_script_path(contract_path, build)

            if not script_path:
                repository = build.get("repository", "./")
                properties = build.get("properties", {})
                model = properties.get("model", "ingest")
                expected = contract_path.parent / repository / f"{model}.py"

                cprint(
                    f"\n⚠️  Build '{build_id}' - Script not found: {expected}\n"
                    "   Hint: for inline-SQL builds use ``engine: sql`` (or "
                    "``pattern: embedded-logic`` + ``properties.sql``). "
                    "For Python builds, create the script at the expected path above."
                )
                total_skipped += 1
                continue

            # Execute build
            result = execute_build(
                build,
                script_path,
                contract_path.parent,
                dry_run=args.dry_run,
                delay=args.delay,
                no_output=args.no_output,
                fail_fast=args.fail_fast,
                force_run=force_run,
            )

        if result == 0:
            total_executed += 1
        else:
            total_failed += 1
            if args.fail_fast:
                break

    # Final summary
    cprint(f"\n{'=' * 80}")
    cprint("📈 Overall Summary")
    cprint(f"{'=' * 80}")
    cprint(f"Total builds: {len(builds)}")
    success(f"Executed: {total_executed}")
    console_error(f"Failed: {total_failed}")
    cprint(f"⏭️  Skipped: {total_skipped}")
    cprint(f"{'=' * 80}\n")

    return 0 if total_failed == 0 else 1
