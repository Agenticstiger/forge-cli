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

"""Bundled marketplace blueprints — curated, offline data-product templates.

``fluid market --blueprints`` is otherwise empty until a registry is configured
(the default endpoint is a localhost dev server). These curated blueprints ship
*in the package*, so discovery works out-of-the-box: they are listed alongside
any registry results and ``fluid market --blueprint-id <id> --instantiate``
renders them locally.

The REST registry renders a blueprint's Jinja2 ``contract_template``
server-side (``POST /{id}/instantiate``); a bundled blueprint has no server, so
:func:`render_bundled_contract` renders it **client-side** with a *sandboxed*
Jinja2 environment (the template is trusted/in-repo; the parameter values are
user input). Each ``contract_template`` renders a valid FLUID 0.7.4 contract —
the shapes are borrowed from ``examples/`` (01-hello-world is the starter base).
Parameters flow only into id / name / metadata / file paths (never into SQL
identifiers), so an instantiated contract is always well-formed.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from fluid_build.util.contract import slugify_identifier

# --- Curated contract templates (Jinja2 → FLUID 0.7.4 YAML) ----------------- #

# In every template, free-text parameter values are interpolated through the
# Jinja2 ``tojson`` filter (no surrounding quotes): ``tojson`` emits a fully
# escaped JSON string, which is a valid YAML scalar, so a value such as
# ``owner_email='x"\ninjected_key: pwned'`` cannot break out of its scalar and
# inject sibling YAML keys. Only the slug-derived values (``product_id``,
# ``name_slug``, ``domain_slug``) are interpolated bare — and only because
# ``slugify_identifier`` already constrains them to a safe ``[a-z0-9_-]``
# charset, so they are safe even as substrings inside SQL strings / file paths.
_STARTER_TEMPLATE = """\
fluidVersion: "0.7.4"
kind: "DataProduct"
id: {{ product_id | tojson }}
name: {{ product_name | tojson }}
description: {{ description | tojson }}
domain: {{ domain | tojson }}
metadata:
  layer: Bronze
  owner:
    team: {{ owner_team | tojson }}
    email: {{ owner_email | tojson }}
builds:
  - id: "{{ name_slug }}_build"
    pattern: "embedded-logic"
    engine: "sql"
    properties:
      sql: |
        SELECT
          'Hello from the {{ name_slug }} data product' AS message,
          CURRENT_TIMESTAMP AS created_at
exposes:
  - exposeId: "{{ name_slug }}_output"
    kind: "table"
    binding:
      platform: "local"
      format: "csv"
      location:
        path: "runtime/out/{{ name_slug }}.csv"
    contract:
      schema:
        - name: "message"
          type: "string"
        - name: "created_at"
          type: "timestamp"
"""

_ANALYTICS_TEMPLATE = """\
fluidVersion: "0.7.4"
kind: "DataProduct"
id: {{ product_id | tojson }}
name: {{ product_name | tojson }}
description: {{ description | tojson }}
domain: {{ domain | tojson }}
metadata:
  layer: Silver
  owner:
    team: {{ owner_team | tojson }}
    email: {{ owner_email | tojson }}
builds:
  - id: "{{ name_slug }}_daily"
    pattern: "embedded-logic"
    engine: "sql"
    properties:
      sql: |
        SELECT
          CAST(CURRENT_DATE AS DATE) AS day,
          COUNT(*) AS record_count
        FROM read_csv_auto('runtime/in/{{ name_slug }}_source.csv')
        GROUP BY 1
exposes:
  - exposeId: "{{ name_slug }}_daily_output"
    kind: "table"
    binding:
      platform: "local"
      format: "parquet"
      location:
        path: "runtime/out/{{ name_slug }}_daily.parquet"
    contract:
      schema:
        - name: "day"
          type: "date"
        - name: "record_count"
          type: "integer"
