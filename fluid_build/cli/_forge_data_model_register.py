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

"""``fluid forge data-model`` argparse registration.

Lifted from ``cli/forge_data_model.py`` (host file was 1586 LOC).
~280 LOC of argparse subparser declarations for the ``learn``,
``from-ddl``, ``from-intent``, ``from-source``, ``validate``,
``diff``, ``dump-ddl`` data-model verbs. The handler functions all
live in the host module; this file just wires their argparse
surface.

``forge_data_model.py`` re-imports :func:`register_forge_subcommand`
at module top so the existing ``register(subparsers)`` glue keeps
working.
"""

from __future__ import annotations

import argparse


def _resolve_handlers():
    """Resolve the handler callbacks via the host module so
    ``set_defaults(data_model_func=run_X_command)`` binds to whatever
    the host re-exports (and any test patches flow through).
    """
    from fluid_build.cli import forge_data_model as _fdm

    return _fdm


def register_forge_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register ``fluid forge data-model``."""
    _h = _resolve_handlers()
    parser = subparsers.add_parser(
        _h.COMMAND,
        help="Forge a reviewable data model contract and logical sidecar",
    )
    # ``required=False`` so a bare ``fluid forge data-model`` doesn't blow up
    # with the bare-bones argparse "the following arguments are required:
    # data_model_action" error.  ``run_data_model_command`` catches the
    # ``data_model_action is None`` case and renders a Rich-friendly panel
    # listing the subcommands instead.
    data_model_sub = parser.add_subparsers(dest="data_model_action", required=False)

    from_ddl = data_model_sub.add_parser("from-ddl", help="Forge a data model from DDL")
    _h._add_common_generation_args(from_ddl)
    from_ddl.add_argument("--ddl", nargs="+", required=True, help="One or more DDL files")
    from_ddl.add_argument(
        "--source-type",
        choices=["snowflake", "bigquery", "postgres", "postgresql", "oracle", "mysql"],
        help="Source SQL dialect hint",
    )
    from_ddl.set_defaults(data_model_func=_h.run_from_ddl_command)

    from_intent = data_model_sub.add_parser(
        "from-intent",
        help="Forge a data model from a business intent file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Forge a reviewable data model contract from a YAML/JSON business "
            "intent file.\n\n"
            "An intent file describes the data product you want: identity, "
            "grain, dimensions, metrics, source hints, rules, and modeling "
            "preferences."
        ),
        epilog=(
            "Required minimum:\n"
            "  data_product.name\n"
            "  data_product.domain\n"
            "  plus at least one grain, dimension, metric, or data source\n\n"
            "Useful fields:\n"
            "  business_context, metrics, grain, dimensions, data_sources,\n"
            "  business_rules, modeling\n\n"
            "Minimal example:\n"
            "  data_product:\n"
            "    name: customer_orders\n"
            "    domain: retail\n"
            "  grain:\n"
            "    entity: order_line\n"
            "    time_dimension: order_date\n"
            "  dimensions:\n"
            "    entities: [customer, product]\n\n"
            "Examples:\n"
            "  fluid forge data-model from-intent --example\n"
            "  fluid forge data-model from-intent --example retail\n"
            "  fluid forge data-model from-intent --schema\n"
            "  fluid forge data-model from-intent --validate intent.yaml\n"
            "  fluid forge data-model from-intent intent.yaml -o contract.fluid.yaml\n"
        ),
    )
    _h._add_common_generation_args(from_intent, output_required=False)
    from_intent.add_argument(
        "intent_file",
        nargs="?",
        metavar="intent-file",
        help="Path to a YAML or JSON business intent file",
    )
    from_intent.add_argument(
        "--example",
        nargs="?",
        const="minimal",
        choices=["minimal", "retail", "telco", "finance"],
        help="Print a YAML intent example (minimal, retail, telco, or finance) and exit",
    )
    from_intent.add_argument(
        "--schema",
        action="store_true",
        help="Print the BusinessIntent JSON Schema and exit",
    )
    from_intent.add_argument(
        "--validate",
        dest="validate_intent",
        metavar="intent-file",
        help="Validate an intent file without writing artifacts",
    )
    from_intent.set_defaults(data_model_func=_h.run_from_intent_command)

    # V1.5 — forge from a configured metadata-source catalog.
    # User-facing vocabulary uses "source" / "metadata source" to
    # disambiguate from forge-cli's existing publish-target catalog
    # role (``fluid publish --target dmm`` etc.). See ``docs/MIGRATION.md``
    # for the full UX-vocabulary table.
    from_source = data_model_sub.add_parser(
        "from-source",
        help=(
            "Forge a data model from a configured metadata source "
            "(snowflake / unity / bigquery / dataplex / glue / "
            "datahub / datamesh_manager)."
        ),
    )
    _h._add_common_generation_args(from_source)
    # Source choices come from the shared registry (built-in catalog +
    # JDBC sources, plus any ``fluid_build.source_adapters`` plugins) so a
    # pip-installed adapter appears in ``--source`` without editing the CLI.
    from fluid_build.copilot.catalog.source_registry import list_source_adapters

    from_source.add_argument(
        "--source",
        # Optional so ``--modeling-technique custom --logical-model <path>`` can
        # forge from a supplied logical model with no source; the handler
        # validates that a source is present for every non-custom run.
        required=False,
        default=None,
        choices=list_source_adapters(),
        help=(
            "Which metadata-source catalog to read from. Catalogs "
            "(snowflake/unity/bigquery/dataplex/glue/datahub/"
            "datamesh_manager) require credentials configured via "
            "``fluid ai setup --source <catalog> --name <credential-id>``. "
            "JDBC sources (postgres/postgresql/mysql/sqlite) accept a "
            "``--uri`` directly. Omit with ``--modeling-technique custom``. "
            "See docs/PROVIDERS.md."
        ),
    )
    from_source.add_argument(
        "--uri",
        default=None,
        help=(
            "JDBC connection URI for ``--source postgres|postgresql|mysql|"
            "sqlite``. Examples: ``postgresql://user:pass@host:5432/db``, "
            "``mysql://user:pass@host:3306/db``, "
            "``sqlite:///path/to/db.sqlite``. Ignored for catalog sources."
        ),
    )
    from_source.add_argument(
        "--credential-id",
        default=None,
        help=(
            "Saved credential name to use (created via "
            "`fluid ai setup --source <catalog> --name <credential-id>`). When omitted, the resolver "
            "falls through to env vars and (with --allow-metadata-service) "
            "to cloud workload-identity."
        ),
    )
    from_source.add_argument(
        "--database",
        default=None,
        help=(
            "Database / project to enumerate (Snowflake DATABASE, BigQuery "
            "PROJECT, Glue DATABASE, etc.). Required for most catalogs."
        ),
    )
    from_source.add_argument(
        "--schema",
        dest="schema_name",
        default=None,
        help=(
            "Schema / dataset / domain to enumerate. Required for Snowflake "
            "/ Unity / BigQuery / Dataplex; optional for Glue / DataHub / DMM."
        ),
    )
    from_source.add_argument(
        "--catalog",
        default=None,
        help=(
            "Unity catalog name OR Dataplex entry-group name. Ignored "
            "for catalogs that don't have a separate catalog-level scope."
        ),
    )
    from_source.add_argument(
        "--tables",
        nargs="+",
        default=None,
        help=(
            "Optional list of table names to enumerate. When omitted, "
            "every table under the (database, schema) is fetched."
        ),
    )
    from_source.add_argument(
        "--name",
        default=None,
        help=(
            "Output model name (used for the sidecar filename and the "
            "contract's id / name fields). Defaults to the schema / "
            "dataset name."
        ),
    )
    from_source.add_argument(
        "--allow-metadata-service",
        action="store_true",
        help=(
            "Permit the credential resolver to use cloud workload-identity "
            "(GCE / GKE / Cloud Run / Lambda metadata service / IAM "
            "instance profile). Off by default — opt in for hosted CI / "
            "production runs (see docs/PROVIDERS.md for the security model)."
        ),
    )
    from_source.set_defaults(data_model_func=_h.run_from_source_command)

    validate = data_model_sub.add_parser(
        "validate", help="Validate a forged data model contract or sidecar"
    )
    _h._add_quiet_arg(validate)
    validate.add_argument("path", help="Path to a contract.fluid.yaml or *.model.json file")
    validate.set_defaults(data_model_func=_h.run_validate_command)

    diff = data_model_sub.add_parser("diff", help="Diff two forged logical sidecars")
    _h._add_quiet_arg(diff)
    diff.add_argument("old", help="Path to the older *.model.json file")
    diff.add_argument("new", help="Path to the newer *.model.json file")
    diff.set_defaults(data_model_func=_h.run_diff_command)

    # Item 6 — capture operator edits and record them to
    # memory/semantic so the next forge biases toward operator
    # preferences. Closes the continuous-learning loop end-to-end.
    learn = data_model_sub.add_parser(
        "learn",
        help=(
            "Capture operator edits between an original forged "
            "contract and a hand-edited version; record the diff "
            "to memory/semantic so the next forge starts closer "
            "to operator preferences."
        ),
    )
    _h._add_quiet_arg(learn)
    learn.add_argument(
        "--original",
        required=True,
        help="Path to the originally-forged contract.fluid.yaml.",
    )
    learn.add_argument(
        "--edited",
        required=True,
        help="Path to the operator-edited contract.fluid.yaml.",
    )
    learn.add_argument(
        "--name",
        default=None,
        help=(
            "Optional contract name for the memory key. Defaults "
            "to the original contract filename stem."
        ),
    )
    learn.set_defaults(data_model_func=_h.run_learn_command)

    dump_ddl = data_model_sub.add_parser(
        "dump-ddl",
        help="Dump DDL from a Snowflake database.schema to a .sql file",
    )
    _h._add_quiet_arg(dump_ddl)
    dump_ddl.add_argument("--database", required=True, help="Snowflake database (e.g. DEMO_DB)")
    dump_ddl.add_argument("--schema", required=True, help="Snowflake schema (e.g. SEEDED)")
    dump_ddl.add_argument("--output", "-o", required=True, help="Path to the .sql file to write")
    dump_ddl.add_argument(
        "--tables",
        nargs="+",
        default=None,
        help=(
            "Optional list of table names to dump. When omitted, the whole "
            "schema is dumped via GET_DDL('SCHEMA', ...)."
        ),
    )
    dump_ddl.add_argument(
        "--role",
        default=None,
        help="Snowflake role override (defaults to env SNOWFLAKE_ROLE)",
    )
    dump_ddl.add_argument(
        "--warehouse",
        default=None,
        help="Snowflake warehouse override (defaults to env SNOWFLAKE_WAREHOUSE)",
    )
    dump_ddl.set_defaults(data_model_func=_h.run_dump_ddl_command)
