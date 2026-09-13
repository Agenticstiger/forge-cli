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

"""Stage 9 verifies what stage 7 provisioned, not what ``binding.format`` says.

``fluid verify`` dispatched on ``binding.format`` against the single literal
``bigquery_table``. Once the IaC emitter stopped doing the same — it resolves a
GCP expose's target from an explicit GCP format OR the shape of
``binding.location`` — the format string no longer identified the resource, and
the two stages disagreed about what an expose *is*:

* ``{platform: gcp, format: csv, location: {project, dataset}}`` is provisioned
  as a BigQuery table, but fell through to the local-file branch and failed the
  whole run with "no location.path declared" — diagnosing a missing file for a
  table that exists. ``error`` is fatal in verify, ``--strict`` or not, so this
  broke the pipeline stage for a target that was applied correctly.
* ``{platform: gcp, format: gcs_file, location: {bucket}}`` reached the same
  local-file branch for the same reason.
* An expose with no ``location.table`` is emitted under a table named for the
  exposure, but verify read ``location.table`` directly and built
  ``project.dataset.`` — three dot-separated parts, so it passed the shape
  check and then went looking for a table named ``""``.

Both halves now resolve through the emitter's own
``resolve_gcp_target`` / ``_bq_table_name`` rather than a second dispatch table
here — PR #475's post-mortem lesson, the same one the emitter fix follows.
"""

from __future__ import annotations

import argparse
import logging
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.verify import (
    _GCP_BIGQUERY,
    _GCP_NO_VERIFIER,
    _gcp_provisioned_kind,
    run,
)
from fluid_build.iac import get_iac_plugin

pytestmark = [pytest.mark.unit, pytest.mark.gcp]

_LOG = logging.getLogger("test_verify_gcp_dispatch")


def _args(contract_path):
    return argparse.Namespace(
        contract=str(contract_path),
        expose_id=None,
        strict=False,
        out=None,
        show_diffs=False,
        env=None,
    )


def _expose(binding, expose_id="daily_orders"):
    return {
        "exposeId": expose_id,
        "binding": binding,
        "contract": {"schema": [{"name": "order_date", "type": "DATE"}]},
    }


def _run(tmp_path, exposes, bq=None):
    """Drive ``verify.run`` with the cloud call stubbed; return (rc, bq_mock)."""
    path = tmp_path / "contract.fluid.yaml"
    path.write_text("id: analytics.demo\n", encoding="utf-8")
    contract = {"id": "analytics.demo", "exposes": exposes}
    bq = bq or MagicMock(return_value={"status": "match", "dimensions": {}})
    with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
        with patch("fluid_build.cli.verify.verify_bigquery_table", bq):
            return run(_args(path), _LOG), bq


class TestClassification:
    """``_gcp_provisioned_kind`` reads the emitter, not the format string."""

    @pytest.mark.parametrize(
        "binding,kind",
        [
            # The regression: provisioned as a BigQuery table.
            ({"platform": "gcp", "format": "csv", "location": {"dataset": "d"}}, _GCP_BIGQUERY),
            (
                {"platform": "gcp", "format": "bigquery_table", "location": {"dataset": "d"}},
                _GCP_BIGQUERY,
            ),
            ({"platform": "gcp", "location": {"dataset": "d"}}, _GCP_BIGQUERY),
            # Auto-detected as GCP by the shared alias table, so verified as GCP.
            ({"platform": "bigquery", "location": {"dataset": "d"}}, _GCP_BIGQUERY),
            # Provisioned, but forge ships no verifier for the resource.
            (
                {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "b"}},
                _GCP_NO_VERIFIER,
            ),
            ({"platform": "gcp", "format": "csv", "location": {"bucket": "b"}}, _GCP_NO_VERIFIER),
            ({"platform": "gcp", "location": {"topic": "t"}}, _GCP_NO_VERIFIER),
            # Not GCP-bound: the format chain still owns these.
            ({"platform": "local", "format": "csv", "location": {"path": "o.csv"}}, ""),
            ({"platform": "snowflake", "format": "snowflake_table", "location": {}}, ""),
            ({"platform": "gcp", "format": "http_api", "location": {}}, ""),
            ({}, ""),
            (None, ""),
        ],
    )
    def test_kind(self, binding, kind):
        assert _gcp_provisioned_kind(binding) == kind


