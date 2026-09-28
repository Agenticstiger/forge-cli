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

"""One contract, two clouds, one pipeline generator: the small collisions.

* Stage 11: the aws and the gcp DAG of one product had the same ``dag_id``
  (``<product>__<build>``) and the same schedule directory, so in one
  Airflow the second sync replaced (and ``--delete-scope product`` deleted)
  the first. The env is now in both.
* Stage 6: the generated plan stage did not run ``--check-sovereignty``, for
  any CI system, so a strict sovereignty violation first failed at apply.
* ``--env gcp`` with no gcp overlay applied the local base unchanged with a
  warning (measured: silver validate and plan rc 0). When the workspace's
  ``expected-environments`` (which fluid-demo-env keeps) declares gcp for the
  product it is now an error. The contract's own ``environments`` block only
  warns: forge-cli applies nothing from it.
"""

from __future__ import annotations

import argparse
import logging

import pytest
import yaml

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT, DEMO_CONTRACT_PATH, load_dag

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.one_contract_two_clouds")
_PRODUCT = "bronze.customer_subscriptions"


# ── Stage 11: the DAG id and the schedule directory name the env ─────────


def _dags(env):
    contract = yaml.safe_load(DEMO_CONTRACT)
    return fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=DEMO_CONTRACT_PATH)


def test_the_aws_and_the_gcp_dag_of_one_product_have_different_ids(monkeypatch):
    ids = {}
    for env in ("aws", "gcp"):
        (source,) = _dags(env).values()
        ids[env] = load_dag(source, monkeypatch).dag["dag_id"]
    assert ids == {
        "aws": f"{_PRODUCT}__aws__ingest_subscriptions",
        "gcp": f"{_PRODUCT}__gcp__ingest_subscriptions",
    }


def test_a_dag_with_no_env_keeps_its_id(monkeypatch):
    (source,) = _dags(None).values()
    assert load_dag(source, monkeypatch).dag["dag_id"] == f"{_PRODUCT}__ingest_subscriptions"


