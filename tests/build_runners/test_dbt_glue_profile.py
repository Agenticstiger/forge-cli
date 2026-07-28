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

"""Pin the dbt-glue profile builder.

A live dbt-glue test is intentionally NOT run by default — Glue
Interactive Sessions cost ~$2-5 per cold start and take 3-5 minutes to
provision (per AWS Glue pricing + the dbt-glue docs). This file pins
the profile-emit logic against a future regression so the dbt-glue
path stays correct without paying for a live run on every CI cycle.

The live counterpart is gated on ``FLUID_AWS_LIVE_DBT_GLUE=1`` and
lives in ``tests/iac/test_iac_aws_real_dbt_mesh_cli_e2e.py``.
"""

from __future__ import annotations

import pytest

from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile

pytestmark = pytest.mark.unit


def _build(platform: str, resources: dict, props: dict | None = None) -> dict:
    """Mini build→profile harness — drives ``_build_generated_dbt_profile``
    with the minimum required fields. The function reads
    ``build.execution.runtime.{platform,resources}`` and
    ``build.properties.*`` (see the function's signature)."""
    build = {
        "engine": "dbt",
        "execution": {
            "runtime": {
                "platform": platform,
                "resources": resources,
            }
        },
        "properties": props or {},
    }
    return _build_generated_dbt_profile(build, {})


class TestDbtGlueProfile:
    """The dbt-glue profile maps ``platform: glue`` (or ``aws-glue``)
    into a dbt 1.9+ glue adapter profile. The shape mirrors
    ``aws-samples/dbt-glue/sample_profiles.yml`` so dbt's adapter
    validator (which uses jsonschema against its own profile schema)
    accepts the emitted profile without surgery."""

    def test_minimum_required_fields(self, monkeypatch):
        """A bare ``platform: glue`` resolves env vars + emits the
        canonical type + region + schema + workers."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/dbt-glue")
        prof = _build("glue", {"schema": "fluid_demo_db"})
        body = prof["default"]["outputs"]["dev"]
        assert body["type"] == "glue"
        assert body["region"] == "eu-west-1"
        assert body["role_arn"] == "arn:aws:iam::000000000000:role/dbt-glue"
        assert body["schema"] == "fluid_demo_db"

    def test_aws_glue_alias_accepted(self, monkeypatch):
        """``platform: aws-glue`` is the published documentation
        spelling; the builder accepts both forms."""
        monkeypatch.setenv("AWS_REGION", "us-east-2")
        monkeypatch.setenv("GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/glue-r")
        prof = _build("aws-glue", {"schema": "ds"})
        assert prof["default"]["outputs"]["dev"]["type"] == "glue"

    def test_session_provisioning_timeout_defaults_to_240s(self, monkeypatch):
        """dbt-glue's upstream default of 20 s is too low for cold
        interactive sessions (which take 3-5 min); the builder bumps
        the default to 240 s."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof = _build("glue", {"schema": "ds"})
        body = prof["default"]["outputs"]["dev"]
        assert body["session_provisioning_timeout_in_seconds"] == 240

    def test_session_timeout_overridable_via_resources(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof = _build(
            "glue",
            {"schema": "ds", "session_provisioning_timeout_in_seconds": 600},
        )
        body = prof["default"]["outputs"]["dev"]
        assert body["session_provisioning_timeout_in_seconds"] == 600

    def test_workers_and_worker_type_defaults(self, monkeypatch):
        """Defaults follow ``sample_profiles.yml``: 5 workers, ``G.1X``
        type, 4 threads. These are the sweet-spot for small interactive
        sessions; the builder accepts overrides via resources/props."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof = _build("glue", {"schema": "ds"})
        body = prof["default"]["outputs"]["dev"]
        assert body["workers"] == 5
        assert body["worker_type"] == "G.1X"
        assert body["threads"] == 4

    def test_worker_overrides_apply(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof = _build("glue", {"schema": "ds", "workers": 20, "worker_type": "G.2X"})
        body = prof["default"]["outputs"]["dev"]
        assert body["workers"] == 20
        assert body["worker_type"] == "G.2X"

    def test_optional_glue_version_and_location(self, monkeypatch):
        """``glue_version`` + ``location`` are optional and only emit
        when supplied (matches the adapter's optional-field handling)."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof_minimal = _build("glue", {"schema": "ds"})
        out_min = prof_minimal["default"]["outputs"]["dev"]
        assert "glue_version" not in out_min
        assert "location" not in out_min

        prof_full = _build(
            "glue",
            {
                "schema": "ds",
                "glue_version": "4.0",
                "location": "s3://fluid-glue-warehouse/",
            },
        )
        out_full = prof_full["default"]["outputs"]["dev"]
        assert out_full["glue_version"] == "4.0"
        assert out_full["location"] == "s3://fluid-glue-warehouse/"

    def test_glue_database_aliases_schema(self, monkeypatch):
        """Glue uses ``schema`` (dbt's term for the target dataset) and
        ``glue_database`` (AWS's term) interchangeably — the builder
        accepts ``glue_database`` and maps it to ``schema``."""
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_GLUE_ROLE_ARN", "arn:aws:iam::000000000000:role/r")
        prof = _build("glue", {"glue_database": "fluid_demo_glue"})
        body = prof["default"]["outputs"]["dev"]
        assert body["schema"] == "fluid_demo_glue"