class TestTheRegressionThisFixes:
    def test_a_gcp_csv_expose_over_a_dataset_is_verified_as_bigquery(self, tmp_path):
        """Previously: routed to the local-file verifier, which failed the run."""
        rc, bq = _run(
            tmp_path,
            [
                _expose(
                    {
                        "platform": "gcp",
                        "format": "csv",
                        "location": {"project": "acme", "dataset": "orders", "table": "daily"},
                    }
                )
            ],
        )
        assert rc == 0
        bq.assert_called_once()
        assert bq.call_args.kwargs["project"] == "acme"
        assert bq.call_args.kwargs["dataset"] == "orders"
        assert bq.call_args.kwargs["table"] == "daily"

    def test_a_gcp_bucket_expose_is_unsupported_not_a_missing_local_file(self, tmp_path):
        """ "No verifier" is not "the check failed" — the run stays green."""
        rc, bq = _run(
            tmp_path,
            [_expose({"platform": "gcp", "format": "csv", "location": {"bucket": "acme-raw"}})],
        )
        assert rc == 0
        bq.assert_not_called()

    def test_a_gcs_file_expose_is_unsupported(self, tmp_path):
        rc, bq = _run(
            tmp_path,
            [
                _expose(
                    {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "acme-raw"}}
                )
            ],
        )
        assert rc == 0
        bq.assert_not_called()


class TestVerifyAddressesTheTableTheEmitterDeclares:
    def test_the_table_name_falls_back_to_the_exposure_id(self, tmp_path):
        """Previously built ``acme.orders.`` and verified a table named ""."""
        rc, bq = _run(
            tmp_path,
            [
                _expose(
                    {"platform": "gcp", "location": {"project": "acme", "dataset": "orders"}},
                    expose_id="daily_orders",
                )
            ],
        )
        assert rc == 0
        assert bq.call_args.kwargs["table"] == "daily_orders"

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "gcp", "location": {"project": "acme", "dataset": "orders"}},
            {
                "platform": "gcp",
                "format": "csv",
                "location": {"project": "acme", "dataset": "orders", "table": "daily"},
            },
            {"platform": "bigquery", "location": {"project": "acme", "dataset": "orders"}},
            {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {"project": "acme", "dataset": "orders", "view": "v"},
            },
        ],
    )
    def test_the_two_stages_address_the_same_table(self, tmp_path, binding):
        """The pairing: whatever ``emit`` declares, ``verify`` goes looking for.

        This is the cross-check that keeps stages 7 and 9 from drifting — the
        failure the shared resolver exists to make unrepresentable.
        """
        exposure = _expose(binding)
        emitted = get_iac_plugin("gcp").emit({"id": "analytics.demo", "exposes": [exposure]}, [])
        table_ids = {b["table_id"] for b in emitted.get("google_bigquery_table", {}).values()}
        assert table_ids, "fixture emits no table — it cannot pin the pairing"

        rc, bq = _run(tmp_path, [exposure])
        assert rc == 0
        assert bq.call_args.kwargs["table"] in table_ids


class TestUnrelatedRoutesAreUntouched:
    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "local", "format": "csv", "location": {"path": "out.csv"}},
            {"platform": "snowflake", "format": "snowflake_table", "location": {"database": "D"}},
        ],
    )
    def test_a_non_gcp_expose_still_takes_the_format_chain(self, tmp_path, binding):
        _rc, bq = _run(tmp_path, [_expose(binding)])
        bq.assert_not_called()

    def test_the_legacy_dialect_still_verifies(self, tmp_path):
        """No ``binding`` at all — ``format`` + ``properties.target``."""
        rc, bq = _run(
            tmp_path,
            [
                {
                    "exposeId": "legacy",
                    "format": "bigquery_table",
                    "properties": {"target": "acme.orders.daily", "schema": []},
                }
            ],
        )
        assert rc == 0
        assert bq.call_args.kwargs["table"] == "daily"
