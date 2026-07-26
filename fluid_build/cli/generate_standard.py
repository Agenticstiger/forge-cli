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

"""``fluid generate standard`` subcommand.

Exports FLUID contracts to industry-standard data product formats.

**Bitol Open Data Product Standard v1.0.0 is the center-stage ODPS in this
codebase.** The Linux Foundation / ODPI ODPS Specification v4.1 is supported
as a secondary, opt-in target for catalogs that require it.

Supported formats:
    odps        Bitol Open Data Product Standard v1.0.0 (bare product YAML;
                the canonical, default ODPS in this codebase)
    odps-bitol  Explicit alias of ``odps`` — same Bitol v1.0.0 output
    odcs        Open Data Contract Standard v3.1 (Bitol.io) — per-port
    odps-v4.1   Open Data Product Specification v4.1 (Linux Foundation /
                ODPI) — secondary back-pocket option, JSON output
    opds        Deprecated letter-swap alias of ``odps-v4.1`` — emits the
                LF/ODPI v4.1 JSON (historical default of this flag)

Usage:
    fluid generate standard contract.fluid.yaml --format odps           # Bitol
    fluid generate standard contract.fluid.yaml --format odps-v4.1      # LF/ODPI
    fluid generate standard --list
"""

from __future__ import annotations

import argparse
import logging
import os

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error

from ._common import CLIError, load_contract_with_overlay, write_json
from ._logging import info

# Ordered Bitol-first. The dispatcher reads the leading entry as the
# canonical-ODPS default; the LF/ODPI v4.1 spec sits behind its explicit
# ``odps-v4.1`` key and the historical ``opds`` letter-swap alias.
SUPPORTED_FORMATS = ["odps", "odps-bitol", "odcs", "odps-v4.1", "opds"]

# Format aliases that resolve to a canonical key. ``opds`` is the historical
# letter-swap of the LF/ODPI spec name (``ODPS`` v4.1) — it has always meant
# "the LF/ODPI export" in this codebase, so it resolves to ``odps-v4.1`` (NOT
# the new Bitol-default ``odps``) so back-compat callers keep getting the
# spec they actually consume. ``odps-bitol`` is an explicit alias of ``odps``
# for callers that want the disambiguation in their CI logs.
FORMAT_ALIASES = {
    "odps-bitol": "odps",  # explicit, no deprecation warning
}
DEPRECATED_FORMAT_ALIASES = {
    "opds": "odps-v4.1",  # historical default of this letter-swap, kept stable
}

DEFAULT_OUTPUTS = {
    "odps": "runtime/exports/product.odps.yaml",
    "odps-bitol": "runtime/exports/product.odps-bitol.yaml",
    "odcs": "runtime/exports/product.odcs.yaml",
    "odps-v4.1": "runtime/exports/product.odps-v4.1.json",
    "opds": "runtime/exports/product.opds.json",
}


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "standard",
        help="Export to data product standards (Bitol ODPS = default; ODCS; LF/ODPI v4.1 opt-in)",
        description=(
            "Export a FLUID contract to an industry-standard data product format.\n\n"
            "Two distinct standards share the ODPS acronym — Bitol's Open Data\n"
            "Product STANDARD v1.0.0 (the center-stage default here) and the LF/ODPI\n"
            "Open Data Product SPECIFICATION v4.1 (a secondary opt-in target):\n\n"
            "Supported formats:\n"
            "  odps        Bitol ODPS v1.0.0 — bare product YAML (default ODPS)\n"
            "  odps-bitol  Explicit alias of ``odps`` (same Bitol v1.0.0 output)\n"
            "  odcs        Open Data Contract Standard v3.1 (Bitol.io) — per-port\n"
            "  odps-v4.1   LF/ODPI Open Data Product Specification v4.1 — JSON,\n"
            "              opt-in back-pocket option for LF-aligned catalogs\n"
            "  opds        Deprecated letter-swap alias of ``odps-v4.1`` (historical\n"
            "              default of this flag; emits the same LF/ODPI v4.1 JSON)\n"
        ),
        epilog="""Examples:
  # Bitol ODPS (the canonical, default export):
  fluid generate standard contract.fluid.yaml --format odps

  # LF/ODPI ODPS v4.1 (opt-in):
  fluid generate standard contract.fluid.yaml --format odps-v4.1 -o out.json

  fluid generate standard --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", help="contract.fluid.yaml")
    p.add_argument(
        "--format",
        "-f",
        dest="standard_format",
        choices=SUPPORTED_FORMATS,
        help="Target standard format",
    )
    p.add_argument("--out", "-o", help="Output file path (auto-detected if not set)")
    p.add_argument("--env", help="Environment overlay")
    p.add_argument(
        "--list", dest="list_formats", action="store_true", help="List supported formats"
    )
    p.set_defaults(generate_sub="standard", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate standard``."""
    return run(args, logger)


