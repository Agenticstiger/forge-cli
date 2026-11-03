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

"""Whole-string validators refuse a value that ends in a newline.

Python's ``$`` matches at the end of the string and also just before a final
``\\n``, so ``re.match(r"^[a-z]+$", "orders\\n")`` succeeds and the caller goes on
to use ``"orders\\n"``: as a path segment, a SQL identifier or literal, a
Terraform attribute, a cache directory. Contract YAML yields such values easily
(a ``|`` block scalar ends in one), and so do ``.env`` files and percent-encoded
URL queries. The validators below end in ``\\Z`` (or call ``fullmatch``), which
matches only at the end of the string.

Each test hands one validator a value with a trailing newline, next to the same
value without it. It fails while that validator's pattern still ends in ``$``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


# --- SQL identifiers and literals --------------------------------------------


def test_quality_engine_refuses_a_selector_ending_in_a_newline():
    from fluid_build.providers import quality_engine

    assert '"amount"' in quality_engine._completeness_sql("t", "amount")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        quality_engine._completeness_sql("t", "amount\n")


def test_catalog_identifier_quoting_refuses_a_trailing_newline():
    from fluid_build.copilot.catalog._patterns import validate_and_quote_identifier
    from fluid_build.copilot.catalog.base import CatalogConfigError

    assert validate_and_quote_identifier("SALES", kind="database") == '"SALES"'
    with pytest.raises(CatalogConfigError):
        validate_and_quote_identifier("SALES\n", kind="database")


@pytest.mark.parametrize(
    ("kind", "value"), [("project", "acme-proj"), ("dataset", "sales"), ("table", "orders")]
)
def test_bigquery_catalog_identifiers_refuse_a_trailing_newline(kind, value):
    from fluid_build.copilot.catalog.base import CatalogConfigError
    from fluid_build.copilot.catalog.bigquery import _quote_ident

    assert _quote_ident(value, kind=kind) == f"`{value}`"
    with pytest.raises(CatalogConfigError):
        _quote_ident(f"{value}\n", kind=kind)


@pytest.mark.parametrize(
    ("kind", "value"), [("project", "acme-proj"), ("dataset", "sales"), ("table", "orders")]
)
def test_bigquery_catalog_name_check_refuses_a_trailing_newline(kind, value):
    # The unquoted check behind `table_name IN ('...')` and FQN parsing.
    from fluid_build.copilot.catalog.base import CatalogConfigError
    from fluid_build.copilot.catalog.bigquery import _validate_ident

    assert _validate_ident(value, kind=kind) == value
    with pytest.raises(CatalogConfigError):
        _validate_ident(f"{value}\n", kind=kind)


def test_jdbc_schema_filter_refuses_a_trailing_newline(monkeypatch, tmp_path):
    pytest.importorskip("duckdb")
    from fluid_build.cli.discover import _jdbc_introspect
    from fluid_build.providers import _duckdb_sandbox

    def _no_connect(*_args, **_kwargs):
        raise AssertionError("introspection connected with an invalid schema filter")

    monkeypatch.setattr(_duckdb_sandbox, "secure_duckdb_connect", _no_connect)
    db_path = tmp_path / "source.sqlite"
    sqlite3.connect(str(db_path)).close()
    with pytest.raises(ValueError, match="Invalid schema filter"):
        _jdbc_introspect.introspect_jdbc(
            source="sqlite", uri=f"sqlite:///{db_path}", schema_filter="main\n"
        )


def test_bigquery_project_quoting_refuses_a_trailing_newline():
    from fluid_build.providers.bigquery_validation import _quote_bq_project

    assert _quote_bq_project("acme-analytics") == "`acme-analytics`"
    with pytest.raises(ValueError, match="Invalid BigQuery project id"):
        _quote_bq_project("acme-analytics\n")


def test_gcp_planner_project_refuses_a_trailing_newline():
    from fluid_build.providers.gcp.plan.planner import _validate_bq_project

    assert _validate_bq_project("acme-analytics") == "acme-analytics"
    with pytest.raises(ValueError, match="Invalid GCP project ID"):
        _validate_bq_project("acme-analytics\n")


def test_snowflake_account_segment_refuses_a_trailing_newline():
    from fluid_build.providers.snowflake.util import config

    assert config._normalize_account_identifier("xy12345.eu-west-1") == "xy12345.eu-west-1"
    with pytest.raises(ValueError, match="Invalid Snowflake account identifier"):
        config._normalize_account_identifier("xy12345\n.eu-west-1")


def test_snowflake_account_region_pattern_refuses_a_trailing_newline():
    # The segment rule runs first and now refuses this input on its own; the
    # region rule must hold without it.
    from fluid_build.providers.snowflake.util import config

    assert config._ACCOUNT_REGION_SEGMENT_RE.match("eu-west-1")
    assert config._ACCOUNT_REGION_SEGMENT_RE.match("eu-west-1\n") is None


# --- Names that become paths, files and cache directories ---------------------


def test_build_identifier_refuses_a_trailing_newline():
    from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier

    assert validate_identifier("orders", kind="contract.id") == "orders"
    with pytest.raises(IdentifierViolation):
        validate_identifier("orders\n", kind="contract.id")


def test_federation_workspace_id_refuses_a_trailing_newline():
    from fluid_build.forge.federation import FederatedWorkspace

    row = {"id": "telco", "kind": "git_registry", "endpoint": "https://github.com/acme/telco"}
    assert FederatedWorkspace.from_dict(row).id == "telco"
    with pytest.raises(ValueError, match="federation workspace id"):
        FederatedWorkspace.from_dict({**row, "id": "telco\n"})


def test_bundle_recorded_env_refuses_a_trailing_newline(tmp_path):
    from fluid_build.forge.core.bundle import resolve_bundle_source_contract

    tgz = tmp_path / "bundle.tgz"
    _, why = resolve_bundle_source_contract(tgz, {"contract": "contract.fluid.yaml", "env": "aws"})
    assert "environment name" not in why
    path, why = resolve_bundle_source_contract(
        tgz, {"contract": "contract.fluid.yaml", "env": "aws\n"}
    )
    assert path is None
    assert "is not an environment name" in why


def test_consumes_overlay_env_refuses_a_trailing_newline():
    from fluid_build.build_runners._embedded_sql_io import ConsumesResolutionError, _validated_env

    assert _validated_env("aws") == "aws"
    with pytest.raises(ConsumesResolutionError):
        _validated_env("aws\n")


@pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold a newline")
@pytest.mark.parametrize(
    ("resolver", "name"),
    [("_resolve_tap_binary", "tap-postgres"), ("_resolve_target_binary", "target-jsonl")],
)
def test_singer_binary_lookup_refuses_a_name_ending_in_a_newline(
    monkeypatch, tmp_path, resolver, name
):
    from fluid_build.build_runners.meltano import runner

    for filename in (name, f"{name}\n"):
        binary = tmp_path / filename
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    resolve = getattr(runner, resolver)

    assert resolve(name) == str(tmp_path / name)
    assert resolve(f"{name}\n") is None


def test_prompt_profile_name_refuses_a_trailing_newline():
    from fluid_build.cli.forge_copilot_prompts import PromptProfileError, _resolve_profile_dir

    assert _resolve_profile_dir("eu-gdpr-strict").name == "eu-gdpr-strict"
    with pytest.raises(PromptProfileError, match="invalid prompt profile name"):
        _resolve_profile_dir("eu-gdpr-strict\n")


def test_prompt_overlay_name_refuses_a_trailing_newline():
    from fluid_build.cli.forge_prompt_overlays import PromptOverlayError, _resolve_overlay_path

    assert _resolve_overlay_path("pii-lockdown").name == "pii-lockdown.yaml"
    with pytest.raises(PromptOverlayError, match="invalid prompt overlay name"):
        _resolve_overlay_path("pii-lockdown\n")


# --- Identifiers written into a contract -------------------------------------


def test_jdbc_forged_product_id_drops_a_trailing_newline(monkeypatch, tmp_path):
    import yaml

    from fluid_build.cli import _forge_data_model_jdbc
    from fluid_build.cli.discover import _jdbc_introspect

    table = _jdbc_introspect.IntrospectedTable(
        schema="public",
        name="orders",
        columns=[_jdbc_introspect.IntrospectedColumn(name="id", type_name="integer")],
    )
    db = _jdbc_introspect.IntrospectedDatabase(
        source_kind="postgres", database="orders", tables=[table]
    )
    monkeypatch.setattr(_jdbc_introspect, "introspect_jdbc", lambda **_kwargs: db)

    def _forged_id(name):
        out = tmp_path / "out.fluid.yaml"
        args = SimpleNamespace(
            source="postgres",
            uri="postgresql://forge@db.example/orders",
            schema_name=None,
            tables=None,
            name=name,
            output=str(out),
        )
        assert _forge_data_model_jdbc._run_from_jdbc_source(args, logging.getLogger("test")) == 0
        return yaml.safe_load(out.read_text(encoding="utf-8"))["id"]

    assert _forged_id("orders") == "orders"
    assert _forged_id("orders\n") == "orders"


def test_dbt_importer_slugifies_an_identifier_ending_in_a_newline():
    from fluid_build.cli.import_workflow.dbt import _safe_identifier

    assert _safe_identifier("orders") == "orders"
    assert _safe_identifier("orders\n") == "orders"


def test_odcs_import_slugifies_an_id_ending_in_a_newline():
    # The bundled schema's identifier pattern ends in `$`; it is matched whole.
    from fluid_build.providers.odcs.mappers.normalize import _root_identifier

    assert _root_identifier("orders", "0.7.5") == ("orders", None)
    assert _root_identifier("orders\n", "0.7.5")[0] == "orders"


def test_odps_import_semver_check_refuses_a_trailing_newline():
    from fluid_build.providers.odps_standard.provider import _is_semver

    assert _is_semver("1.2.3", "0.7.5") is True
    assert _is_semver("1.2.3\n", "0.7.5") is False


def test_mission_predicate_path_refuses_a_trailing_newline():
    from fluid_build.copilot.missions.spec import MissionSpecError, validate_predicate_path

    # A stripped path can still carry one at the end of an inner segment.
    validate_predicate_path("exposes[*].contract.dq", label="checks[0]")
    with pytest.raises(MissionSpecError, match=r"invalid path segment 'contract\\n'"):
        validate_predicate_path("exposes[*].contract\n.dq", label="checks[0]")


# --- Values emitted into OpenTofu ---------------------------------------------


def test_row_filter_name_refuses_a_trailing_newline():
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.row_access import row_filters_for

    def _expose(name):
        row_filter = {
            "principal": "group:analysts@northwind.example",
            "name": name,
            "where": "consent = true",
        }
        return {
            "exposeId": "profile",
            "contract": {"schema": [{"name": "consent", "type": "boolean"}]},
            "policy": {"authz": {"rowFilters": [row_filter]}},
        }

    assert row_filters_for(_expose("analysts_consented"))[0].name == "analysts_consented"
    with pytest.raises(UnsupportedBindingError, match="must be lowercase letters"):
        row_filters_for(_expose("analysts_consented\n"))


def _aws_contract(region):
    location = {
        "bucket": "acme-lake",
        "path": "bronze/orders/",
        "database": "bronze",
        "table": "orders",
        "region": region,
    }
    expose = {
        "exposeId": "orders",
        "binding": {"platform": "aws", "format": "parquet", "location": location},
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }
    return {"id": "bronze.orders", "exposes": [expose]}


def test_aws_binding_region_ending_in_a_newline_is_refused(monkeypatch):
    # Neither dropped (the provider would take the environment's region) nor
    # stripped (sovereignty checks the value as written, so a stripped pin
    # could apply a region its deniedRegions names).
    from fluid_build.iac import get_iac_plugin, provider_config
    from fluid_build.iac.base import UnsupportedBindingError

    for name in [k for k in os.environ if k.startswith("AWS_ENDPOINT_URL")]:
        monkeypatch.delenv(name)
    plugin = get_iac_plugin("aws")

    assert provider_config(plugin, _aws_contract("eu-west-1"))["region"] == "eu-west-1"
    with pytest.raises(UnsupportedBindingError, match="whitespace around it"):
        provider_config(plugin, _aws_contract("eu-west-1\n"))


def test_aws_recorded_region_ending_in_a_newline_is_not_a_region():
    from fluid_build.iac.providers.aws import recorded_regions

    assert recorded_regions([{"values": {"region": "eu-west-1"}}]) == {"eu-west-1"}
    assert recorded_regions([{"values": {"region": "eu-west-1\n"}}]) == set()


@pytest.mark.parametrize(
    "query", ["branch=main%0A", "tag=v1.2%0A", "commit=0a1b2c3%0A", "branch=main&path=dbt%0A"]
)
def test_databricks_git_source_refuses_a_ref_or_path_ending_in_a_newline(query):
    from fluid_build.iac.providers.databricks_jobs import _git_source

    clean, refused = [], []
    repository = f"https://github.com/acme/jobs?{query}"
    assert _git_source(repository.replace("%0A", ""), "builds[0]", clean) is not None
    assert not clean
    assert _git_source(repository, "builds[0]", refused) is None
    assert refused


# --- Name validators other code calls ---------------------------------------


@pytest.mark.parametrize(
    ("method", "value"),
    [
        ("validate_s3_bucket_name", "acme-lake"),
        ("validate_glue_database_name", "bronze"),
        ("validate_glue_table_name", "orders"),
        ("validate_lambda_function_name", "ingest-orders"),
    ],
)
def test_aws_resource_names_refuse_a_trailing_newline(method, value):
    from fluid_build.providers.aws.util.validation import ResourceValidator

    check = getattr(ResourceValidator("123456789012", "eu-west-1"), method)
    assert check(value) == (True, None)
    valid, message = check(f"{value}\n")
    assert valid is False
    assert f"{value}\\n" in message  # the refused newline is shown, not printed


@pytest.mark.parametrize(
    ("resource_type", "name"),
    [("dataset", "sales"), ("bucket", "acme-lake"), ("topic", "orders-events"), ("job", "nightly")],
)
def test_gcp_resource_names_refuse_a_trailing_newline(resource_type, name):
    from fluid_build.providers.gcp.util.names import validate_name

    assert validate_name(name, resource_type) is True
    assert validate_name(f"{name}\n", resource_type) is False


def test_project_name_check_refuses_a_trailing_newline():
    from fluid_build.cli.security import InputSanitizer

    assert InputSanitizer.validate_project_name("my-project") is True
    assert InputSanitizer.validate_project_name("my-project\n") is False


def test_forge_project_name_refuses_a_trailing_newline():
    from fluid_build.cli.forge_validation import validate_project_name

    assert validate_project_name("orders-product") == (True, None)
    assert validate_project_name("orders-product\n")[0] is False


def test_forge_template_name_refuses_a_trailing_newline():
    from fluid_build.cli.forge_validation import validate_template_name

    assert validate_template_name("starter") == (True, None)
    assert validate_template_name("starter\n")[0] is False