"""

# Shared parameter set (kept identical so the wizard/UX is consistent).
_COMMON_PARAMS: List[Dict[str, Any]] = [
    {
        "name": "product_name",
        "required": True,
        "type": "string",
        "description": "Human-readable data-product name (also drives the id).",
    },
    {
        "name": "domain",
        "required": False,
        "type": "string",
        "default": "example",
        "description": "Business domain (id prefix).",
    },
    {
        "name": "owner_team",
        "required": False,
        "type": "string",
        "default": "data-team",
        "description": "Owning team.",
    },
    {
        "name": "owner_email",
        "required": False,
        "type": "string",
        "default": "team@example.com",
        "description": "Owner contact email.",
    },
]

BUNDLED_BLUEPRINTS: List[Dict[str, Any]] = [
    {
        "id": "fluid.starter",
        "name": "Starter Data Product",
        "description": "Minimal Bronze data product (embedded SQL → local CSV) — the "
        "quickest way to a valid, runnable contract.",
        "category": "starter",
        "version": "1.0.0",
        "labels": {"maturity": "stable", "source": "bundled", "license": "Apache-2.0"},
        "source": "bundled",
        "tags": ["starter", "local", "bronze", "sql"],
        "parameters": _COMMON_PARAMS,
        "contract_template": _STARTER_TEMPLATE,
    },
    {
        "id": "fluid.analytics-daily",
        "name": "Daily Analytics Aggregate",
        "description": "Silver analytics product: a daily record-count aggregate over a "
        "local CSV source, exposed as Parquet. Edit the SQL to fit your source.",
        "category": "analytics",
        "version": "1.0.0",
        "labels": {"maturity": "stable", "source": "bundled", "license": "Apache-2.0"},
        "source": "bundled",
        "tags": ["analytics", "silver", "aggregate", "duckdb"],
        "parameters": _COMMON_PARAMS,
        "contract_template": _ANALYTICS_TEMPLATE,
    },
]

_BUNDLED_BY_ID: Dict[str, Dict[str, Any]] = {b["id"]: b for b in BUNDLED_BLUEPRINTS}


def list_bundled_blueprints() -> List[Dict[str, Any]]:
    """All bundled blueprints (deep copies, so callers can't mutate the registry)."""
    return [copy.deepcopy(b) for b in BUNDLED_BLUEPRINTS]


def get_bundled_blueprint(blueprint_id: str) -> Optional[Dict[str, Any]]:
    """A bundled blueprint by id, or ``None`` (a deep copy — safe to mutate)."""
    bp = _BUNDLED_BY_ID.get(blueprint_id)
    return copy.deepcopy(bp) if bp is not None else None


def is_bundled(blueprint_id: str) -> bool:
    return blueprint_id in _BUNDLED_BY_ID


def render_bundled_contract(bp: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Render a bundled blueprint's Jinja2 ``contract_template`` into a contract.

    Renders with a **sandboxed** Jinja2 environment (the template is in-repo and
    trusted; the parameter values are user input). Applies parameter defaults,
    enforces required ones, and derives FLUID-safe identifiers via
    :func:`slugify_identifier` so the rendered ``id`` is always valid.
    """
    # Lazy imports keep the CLI startup graph lean (this is only hit on a
    # bundled `--instantiate`, not on `fluid --help`).
    import yaml
    from jinja2.sandbox import SandboxedEnvironment

    ctx: Dict[str, Any] = {}
    for p in bp.get("parameters", []):
        ctx[p["name"]] = params.get(p["name"], p.get("default"))

    missing = [
        p["name"]
        for p in bp.get("parameters", [])
        if p.get("required") and not str(ctx.get(p["name"]) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"Missing required blueprint parameter(s): {', '.join(missing)}. "
            'Pass them via --params \'{"product_name": "..."}\' or --interactive.'
        )

    name_slug = slugify_identifier(
        str(ctx.get("product_name") or "data_product"), fallback="data_product"
    )
    domain_slug = slugify_identifier(str(ctx.get("domain") or "default"), fallback="default")
    ctx["name_slug"] = name_slug
    ctx["domain_slug"] = domain_slug
    ctx["product_id"] = f"{domain_slug}.{name_slug}"
    if not str(ctx.get("description") or "").strip():
        ctx["description"] = f"{ctx.get('product_name')} — generated from the {bp['id']} blueprint."

    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    rendered = env.from_string(bp["contract_template"]).render(**ctx)
    contract = yaml.safe_load(rendered)
    if not isinstance(contract, dict):
        raise ValueError(
            f"Bundled blueprint {bp.get('id')!r} did not render a valid contract mapping."
        )
    return contract