def test_each_env_gets_its_own_schedule_directory(tmp_path, monkeypatch):
    """schedule-sync --delete-scope product mirrors each directory with delete:
    one directory per env is what keeps the aws sync off the gcp DAG."""
    from fluid_build.cli import generate_artifacts
    from tests.cli._schedule_dag_fixtures import write_project

    monkeypatch.delenv("FLUID_ENV", raising=False)
    write_project(tmp_path)
    contract_dir = (tmp_path / DEMO_CONTRACT_PATH).parent
    (contract_dir / "overlays" / "gcp.yaml").write_text(
        yaml.safe_dump(
            {
                "exposes": [
                    {
                        "binding": {
                            "platform": "gcp",
                            "format": "bigquery_table",
                            "location": {
                                "project": "p",
                                "dataset": "d",
                                "table": "t",
                                "region": "europe-west1",
                            },
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for env in ("aws", "gcp"):
        parser = argparse.ArgumentParser()
        generate_artifacts.register_subcommand(parser.add_subparsers())
        argv = ["artifacts", DEMO_CONTRACT_PATH, "--out", f"dist-{env}", "--env", env]
        assert generate_artifacts.run(parser.parse_args(argv), _LOG) == 0
        scopes = sorted(p.name for p in (tmp_path / f"dist-{env}" / "schedule").iterdir())
        assert scopes == [f"{_PRODUCT}__{env}"]


# ── Stage 6: every generated plan stage checks sovereignty ────────────────


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _plan_commands(provider, complexity):
    """Every rendered command that runs ``fluid plan`` (Jenkins: stage 6's body)."""
    from fluid_build.forge.core.pipeline_templates import PipelineConfig, PipelineTemplateGenerator

    files = PipelineTemplateGenerator().generate_pipeline(
        PipelineConfig(provider=provider, complexity=complexity)
    )
    found = []
    for text in files.values():
        if provider.value == "jenkins":
            start = text.index("stage('6 - plan')")
            found.append(text[start : text.index("stage('7", start)])
            continue
        for doc in yaml.safe_load_all(text):
            found.extend(s for s in _strings(doc) if "fluid plan" in s)
    return found


def _cases():
    from fluid_build.forge.core.pipeline_templates import PipelineComplexity, PipelineProvider

    return [(p, c) for p in PipelineProvider for c in PipelineComplexity]


@pytest.mark.parametrize(
    "provider, complexity", _cases(), ids=lambda v: getattr(v, "value", str(v))
)
def test_every_generated_plan_stage_checks_sovereignty(provider, complexity):
    commands = _plan_commands(provider, complexity)
    if provider.value == "tekton" and complexity.value == "basic":
        # The basic Tekton pipeline references a `fluid-plan` Task it does not
        # render; there is no plan command in it to carry the flag.
        assert commands == []
        return
    assert commands, "no plan command rendered"
    for command in commands:
        assert "--check-sovereignty" in command, command


# ── --env for a declared environment with no overlay is refused ───────────

_SILVER = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "silver.subscription_status_summary",
    "name": "Subscription Status Summary",
    "description": "Declared-env fixture.",
    "domain": "Customer",
    "metadata": {"layer": "Silver", "owner": {"team": "data-platform", "email": "dp@example.com"}},
    "exposes": [
        {
            "exposeId": "summary",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "o.parquet"},
            },
            "contract": {"schema": [{"name": "status", "type": "STRING"}]},
        }
    ],
}


@pytest.fixture
def silver(tmp_path, monkeypatch):
    """fluid-demo-env's layout: a workspace declaring each product's targets,
    and a product with an aws overlay but (yet) no gcp one."""
    from fluid_build import loader

    loader._NOTED_MISSING_OVERLAYS.clear()
    (tmp_path / "fluid.workspace.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {"name": "demo"},
                "expected-environments": {"subscription_status_summary": ["local", "aws", "gcp"]},
            }
        ),
        encoding="utf-8",
    )
    product = tmp_path / "contracts" / "subscription_status_summary"
    (product / "overlays").mkdir(parents=True)
    (product / "overlays" / "aws.yaml").write_text(
        yaml.safe_dump({"exposes": [{"binding": {"platform": "aws", "format": "parquet"}}]}),
        encoding="utf-8",
    )
    contract = product / "contract.fluid.yaml"
    contract.write_text(yaml.safe_dump(_SILVER, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(product)
    return contract


def test_a_declared_env_with_no_overlay_is_refused(silver):
    from fluid_build._contract_loader import CLIError
    from fluid_build.loader import load_with_overlay

    with pytest.raises(CLIError) as exc:
        load_with_overlay(silver, "gcp")
    assert exc.value.event == "overlay_declared_but_missing"
    assert exc.value.context["declared_by"] == (
        "fluid.workspace.yaml expected-environments (subscription_status_summary)"
    )
    assert "bound to local" in str(exc.value)


def test_validate_plan_and_bundle_all_refuse_it(silver, tmp_path):
    from fluid_build.cli import main

    assert main(["validate", str(silver), "--env", "gcp"]) == 1
    assert main(["plan", str(silver), "--env", "gcp", "--out", str(tmp_path / "p.json")]) == 1
    assert not (tmp_path / "p.json").exists()
    assert main(["bundle", str(silver), "--env", "gcp", "--out", str(tmp_path / "b.tgz")]) != 0


def test_the_env_the_base_is_bound_to_and_dev_are_the_base(silver):
    from fluid_build.loader import load_with_overlay

    assert load_with_overlay(silver, "local")["exposes"][0]["binding"]["platform"] == "local"
    assert load_with_overlay(silver, "dev")["exposes"][0]["binding"]["platform"] == "local"
    assert load_with_overlay(silver, "aws")["exposes"][0]["binding"]["platform"] == "aws"


def test_an_undeclared_env_still_warns_and_says_what_the_base_binds_to(silver, caplog):
    from fluid_build.loader import load_with_overlay

    caplog.set_level(logging.WARNING, logger="fluid.loader")
    load_with_overlay(silver, "prod")
    (record,) = [r for r in caplog.records if "overlay_not_found" in r.getMessage()]
    assert "it binds to local, not to 'prod'" in record.getMessage()


def test_the_contract_s_own_environments_block_warns_and_does_not_refuse(silver, caplog):
    """A schema-valid ``environments`` block keeps validating, as on 0.16.5.

    forge-cli applies nothing from that block, so refusing on it offered one
    fix only: deleting a valid declaration. Only the workspace's
    ``expected-environments`` refuses; the block is named in the warning.
    """
    from fluid_build.loader import load_with_overlay

    doc = dict(_SILVER, environments={"staging": {}, "prod": {}})
    silver.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="fluid.loader")
    base = load_with_overlay(silver, "staging")
    assert base["exposes"][0]["binding"]["platform"] == "local"
    (record,) = [r for r in caplog.records if "overlay_not_found" in r.getMessage()]
    assert "environments block names 'staging'" in record.getMessage()
    assert record.declared_in_environments_block is True


def test_validate_env_named_only_by_the_environments_block_passes(tmp_path, monkeypatch):
    """No workspace, an ``environments`` block, no overlays: rc 0, with a warning."""
    from fluid_build import loader
    from fluid_build.cli import main

    loader._NOTED_MISSING_OVERLAYS.clear()
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(
        yaml.safe_dump(dict(_SILVER, environments={"staging": {}, "prod": {}}), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["validate", str(path), "--env", "prod"]) == 0
