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

"""End-to-end integration test: forge → generate → ``dbt parse``.

Takes a minimal retail intent through ``fluid forge data-model from-intent``,
then ``fluid generate speed-transformation --dbt-validate``, and finally
asserts the embedded ``dbt parse`` gate exits clean against the
generated project. This is the canary that catches regressions between
the forge emitter, the dbt project scaffold, and the dbt parse gate.

Automatically skipped when ``dbt`` is not on ``PATH`` so the suite stays
green on CI runners that don't install dbt. Runs against DuckDB via the
default ``local`` platform — no cloud warehouse creds needed.
"""

from __future__ import annotations

import logging
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from fluid_build.cli.forge_data_model import run_from_intent_command
from fluid_build.cli.generate_speed_transformation import run as run_speed_transformation

pytestmark = pytest.mark.skipif(
    shutil.which("dbt") is None,
    reason="dbt not installed; skipping end-to-end dbt parse gate test.",
)


def test_forge_generate_dbt_parse_dimensional(tmp_path: Path) -> None:
    """Retail → dimensional → dbt project must parse cleanly end-to-end.

    Mirrors the manual Phase 6 validation flow:
      1. forge data-model from-intent (heuristic, no LLM)
      2. generate speed-transformation --all-builds --dbt-validate
      3. dbt parse gate runs inside step 2; returncode 0 ⇒ pass
    """
    intent_path = tmp_path / "intent.yaml"
    intent_path.write_text(
        """
business_context:
  problem_statement: "Track retail sales line items for revenue analytics"

data_product:
  name: retail_sales
  domain: retail
  description: "Retail sales analytics (e2e dbt parse gate)"
  owner: analytics@example.com

grain:
  entity: sales_line
  description: "One row per sales line item"

dimensions:
  entities: [customer, product, store, date]
  attributes: [customer_name, product_name, store_name, day]

metrics:
  - name: net_amount
    description: "Net amount per line"
  - name: quantity
    description: "Units per line"

consumption:
  use_cases: ["Revenue by segment", "Basket composition"]

modeling:
  technique: dimensional
""",
        encoding="utf-8",
    )
    contract_path = tmp_path / "retail_sales.fluid.yaml"
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
    logger = logging.getLogger("test_forge_generate_dbt_parse_dimensional")
    assert run_from_intent_command(forge_args, logger) == 0, "forge from-intent must succeed"
    assert contract_path.exists(), "forge must emit the Fluid contract"

    out_dir = tmp_path / "dbt_retail"
    speed_args = Namespace(
        list_engines=False,
        contract=str(contract_path),
        output=str(out_dir),
        build_index=0,
        model=None,
        all_builds=True,
        concurrency=4,
        overwrite=True,
        env=None,
        verbose=False,
        dbt_validate=True,
    )

    rc = run_speed_transformation(speed_args, logger)
    assert rc == 0, (
        "speed-transformation must exit 0 when dbt parse gate passes; "
        f"actual rc={rc}. Gate failures bump the exit code."
    )

    # In ``all_builds=True`` mode each build lands under its own
    # subdirectory of ``out_dir``; the dimensional heuristic ships a
    # single ``main`` build. Locate the emitted project root the same
    # way the dbt parse gate does — by walking for the ``dbt_project.yml``.
    project_files = list(out_dir.rglob("dbt_project.yml"))
    assert project_files, f"expected exactly one dbt_project.yml under {out_dir}; found none"
    assert (
        len(project_files) == 1
    ), f"expected exactly one dbt_project.yml; found {len(project_files)}"
    project_dir = project_files[0].parent

    # The generator must have emitted the pieces that make dbt parse
    # possible without manual intervention: project file, profiles file,
    # and at least the canonical fact + dim models.
    assert (project_dir / "profiles.yml").exists(), (
        "profiles.yml must be emitted so fresh users without ~/.dbt/profiles.yml "
        "can run dbt parse immediately"
    )
    assert (project_dir / "models" / "marts" / "fact_sales_line.sql").exists()
