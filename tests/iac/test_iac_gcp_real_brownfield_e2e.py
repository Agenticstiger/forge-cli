# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 3 — brownfield ``tofu import`` on real GCP.

Closes the brownfield gap surfaced by the gap-analysis: the GCP
plugin's :meth:`discover_imports` used to return ``[]``, so a first
apply against pre-existing infrastructure failed with
``409 Already Exists``. The plugin now emits ``import {}`` blocks
following the ``hashicorp/google`` provider's documented id format,
and the apply engine's ``_adopt_existing`` tolerates missing-resource
imports.

Test path: pre-create a BQ dataset out of band → apply a contract
that names the same dataset → verify ``tofu apply`` succeeds (the
dataset was adopted into state and reconciled, not re-created).

Gated on ``FLUID_IAC_LIVE_GCP=1`` like every other Stage 3 GCP test.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.iac import get_iac_plugin, runner

from .conftest import (
    GCP_LIVE_ENABLED,
    GCP_LIVE_PROJECT,
    GCP_LIVE_REGION,
    GCP_LIVE_SKIP_REASON,
    gcp_real_client,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.gcp,
    pytest.mark.slow,
    pytest.mark.skipif(not GCP_LIVE_ENABLED, reason=GCP_LIVE_SKIP_REASON),
]


def _brownfield_contract(*, dataset: str, table: str, cid: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": "Brownfield import test",
        "domain": "ledger",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng", "email": "x@x.co"}},
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": dataset,
                        "table": table,
                        "region": GCP_LIVE_REGION,
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {"name": "amount", "type": "integer"},
                    ]
                },
            }
        ],
    }


def test_real_brownfield_bq_dataset_adopted(gcp_real_project, gcp_account, monkeypatch):
    """A BigQuery dataset that already exists is adopted via ``tofu import``;
    apply reconciles rather than failing 409 Already Exists.
    """
    dataset = gcp_real_project.name("brownfield").replace("-", "_")
    table = "events"
    cid = "iac.gcp.brownfield"

    # discover_imports reads GOOGLE_PROJECT from env; make sure it's set
    # for this test process so the emitted import-id uses the real project.
    monkeypatch.setenv("GOOGLE_PROJECT", GCP_LIVE_PROJECT)

    # Pre-create the dataset OUT OF BAND.
    bq = gcp_real_client("bigquery")
    from google.cloud.bigquery import Dataset

    ds_ref = bq.create_dataset(Dataset(f"{GCP_LIVE_PROJECT}.{dataset}"), exists_ok=False)
    try:
        contract = _brownfield_contract(dataset=dataset, table=table, cid=cid)
        plugin = get_iac_plugin("gcp")
        blocks = plugin.discover_imports(contract)
        addrs = {b.to: b.id for b in blocks}
        ds_addrs = [a for a in addrs if a.startswith("google_bigquery_dataset.")]
        assert len(ds_addrs) == 1
        # The import id uses the full ``projects/<p>/datasets/<d>`` path.
        assert addrs[ds_addrs[0]] == f"projects/{GCP_LIVE_PROJECT}/datasets/{dataset}"

        # Emit + init + import + apply via the fixture's runner.
        gcp_real_project.emit(contract)
        init = gcp_real_project.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"

        adopted = 0
        for block in blocks:
            result = runner.tofu_import(
                str(gcp_real_project.workdir),
                block.to,
                block.id,
                env=gcp_real_project.env,
            )
            if result.ok:
                adopted += 1
        assert adopted >= 1, "expected at least the pre-existing BQ dataset to import"

        plan = gcp_real_project.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = gcp_real_project.apply()
        assert applied.ok, (
            f"tofu apply failed (409 AlreadyExists is the symptom of a "
            f"brownfield-discovery regression):\n{applied.stderr or applied.stdout}"
        )
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            bq.delete_dataset(ds_ref, delete_contents=True, not_found_ok=True)