def run(args, logger: logging.Logger) -> int:
    if getattr(args, "list_formats", False):
        cprint("Supported data product standard formats:\n")
        for fmt in SUPPORTED_FORMATS:
            cprint(f"  {fmt:<14} {_format_description(fmt)}")
        cprint("\nUsage: fluid generate standard contract.fluid.yaml --format <format>")
        return 0

    fmt = getattr(args, "standard_format", None)
    if not fmt:
        cprint("Error: --format is required. Use --list to see available formats.")
        return 1

    contract_path = getattr(args, "contract", None)
    if not contract_path:
        cprint("Error: contract path is required.")
        return 1

    try:
        return _export_format(fmt, contract_path, args, logger)
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, f"generate_standard_{fmt}_failed", {"error": str(e)})


def _format_description(fmt: str) -> str:
    descriptions = {
        "odps": "Bitol Open Data Product Standard v1.0.0 (default, center-stage)",
        "odps-bitol": "Explicit alias of 'odps' (Bitol ODPS v1.0.0)",
        "odcs": "Open Data Contract Standard v3.1 (Bitol.io) — per exposed port",
        "odps-v4.1": "LF/ODPI Open Data Product Specification v4.1 (opt-in)",
        "opds": "Deprecated letter-swap alias of 'odps-v4.1' (LF/ODPI v4.1 JSON)",
    }
    return descriptions.get(fmt, fmt)


def _export_format(fmt: str, contract_path: str, args, logger: logging.Logger) -> int:
    """Delegate to the appropriate exporter.

    Aliases resolve to a canonical key BEFORE dispatch — both ``FORMAT_ALIASES``
    (silent) and ``DEPRECATED_FORMAT_ALIASES`` (with WARNING) collapse here.
    The dispatch table is keyed on the canonical names only.
    """
    env = getattr(args, "env", None)
    if fmt in DEPRECATED_FORMAT_ALIASES:
        canonical = DEPRECATED_FORMAT_ALIASES[fmt]
        logger.warning(
            "deprecated_format_alias",
            extra={"alias": fmt, "canonical": canonical},
        )
        # Banner goes to STDERR so users piping the exporter's stdout to a
        # file (`... > out.json`) get clean JSON without the warning bleed.
        console_error(
            f"WARNING: --format {fmt} is a deprecated letter-swap alias of "
            f"--format {canonical} (LF/ODPI ODPS v4.1 JSON). The canonical Bitol "
            f"ODPS v1.0.0 export is --format odps."
        )
        fmt = canonical
    elif fmt in FORMAT_ALIASES:
        fmt = FORMAT_ALIASES[fmt]

    out = getattr(args, "out", None) or DEFAULT_OUTPUTS.get(
        fmt, f"runtime/exports/product.{fmt}.yaml"
    )

    if fmt == "odps":
        # Bitol ODPS v1.0.0 — the center-stage default.
        return _export_odps_bitol(contract_path, env, out, logger)
    elif fmt == "odcs":
        return _export_odcs(contract_path, env, out, logger)
    elif fmt == "odps-v4.1":
        return _export_odps_v4_1(contract_path, env, out, logger)
    else:
        cprint(f"Unknown format: {fmt}")
        return 1


