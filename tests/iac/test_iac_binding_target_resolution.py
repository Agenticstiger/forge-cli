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

"""Which exposures each IaC plugin claims, and what they resolve to.

The regression these pin: an exposure that ``fluid generate iac`` routes to a
plugin and that plugin then skips — a silent no-op on the *matching* provider,
the class of bug the ``--provider`` cross-check cannot catch because the
provider is right.

Two shapes of it existed:

* the GCP emitter dispatched purely on ``binding.format``, so a
  ``platform: gcp`` expose with no format, or with the schema-valid ``gcs_file``
  spelling the emitter never grew, emitted nothing;
* the AWS and Snowflake emitters compared ``binding.platform`` to a literal, so
  an alias the auto-detector accepts (``glue``, ``s3``, ``bigquery`` …) routed
  to a plugin that then claimed none of the contract's exposures.

Both are now one table (``iac.provider_match``) and, for GCP, one
resolver (``providers.gcp.resolve_gcp_target``) shared by ``emit``,
``emit_data``, ``discover_imports`` and the validate-time gate.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from fluid_build.iac import get_iac_plugin, is_cloud
from fluid_build.iac.provider_match import (
    PROVIDER_ALIASES,
    canonical_cloud,
    detect_clouds,
)
from fluid_build.iac.providers import gcp as gcp_plugin
from fluid_build.iac.providers.gcp import resolve_gcp_target, validate_gcp_binding

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _contract(exposes, **extra):
    return {"id": "analytics.demo", "name": "Demo", "exposes": exposes, **extra}


def _expose(binding, expose_id="orders", schema=None):
    return {
        "exposeId": expose_id,
        "binding": binding,
        "contract": {
            "schema": schema if schema is not None else [{"name": "id", "type": "string"}]
        },
    }


class TestCanonicalCloudIsOneTable:
    """The detector and the emitters normalise platform tokens identically."""

    @pytest.mark.parametrize(
        "token,cloud",
        [
            ("gcp", "gcp"),
            ("bigquery", "gcp"),
            ("google", "gcp"),
            ("gcs", "gcp"),
            ("aws", "aws"),
            ("glue", "aws"),
            ("s3", "aws"),
            ("athena", "aws"),
            ("redshift", "aws"),
            ("snowflake", "snowflake"),
            ("confluent", "confluent"),
            ("  GCP  ", "gcp"),
            ("nonesuch", ""),
            (None, ""),
            (42, ""),
        ],
    )
    def test_tokens_map(self, token, cloud):
        assert canonical_cloud(token) == cloud

    def test_every_consumer_reads_the_same_table(self, monkeypatch):
        """A token added to the one table reaches every consumer.

        Asserted by ADDING one and watching it propagate, rather than by
        comparing each consumer to the table (which holds for any table
        contents and so cannot fail). ``provider_match`` is the single home
        (PR #546); ``generate_iac`` delegates to it, and the plugins'
        per-exposure filter is built on it.
        """
        from fluid_build.cli.generate_iac import _canonical_cloud

        monkeypatch.setitem(PROVIDER_ALIASES, "bigtable", "gcp")
        assert canonical_cloud("bigtable") == "gcp"
        assert _canonical_cloud("bigtable") == "gcp"  # the CLI's detector
        assert is_cloud({"platform": "bigtable"}, "gcp") is True  # the plugins' filter
        assert detect_clouds({"exposes": [_expose({"platform": "bigtable"})]}) == ["gcp"]
        # ...and the GCP emitter then actually claims such an exposure.
        binding = {"platform": "bigtable", "location": {"dataset": "d"}}
        assert resolve_gcp_target(binding) == gcp_plugin.BIGQUERY_TABLE

    def test_is_cloud_tolerates_non_mappings(self):
        assert is_cloud({"platform": "bigquery"}, "gcp") is True
        assert is_cloud({}, "gcp") is False
        assert is_cloud(None, "gcp") is False


class TestEveryDetectedCloudClaimsItsExposure:
    """Auto-detect and emit must agree, or the module comes out empty."""

    @pytest.mark.parametrize(
        "platform,cloud,location",
        [
            ("gcp", "gcp", {"dataset": "sales", "table": "orders"}),
            ("bigquery", "gcp", {"project": "acme", "dataset": "sales"}),
            ("gcs", "gcp", {"bucket": "acme-raw"}),
            ("aws", "aws", {"database": "db", "table": "orders", "bucket": "b"}),
            ("glue", "aws", {"database": "db", "table": "orders", "bucket": "b"}),
            ("s3", "aws", {"bucket": "acme-raw"}),
            ("redshift", "aws", {"workgroup": "wg", "namespace": "ns"}),
            ("snowflake", "snowflake", {"database": "DB", "schema": "SC", "table": "T"}),
        ],
    )
    def test_detected_plugin_emits_something(self, platform, cloud, location):
        contract = _contract([_expose({"platform": platform, "location": location})])
        assert cloud in detect_clouds(contract)
        assert get_iac_plugin(cloud).emit(contract, []) != {}


class TestResolveGcpTarget:
    @pytest.mark.parametrize(
        "binding,target",
        [
            # An explicit GCP format wins, whatever the platform says — the
            # five spellings this emitter has always dispatched on.
            ({"format": "bigquery_table"}, gcp_plugin.BIGQUERY_TABLE),
            ({"format": "bigquery_view"}, gcp_plugin.BIGQUERY_VIEW),
            ({"format": "gcs_bucket"}, gcp_plugin.GCS_BUCKET),
            (
                {"format": "iceberg", "location": {"bucket": "wh"}},
                gcp_plugin.ICEBERG_STORAGE,
            ),
            (
                {"format": "iceberg_table", "location": {"warehouse": "gs://wh/data"}},
                gcp_plugin.ICEBERG_STORAGE,
            ),
            # An Iceberg expose with no derivable bucket emits nothing, so it
            # must resolve to nothing — otherwise the gate would wave through
            # an exposure the emitter silently skips.
            ({"format": "iceberg", "location": {"catalog": "bigquery"}}, None),
            ({"format": "iceberg"}, None),
            ({"format": "pubsub_topic"}, gcp_plugin.PUBSUB_TOPIC),
            # Otherwise the location shape decides, for a GCP platform.
            (
                {"platform": "gcp", "location": {"dataset": "d"}},
                gcp_plugin.BIGQUERY_TABLE,
            ),
            # Shape inference never produces a VIEW: `view`/`query` are not in
            # the schema's `bindingLocation`, and a view inferred without a
            # body would emit `view: {query: ""}`, which BigQuery rejects.
            # Naming a view is what `format: bigquery_view` is for.
            (
                {"platform": "gcp", "location": {"dataset": "d", "view": "v"}},
                gcp_plugin.BIGQUERY_TABLE,
            ),
            # ...and `subscription` alone is not a Pub/Sub topic: it supplies
            # no topic name, so inferring one made `_emit_pubsub` fabricate a
            # `<contract>-topic` found nowhere in the contract.
            ({"platform": "gcp", "location": {"subscription": "s"}}, None),
            (
                {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "b"}},
                gcp_plugin.GCS_BUCKET,
            ),
            (
                {"platform": "gcp", "location": {"topic": "t"}},
                gcp_plugin.PUBSUB_TOPIC,
            ),
            (
                {"platform": "bigquery", "location": {"project": "p", "dataset": "d"}},
                gcp_plugin.BIGQUERY_TABLE,
            ),
            # Nothing to go on.
            ({"platform": "gcp", "location": {"project": "p"}}, None),
            ({"platform": "gcp"}, None),
            # Shape inference is gated on the platform: an AWS bucket is not
            # a GCS bucket, even though both spell the key ``bucket``.
            ({"platform": "aws", "location": {"bucket": "b"}}, None),
            ({"platform": "snowflake", "location": {"database": "d"}}, None),
            ({"format": "parquet", "location": {"table": "t"}}, None),
            (None, None),
        ],
    )
    def test_resolution(self, binding, target):
        assert resolve_gcp_target(binding) == target


class TestGcpEmitsForTheResolvedTarget:
    def test_platform_only_bigquery_expose_emits(self):
        """The reported repro: platform + location, no ``binding.format``."""
        res = get_iac_plugin("gcp").emit(
            _contract(
                [
                    _expose(
                        {
                            "platform": "bigquery",
                            "location": {"project": "acme", "dataset": "orders"},
                        },
                        expose_id="daily_orders",
                    )
                ]
            ),
            [],
        )
        assert "google_bigquery_dataset" in res
        # No ``location.table`` — the table is named for the exposure.
        table = res["google_bigquery_table"]["analytics_demo_daily_orders"]
        assert table["table_id"] == "daily_orders"

    def test_schema_valid_gcs_file_spelling_emits(self):
        """``gcs_file`` is the only GCS spelling the contract schema admits."""
        res = get_iac_plugin("gcp").emit(
            _contract(
                [
                    _expose(
                        {
                            "platform": "gcp",
                            "format": "gcs_file",
                            "location": {"bucket": "acme-raw"},
                        }
                    )
                ],
            ),
            [],
        )
        assert res["google_storage_bucket"]["analytics_demo_acme_raw"]["name"] == "acme-raw"

    def test_unresolvable_expose_still_emits_nothing(self):
        assert (
            get_iac_plugin("gcp").emit(
                _contract([_expose({"platform": "gcp", "location": {"project": "acme"}})]), []
            )
            == {}
        )

    def test_shape_inference_does_not_claim_a_foreign_platform(self):
        """A bucket named by a Snowflake expose is not a GCS bucket.

        This pins what the change actually guarantees: STEP 2 of the resolver
        is gated on the platform. It deliberately uses a format that carries
        no GCP meaning, so the exposure can only be claimed via shape — a
        fixture whose format could never have matched would pass here for the
        wrong reason.

        Step 1 (the explicit-format table) remains platform-agnostic, which is
        pre-existing behaviour: ``format: iceberg`` on a non-GCP binding is
        still claimed. See the module docstring.
        """
        contract = _contract(
            [
                _expose(
                    {
                        "platform": "snowflake",
                        "format": "parquet",
                        "location": {"database": "DB", "schema": "SC", "bucket": "b"},
                    }
                )
            ]
        )
        assert get_iac_plugin("gcp").emit(contract, []) == {}

    def test_the_explicit_format_table_is_still_platform_agnostic(self):
        """Documents a PRE-EXISTING leak this change does not alter.

        ``_FORMAT_TARGETS`` is consulted before any platform check, so a GCP
        format spelling on a foreign binding is claimed by the GCP emitter —
        byte-identical to the behaviour before this change. Pinned so the
        behaviour is visible and a future narrowing is a deliberate, reviewed
        edit rather than a silent one.
        """
        contract = _contract(
            [_expose({"platform": "aws", "format": "iceberg", "location": {"bucket": "s3-lake"}})]
        )
        assert "google_storage_bucket" in get_iac_plugin("gcp").emit(contract, [])


class TestEmitAndImportsAgree:
    """discover_imports addresses what emit declares, or brownfield apply fails."""

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "bigquery", "location": {"dataset": "orders"}},
            {"platform": "gcp", "location": {"dataset": "orders", "table": "daily"}},
            {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "acme-raw"}},
            {"platform": "gcp", "location": {"topic": "events"}},
            # An Iceberg warehouse's bucket comes from ``iceberg_bucket_name``,
            # where ``warehouse`` beats ``location.bucket``. Reading the
            # location keys directly imported nothing here, and the WRONG
            # bucket in the both-keys case below.
            {"platform": "gcp", "format": "iceberg", "location": {"warehouse": "gs://wh/data"}},
            {
                "platform": "gcp",
                "format": "iceberg",
                "location": {"bucket": "raw", "warehouse": "gs://other/x"},
            },
        ],
    )
    def test_every_emitted_resource_has_an_import_block(self, binding):
        contract = _contract([_expose(binding, expose_id="daily")])
        plugin = get_iac_plugin("gcp")
        emitted = {
            f"{kind}.{name}"
            for kind, bodies in plugin.emit(contract, []).items()
            for name in bodies
        }
        imported = {block.to for block in plugin.discover_imports(contract, [])}
        assert emitted, "fixture emits nothing — it cannot pin the pairing"
        assert emitted <= imported, "emit declares a resource with no import block"
        # ...and the reverse: an import for a container ``emit`` never declares
        # would adopt infrastructure this module does not manage.
        assert imported <= emitted, "import block addresses an undeclared resource"

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "gcp", "location": {"dataset": "d", "bucket": "b"}},
            {"platform": "gcp", "format": "csv", "location": {"dataset": "d", "bucket": "b"}},
        ],
    )
    def test_a_location_naming_two_containers_imports_only_the_resolved_one(self, binding):
        """``emit`` resolves ONE target per exposure, so importing both would
        address a container the module never declares."""
        contract = _contract([_expose(binding, expose_id="daily")])
        plugin = get_iac_plugin("gcp")
        imported = {block.to for block in plugin.discover_imports(contract, [])}
        assert not any(a.startswith("google_storage_bucket.") for a in imported)
        assert any(a.startswith("google_bigquery_dataset.") for a in imported)


class TestEmitAndEmitDataAgreeUnderSharedPackaging:
    """A pooled container must be looked up, not declared — and every
    ``${data.…}`` the resources reference must exist, or ``tofu validate``
    fails with "Reference to undeclared resource"."""

    _SHARED = {"mode": "shared", "pool": "acme-pool"}
    _GRANTS = {"grants": [{"principal": "group:a@acme.com", "permissions": ["read"]}]}

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "gcp", "location": {"dataset": "pool_ds"}},
            {"platform": "bigquery", "location": {"project": "p", "dataset": "pool_ds"}},
            {
                "platform": "gcp",
                "format": "gcs_file",
                "location": {"bucket": "pool-bkt", "path": "tenant/x"},
            },
            {
                "platform": "gcp",
                "format": "iceberg",
                "location": {"warehouse": "gs://pool-bkt/wh", "path": "tenant/x"},
            },
        ],
    )
    def test_every_data_reference_is_declared(self, binding):
        plugin = get_iac_plugin("gcp")
        contract = _contract([_expose(binding)], packaging=self._SHARED, accessPolicy=self._GRANTS)
        resources = plugin.emit(contract, [])
        declared = {
            f"data.{kind}.{name}"
            for kind, bodies in plugin.emit_data(contract, []).items()
            for name in bodies
        }
        # ``${data.<kind>.<name>.<attr>}`` -> the ``data.<kind>.<name>`` address.
        referenced = {
            ".".join(token.strip("${}").split(".")[:3])
            for token in json.dumps(resources).split('"')
            if token.startswith("${data.")
        }
        assert referenced, "fixture references no pooled container — it cannot pin this"
        assert referenced <= declared, f"undeclared: {sorted(referenced - declared)}"


class TestValidateGcpBindingIsTheLoudHalf:
    def test_a_format_naming_a_container_it_omits_is_an_error(self):
        """``gcs_file`` names Cloud Storage, so a location with no bucket is
        unambiguously broken — the one case that hard-fails `fluid validate`."""
        errors, warnings = validate_gcp_binding(
            _contract(
                [_expose({"platform": "gcp", "format": "gcs_file", "location": {"project": "p"}})]
            )
        )
        assert warnings == []
        assert len(errors) == 1
        assert "orders" in errors[0]
        assert "no GCP resource" in errors[0]
        assert "no 'bucket' key" in errors[0]

    def test_an_unresolvable_generic_expose_only_warns(self):
        """`fluid validate` runs for every contract, including ones that never
        reach `generate iac`, so an ambiguous case must not stop the pipeline.
        Since #546 the empty module is itself a hard failure at the stage that
        needs the resource."""
        errors, warnings = validate_gcp_binding(
            _contract([_expose({"platform": "gcp", "location": {"project": "acme"}})])
        )
        assert errors == []
        assert len(warnings) == 1
        assert "orders" in warnings[0]
        # Names the fix in the resolver's own vocabulary.
        assert "dataset (BigQuery)" in warnings[0]
        assert "bucket (Cloud Storage)" in warnings[0]
        assert "topic (Pub/Sub)" in warnings[0]

    def test_iceberg_is_left_to_its_own_gate(self):
        """An Iceberg expose with no derivable bucket resolves to nothing, but
        ``iceberg_validation`` reports it with the specific missing
        prerequisite. Two messages for one cause would read as two problems."""
        from fluid_build.iac.iceberg_validation import validate_iceberg_bindings

        contract = _contract(
            [_expose({"platform": "gcp", "format": "iceberg", "location": {"catalog": "bigquery"}})]
        )
        assert get_iac_plugin("gcp").emit(contract, []) == {}
        assert validate_gcp_binding(contract) == ([], [])
        ice_errors, _ = validate_iceberg_bindings(contract)
        assert ice_errors, "the Iceberg gate must be the one that speaks"

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "gcp", "format": "bigquery_table", "location": {"dataset": "d"}},
            {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "b"}},
            {"platform": "gcp", "location": {"dataset": "d"}},
            # Ports with no hashicorp/google resource by design.
            {"platform": "gcp", "format": "http_api", "location": {"url": "https://x"}},
            {"platform": "gcp", "format": "grpc_api", "location": {}},
            {"platform": "gcp", "format": "kafka_topic", "location": {"topic_name": "t"}},
            {"platform": "gcp", "format": "postgres_table", "location": {"database": "d"}},
            # Not this plugin's exposure at all.
            {"platform": "snowflake", "format": "snowflake_table", "location": {}},
            {"platform": "aws", "format": "parquet", "location": {"bucket": "b"}},
        ],
    )
    def test_quiet_when_there_is_nothing_to_say(self, binding):
        assert validate_gcp_binding(_contract([_expose(binding)])) == ([], [])

    def test_the_escape_hatch_format_only_warns(self):
        errors, warnings = validate_gcp_binding(
            _contract([_expose({"platform": "gcp", "format": "other", "location": {}})])
        )
        assert errors == []
        assert len(warnings) == 1

    @pytest.mark.parametrize(
        "binding",
        [
            {"platform": "gcp", "location": {"dataset": "d"}},
            {"platform": "gcp", "location": {"bucket": "b"}},
            {"platform": "gcp", "location": {"topic": "t"}},
            {"platform": "gcp", "location": {}},
            {"platform": "gcp", "format": "gcs_file", "location": {"bucket": "b"}},
            {"platform": "gcp", "format": "csv", "location": {}},
            {"platform": "bigquery", "location": {"dataset": "d"}},
            # The cases an earlier revision of this pairing missed.
            {"platform": "gcp", "format": "gcs_file", "location": {"project": "p"}},
            {"platform": "gcp", "format": "iceberg", "location": {"warehouse": "gs://wh/d"}},
            {"platform": "gcp", "location": {"subscription": "s"}},
        ],
    )
    def test_the_gate_and_the_emitter_never_disagree(self, binding):
        """The pairing, in both directions — #475's post-mortem lesson.

        Any report (error OR warning) means the emitter produced nothing;
        silence means it produced something. The error/warning split is about
        how loud to be, not about whether there is something to say.
        """
        contract = _contract([_expose(binding)])
        errors, warnings = validate_gcp_binding(contract)
        emitted = get_iac_plugin("gcp").emit(contract, []) != {}
        assert emitted is not bool(errors + warnings)

    def test_a_contract_with_no_exposes_is_quiet(self):
        assert validate_gcp_binding({"id": "x"}) == ([], [])
        assert validate_gcp_binding({"id": "x", "exposes": [None, "junk"]}) == ([], [])


class TestTheGateIsWiredIntoFluidValidate:
    """A gate that is never called is a gate that is not there.

    Runs the real ``fluid validate`` so the wiring in ``cli/validate.py`` —
    whose ``try/except`` around every binding check swallows an import error
    into a verbose-only note — is exercised, not assumed.
    """

    _CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: analytics.demo
name: demo
metadata:
  owner:
    team: data-team
  layer: Gold
  productType: CDP
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: gcp
      format: gcs_file
      location:
        project: acme
{extra}
    contract:
      schema:
        - name: id
          type: string
"""

    def _validate(self, tmp_path, extra=""):
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(self._CONTRACT.format(extra=extra), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "validate", str(path)],
            capture_output=True,
            text=True,
        )

    def test_an_unresolvable_gcp_expose_fails_validate(self, tmp_path):
        result = self._validate(tmp_path)
        assert result.returncode != 0
        # Not coupled to the renderer's error COUNT — unrelated gate drift
        # elsewhere would break that without this gate having changed.
        assert "resolves to no GCP resource" in result.stdout

    def test_naming_the_container_makes_it_valid_and_emitting(self, tmp_path):
        result = self._validate(tmp_path, extra="        bucket: acme-raw")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Valid FLUID contract" in result.stdout


