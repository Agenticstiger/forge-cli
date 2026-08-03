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

"""``fluid generate iac`` subcommand.

Compiles a FLUID contract into an OpenTofu ``.tf.json`` module — the
autogenerator path. The module is emitted for review; this command never
provisions anything (apply it yourself, or run ``fluid apply``, which
provisions cloud contracts through the OpenTofu engine). Pass ``--validate``
to additionally run ``tofu validate`` on the emitted module.
"""

from __future__ import annotations

import argparse
import logging
import os
import re

from fluid_build.cli.console import cprint
from fluid_build.iac import IAC_PLUGINS, assemble_tofu_document, get_iac_plugin, render_tofu_json
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.packaging import resolve_packaging

from ._common import CLIError, load_contract_with_overlay, resolve_env_templates_in_contract
from ._logging import info

_PROVIDER_CHOICES = ["auto", *sorted(IAC_PLUGINS)]


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "iac",
        help="Compile a contract to an OpenTofu .tf.json module",
        description=(
            "Compile a FLUID contract into an OpenTofu `.tf.json` module.\n\n"
            "The module is emitted for review — apply it with `tofu`.\n"
            "Supported clouds: " + ", ".join(sorted(IAC_PLUGINS)) + "."
        ),
        epilog="""Examples:
  fluid generate iac contract.fluid.yaml
  fluid generate iac contract.fluid.yaml --provider gcp --out infra/
  fluid generate iac contract.fluid.yaml --validate
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="auto",
        help="Target cloud (default: auto-detect from the contract)",
    )
    p.add_argument("--out", "-o", default="runtime/iac", help="Output directory")
    p.add_argument("--env", help="Environment overlay")
    p.add_argument(
        "--shadow",
        action="store_true",
        help="After emitting, shadow-compare resource parity against the native planner",
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="After emitting, run `tofu validate` on the module (needs `tofu` on PATH)",
    )
    p.set_defaults(generate_sub="iac", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate iac``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
        contract = resolve_env_templates_in_contract(contract)
        # Packaging-modes PR1 (RFC-packaging-modes.md): resolve the packaging
        # block at the entry point. Read-only for now — the resolution is
        # computed (so an invalid `packaging` block fails fast with a typed
        # PackagingError) but nothing consumes it yet; the provider emitters
        # cut over in PR2. tests/iac/test_iac_packaging_default_pin.py pins
        # that this call leaves every legacy contract's module byte-identical.
        packaging = resolve_packaging(contract)
        logger.debug("packaging resolved: legacy=%s pool=%s", packaging.is_legacy, packaging.pool)
        provider = _resolve_provider(contract, getattr(args, "provider", "auto"))
        plugin = get_iac_plugin(provider)
        actions = native_actions(contract, logger)
        resources = plugin.emit(contract, actions)
        count = sum(len(items) for items in resources.values())
        provider_cfg = plugin.provider_block()
        document = assemble_tofu_document(
            required_providers=plugin.required_providers,
            resources=resources,
            data=plugin.emit_data(contract, actions),
            # `.tf.json` keys the provider block by the provider's local name.
            provider={plugin.name: provider_cfg} if provider_cfg else None,
        )
        out_dir = getattr(args, "out", None) or "runtime/iac"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "main.tf.json")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_tofu_json(document))
    except CLIError:
        raise
    except UnsupportedBindingError as exc:
        # A binding the provider cannot emit. Typed + actionable rather than
        # collapsed into the generic generate_iac_failed slug, because the
        # remediation is contract-level, not tooling-level.
        raise CLIError(
            1,
            "unsupported_binding",
            {"kind": exc.kind, "error": str(exc), "remediation": list(exc.remediation)},
        )
    except Exception as e:
        raise CLIError(1, "generate_iac_failed", {"error": str(e)})

    info(logger, "generate_iac_ok", provider=provider, resources=count, out=out_path)
    if count == 0:
        cprint(
            f"\nWarning: no {provider} resources found in the contract — emitted an empty module."
        )
    cprint(f"\nWrote OpenTofu module: {out_path}  (provider: {provider}, {count} resources)")

    if getattr(args, "validate", False):
        _validate_with_tofu(out_dir)

    cprint("\nReview and apply with OpenTofu:")
    cprint(f"  tofu -chdir={out_dir} init")
    cprint(f"  tofu -chdir={out_dir} plan")

    if getattr(args, "shadow", False):
        _print_shadow_report(contract, plugin, logger)
    return 0


def _validate_with_tofu(out_dir: str) -> None:
    """Run ``tofu init -backend=false`` + ``tofu validate`` on the emitted module.

    ``tofu validate`` needs the provider schemas, so ``init`` (provider
    download, no backend) runs first — registry network access is required
    on the first run; later runs reuse the cached ``.terraform`` directory.
    Raises ``CLIError`` when ``tofu`` is absent or either step fails, so
    ``--validate`` is a usable CI gate.
    """
    from fluid_build.iac import runner

    if runner.tofu_path() is None:
        raise CLIError(
            1,
            "generate_iac_no_tofu",
            {
                "error": (
                    "--validate needs the `tofu` (OpenTofu) binary on PATH — install "
                    "it from https://opentofu.org/docs/intro/install/"
                )
            },
        )
    cprint("\nValidating with OpenTofu (running tofu init + validate)...")
    init = runner.tofu_init(out_dir, backend=False)
    if not init.ok:
        raise CLIError(
            1,
            "generate_iac_validate_failed",
            {"error": f"`tofu init` failed:\n{init.stderr or init.stdout}"},
        )
    result = runner.tofu_validate(out_dir)
    if not result.ok:
        raise CLIError(
            1,
            "generate_iac_validate_failed",
            {"error": f"`tofu validate` failed:\n{result.stderr or result.stdout}"},
        )
    cprint("OpenTofu validation passed.")


# Cloud aliases → canonical IaC plugin name. The contract surface uses a few
# interchangeable spellings (``binding.provider: aws`` vs ``binding.platform:
# aws``; ``google``/``bigquery`` for GCP; ``duckdb`` for the in-process local
# runner). Normalising here means a contract authored with any documented
# spelling auto-detects, instead of only the single ``exposes[].binding.platform``
# token the pre-2026-06 resolver inspected.
_PROVIDER_ALIASES = {
    "aws": "aws",
    "s3": "aws",
    "glue": "aws",
    "athena": "aws",
    "redshift": "aws",
    "gcp": "gcp",
    "google": "gcp",
    "gcs": "gcp",
    "bigquery": "gcp",
    "snowflake": "snowflake",
    # Confluent Cloud Tableflow (managed Kafka->Iceberg) — ``binding.platform:
    # confluent`` auto-detects to the OpenTofu confluent plugin, matching its
    # entry in OPENTOFU_DEFAULT_PROVIDERS.
    "confluent": "confluent",
    # ``local`` (and its DuckDB engine) is a recognised target but has no
    # OpenTofu plugin — it runs in-process. Detected so we can emit an
    # actionable error rather than the misleading "no supported cloud".
    "local": "local",
    "duckdb": "local",
}

# An unambiguous AWS region (``us-east-1``, ``eu-west-2`` …) is a last-resort
# detection hint — GCP regions (``us-central1``) and the dash-suffixed AWS form
# are distinguishable by the trailing ``-<n>``.
_AWS_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")


def _canonical_cloud(token: object) -> str:
    """Map a raw contract platform/provider token to a canonical cloud name.

    Returns the canonical name (``aws``/``gcp``/``snowflake``/``local``) or
    ``""`` when the token is empty or unrecognised.
    """
    if not isinstance(token, str):
        return ""
    return _PROVIDER_ALIASES.get(token.strip().lower().replace("-", "_"), "")


def _detect_clouds(contract) -> list[str]:
    """Collect every canonical cloud declared anywhere in the contract.

    Inspects, in the order the spec documents them:

    * ``exposes[].binding.provider`` / ``exposes[].binding.platform``
    * top-level ``binding.provider`` / ``binding.platform`` (Snowflake- and
      single-binding contracts that declare the cloud once at the root)
    * ``builds[].provider`` and ``builds[].execution.runtime.platform``

    Falls back to an unambiguous AWS region (``binding.region`` /
    ``binding.location.region``) only when no platform/provider token was
    found, so a region never overrides an explicit binding. Order-preserving
    and de-duplicated.
    """
    found: list[str] = []

    def _add(token: object) -> None:
        cloud = _canonical_cloud(token)
        if cloud and cloud not in found:
            found.append(cloud)

    # exposes[] data-plane bindings (most specific).
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if isinstance(binding, dict):
            _add(binding.get("platform"))
            _add(binding.get("provider"))

    # Top-level binding — Snowflake-style + contracts that declare the cloud
    # once at the root via either ``platform`` or ``provider``.
    top_binding = contract.get("binding")
    if isinstance(top_binding, dict):
        _add(top_binding.get("platform"))
        _add(top_binding.get("provider"))

    # builds[].provider and builds[].execution.runtime.platform.
    for build in contract.get("builds") or []:
        if not isinstance(build, dict):
            continue
        _add(build.get("provider"))
        runtime = (build.get("execution") or {}).get("runtime") or {}
        if isinstance(runtime, dict):
            _add(runtime.get("platform"))

    # Region fallback — only consulted when nothing stronger was declared.
    if not found:
        for region in _candidate_regions(contract):
            if _AWS_REGION_RE.match(region.strip().lower()):
                found.append("aws")
                break

    return found


def _candidate_regions(contract) -> list[str]:
    """Region strings declared on the top-level / expose bindings."""
    regions: list[str] = []

    def _collect(binding: object) -> None:
        if not isinstance(binding, dict):
            return
        for value in (binding.get("region"), (binding.get("location") or {}).get("region")):
            if isinstance(value, str) and value:
                regions.append(value)

    _collect(contract.get("binding"))
    for exposure in contract.get("exposes") or []:
        _collect(exposure.get("binding") or {})
    return regions


def _resolve_provider(contract, requested: str) -> str:
    """Return the IaC plugin name for the contract, or raise ``CLIError``.

    With an explicit ``--provider`` the request is honoured verbatim. With
    ``auto`` (the default) the cloud is detected from the contract's binding
    declarations — ``binding.provider``/``binding.platform`` (top-level or per
    expose), ``builds[].provider``, or an unambiguous region — so the common
    ``binding.provider: aws`` shape resolves instead of erroring.
    """
    if requested and requested != "auto":
        return requested

    found = _detect_clouds(contract)
    supported = "/".join(sorted(IAC_PLUGINS))

    if not found:
        raise CLIError(
            1,
            "generate_iac_no_provider",
            {
                "error": (
                    "could not detect a target cloud from the contract — declare "
                    "`binding.provider` (or `binding.platform`) as one of "
                    f"{supported}, or pass --provider ({supported})"
                )
            },
        )

    if len(found) > 1:
        raise CLIError(
            1,
            "generate_iac_ambiguous_provider",
            {"error": f"contract spans multiple clouds {found} — pass --provider explicitly"},
        )

    cloud = found[0]
    if cloud not in IAC_PLUGINS:
        # ``local``/DuckDB is a real, detected target but runs in-process — it
        # has no OpenTofu module to emit. Say so explicitly instead of the
        # misleading "no supported cloud".
        raise CLIError(
            1,
            "generate_iac_local_target",
            {
                "error": (
                    f"contract targets `{cloud}`, which runs in-process and has no "
                    f"infrastructure to provision — `generate iac` supports {supported}. "
                    "Run it with `fluid apply` (local engine) instead."
                )
            },
        )
    return cloud


def native_actions(contract, logger: logging.Logger) -> list:
    """Best-effort native ``provider.plan()`` actions for the contract.

    The OpenTofu emitter consumes these to translate the schedule /
    orchestration ops the planner interprets (see ``iac.base``); shadow-
    compare diffs them against the emitter's output. Returns ``[]`` when
    the native provider cannot be constructed (e.g. no credentials) — the
    emitter then falls back to the ``exposes[]`` data-plane only.

    Container-creation ops for containers the contract declares ``shared``
    are dropped here (RFC-packaging-modes.md file 8, "a plan that lists
    creations that won't happen is worse than a changed count").
    ``apply_packaging_to_plan`` did this for ``plan.json`` only, so the
    apply path kept announcing three actions for a contract that owns
    exactly one leaf table — while ``tofu`` correctly planned ``+1``.
    This is the apply-side chokepoint, matching ``cli/plan.py``'s.
    """
    try:
        from ._common import build_provider, resolve_provider_from_contract

        name, loc = resolve_provider_from_contract(contract)
        native = build_provider(name, loc.get("project"), loc.get("region"), logger)
        if hasattr(native, "plan"):
            return _drop_referenced_container_actions(contract, list(native.plan(contract)), logger)
    except Exception as exc:  # noqa: BLE001 — native planner is best-effort
        logger.debug("shadow: native planner unavailable: %s", exc)
    return []


def _drop_referenced_container_actions(contract, actions: list, logger: logging.Logger) -> list:
    """Remove creation ops for REFERENCED containers; announce what left.

    A no-op (identity, no event) for a contract with no ``packaging`` block
    and for one that owns every container, so legacy behaviour is unchanged.
    """
    try:
        from fluid_build.iac.plan_packaging import filter_referenced_container_actions

        kept, dropped = filter_referenced_container_actions(contract, actions)
    except Exception as exc:  # noqa: BLE001 — never let the filter break apply
        logger.debug("packaging: action filter unavailable: %s", exc)
        return actions
    if not dropped:
        return actions
    # The provider's own ``plan_completed`` event reports what its planner
    # produced, which is not what apply will do once REFERENCED containers are
    # excluded. ``actions_count`` here is the effective figure a CI parser
    # should key on.
    info(
        logger,
        "packaging_actions_filtered",
        planner_actions_count=len(actions),
        actions_count=len(kept),
        dropped=dropped,
    )
    cprint(
        f"\n  packaging:   {len(dropped)} container-creation action(s) dropped — "
        f"{', '.join(sorted({str(d.get('container')) for d in dropped}))} "
        "referenced from a shared pool, not created here"
    )
    return list(kept)


def _print_shadow_report(contract, plugin, logger: logging.Logger) -> None:
    """Run shadow-compare and print the native↔OpenTofu parity report."""
    from fluid_build.iac import shadow_compare

    actions = native_actions(contract, logger)
    if not actions:
        cprint(
            "\nShadow-compare: native planner produced no actions "
            "(no credentials, or provider unavailable) — emitted OpenTofu only."
        )
        return
    report = shadow_compare(contract, plugin=plugin, native_actions=actions)
    cprint(f"\nShadow-compare — {report.summary()}")
    for res in report.native_only:
        cprint(f"  native-only (OpenTofu gap): {res.kind} {res.identity}")
    for res in report.opentofu_only:
        cprint(f"  opentofu-only: {res.kind} {res.identity}")
