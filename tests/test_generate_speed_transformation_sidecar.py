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

import logging
from argparse import Namespace

import pytest

from fluid_build.cli.forge_data_model import run_from_ddl_command, run_from_intent_command
from fluid_build.cli.generate_speed_transformation import run as run_speed_transformation


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_speed_transformation_consumes_model_sidecar(tmp_path):
    intent_path = tmp_path / "intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: customer_orders
  domain: retail
grain:
  entity: order_line
  time_dimension: order_date
dimensions:
  entities: [customer, product]
  attributes: [name, category]
""",
        encoding="utf-8",
    )
    contract_path = tmp_path / "customer_orders.fluid.yaml"
    forge_args = Namespace(
        intent_file=str(intent_path),
        technique="dimensional",
        output=str(contract_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        tiered=False,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
    )
    logger = logging.getLogger("test")
    assert run_from_intent_command(forge_args, logger) == 0

    out_dir = tmp_path / "generated"
    speed_args = Namespace(
        list_engines=False,
        contract=str(contract_path),
        output=str(out_dir),
        build_index=0,
        model=None,
        all_builds=False,
        concurrency=4,
        overwrite=True,
        env=None,
        verbose=False,
    )

    assert run_speed_transformation(speed_args, logger) == 0
    assert (out_dir / "models" / "staging" / "dim_customer.sql").exists()
    assert (out_dir / "models" / "staging" / "dim_product.sql").exists()
    # Heuristic emits the canonical ``fact_*`` prefix. We still accept the
    # legacy ``fct_*`` prefix for backward compatibility with older cached
    # sidecars (builder_agent strips both via ``removeprefix``).
    assert (out_dir / "models" / "marts" / "fact_order_line.sql").exists()


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_speed_transformation_emits_source_backed_ddl_models(tmp_path):
    ddl_path = tmp_path / "snowflake.sql"
    ddl_path.write_text(
        """
create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."ACCOUNT" (
    "ACCOUNT_ID" VARCHAR(64) PRIMARY KEY,
    "PARTY_ID" VARCHAR(64),
    "STATUS" VARCHAR(32)
);
create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."PARTY" (
    "PARTY_ID" VARCHAR(64) PRIMARY KEY,
    "PARTY_TYPE" VARCHAR(32)
);
""",
        encoding="utf-8",
    )
    contract_path = tmp_path / "telco.fluid.yaml"
    forge_args = Namespace(
        ddl=[str(ddl_path)],
        source_type="snowflake",
        technique="data_vault_2",
        output=str(contract_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        tiered=False,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
        industry=None,
        deterministic=True,
    )
    logger = logging.getLogger("test")
    assert run_from_ddl_command(forge_args, logger) == 0

    out_dir = tmp_path / "generated_ddl"
    speed_args = Namespace(
        list_engines=False,
        contract=str(contract_path),
        output=str(out_dir),
        build_index=0,
        model=None,
        all_builds=False,
        concurrency=4,
        overwrite=True,
        env=None,
        verbose=False,
    )

    assert run_speed_transformation(speed_args, logger) == 0
    sources = (out_dir / "models" / "sources.yml").read_text(encoding="utf-8")
    hub_sql = (out_dir / "models" / "staging" / "hub_account.sql").read_text(encoding="utf-8")
    link_sql = (out_dir / "models" / "intermediate" / "lnk_account_party.sql").read_text(
        encoding="utf-8"
    )
    assert "name: raw" in sources
    assert "name: ACCOUNT" in sources
    assert "source('raw', 'ACCOUNT')" in hub_sql
    assert "adapter.get_relation" in hub_sql
    assert "source('raw', 'PARTY')" in link_sql


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_from_ddl_uses_real_uppercase_id_when_primary_keys_are_missing(tmp_path):
    ddl_path = tmp_path / "snowflake.sql"
    ddl_path.write_text(
        """
create or replace TABLE "TELCO_LAB"."TELCO_STAGE_LOAD"."ACCOUNT" (
    "ACCOUNT_ID" VARCHAR(64),
    "STATUS" VARCHAR(32)
);
""",
        encoding="utf-8",
    )
    contract_path = tmp_path / "telco.fluid.yaml"
    forge_args = Namespace(
        ddl=[str(ddl_path)],
        source_type="snowflake",
        technique="data_vault_2",
        output=str(contract_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        tiered=False,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
        industry=None,
        deterministic=True,
    )
    logger = logging.getLogger("test")
    assert run_from_ddl_command(forge_args, logger) == 0

    sidecar = contract_path.with_name(f"{contract_path.name}.model.json").read_text(
        encoding="utf-8"
    )
    assert '"ACCOUNT_ID"' in sidecar

    out_dir = tmp_path / "generated_no_pk"
    speed_args = Namespace(
        list_engines=False,
        contract=str(contract_path),
        output=str(out_dir),
        build_index=0,
        model=None,
        all_builds=False,
        concurrency=4,
        overwrite=True,
        env=None,
        verbose=False,
    )

    assert run_speed_transformation(speed_args, logger) == 0
    hub_sql = (out_dir / "models" / "staging" / "hub_account.sql").read_text(encoding="utf-8")
    assert "ACCOUNT_ID" in hub_sql
    assert "account_id" not in hub_sql