class TestAnInferredBucketIsAPrefixTenantNotAnOwner:
    """``force_destroy`` follows declared OWNERSHIP, not the dispatch route.

    ``_emit_gcs`` sets ``force_destroy: true``, which overrides GCS's refusal
    to delete a non-empty bucket. That is only safe on a bucket the product
    owns, and the contract says which case it is:

    * ``format: gcs_bucket`` names the bucket itself as the port — ownership,
      so the flag stands even alongside a ``path`` (pinned in
      ``test_iac_packaging_emit.py::test_isolated_bucket_iam_carries_no_condition``).
    * an Iceberg warehouse, or a file-ish expose resolved from the location
      shape, merely cites the container its data lives in. A declared ``path``
      then makes it one prefix tenant of a conventionally shared root, where a
      whole-bucket destroy would take another product's data with it.

    Shape inference gave ``gcs_file`` / ``csv`` / format-less bindings a NEW
    route into ``_emit_gcs``. These pin that the new route inherits the
    prefix-tenant treatment rather than silently claiming ownership.
    """

    _OWNS_BUCKET = "gcs_bucket"
    #: Routes where the bucket is cited, not claimed.
    _PREFIX_ROUTES = ["iceberg", "iceberg_table", "gcs_file", "csv", "parquet", None]

    def _bucket_body(self, location, fmt):
        binding = {"platform": "gcp", "location": dict(location)}
        if fmt is not None:
            binding["format"] = fmt
        res = get_iac_plugin("gcp").emit(_contract([_expose(binding)]), [])
        buckets = list(res.get("google_storage_bucket", {}).values())
        assert buckets, f"format={fmt!r} emitted no bucket — fixture cannot pin the flag"
        return buckets[0]

    @pytest.mark.parametrize("fmt", _PREFIX_ROUTES)
    def test_a_declared_prefix_is_never_force_destroyed(self, fmt):
        body = self._bucket_body({"bucket": "corp-lake", "path": "products/orders/"}, fmt)
        assert "force_destroy" not in body

    @pytest.mark.parametrize("fmt", _PREFIX_ROUTES + [_OWNS_BUCKET])
    def test_a_bucket_with_no_prefix_is_owned_and_keeps_the_default(self, fmt):
        assert self._bucket_body({"bucket": "acme-raw"}, fmt)["force_destroy"] is True

    @pytest.mark.parametrize("path", ["products/orders/", "/", "  ", None])
    def test_naming_the_bucket_as_the_port_always_declares_ownership(self, path):
        """``gcs_bucket`` is unaffected by this change, prefix or not.

        ``"/"`` and whitespace normalise to an empty prefix, matching the
        existing pool guard's reading of the same field.
        """
        loc = {"bucket": "acme-lake"}
        if path is not None:
            loc["path"] = path
        assert self._bucket_body(loc, self._OWNS_BUCKET)["force_destroy"] is True

    def test_every_cited_bucket_route_agrees(self):
        """The invariant: among routes that only CITE the bucket, the spelling
        must not change the blast radius."""
        for location in ({"bucket": "corp-lake", "path": "products/orders/"}, {"bucket": "raw"}):
            flags = {
                fmt: self._bucket_body(location, fmt).get("force_destroy")
                for fmt in self._PREFIX_ROUTES
            }
            assert len(set(flags.values())) == 1, f"format decided force_destroy: {flags}"