def _export_odps_v4_1(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to LF/ODPI Open Data Product Specification v4.1 — JSON.

    Routes through :class:`OdpsProvider` and unwraps the provider's
    ``{..., artifacts: {...}}`` envelope so the on-disk file is the bare ODPS
    v4.1 document (``{schema, version, product}``) per the upstream spec, not
    the FLUID-internal wrapper.
    """
    from fluid_build.providers.opds.opds import OdpsProvider

    c = load_contract_with_overlay(contract_path, env, logger)
    provider = OdpsProvider()
    rendered = provider.render(c)

    # Unwrap the provider's ``{opds_version, generator, ..., artifacts: {...}}``
    # envelope so the on-disk file is the canonical bare ODPS v4.1 doc.
    payload = rendered.get("artifacts", rendered) if isinstance(rendered, dict) else rendered

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    write_json(out, payload)
    info(logger, "generate_standard_odps_v4_1_ok", out=out)
    return 0


# Back-compat alias — earlier callers / tests may have imported these names.
_export_opds = _export_odps_v4_1
_export_odps = _export_odps_v4_1  # NOTE: when called via --format dispatch, the
# dispatcher remaps fmt before calling, so user-visible ``--format odps`` goes to
# Bitol. This direct symbol is only for callers that imported the function name.


def _export_odcs(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to Open Data Contract Standard v3.1 (Bitol.io) — one file per port.

    ODCS is **per-exposed-port** by definition — every ``exposes[]`` entry in
    the FLUID contract becomes its own ``DataContract`` document. The CLI
    flag ``--out`` names a single file, so we have two modes:

    * If ``--out`` is a directory (or ends in ``/``) — emit
      ``product.odcs.<exposeId>.yaml`` per port into that directory and
      return the count.
    * If ``--out`` is a single file path — emit a multi-document YAML stream
      (``---``-separated) so the consumer can either slice with ``yq`` or
      load all ports in one read. Single-port contracts collapse to one
      document (no leading ``---``).

    The earlier fallback emitted a hand-rolled
    ``{kind, apiVersion, id, info}`` skeleton that did NOT validate against
    the vendored ODCS v3.1.0 schema; that path is gone — every emission now
    routes through :class:`OdcsProvider.render_all_ports`, which produces
    schema-conformant docs.
    """
    import yaml as _yaml

    from fluid_build.loader import load_contract
    from fluid_build.providers.odcs.provider import OdcsProvider

    c = load_contract(contract_path)
    provider = OdcsProvider()
    results = provider.render_all_ports(c)  # [(expose_id, odcs_doc), ...]

    if not results:
        cprint("Error: contract has no exposes[] entries to export as ODCS.")
        return 1

    # Directory mode: write one file per port.
    if out.endswith(os.sep) or os.path.isdir(out):
        out_dir = out.rstrip(os.sep)
        os.makedirs(out_dir, exist_ok=True)
        # exposeId is document-controlled; gate it through the shared
        # provider path-safety helper so a foreign contract cannot name a
        # file outside out_dir. A schema-valid id is used verbatim.
        from fluid_build.providers._path_safety import safe_output_path

        written: list[str] = []
        for eid, doc in results:
            path = str(
                safe_output_path(out_dir, f"product.odcs.{eid}", ".yaml", fallback="product.odcs")
            )
            with open(path, "w", encoding="utf-8") as fh:
                _yaml.safe_dump(doc, fh, default_flow_style=False, sort_keys=False)
            written.append(path)
        info(logger, "generate_standard_odcs_ok", out=out_dir, ports=len(written))
        cprint(f"✓ Wrote {len(written)} ODCS v3.1.0 doc(s) to {out_dir}/")
        return 0

    # Single-file mode: multi-document YAML stream (one ---block per port).
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    docs = [doc for _, doc in results]
    with open(out, "w", encoding="utf-8") as fh:
        _yaml.safe_dump_all(docs, fh, default_flow_style=False, sort_keys=False)
    info(
        logger,
        "generate_standard_odcs_ok",
        out=out,
        ports=len(docs),
        mode="multi-doc-yaml" if len(docs) > 1 else "single-doc",
    )
    if len(docs) > 1:
        cprint(
            f"✓ Wrote {len(docs)} ODCS v3.1.0 docs to {out} (multi-document YAML; "
            f"pass a directory path to --out for one file per port)"
        )
    else:
        cprint(f"✓ Wrote 1 ODCS v3.1.0 doc to {out}")
    return 0


def _export_odps_bitol(contract_path: str, env, out: str, logger: logging.Logger) -> int:
    """Export to Bitol ODPS v1.0.0 — bare product file at ``out``.

    Bare product only (no per-port ODCS siblings). For the canonical Bitol
    fragments bundle (1 ODPS + N ``<contractId>.odcs.yaml`` together in
    one directory) call ``BitolOdpsProvider.render(out_dir=...)`` directly
    or use ``fluid generate artifacts --emit odps-bitol`` which routes
    through ``artifact_fanout._emit_odps_bitol``.
    """
    try:
        from fluid_build.loader import load_contract
        from fluid_build.providers.odps_standard import BitolOdpsProvider

        c = load_contract(contract_path)
        provider = BitolOdpsProvider()
        provider.strict_validation = False  # bare-product flow shouldn't enforce sibling validation
        bundle = provider.render(c)
        product = bundle["product"]

        os.makedirs(os.path.dirname(out), exist_ok=True)
        import yaml as _yaml

        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(product, f, default_flow_style=False)
        info(logger, "generate_standard_odps_bitol_ok", out=out)
        return 0
    except ImportError:
        # Fallback
        c = load_contract_with_overlay(contract_path, env, logger)
        import yaml as _yaml

        odps_bitol = {
            "dataProductSpecification": "1.0.0",
            "info": {
                "title": c.get("name"),
                "x-fluidId": c.get("id"),
            },
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _yaml.safe_dump(odps_bitol, f, default_flow_style=False)
        info(logger, "generate_standard_odps_bitol_ok", out=out)
        return 0
