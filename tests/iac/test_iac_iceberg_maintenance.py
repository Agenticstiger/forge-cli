# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The OpenTofu half of Iceberg table maintenance (``exposes[].iceberg.maintenance``): backend
spark uploads the maintenance job config beside its writer build's and lets the scheduler read
it (``iac/providers/aws_ingest.py::_emit_maintenance``); backend glue provisions the Glue Data
Catalog's table optimizers (``iac/providers/aws.py::_emit_table_optimizers``)."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from fluid_build.build_runners.spark import _desired
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import build_module
from fluid_build.maintenance import plugins
from tests.iac import test_iac_aws_iceberg_metadata as glue_base
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    CONTRACT as SPARK_CONTRACT,
)
from tests.iac.test_iac_aws_spark_infra import KEY, operator_policy

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCOUNT = "123456789012"
ACCOUNT_REF = "${data.aws_caller_identity.fluid_lf_caller.account_id}"
ROLE = "arn:aws:iam::123456789012:role/glue-optimizer"
PRODUCT = "commerce.orders-stream"
MAINTAIN_KEY = f"{KEY}_maintain_order_lines"
MAINTAIN_OBJECT = f"_fluid/spark/{PRODUCT}/ingest-orders/maintenance/order_lines/job.json"
CALLER = "commerce_orders_stream_spark_caller"

SPARK_BLOCK = {
    "schedule": "0 3 * * *",
    "compaction": {"targetFileSizeMb": 256},
    "rewriteManifests": {},
    "expireSnapshots": {"olderThan": "P5D"},
    "removeOrphanFiles": {"olderThan": "P3D"},
}
GLUE_BLOCK = {
    "backend": "glue",
    "roleArn": ROLE,
    "compaction": {},
    "expireSnapshots": {"olderThan": "P7D", "retainLast": 2},
    "removeOrphanFiles": {},
}
OPTIMIZERS = "aws_glue_catalog_table_optimizer"
TABLE = "lane_ice_lake_events"


def emit(contract):
    plugin = get_iac_plugin("aws")
    return plugin.emit(contract), plugin.emit_data(contract)


@pytest.fixture
def plain():
    return copy.deepcopy(SPARK_CONTRACT)


@pytest.fixture
def maintained():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["exposes"][0]["iceberg"] = {"maintenance": copy.deepcopy(SPARK_BLOCK)}
    return contract


@pytest.fixture
def glue():
    contract = copy.deepcopy(glue_base.CONTRACT)
    contract["exposes"][0]["iceberg"] = {"maintenance": copy.deepcopy(GLUE_BLOCK)}
    return contract


def glue_block(contract):
    return contract["exposes"][0]["iceberg"]["maintenance"]


def optimizers(contract):
    return emit(contract)[0][OPTIMIZERS]


# --- backend spark: the job config OpenTofu uploads -------------------------------------


def test_the_maintenance_config_is_uploaded_beside_the_builds(maintained):
    resources, _ = emit(maintained)
    objects = resources["aws_s3_object"]
    assert {f"{KEY}_config", f"{KEY}_runner", MAINTAIN_KEY} <= set(objects)
    body = objects[MAINTAIN_KEY]
    build = objects[f"{KEY}_config"]
    assert body["key"] == MAINTAIN_OBJECT
    assert body["content_type"] == "application/json"
    assert body["bucket"] == build["bucket"]
    assert body["tags"] == build["tags"]
    config = json.loads(str(body["content"]).replace(ACCOUNT_REF, ACCOUNT))
    assert config["mode"] == "maintenance" and config["build"] == "ingest-orders"
    assert config["maintenance"]["expose"] == "order_lines"
    assert [s["step"] for s in config["maintenance"]["steps"]] == [
        "rewrite_data_files",
        "rewrite_manifests",
        "expire_snapshots",
        "remove_orphan_files",
    ]


def test_the_uploaded_config_is_the_one_apply_compiles_for_the_table(maintained):
    resources, _ = emit(maintained)
    text = str(resources["aws_s3_object"][MAINTAIN_KEY]["content"]).replace(ACCOUNT_REF, ACCOUNT)
    (run,) = maintenance_runs(maintained, account=ACCOUNT)
    assert "${" not in text
    assert _desired.same_config(text.encode("utf-8"), run.job_text)
    assert run.config_matches(text.encode("utf-8"))
    assert run.job_uri == f"s3://commerce-lake/{MAINTAIN_OBJECT}"
    # Another config in its place is refused.
    changed = json.loads(text)
    changed["maintenance"]["steps"].pop()
    assert not run.config_matches(json.dumps(changed).encode("utf-8"))


