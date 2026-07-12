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

"""Pins for `fluid init` provider quickstarts (local / GCP / Snowflake).

The additive "quickstart options by provider" surface (Trello 69d4c9c0):

* two new bundled starter blueprints — ``fluid.starter-gcp`` and
  ``fluid.starter-snowflake`` — each rendering a *valid* provider-bound
  contract fully offline (no cloud, no AI key), and
* ``fluid init --quickstart --provider gcp|snowflake`` routing to the
  matching provider starter while ``local`` (and any provider without a
  bundled starter) keep the existing customer-360 quickstart unchanged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.cli import init as init_mod
from fluid_build.cli._init_modes import blueprint_mode
from fluid_build.cli._market_bundled_blueprints import (
    get_bundled_blueprint,
    list_bundled_blueprints,
    render_bundled_contract,
)

_LOG = logging.getLogger("test.init.provider_quickstart")


# --- the two new provider starter blueprints ------------------------------- #


@pytest.mark.parametrize(
    "blueprint_id,platform,fmt",
    [
        ("fluid.starter-gcp", "gcp", "bigquery_table"),
        ("fluid.starter-snowflake", "snowflake", "snowflake_table"),
    ],
)
def test_provider_starter_renders_valid_contract(blueprint_id, platform, fmt):
    bp = get_bundled_blueprint(blueprint_id)
    assert bp is not None, f"{blueprint_id} must be a bundled blueprint"
    assert bp["source"] == "bundled"
    contract = render_bundled_contract(bp, {"product_name": "Customer Signups"})
    # Renders the current bundled schema version and passes the real validator.
    from fluid_build.schema_manager import FluidSchemaManager

    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    assert result.is_valid, f"{blueprint_id} invalid: {result.errors}"
    binding = contract["exposes"][0]["binding"]
    assert binding["platform"] == platform
    assert binding["format"] == fmt


def test_gcp_starter_derives_underscore_table_identifier():
    # BigQuery table names disallow '-'; the starter must emit an underscore form.
    bp = get_bundled_blueprint("fluid.starter-gcp")
    contract = render_bundled_contract(bp, {"product_name": "Customer Signups"})
    loc = contract["exposes"][0]["binding"]["location"]
    assert loc["table"] == "customer_signups"
    assert "-" not in loc["table"]
    # Placeholder addressing defaults are present so the contract is complete.
    assert loc["project"] and loc["dataset"]


def test_snowflake_starter_derives_upper_table_identifier():
    # Snowflake convention is UPPER_SNAKE for the physical table.
    bp = get_bundled_blueprint("fluid.starter-snowflake")
    contract = render_bundled_contract(bp, {"product_name": "Customer Signups"})
    loc = contract["exposes"][0]["binding"]["location"]
    assert loc["table"] == "CUSTOMER_SIGNUPS"
    assert loc["account"] and loc["database"] and loc["schema"]


def test_provider_starters_listed_in_bundled_registry():
    # They surface automatically in the interactive "Start from a blueprint"
    # picker (which lists every bundled blueprint) — no menu edit needed.
    ids = {b["id"] for b in list_bundled_blueprints()}
    assert {"fluid.starter-gcp", "fluid.starter-snowflake"} <= ids


def test_provider_blueprint_map_keys_are_real_bundled_ids():
    ids = {b["id"] for b in list_bundled_blueprints()}
    for provider, bp_id in init_mod._QUICKSTART_PROVIDER_BLUEPRINTS.items():
        assert bp_id in ids, f"{provider} maps to missing blueprint {bp_id}"


# --- direct --blueprint flag is a non-interactive intent (no industry prompt) #


def test_direct_blueprint_flag_is_non_interactive(tmp_path, monkeypatch):
    # An explicit --blueprint id must skip the interactive industry picker (which
    # would EOF under a scripted/piped stdin), the same way --template does. This
    # runs the real init entry point end-to-end with no stdin.
    import io

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    args = SimpleNamespace(
        name="sf-direct",
        blueprint="fluid.starter-snowflake",
        quickstart=False,
        blank=False,
        template=None,
        list_templates=False,
        discover=None,
        agent=None,
        target_dir=None,
        provider="snowflake",
        yes=False,
        dry_run=False,
        quiet=True,
        workspace_lock=None,
        data_product_type=None,
    )
    rc = init_mod.run(args, _LOG)
    assert rc == 0
    assert (tmp_path / "sf-direct" / "contract.fluid.yaml").exists()


# --- detect_mode: provider-aware quickstart routing ------------------------ #


def _qs_args(**kw):
    base = dict(
        quickstart=True,
        blank=False,
        template=None,
        blueprint=None,
        provider="local",
        yes=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_quickstart_gcp_routes_to_gcp_starter():
    args = _qs_args(provider="gcp")
    assert init_mod.detect_mode(args, _LOG) == "blueprint"
    assert args.blueprint == "fluid.starter-gcp"
    assert args.yes is True


def test_quickstart_snowflake_routes_to_snowflake_starter():
    args = _qs_args(provider="snowflake")
    assert init_mod.detect_mode(args, _LOG) == "blueprint"
    assert args.blueprint == "fluid.starter-snowflake"


def test_quickstart_local_is_unchanged_customer_360():
    # The default flow must be byte-for-byte unchanged.
    args = _qs_args(provider="local")
    assert init_mod.detect_mode(args, _LOG) == "template"
    assert args.template == "customer-360"
    assert args.blueprint is None


def test_quickstart_no_provider_defaults_to_local_template():
    args = _qs_args(provider=None)
    assert init_mod.detect_mode(args, _LOG) == "template"
    assert args.template == "customer-360"


def test_quickstart_aws_has_no_starter_falls_back_to_template():
    # aws/azure have no bundled starter yet — keep the existing behavior rather
    # than error, so no provider value regresses.
    args = _qs_args(provider="aws")
    assert init_mod.detect_mode(args, _LOG) == "template"
    assert args.template == "customer-360"


def test_quickstart_provider_does_not_clobber_explicit_template():
    # An explicit --template wins over the provider-quickstart shortcut.
    args = _qs_args(provider="gcp", template="customer-360")
    assert init_mod.detect_mode(args, _LOG) == "template"
    assert args.blueprint is None


# --- end-to-end: the provider quickstart writes a validatable contract ----- #


def _bp_args(**kw):
    base = dict(
        name="demo",
        blueprint="fluid.starter-gcp",
        provider="gcp",
        dry_run=False,
        non_interactive=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "blueprint_id,platform",
    [
        ("fluid.starter-gcp", "gcp"),
        ("fluid.starter-snowflake", "snowflake"),
    ],
)
def test_blueprint_mode_writes_valid_provider_contract(
    tmp_path, monkeypatch, blueprint_id, platform
):
    monkeypatch.chdir(tmp_path)
    rc = blueprint_mode(_bp_args(blueprint=blueprint_id, provider=platform), _LOG)
    assert rc == 0
    contract = tmp_path / "demo" / "contract.fluid.yaml"
    assert contract.exists()
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    assert data["exposes"][0]["binding"]["platform"] == platform

    from fluid_build.cli.validate import run_on_contract_dict

    _result, code = run_on_contract_dict(data, strict=False, logger=_LOG)
    assert code == 0, data
