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

"""What real AWS showed about the Glue and provider emit.

Athena could not read a table forge-cli created: every query failed with
HIVE_UNSUPPORTED_FORMAT "Unable to create input format", because the Glue
table declared no Hive input format or SerDe. With the Parquet classes added
to the same live table, ``SELECT COUNT(*)`` returned the source's 10,172 rows.

And the provider took its region from the environment only, so a contract
bound to eu-west-1 applied from a shell defaulting to us-east-1 created its
Glue database in us-east-1, where a teardown that checked eu-west-1 then
reported it gone. That orphan was found in the demo account.
"""

from __future__ import annotations

import json
import logging

import pytest

from fluid_build.iac import build_module, get_iac_plugin, provider_config

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _expose(fmt="parquet", region="eu-west-1", table="orders", expose_id="orders"):
    loc = {"bucket": "acme-lake", "path": f"bronze/{table}/", "database": "bronze", "table": table}
    if region:
        loc["region"] = region
    return {
        "exposeId": expose_id,
        "binding": {"platform": "aws", "format": fmt, "location": loc},
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }


def _contract(*exposes):
    return {"id": "bronze.orders", "exposes": list(exposes)}


def _glue_table(res):
    return next(iter(res["aws_glue_catalog_table"].values()))


def test_a_parquet_glue_table_declares_the_hive_classes_athena_reads_it_through():
    sd = _glue_table(get_iac_plugin("aws").emit(_contract(_expose())))["storage_descriptor"]
    assert sd["input_format"] == "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    assert sd["output_format"] == "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
    assert sd["ser_de_info"]["serialization_library"] == (
        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    )
    assert sd["location"] == "s3://acme-lake/bronze/orders/"


@pytest.mark.parametrize("fmt", ["iceberg", "csv", "json"])
def test_other_formats_are_unchanged(fmt):
    """Iceberg is read through its metadata; csv and json are not verified live."""
    sd = _glue_table(get_iac_plugin("aws").emit(_contract(_expose(fmt=fmt))))["storage_descriptor"]
    assert set(sd) == {"columns", "location"}


def test_the_bindings_region_is_the_providers_region():
    assert provider_config(get_iac_plugin("aws"), _contract(_expose())) == {"region": "eu-west-1"}


def test_it_reaches_the_rendered_module():
    doc = json.loads(build_module(get_iac_plugin("aws"), _contract(_expose())))
    assert doc["provider"]["aws"]["region"] == "eu-west-1"


def test_it_wins_over_the_environment(monkeypatch):
    """The region the environment names is exactly what it must not decide."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert provider_config(get_iac_plugin("aws"), _contract(_expose()))["region"] == "eu-west-1"


def test_no_region_in_the_binding_emits_no_provider_block(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    assert provider_config(get_iac_plugin("aws"), _contract(_expose(region=None))) == {}


def test_bindings_in_two_regions_keep_the_environment_and_say_so(caplog):
    contract = _contract(
        _expose(region="eu-west-1"),
        _expose(region="eu-central-1", table="refunds", expose_id="refunds"),
    )
    with caplog.at_level(logging.WARNING):
        cfg = provider_config(get_iac_plugin("aws"), contract)
    assert "region" not in cfg
    assert "aws_bindings_span_regions" in caplog.text


def test_the_emulator_settings_survive_with_the_region(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:5001")
    cfg = provider_config(get_iac_plugin("aws"), _contract(_expose()))
    assert cfg["region"] == "eu-west-1"
    assert cfg["s3_use_path_style"] is True


def test_a_plugin_without_the_hook_is_unchanged():
    plugin = get_iac_plugin("gcp")
    assert not hasattr(plugin, "provider_block_for")
    assert provider_config(plugin, {"exposes": []}) == plugin.provider_block()