def test_a_changed_maintenance_changes_the_uploaded_config_and_nothing_of_the_builds(
    maintained,
):
    before, _ = emit(maintained)
    maintained["exposes"][0]["iceberg"]["maintenance"]["expireSnapshots"] = {"olderThan": "P9D"}
    after, _ = emit(maintained)
    assert after["aws_s3_object"][MAINTAIN_KEY] != before["aws_s3_object"][MAINTAIN_KEY]
    assert after["aws_s3_object"][f"{KEY}_config"] == before["aws_s3_object"][f"{KEY}_config"]


def test_the_scheduler_caller_may_read_the_maintenance_config(maintained):
    resources, data = emit(maintained)
    document = data["aws_iam_policy_document"][CALLER]
    assert CALLER in resources["aws_iam_policy"]
    artifacts = next(s for s in document["statement"] if s["sid"] == "Artifacts")
    assert artifacts["actions"] == ["s3:GetObject"]
    assert f"arn:aws:s3:::commerce-lake/{MAINTAIN_OBJECT}" in artifacts["resources"]
    # beside the build's own two objects
    assert any(r.endswith("/ingest-orders/job.json") for r in artifacts["resources"])
    assert len(artifacts["resources"]) == 3
    # an encrypted bucket's key is usable for it too
    kms = next(s for s in document["statement"] if s["sid"] == "S3Kms")
    context = next(c for c in kms["condition"] if c["test"] == "StringLike")
    assert f"arn:aws:s3:::commerce-lake/{MAINTAIN_OBJECT}" in context["values"]


def test_the_block_adds_resources_and_changes_nothing_else(plain, maintained):
    base = json.loads(build_module(get_iac_plugin("aws"), plain))
    assert "/maintenance/order_lines" not in json.dumps(base)
    document = json.loads(build_module(get_iac_plugin("aws"), maintained))
    objects = document["resource"]["aws_s3_object"]
    maintenance_object = objects.pop(MAINTAIN_KEY)
    assert maintenance_object["key"] == MAINTAIN_OBJECT
    caller = document["data"]["aws_iam_policy_document"][CALLER]
    for statement in caller["statement"]:
        for field in ("resources", "values"):
            if field in statement:
                statement[field] = [v for v in statement[field] if "/maintenance/" not in v]
        for condition in statement.get("condition") or []:
            condition["values"] = [v for v in condition["values"] if "/maintenance/" not in v]
    # The Glue table carries a copy of the contract text (its parameters, which a state never
    # reads back): that is the one place the block shows beyond the additions.
    for doc in (document, base):
        for table in doc["resource"]["aws_glue_catalog_table"].values():
            table["parameters"].pop("fluid_contract", None)
    assert document == base
    assert json.dumps(document, sort_keys=True) == json.dumps(base, sort_keys=True)


def test_a_contract_without_the_block_renders_the_same_every_time(plain):
    plugin = get_iac_plugin("aws")
    assert build_module(plugin, plain) == build_module(plugin, copy.deepcopy(plain))


def test_a_table_of_another_build_gets_no_object_beside_this_one(maintained):
    other = copy.deepcopy(maintained["builds"][0])
    other["id"] = "ingest-other"
    other["outputs"] = ["order_lines_2"]
    maintained["builds"].append(other)
    expose = copy.deepcopy(maintained["exposes"][0])
    expose["exposeId"] = "order_lines_2"
    expose["binding"]["location"]["table"] = "order_lines_2"
    expose["binding"]["location"]["path"] = "iceberg/commerce/order_lines_2/"
    expose["iceberg"] = {"maintenance": copy.deepcopy(SPARK_BLOCK)}
    maintained["exposes"].append(expose)
    resources, _ = emit(maintained)
    keys = sorted(k for k in resources["aws_s3_object"] if "_maintain_" in k)
    assert keys == [
        f"{KEY}_maintain_order_lines",
        "commerce_orders_stream_ingest_other_spark_maintain_order_lines_2",
    ]


