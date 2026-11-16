# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The migration never deletes state; it prints the command that would."""

from fluid_build.iac.state_migration import MIGRATED, StateReconciliation


def _summary(legacy):
    return StateReconciliation(MIGRATED, legacy, {"s3": {"bucket": "b", "key": "new"}}, 3).summary()


def test_s3_hint_names_the_object_and_the_lock_and_the_region():
    out = _summary(
        {"s3": {"bucket": "b", "key": "fluid/x/terraform.tfstate", "region": "eu-west-1"}}
    )
    assert "the old object is left in place" in out
    assert "aws s3 rm s3://b/fluid/x/terraform.tfstate --region eu-west-1" in out
    assert "aws s3 rm s3://b/fluid/x/terraform.tfstate.tflock --region eu-west-1" in out


def test_gcs_hint_names_the_default_state_object():
    out = _summary({"gcs": {"bucket": "b", "prefix": "fluid/x"}})
    assert "gcloud storage rm gs://b/fluid/x/default.tfstate" in out


def test_hint_quotes_a_hostile_key():
    out = _summary({"s3": {"bucket": "b", "key": "k; rm -rf ~"}})
    assert "'s3://b/k; rm -rf ~'" in out
