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

"""GCP governance through the real ``tofu`` and terraform-provider-google, on a live table.

What the data-loss gate decides depends on what the provider plans for a table
that already exists: BigQuery cannot partition an existing table, or re-key one in
place, so both must plan a replacement (``remove`` in the change summary, which
``fluid apply`` refuses without ``--allow-data-loss``), while a new retention period
must stay an in-place change. Only a real plan against real state shows that.

The state is made by a real ``tofu apply`` against ``_fake_bigquery.FakeBigQuery``,
an in-process stand-in for the BigQuery REST API (the goccy emulator crashes the
provider on apply). It also shows the dataset grants are added to the dataset's
access list, not written over it. No Google credential or endpoint is involved.

Not proven here: what real BigQuery accepts (a load into a policy-tagged column,
the service agent's use of the key), Cloud KMS and Data Catalog resources (the
fake serves BigQuery only; they are proven by ``tofu validate`` and the rendering
tests), and IAM evaluation.

Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry (or a
provider cache) for hashicorp/google.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.cli._apply_opentofu_engine import _data_loss_blocked
from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

from ._fake_bigquery import DEFAULT_ACCESS, FakeBigQuery

pytestmark = [pytest.mark.integration, pytest.mark.gcp, pytest.mark.provider]

_SKIP = runner.tofu_path() is None
PROJECT = "fluid-fake"
TABLE = "google_bigquery_table.gov_plan_orders"
DATASET = "google_bigquery_dataset.gov_plan_sales"
KEY = f"projects/{PROJECT}/locations/europe-west1/keyRings/ring/cryptoKeys/orders"


@pytest.fixture
def fake() -> Iterator[FakeBigQuery]:
    server = FakeBigQuery().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="module")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    """No Google credential reachable: every ``GOOGLE_*`` / gcloud variable is dropped."""
    env = {
        k: v
        for k, v in build_tofu_env().items()
        if not k.startswith(("GOOGLE_", "CLOUDSDK_", "GCLOUD_"))
    }
    env["CLOUDSDK_CONFIG"] = str(tmp_path_factory.mktemp("no-gcloud"))
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(tmp_path_factory.mktemp("tofu-plugin-cache")))
    return env


def _contract(
    *, retention: Optional[str] = None, kms: Optional[str] = None, field: bool = False
) -> Dict[str, Any]:
    binding: Dict[str, Any] = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {
            "project": PROJECT,
            "dataset": "sales",
            "table": "orders",
            "region": "europe-west1",
        },
        "principals": {"group:readers@company.example": "group:readers@fluid-fake.test-corp.com"},
    }
    if field:
        binding["location"]["partitionBy"] = ["created_at"]
    if kms:
        binding["encryption"] = {"kms": kms}
    exposure: Dict[str, Any] = {
        "exposeId": "orders",
        "binding": binding,
        "contract": {
            "schema": [
                {"name": "id", "type": "string", "required": True},
                {"name": "created_at", "type": "timestamp"},
            ]
        },
    }
    if retention:
        exposure["lifecycle"] = {"retention": retention, "expire": True}
    return {
        "fluidVersion": "0.7.6",
        "id": "gov.plan",
        "accessPolicy": {
            "grants": [{"principal": "group:readers@company.example", "permissions": ["read"]}]
        },
        "exposes": [exposure],
    }


def _write(contract: Dict[str, Any], workdir: Path, endpoint: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "main.tf.json").write_text(build_module(get_iac_plugin("gcp"), contract))
    provider = {
        "provider": {
            "google": {
                "project": PROJECT,
                "access_token": "fake-token",  # pragma: allowlist secret — not a credential
                "big_query_custom_endpoint": endpoint,
                "add_terraform_attribution_label": False,
            }
        }
    }
    (workdir / "provider.tf.json").write_text(json.dumps(provider))


def _init(workdir: Path, env: Dict[str, str]) -> None:
    done = runner.tofu_init(str(workdir), backend=False, env=env)
    if not done.ok and "registry" in (done.stderr + done.stdout).lower():
        pytest.skip(f"tofu init could not reach the provider registry: {done.stderr[:200]}")
    assert done.ok, done.stderr or done.stdout


def _plan(workdir: Path, env: Dict[str, str]) -> Dict[str, Any]:
    plan = runner.tofu_plan(str(workdir), out_file="tfplan", env=env)
    assert plan.ok, plan.stderr or plan.stdout
    shown = runner.tofu_show_plan(str(workdir), plan_file="tfplan", env=env)
    assert shown is not None
    return {
        "summary": runner.change_summary(plan),
        "changes": {c["address"]: c for c in shown.get("resource_changes") or []},
    }


def _apply(workdir: Path, env: Dict[str, str]) -> None:
    done = runner.tofu_apply(str(workdir), plan_file="tfplan", env=env)
    assert done.ok, done.stderr or done.stdout


def _actions(plan: Dict[str, Any], address: str) -> List[str]:
    return list(plan["changes"][address]["change"]["actions"])


def _live(contract: Dict[str, Any], workdir: Path, fake: FakeBigQuery, env: Dict[str, str]):
    _write(contract, workdir, fake.endpoint)
    _init(workdir, env)
    _plan(workdir, env)
    _apply(workdir, env)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_dataset_grants_are_added_to_its_access_list_not_written_over_it(tmp_path, fake, tofu_env):
    """The dataset keeps the entries BigQuery gave it, and gains the mapped member.

    Before, the emitter wrote an authoritative ``access`` list of the contract's
    principals as written: the project's owners and the creator were dropped, and
    the logical principal itself was the grantee.
    """
    _live(_contract(), tmp_path, fake, tofu_env)
    access = fake.datasets[(PROJECT, "sales")]["access"]
    for entry in DEFAULT_ACCESS:
        assert entry in access
    assert {"role": "READER", "groupByEmail": "readers@fluid-fake.test-corp.com"} in access
    assert not any("company.example" in json.dumps(e) for e in access)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
@pytest.mark.parametrize("field", [False, True], ids=["ingestion-time", "partition-column"])
def test_adding_retention_to_a_live_table_plans_its_replacement(tmp_path, fake, tofu_env, field):
    """BigQuery cannot partition an existing table; the plan must say replace.

    Without the ``terraform_data`` trigger the provider plans adding ingestion-time
    partitioning as an in-place update, which the API refuses at apply, and the
    data-loss gate never sees a removal.
    """
    _live(_contract(), tmp_path, fake, tofu_env)
    _write(_contract(retention="P30D", field=field), tmp_path, fake.endpoint)
    plan = _plan(tmp_path, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert plan["summary"]["remove"] >= 1
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=True)
    after = plan["changes"][TABLE]["change"]["after"]
    assert after["time_partitioning"][0]["expiration_ms"] == 30 * 86_400_000
    assert after["time_partitioning"][0]["type"] == "DAY"
    assert after["time_partitioning"][0].get("field") == ("created_at" if field else None)
    # Never a whole-table TTL: the module sets none, and the plan keeps the live one (none).
    module = json.loads((tmp_path / "main.tf.json").read_text())
    assert "expiration_time" not in module["resource"]["google_bigquery_table"]["gov_plan_orders"]
    assert after.get("expiration_time") in (None, 0)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_new_retention_period_is_an_in_place_change(tmp_path, fake, tofu_env):
    _live(_contract(retention="P30D"), tmp_path, fake, tofu_env)
    _write(_contract(retention="P90D"), tmp_path, fake.endpoint)
    plan = _plan(tmp_path, tofu_env)
    assert _actions(plan, TABLE) == ["update"]
    assert plan["summary"]["remove"] == 0
    assert not _data_loss_blocked(plan["summary"], allow_data_loss=False)
    after = plan["changes"][TABLE]["change"]["after"]
    assert after["time_partitioning"][0]["expiration_ms"] == 90 * 86_400_000


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_the_same_contract_plans_no_change(tmp_path, fake, tofu_env):
    """Applied once, the governed table plans clean: no perpetual replacement."""
    _live(_contract(retention="P30D", kms=KEY), tmp_path, fake, tofu_env)
    plan = _plan(tmp_path, tofu_env)
    assert plan["summary"] == {"add": 0, "change": 0, "remove": 0}


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_adding_a_key_to_a_live_table_plans_its_replacement_and_rekeys_the_dataset(
    tmp_path, fake, tofu_env
):
    _live(_contract(), tmp_path, fake, tofu_env)
    _write(_contract(kms=KEY), tmp_path, fake.endpoint)
    plan = _plan(tmp_path, tofu_env)
    assert _actions(plan, TABLE) == ["delete", "create"]
    assert _actions(plan, DATASET) == ["update"]
    assert _data_loss_blocked(plan["summary"], allow_data_loss=False)
    table = plan["changes"][TABLE]["change"]["after"]
    dataset = plan["changes"][DATASET]["change"]["after"]
    assert table["encryption_configuration"][0]["kms_key_name"] == KEY
    assert dataset["default_encryption_configuration"][0]["kms_key_name"] == KEY
    # Applied, the fake holds what BigQuery would be sent.
    _apply(tmp_path, tofu_env)
    assert fake.tables[(PROJECT, "sales", "orders")]["encryptionConfiguration"] == {
        "kmsKeyName": KEY
    }


# ── Revoking a grant is not data loss ────────────────────────────────────

MEMBERS = "google_bigquery_dataset_iam_member"


def _with_readers(*readers: str) -> Dict[str, Any]:
    contract = _contract()
    contract["exposes"][0]["binding"].pop("principals")
    contract["accessPolicy"]["grants"] = [
        {"principal": f"group:{r}", "permissions": ["read"]} for r in readers
    ]
    return contract


def _data_bearing(workdir: Path, env: Dict[str, str]):
    from fluid_build.cli._apply_opentofu_engine import _data_bearing_changes

    plan = runner.tofu_plan(str(workdir), out_file="tfplan", env=env)
    assert plan.ok, plan.stderr or plan.stdout
    summary = runner.change_summary(plan)
    return summary, _data_bearing_changes(summary, runner.planned_removals(plan))


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_revoking_a_reader_passes_the_data_loss_gate(tmp_path, fake, tofu_env):
    """A removed grant is a member destroy: counted, it needed --allow-data-loss."""
    _live(_with_readers("readers@corp-a.com", "departed@corp-a.com"), tmp_path, fake, tofu_env)
    _write(_with_readers("readers@corp-a.com"), tmp_path, fake.endpoint)
    summary, (data_changes, revoked) = _data_bearing(tmp_path, tofu_env)
    assert summary["remove"] == 1
    assert _data_loss_blocked(summary, allow_data_loss=False)  # what the gate saw before
    assert data_changes["remove"] == 0
    assert not _data_loss_blocked(data_changes, allow_data_loss=False)
    assert len(revoked) == 1 and revoked[0].startswith(f"{MEMBERS}.")
    _apply(tmp_path, tofu_env)
    access = json.dumps(fake.datasets[(PROJECT, "sales")]["access"])
    assert "departed@corp-a.com" not in access and "readers@corp-a.com" in access


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_table_replacement_is_still_gated_next_to_a_revocation(tmp_path, fake, tofu_env):
    _live(_with_readers("readers@corp-a.com", "departed@corp-a.com"), tmp_path, fake, tofu_env)
    contract = _with_readers("readers@corp-a.com")
    contract["exposes"][0]["lifecycle"] = {"retention": "P30D", "expire": True}
    _write(contract, tmp_path, fake.endpoint)
    _, (data_changes, revoked) = _data_bearing(tmp_path, tofu_env)
    assert len(revoked) == 1
    assert data_changes["remove"] == 1  # the table's replacement
    assert _data_loss_blocked(data_changes, allow_data_loss=False)


def test_removals_the_plan_does_not_itemise_all_count():
    """Fail closed: an event stream that misses a removal gates every removal."""
    from fluid_build.cli._apply_opentofu_engine import _data_bearing_changes

    changes = {"add": 0, "change": 0, "remove": 2}
    counted, revoked = _data_bearing_changes(changes, [(f"{MEMBERS}.a", MEMBERS)])
    assert counted["remove"] == 2 and revoked == []


# ── From an older forge-cli's authoritative access list ──────────────────


def _main_module(contract: Dict[str, Any]) -> str:
    """What forge-cli 0.16.6 and earlier emitted: the grants as the dataset's ``access``."""
    from fluid_build.iac.access import normalize_access_grants
    from fluid_build.iac.providers.gcp import _bq_access_entries

    module = json.loads(build_module(get_iac_plugin("gcp"), contract))
    resources = module["resource"]
    resources.pop(MEMBERS)
    (dataset,) = resources["google_bigquery_dataset"].values()
    dataset["access"] = _bq_access_entries(normalize_access_grants(contract))
    return json.dumps(module)


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_a_grant_removed_while_leaving_the_authoritative_list_is_revoked(tmp_path, fake, tofu_env):
    """Measured without the reconciliation: plan +1 ~0 -0, departed@ kept READER, re-plan clean.

    The provider keeps ``access`` as Computed once the module stops setting it, so
    an entry the old list held and no member resource covers was never revoked.
    """
    import logging

    from fluid_build.cli._apply_opentofu_engine import _reconcile_with_state

    before = _with_readers("readers@corp-a.com", "departed@corp-a.com")
    _write(before, tmp_path, fake.endpoint)
    (tmp_path / "main.tf.json").write_text(_main_module(before))
    _init(tmp_path, tofu_env)
    _plan(tmp_path, tofu_env)
    _apply(tmp_path, tofu_env)
    live = json.dumps(fake.datasets[(PROJECT, "sales")]["access"])
    assert "departed@corp-a.com" in live

    _write(_with_readers("readers@corp-a.com"), tmp_path, fake.endpoint)
    reports = _reconcile_with_state(
        get_iac_plugin("gcp"),
        tmp_path / "main.tf.json",
        str(tmp_path),
        tofu_env,
        logging.getLogger("test"),
    )
    assert reports == [
        {"dataset": "sales", "revoked": ["READER group:departed@corp-a.com"], "blocked": False}
    ]
    plan = _plan(tmp_path, tofu_env)
    assert _actions(plan, DATASET) == ["update"]
    assert plan["summary"]["remove"] == 0
    _apply(tmp_path, tofu_env)
    live = json.dumps(fake.datasets[(PROJECT, "sales")]["access"])
    assert "departed@corp-a.com" not in live and "readers@corp-a.com" in live

    # The next run finds the member resources in state and leaves ``access`` unset.
    _write(_with_readers("readers@corp-a.com"), tmp_path, fake.endpoint)
    assert (
        _reconcile_with_state(
            get_iac_plugin("gcp"),
            tmp_path / "main.tf.json",
            str(tmp_path),
            tofu_env,
            logging.getLogger("test"),
        )
        == []
    )
    assert _plan(tmp_path, tofu_env)["summary"] == {"add": 0, "change": 0, "remove": 0}


@pytest.mark.skipif(_SKIP, reason="needs `tofu` on PATH")
def test_an_unchanged_contract_leaving_the_authoritative_list_revokes_nothing(
    tmp_path, fake, tofu_env
):
    import logging

    from fluid_build.cli._apply_opentofu_engine import _reconcile_with_state

    contract = _with_readers("readers@corp-a.com")
    _write(contract, tmp_path, fake.endpoint)
    (tmp_path / "main.tf.json").write_text(_main_module(contract))
    _init(tmp_path, tofu_env)
    _plan(tmp_path, tofu_env)
    _apply(tmp_path, tofu_env)
    _write(contract, tmp_path, fake.endpoint)
    assert (
        _reconcile_with_state(
            get_iac_plugin("gcp"),
            tmp_path / "main.tf.json",
            str(tmp_path),
            tofu_env,
            logging.getLogger("test"),
        )
        == []
    )
    plan = _plan(tmp_path, tofu_env)
    assert plan["summary"]["remove"] == 0