def test_a_glue_table_uploads_no_spark_config(plain):
    plain["exposes"][0]["iceberg"] = {
        "maintenance": {"backend": "glue", "roleArn": ROLE, "compaction": {}}
    }
    resources, _ = emit(plain)
    assert not [k for k in resources["aws_s3_object"] if "_maintain_" in k]


def test_an_invalid_spark_block_is_refused_at_generate_time(maintained):
    maintained["exposes"][0]["iceberg"]["maintenance"]["schedule"] = "not a cron"
    with pytest.raises(UnsupportedBindingError) as err:
        emit(maintained)
    assert "iceberg-maintenance" in str(err.value) or "schedule" in str(err.value)


# --- a plugin's action ------------------------------------------------------------------


class Erasure:
    """A stub kind: one DELETE on the table, from the options."""

    def check(self, options):
        return []

    def statements(self, table, options):
        return [f"DELETE FROM {table} WHERE customer_id = '{options['customer']}'"]


@pytest.fixture
def stub_kind(monkeypatch):
    seen = []

    def action_for(name):
        seen.append(name)
        if name != "gdpr-erasure":
            raise plugins.MaintenanceActionError(f"no kind {name}")
        return Erasure()

    monkeypatch.setattr(plugins, "action_for", action_for)
    return SimpleNamespace(seen=seen)


def with_action(contract, kind="gdpr-erasure"):
    contract["exposes"][0]["iceberg"]["maintenance"]["actions"] = [
        {"name": "erase", "kind": kind, "options": {"customer": "c-1"}}
    ]
    return contract


def test_a_plugin_actions_statements_land_in_the_uploaded_config(maintained, stub_kind):
    resources, _ = emit(with_action(maintained))
    text = str(resources["aws_s3_object"][MAINTAIN_KEY]["content"]).replace(ACCOUNT_REF, ACCOUNT)
    steps = json.loads(text)["maintenance"]["steps"]
    # the action runs first, so the files it rewrites are compacted in the same run
    assert steps[0] == {
        "step": "statement",
        "name": "erase",
        "sql": "DELETE FROM fluid.commerce.order_lines WHERE customer_id = 'c-1'",
    }
    assert steps[1]["step"] == "rewrite_data_files"
    # and apply's compiled run carries the same bytes
    (run,) = maintenance_runs(with_action(maintained), account=ACCOUNT)
    assert run.config_matches(text.encode("utf-8"))
    assert stub_kind.seen


def test_a_statement_for_another_table_is_refused_at_generate_time(maintained, monkeypatch):
    class Wrong(Erasure):
        def statements(self, table, options):
            return ["DELETE FROM fluid.commerce.other WHERE 1 = 1"]

    monkeypatch.setattr(plugins, "action_for", lambda name: Wrong())
    with pytest.raises(UnsupportedBindingError) as err:
        emit(with_action(maintained))
    assert "iceberg-maintenance-action" in repr(err.value) or "own table" in str(err.value)
    assert "other" not in str(err.value).split("own table")[0]  # the SQL is never quoted


def test_a_missing_kind_is_refused_not_dropped(maintained, stub_kind):
    with pytest.raises(UnsupportedBindingError):
        emit(with_action(maintained, kind="nowhere"))


# --- backend glue: the table optimizers -------------------------------------------------


def test_glue_gets_the_three_optimizers(glue):
    found = optimizers(glue)
    assert set(found) == {
        f"{TABLE}_compaction",
        f"{TABLE}_retention",
        f"{TABLE}_orphan_file_deletion",
    }
    types = {k: v["type"] for k, v in found.items()}
    assert types == {
        f"{TABLE}_compaction": "compaction",
        f"{TABLE}_retention": "retention",
        f"{TABLE}_orphan_file_deletion": "orphan_file_deletion",
    }
    for body in found.values():
        assert body["catalog_id"] == ACCOUNT_REF
        assert body["database_name"] == f"${{aws_glue_catalog_table.{TABLE}.database_name}}"
        assert body["table_name"] == f"${{aws_glue_catalog_table.{TABLE}.name}}"
        assert body["configuration"]["role_arn"] == ROLE
        assert body["configuration"]["enabled"] is True


def test_the_optimizers_name_the_snapshot_and_orphan_settings(glue):
    found = optimizers(glue)
    retention = found[f"{TABLE}_retention"]["configuration"]["retention_configuration"]
    assert retention["iceberg_configuration"] == {
        "snapshot_retention_period_in_days": 7,
        "number_of_snapshots_to_retain": 2,
        "clean_expired_files": True,
    }
    orphan = found[f"{TABLE}_orphan_file_deletion"]["configuration"]
    assert orphan["orphan_file_deletion_configuration"]["iceberg_configuration"] == {
        "orphan_file_retention_period_in_days": 3
    }


def test_defaults_write_no_compaction_configuration_and_no_run_rate(glue):
    found = optimizers(glue)
    assert "compaction_configuration" not in found[f"{TABLE}_compaction"]["configuration"]
    assert "run_rate_in_hours" not in json.dumps(found)


def test_a_non_default_strategy_files_and_run_rate_appear(glue):
    block = glue_block(glue)
    block["compaction"] = {"strategy": "sort", "minInputFiles": 10, "deleteFileThreshold": 3}
    block["runEveryHours"] = 12
    found = optimizers(glue)
    assert found[f"{TABLE}_compaction"]["configuration"]["compaction_configuration"] == {
        "iceberg_configuration": {
            "strategy": "sort",
            "min_input_files": 10,
            "delete_file_threshold": 3,
        }
    }
    retention = found[f"{TABLE}_retention"]["configuration"]["retention_configuration"]
    orphan = found[f"{TABLE}_orphan_file_deletion"]["configuration"]
    assert retention["iceberg_configuration"]["run_rate_in_hours"] == 12
    assert orphan["orphan_file_deletion_configuration"]["iceberg_configuration"] == {
        "orphan_file_retention_period_in_days": 3,
        "run_rate_in_hours": 12,
    }
    # a compaction optimizer takes no run rate
    assert "run_rate_in_hours" not in json.dumps(found[f"{TABLE}_compaction"])


def test_only_the_declared_steps_get_an_optimizer(glue):
    block = glue_block(glue)
    del block["expireSnapshots"], block["removeOrphanFiles"]
    assert set(optimizers(glue)) == {f"{TABLE}_compaction"}


def test_a_disabled_step_gets_no_optimizer(glue):
    glue_block(glue)["removeOrphanFiles"] = {"enabled": False}
    assert f"{TABLE}_orphan_file_deletion" not in optimizers(glue)


def test_the_catalog_id_data_source_is_emitted(glue):
    plugin = get_iac_plugin("aws")
    assert plugin.emit_data(glue)["aws_caller_identity"]["fluid_lf_caller"] == {}


def test_a_table_without_maintenance_has_no_optimizer_and_no_caller_lookup():
    contract = copy.deepcopy(glue_base.CONTRACT)
    resources, data = emit(contract)
    assert OPTIMIZERS not in resources
    assert "fluid_lf_caller" not in data.get("aws_caller_identity", {})


def test_the_optimizers_render_into_the_module(glue):
    document = json.loads(build_module(get_iac_plugin("aws"), glue))
    assert set(document["resource"][OPTIMIZERS]) == set(optimizers(glue))
    assert "fluid_lf_caller" in document["data"]["aws_caller_identity"]


@pytest.mark.parametrize(
    "change, says",
    [
        ({"runEveryHours": 1}, "runEveryHours"),
        ({"roleArn": "not-an-arn"}, "roleArn"),
        ({"schedule": "0 3 * * *"}, "schedule"),
        ({"compaction": {"targetFileSizeMb": 64}}, "targetFileSizeMb"),
        ({"compaction": {"strategy": "nope"}}, "strategy"),
    ],
)
def test_an_invalid_glue_block_raises_the_binding_error_at_generate_time(glue, change, says):
    glue_block(glue).update(change)
    with pytest.raises(UnsupportedBindingError) as err:
        emit(glue)
    assert says in str(err.value)
    assert "iceberg-maintenance" in repr(err.value) or "maintenance" in str(err.value)


def test_a_glue_block_with_no_role_is_refused(glue):
    del glue_block(glue)["roleArn"]
    with pytest.raises(UnsupportedBindingError, match="roleArn"):
        emit(glue)
