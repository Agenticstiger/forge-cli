# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Masked columns, row filters and governance labels, on GCP and on AWS.

``policy.authz.columnRestrictions[]`` with ``access: mask`` lets a principal read a
column masked by the platform; ``policy.authz.rowFilters[]`` lets it read only the
rows a predicate selects; the contract's labels, classification and sovereignty land
on the resources as labels (GCP) and tags or parameters (AWS). These pin what each
emitter writes, and what is refused rather than dropped.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.column_access import column_masks, restrictions_for
from fluid_build.iac.governance_labels import governance_labels
from fluid_build.iac.principals import aws_principal_account, aws_principal_key
from fluid_build.iac.row_access import row_filters_for

ANALYSTS = "group:analysts@northwind.example"
STEWARDS = "group:data-stewards@northwind.example"
PIPELINE = "serviceAccount:fluid-pipeline@northwind.example"
GCP_MAP = {
    ANALYSTS: "serviceAccount:analyst@northwind-demo.iam.gserviceaccount.com",
    STEWARDS: "serviceAccount:steward@northwind-demo.iam.gserviceaccount.com",
    PIPELINE: "serviceAccount:deploy@northwind-demo.iam.gserviceaccount.com",
}
ACCOUNT = "123456789012"
ANALYST_ARN = f"arn:aws:iam::{ACCOUNT}:role/analyst"
STEWARD_ARN = f"arn:aws:iam::{ACCOUNT}:role/steward"
DEFINER_ARN = f"arn:aws:iam::{ACCOUNT}:role/view-definer"
AWS_MAP = {
    ANALYSTS: ANALYST_ARN,
    STEWARDS: STEWARD_ARN,
    PIPELINE: f"arn:aws:iam::{ACCOUNT}:role/box",
}

DENY_ID = {"principal": ANALYSTS, "columns": ["customer_id"], "access": "deny"}
MASK_MSISDN = {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask", "mask": "last_four"}
CONSENT = {
    "principal": ANALYSTS,
    "name": "analysts_consented",
    "where": "consent = true AND (suppressed IS NULL OR suppressed = false)",
}
SCHEMA = [
    {"name": "customer_id", "type": "string"},
    {"name": "msisdn", "type": "string"},
    {"name": "msisdn_hash", "type": "string"},
    {"name": "consent", "type": "boolean"},
    {"name": "suppressed", "type": "boolean"},
    {"name": "score", "type": "integer"},
]


def _expose(
    binding: Dict[str, Any],
    restrictions: Optional[List[Dict[str, Any]]] = None,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    authz: Dict[str, Any] = {}
    if restrictions is not None:
        authz["columnRestrictions"] = restrictions
    if row_filters is not None:
        authz["rowFilters"] = row_filters
    expose: Dict[str, Any] = {
        "exposeId": "profile",
        "binding": binding,
        "labels": {"layer": "silver"},
        "contract": {"schema": copy.deepcopy(SCHEMA)},
        "policy": {"classification": "Confidential", "authz": authz},
    }
    return expose


def _contract(expose: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "id": "silver.profile",
        "labels": {"gdpr-article": "art6-1b-contractual", "Cost Center": "NW CX 001"},
        "sovereignty": {
            "jurisdiction": "EU",
            "regulatoryFramework": ["GDPR"],
            "dataResidency": True,
        },
        "accessPolicy": {
            "grants": [
                {"principal": STEWARDS, "permissions": ["read", "select", "query"]},
                {"principal": ANALYSTS, "permissions": ["read", "select", "query"]},
                {"principal": PIPELINE, "permissions": ["read", "write", "insert"]},
            ]
        },
        "exposes": [expose],
    }


def _gcp(restrictions=None, row_filters=None) -> Dict[str, Any]:
    binding = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {
            "project": "northwind-demo",
            "dataset": "silver",
            "table": "profile",
            "region": "europe-west3",
        },
        "principals": GCP_MAP,
    }
    return _contract(_expose(binding, restrictions, row_filters))


def _aws(restrictions=None, row_filters=None, masked_views=True) -> Dict[str, Any]:
    lake_formation: Dict[str, Any] = {
        "registerLocation": True,
        "grants": [
            {"principal": STEWARD_ARN, "permissions": ["SELECT", "DESCRIBE"]},
            {"principal": ANALYST_ARN, "permissions": ["SELECT", "DESCRIBE"]},
        ],
    }
    if masked_views:
        lake_formation["maskedViews"] = {
            "definer": DEFINER_ARN,
            "validationConnection": "views-conn",
        }
    binding = {
        "platform": "aws",
        "format": "parquet",
        "location": {
            "database": "silver",
            "table": "profile",
            "bucket": "nw-silver",
            "path": "silver/profile/",
        },
        "principals": AWS_MAP,
        "governance": {"lakeFormation": lake_formation},
    }
    return _contract(_expose(binding, restrictions, row_filters))


def _gcp_resources(contract: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(get_iac_plugin("gcp"), contract)).get("resource") or {}


def _aws_resources(contract: Dict[str, Any]) -> Dict[str, Any]:
    return get_iac_plugin("aws").emit(contract)


def _refusal(cloud: str, contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        if cloud == "gcp":
            build_module(get_iac_plugin("gcp"), contract)
        else:
            get_iac_plugin("aws").emit(contract)
    return raised.value


# ── the shared derivations ───────────────────────────────────────────────


class TestTheSharedDerivations:
    def test_a_mask_needs_a_rule_and_only_a_mask_takes_one(self):
        for bad, fragment in [
            ({"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask"}, "needs a mask rule"),
            ({**MASK_MSISDN, "mask": "rot13"}, "needs a mask rule"),
            ({**DENY_ID, "mask": "last_four"}, "masks nothing"),
        ]:
            with pytest.raises(UnsupportedBindingError) as raised:
                restrictions_for(_expose({}, [bad]))
            assert fragment in str(raised.value)

    def test_a_column_has_one_masking_rule(self):
        two = [MASK_MSISDN, {**MASK_MSISDN, "principal": STEWARDS, "mask": "first_four"}]
        with pytest.raises(UnsupportedBindingError) as raised:
            restrictions_for(_expose({}, two))
        assert "one masking rule" in str(raised.value)

    def test_a_deny_beats_a_mask_and_a_mask_never_grants(self):
        expose = _expose({}, [MASK_MSISDN, {**DENY_ID, "columns": ["msisdn"]}])
        restrictions = restrictions_for(expose)
        masks = column_masks(
            expose, restrictions, lambda p: (p,), frozenset({ANALYSTS, STEWARDS}), where="t"
        )
        assert masks == {}
        masks = column_masks(
            _expose({}, [MASK_MSISDN]),
            restrictions_for(_expose({}, [MASK_MSISDN])),
            lambda p: (p,),
            frozenset({STEWARDS}),
            where="t",
        )
        assert masks == {}, "a masked principal that is not a reader reads nothing"

    @pytest.mark.parametrize(
        "where, fragment",
        [
            ("consent = true; DROP TABLE x", "statement separator"),
            ("consent = true -- x", "statement separator"),
            ("nope = 1", "does not declare"),
            ("consent IN (SELECT 1)", "one boolean expression"),
            ("consent =", "does not parse"),
        ],
    )
    def test_a_row_filter_is_one_boolean_expression_over_the_schema(self, where, fragment):
        with pytest.raises(UnsupportedBindingError) as raised:
            row_filters_for(_expose({}, row_filters=[{"principal": ANALYSTS, "where": where}]))
        assert fragment in str(raised.value)

    def test_a_principal_has_one_row_filter(self):
        with pytest.raises(UnsupportedBindingError) as raised:
            row_filters_for(_expose({}, row_filters=[CONSENT, {**CONSENT, "name": "other"}]))
        assert "a second time" in str(raised.value)

    def test_governance_labels_are_the_contracts_in_the_gcp_label_alphabet(self):
        contract = _gcp()
        labels = governance_labels(contract, contract["exposes"][0])
        assert labels["gdpr-article"] == "art6-1b-contractual"
        assert labels["cost-center"] == "nw-cx-001"
        assert labels["fluid_classification"] == "confidential"
        assert labels["fluid_jurisdiction"] == "eu"
        assert labels["fluid_regulation"] == "gdpr"
        assert labels["fluid_residency"] == "true"
        assert labels["layer"] == "silver"

    def test_governance_labels_keep_sixty_unless_the_caller_lifts_the_cap(self):
        contract = {"labels": {f"k{i:03d}": "v" for i in range(75)}}
        assert len(governance_labels(contract, {})) == 60
        assert len(governance_labels(contract, {}, limit=60)) == 60
        assert len(governance_labels(contract, {}, limit=None)) == 75
        assert list(governance_labels(contract, {}, limit=2)) == ["k000", "k001"]


# ── GCP ──────────────────────────────────────────────────────────────────


class TestGcp:
    def test_a_masked_column_gets_its_own_tag_a_data_policy_and_masked_readers(self):
        res = _gcp_resources(_gcp([DENY_ID, MASK_MSISDN]))
        tags = res["google_data_catalog_policy_tag"]
        assert len(tags) == 2, "the masked column never shares a tag with a denied one"
        (policy,) = res["google_bigquery_datapolicy_data_policy"].values()
        assert policy["data_policy_type"] == "DATA_MASKING_POLICY"
        assert policy["data_masking_policy"] == {"predefined_expression": "LAST_FOUR_CHARACTERS"}
        assert policy["location"] == "europe-west3"
        masked_tag = next(k for k, t in tags.items() if t["display_name"].endswith("msisdn"))
        assert masked_tag in policy["policy_tag"]
        members = {
            (m["role"], m["member"])
            for m in res["google_bigquery_datapolicy_data_policy_iam_member"].values()
        }
        assert members == {("roles/bigquerydatapolicy.maskedReader", GCP_MAP[ANALYSTS])}
        raw = {
            m["member"]
            for m in res["google_data_catalog_policy_tag_iam_member"].values()
            if masked_tag in m["policy_tag"]
        }
        assert GCP_MAP[ANALYSTS] not in raw, "a masked principal never reads the raw column"
        assert GCP_MAP[STEWARDS] in raw

    def test_a_data_policy_id_stays_under_bigquery_s_limit(self):
        """BigQuery refuses a data policy id of 200 characters or more (measured: 300 refused)."""
        import dataclasses

        from fluid_build.iac.providers import gcp_governance as gov

        short = gov.TagGroup(
            key="silver_customer_customer_profile_360_tga_silver_customer_profile_360_x",
            display_name="msisdn",
            columns=("msisdn",),
            readers=(),
            mask="last_four",
        )
        assert short.data_policy_id == f"{short.key}_mask", "a short id is unchanged"
        long_a = dataclasses.replace(short, key="p" * 250 + "_a")
        long_b = dataclasses.replace(short, key="p" * 250 + "_b")
        assert len(long_a.data_policy_id) <= gov.DATA_POLICY_ID_MAX
        assert long_a.data_policy_id != long_b.data_policy_id, "two long ids stay distinct"
        assert long_a.data_policy_id.replace("_", "").isalnum()

    def test_the_hashed_column_stays_readable(self):
        res = _gcp_resources(_gcp([DENY_ID, MASK_MSISDN]))
        table = next(iter(res["google_bigquery_table"].values()))
        schema = json.loads(table["schema"])
        tagged = {f["name"] for f in schema if f.get("policyTags")}
        assert tagged == {"customer_id", "msisdn"}

    def test_each_row_filter_is_a_row_access_policy_and_everyone_else_reads_every_row(self):
        res = _gcp_resources(_gcp([DENY_ID], [CONSENT]))
        policies = {p["policy_id"]: p for p in res["google_bigquery_row_access_policy"].values()}
        assert set(policies) == {"analysts_consented", "fluid_all_rows"}
        assert policies["analysts_consented"]["filter_predicate"] == CONSENT["where"]
        assert policies["analysts_consented"]["grantees"] == [GCP_MAP[ANALYSTS]]
        assert policies["fluid_all_rows"]["filter_predicate"] == "TRUE"
        assert set(policies["fluid_all_rows"]["grantees"]) == {GCP_MAP[STEWARDS], GCP_MAP[PIPELINE]}

    def test_a_write_only_filtered_principal_reads_only_its_rows(self):
        """A writer reads rows too (roles/bigquery.dataEditor): left out of its own filter it
        landed in fluid_all_rows, and BigQuery would have shown it every row."""
        contract = _gcp(row_filters=[{"principal": PIPELINE, "where": "consent = true"}])
        contract["accessPolicy"]["grants"][2]["permissions"] = ["write", "insert"]
        res = _gcp_resources(contract)
        policies = {p["policy_id"]: p for p in res["google_bigquery_row_access_policy"].values()}
        assert policies["fluid_pipeline_rows"]["grantees"] == [GCP_MAP[PIPELINE]]
        assert GCP_MAP[PIPELINE] not in policies["fluid_all_rows"]["grantees"]

    def test_a_filtered_principal_with_no_grant_is_still_filtered(self):
        """Granted elsewhere (a project role), it reads only the rows its filter selects."""
        contract = _gcp(row_filters=[CONSENT])
        grants = contract["accessPolicy"]["grants"]
        contract["accessPolicy"]["grants"] = [g for g in grants if g["principal"] != ANALYSTS]
        res = _gcp_resources(contract)
        policies = {p["policy_id"]: p for p in res["google_bigquery_row_access_policy"].values()}
        assert policies["analysts_consented"]["grantees"] == [GCP_MAP[ANALYSTS]]
        assert GCP_MAP[ANALYSTS] not in policies["fluid_all_rows"]["grantees"]

    def test_a_row_filter_on_a_view_is_refused_not_dropped(self):
        contract = _gcp(row_filters=[CONSENT])
        contract["exposes"][0]["binding"]["format"] = "bigquery_view"
        with pytest.raises(UnsupportedBindingError) as raised:
            _gcp_resources(contract)
        assert raised.value.kind == "row-filter-view"

    def test_a_row_filter_on_a_gcs_expose_is_refused_not_dropped(self):
        contract = _gcp(row_filters=[CONSENT])
        binding = contract["exposes"][0]["binding"]
        binding.update(format="gcs_bucket", location={"bucket": "lake"})
        with pytest.raises(UnsupportedBindingError) as raised:
            _gcp_resources(contract)
        assert raised.value.kind == "gcp-governance-unsupported-target"
        assert "policy.authz.rowFilters" in str(raised.value)

    def test_the_contracts_governance_labels_reach_dataset_table_and_key(self):
        res = _gcp_resources(_gcp())
        for kind in ("google_bigquery_dataset", "google_bigquery_table"):
            labels = next(iter(res[kind].values()))["labels"]
            assert labels["fluid_regulation"] == "gdpr"
            assert labels["managed_by"] == "fluid", "fluid's own labels win"

    def test_no_mask_no_filter_emits_none_of_the_new_resources(self):
        res = _gcp_resources(_gcp([DENY_ID]))
        assert "google_bigquery_datapolicy_data_policy" not in res
        assert "google_bigquery_row_access_policy" not in res


# ── AWS ──────────────────────────────────────────────────────────────────


def _grant(res: Dict[str, Any], principal: str) -> Dict[str, Any]:
    (grant,) = [
        g
        for g in res["aws_lakeformation_permissions"].values()
        if g["principal"] == principal
        and ("table_with_columns" in g or "data_cells_filter" in g or "table" in g)
        and not str(g.get("table", [{}])[0].get("name", "")).count("masked")
    ]
    return grant


class TestAws:
    def test_a_filtered_principal_reads_through_a_data_cells_filter(self):
        res = _aws_resources(_aws([DENY_ID], [CONSENT]))
        (flt,) = res["aws_lakeformation_data_cells_filter"].values()
        data = flt["table_data"][0]
        assert data["name"] == "analysts_consented"
        assert data["row_filter"] == [{"filter_expression": CONSENT["where"]}]
        assert data["column_wildcard"] == [{"excluded_column_names": ["customer_id"]}]
        grant = _grant(res, ANALYST_ARN)
        assert grant["permissions"] == ["SELECT"]
        assert grant["data_cells_filter"][0]["name"] == "analysts_consented"
        assert "table_with_columns" not in grant and "table" not in grant
        assert "table" in _grant(res, STEWARD_ARN), "an unfiltered reader keeps its table grant"

    def test_a_masked_principal_reads_a_protected_view_and_never_the_raw_column(self):
        res = _aws_resources(_aws([DENY_ID, MASK_MSISDN], [CONSENT]))
        views = [
            t
            for t in res["aws_glue_catalog_table"].values()
            if t.get("table_type") == "VIRTUAL_VIEW"
        ]
        assert len(views) == 1
        view = views[0]
        assert view["name"] == "profile_masked"
        vd = view["view_definition"][0]
        assert vd["definer"] == DEFINER_ARN and vd["is_protected"] is True
        rep = vd["representations"][0]
        assert rep["dialect"] == "ATHENA" and rep["validation_connection"] == "views-conn"
        sql = rep["view_original_text"]
        assert 'concat(\'XXXXX\', substr("msisdn", -4)) END AS "msisdn"' in sql
        assert '"customer_id"' not in sql, "a denied column is not in the view"
        assert '"msisdn_hash"' in sql, "the hashed column is"
        assert sql.endswith(
            f"WHERE {CONSENT['where']}"
        ), "the view applies the principal's row filter"
        grants = res["aws_lakeformation_permissions"].values()
        definer = [g for g in grants if g["principal"] == DEFINER_ARN]
        assert any(
            g.get("permissions_with_grant_option") == ["SELECT"] and "table" in g for g in definer
        )
        # Lake Formation's view prerequisites: CREATE_TABLE on the view's database.
        assert any(
            g["permissions"] == ["CREATE_TABLE", "DESCRIBE"] and "database" in g for g in definer
        )
        view_grant = [
            g for g in grants if g["principal"] == ANALYST_ARN and "masked" in str(g.get("table"))
        ]
        assert view_grant and view_grant[0]["permissions"] == ["SELECT", "DESCRIBE"]
        flt = next(iter(res["aws_lakeformation_data_cells_filter"].values()))["table_data"][0]
        assert "msisdn" in flt["column_wildcard"][0]["excluded_column_names"]

    def test_a_masked_view_selects_only_the_columns_its_readers_grant_lists(self):
        """The view reads the table as its definer: a reader whose grant lists its columns
        read every other column raw through it, a denied one included."""
        deny_score = {"principal": ANALYSTS, "columns": ["score"], "access": "deny"}
        contract = _aws([MASK_MSISDN, deny_score])
        grant = contract["exposes"][0]["binding"]["governance"]["lakeFormation"]["grants"][1]
        grant.update(permissions=["SELECT"], columns=["consent", "suppressed"])
        res = _aws_resources(contract)
        (view,) = [
            t
            for t in res["aws_glue_catalog_table"].values()
            if t.get("table_type") == "VIRTUAL_VIEW"
        ]
        sql = view["view_definition"][0]["representations"][0]["view_original_text"]
        assert '"consent"' in sql and '"suppressed"' in sql
        assert "concat('XXXXX', substr(\"msisdn\", -4))" in sql
        for raw in ("customer_id", "msisdn_hash", "score"):
            assert f'"{raw}"' not in sql, raw

    def test_a_masked_view_never_selects_a_column_the_grant_excludes(self):
        res = _aws_resources(_aws([MASK_MSISDN, DENY_ID]))
        (view,) = [
            t
            for t in res["aws_glue_catalog_table"].values()
            if t.get("table_type") == "VIRTUAL_VIEW"
        ]
        sql = view["view_definition"][0]["representations"][0]["view_original_text"]
        assert '"customer_id"' not in sql and '"msisdn_hash"' in sql

    def test_a_mask_without_a_view_definer_is_refused(self):
        error = _refusal("aws", _aws([MASK_MSISDN], masked_views=False))
        assert error.kind == "column-mask-unenforceable"

    def test_a_text_mask_on_a_non_string_column_is_refused(self):
        bad = {"principal": ANALYSTS, "columns": ["score"], "access": "mask", "mask": "last_four"}
        assert _refusal("aws", _aws([bad])).kind == "column-mask-type"

    def test_the_contracts_governance_labels_reach_bucket_and_table(self):
        res = _aws_resources(_aws())
        bucket = next(iter(res["aws_s3_bucket"].values()))
        assert bucket["tags"]["fluid_regulation"] == "gdpr"
        table = next(
            t
            for t in res["aws_glue_catalog_table"].values()
            if t.get("table_type") != "VIRTUAL_VIEW"
        )
        assert table["parameters"]["gdpr-article"] == "art6-1b-contractual"
        assert table["parameters"]["classification"] == "parquet", "the format parameter is kept"


# ── the schema ───────────────────────────────────────────────────────────


class TestSchema:
    def _validate(self, contract: Dict[str, Any]) -> List[str]:
        import jsonschema

        import fluid_build

        path = Path(fluid_build.__file__).parent / "schemas" / "fluid-schema-0.7.6.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        validator = jsonschema.validators.validator_for(schema)(schema)
        return [e.message for e in validator.iter_errors(contract)]

    def test_mask_row_filters_and_masked_views_are_valid_076(self):
        errors = self._validate(_aws([DENY_ID, MASK_MSISDN], [CONSENT]))
        assert not [e for e in errors if "mask" in e or "rowFilters" in e or "maskedViews" in e]

    def test_a_mask_without_its_rule_is_refused_by_the_schema(self):
        bad = {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask"}
        errors = self._validate(_aws([bad]))
        assert any("mask" in e for e in errors)


# ── fluid verify: a grant on a data cells filter ─────────────────────────


class _FakeLf:
    """Lake Formation as ListPermissions and GetDataCellsFilter answer it."""

    def __init__(self, excluded: Optional[List[str]], readable: bool = True):
        self.excluded, self.readable = excluded, readable

    def list_permissions(self, **kwargs):
        table = {"Table": {"DatabaseName": "silver", "Name": "profile"}}
        flt = {
            "DataCellsFilter": {
                "TableCatalogId": ACCOUNT,
                "DatabaseName": "silver",
                "TableName": "profile",
                "Name": "analysts_consented",
            }
        }
        steward = {
            "Principal": {"DataLakePrincipalIdentifier": STEWARD_ARN},
            "Resource": table,
            "Permissions": ["SELECT"],
        }
        analyst = {
            "Principal": {"DataLakePrincipalIdentifier": ANALYST_ARN},
            "Resource": flt,
            "Permissions": ["SELECT"],
        }
        if kwargs.get("ResourceType") == "TABLE":
            return {"PrincipalResourcePermissions": [steward]}
        return {"PrincipalResourcePermissions": [steward, analyst]}

    def get_data_cells_filter(self, **kwargs):
        if not self.readable:
            raise RuntimeError("AccessDenied")
        return {"DataCellsFilter": {"ColumnWildcard": {"ExcludedColumnNames": self.excluded}}}


class TestVerifyDataCellsFilters:
    def _check(self, lf: _FakeLf) -> Dict[str, Any]:
        from fluid_build.cli._verify_lf_columns import column_restrictions_dimension

        contract = _aws([DENY_ID, MASK_MSISDN], [CONSENT])
        expose = contract["exposes"][0]
        return column_restrictions_dimension(
            "profile",
            expose,
            expose["binding"],
            region="eu-north-1",
            factory=lambda svc, region: lf,
        )

    def test_a_filter_that_excludes_the_restricted_columns_passes(self):
        result = self._check(_FakeLf(["customer_id", "msisdn"]))
        assert result["status"] == "pass", result

    def test_a_filter_that_leaves_a_masked_column_readable_fails(self):
        result = self._check(_FakeLf(["customer_id"]))
        assert result["status"] == "fail" and "msisdn" in result["message"]

    def test_a_filter_it_cannot_read_fails_closed(self):
        result = self._check(_FakeLf(["customer_id", "msisdn"], readable=False))
        assert result["status"] == "fail"


# ── AWS: a grant is matched to the policy by the principal it names, however spelt ──


def _aws_grants(grants: List[Dict[str, Any]], restrictions=None, row_filters=None):
    contract = _aws(restrictions, row_filters)
    binding = contract["exposes"][0]["binding"]
    binding["governance"]["lakeFormation"]["grants"] = grants
    return contract


STEWARD_GRANT = {"principal": STEWARD_ARN, "permissions": ["SELECT", "DESCRIBE"]}
#: The analyst role, written as IAM would also accept it: with its path, or in other case.
ANALYST_SPELLINGS = [
    f"arn:aws:iam::{ACCOUNT}:role/data/analysts/analyst",
    f"arn:aws:iam::{ACCOUNT}:role/Analyst",
    f"arn:aws:iam::{ACCOUNT}:role/ANALYST",
]


def _table_reads(res: Dict[str, Any], principal: str) -> List[Dict[str, Any]]:
    """The grants that let ``principal`` read the table itself (not a masked view), every row."""
    return [
        g
        for g in res.get("aws_lakeformation_permissions", {}).values()
        if g["principal"] == principal
        and ("table" in g or "table_with_columns" in g)
        and "masked" not in str(g.get("table", [{}])[0].get("name", ""))
        and {"SELECT", "ALL"}
        & {*g.get("permissions", ()), *g.get("permissions_with_grant_option", ())}
    ]


class TestAwsGrantPrincipalSpelling:
    """eDKBar8G: a read grant written differently from the policy's principal."""

    @pytest.mark.parametrize(
        "spelling, key",
        [
            *[(s, ANALYST_ARN) for s in ANALYST_SPELLINGS],
            (f" {ANALYST_ARN} ", ANALYST_ARN),
            ("arn:aws-cn:iam::123456789012:user/a/B", "arn:aws-cn:iam::123456789012:user/b"),
            (f"arn:aws:iam::{ACCOUNT}:root", ACCOUNT),
            ("IAM_ALLOWED_PRINCIPALS", "IAM_ALLOWED_PRINCIPALS"),
            (
                f"arn:aws:iam::{ACCOUNT}:saml-provider/idp",
                f"arn:aws:iam::{ACCOUNT}:saml-provider/idp",
            ),
        ],
    )
    def test_one_key_per_role_or_user_whatever_its_path_or_case(self, spelling, key):
        assert aws_principal_key(spelling) == key

    def test_roles_in_two_accounts_or_of_two_kinds_have_two_keys(self):
        assert aws_principal_key(ANALYST_ARN) != aws_principal_key(
            "arn:aws:iam::210987654321:role/analyst"
        )
        assert aws_principal_key(ANALYST_ARN) != aws_principal_key(
            f"arn:aws:iam::{ACCOUNT}:user/analyst"
        )
        assert aws_principal_account(ANALYST_SPELLINGS[0]) == ACCOUNT
        assert aws_principal_account("IAM_ALLOWED_PRINCIPALS") is None

    @pytest.mark.parametrize("spelling", ANALYST_SPELLINGS)
    def test_a_grant_to_the_filtered_role_in_another_spelling_reads_through_the_filter(
        self, spelling
    ):
        grant = {"principal": spelling, "permissions": ["SELECT", "DESCRIBE"]}
        res = _aws_resources(_aws_grants([STEWARD_GRANT, grant], row_filters=[CONSENT]))
        assert _table_reads(res, spelling) == [], "the filtered role reads every row"
        (filtered,) = [
            g for g in res["aws_lakeformation_permissions"].values() if "data_cells_filter" in g
        ]
        assert filtered["principal"] == spelling
        assert filtered["data_cells_filter"][0]["name"] == "analysts_consented"

    def test_the_mapped_identity_in_another_spelling_still_filters_the_plain_grant(self):
        contract = _aws(row_filters=[CONSENT])
        contract["exposes"][0]["binding"]["principals"][ANALYSTS] = ANALYST_SPELLINGS[0]
        res = _aws_resources(contract)
        assert _table_reads(res, ANALYST_ARN) == []

    def test_a_select_held_only_with_the_grant_option_is_a_read_and_is_refused(self):
        grant = {
            "principal": ANALYST_ARN,
            "permissions": ["DESCRIBE"],
            "permissionsWithGrantOption": ["SELECT"],
        }
        err = _refusal("aws", _aws_grants([STEWARD_GRANT, grant], row_filters=[CONSENT]))
        assert err.kind == "row-filter-unenforceable"
        assert "permissionsWithGrantOption" in str(err)

    def test_a_second_grant_option_select_beside_the_filtered_grant_is_refused(self):
        plain = {"principal": ANALYST_ARN, "permissions": ["SELECT"]}
        regrant = {
            "principal": ANALYST_SPELLINGS[0],
            "permissions": ["DESCRIBE"],
            "permissionsWithGrantOption": ["SELECT"],
        }
        err = _refusal("aws", _aws_grants([plain, regrant], row_filters=[CONSENT]))
        assert err.kind == "row-filter-unenforceable"

    @pytest.mark.parametrize(
        "everyone",
        [
            "IAM_ALLOWED_PRINCIPALS",
            ACCOUNT,
            f"arn:aws:iam::{ACCOUNT}:root",
            "arn:aws:organizations::111122223333:organization/o-abcdefghij",
            "arn:aws:organizations::111122223333:ou/o-abcdefghij/ou-ab12-cdef3456",
        ],
    )
    def test_a_read_grant_to_a_set_that_may_hold_the_filtered_role_is_refused(self, everyone):
        grants = [
            {"principal": ANALYST_ARN, "permissions": ["SELECT"]},
            {"principal": everyone, "permissions": ["SELECT"]},
        ]
        err = _refusal("aws", _aws_grants(grants, row_filters=[CONSENT]))
        assert err.kind == "row-filter-unenforceable"

    def test_a_read_grant_to_another_account_is_not_the_filtered_role(self):
        grants = [
            {"principal": ANALYST_ARN, "permissions": ["SELECT"]},
            {"principal": "210987654321", "permissions": ["SELECT"]},
        ]
        res = _aws_resources(_aws_grants(grants, row_filters=[CONSENT]))
        assert _table_reads(res, ANALYST_ARN) == []
        assert len(_table_reads(res, "210987654321")) == 1

    def test_two_filters_on_one_role_are_refused(self):
        contract = _aws(row_filters=[CONSENT, {"principal": STEWARDS, "where": "score > 1"}])
        contract["exposes"][0]["binding"]["principals"][STEWARDS] = ANALYST_SPELLINGS[1]
        err = _refusal("aws", contract)
        assert err.kind == "row-filter-unenforceable"

    @pytest.mark.parametrize("spelling", ANALYST_SPELLINGS[:2])
    def test_a_denied_column_is_excluded_from_a_grant_in_another_spelling(self, spelling):
        grant = {"principal": spelling, "permissions": ["SELECT"]}
        res = _aws_resources(_aws_grants([STEWARD_GRANT, grant], restrictions=[DENY_ID]))
        assert _table_reads(res, STEWARD_ARN)[0].get("table"), "the steward reads every column"
        (analyst,) = _table_reads(res, spelling)
        assert analyst["table_with_columns"][0]["excluded_column_names"] == ["customer_id"]

    def test_a_denied_column_under_a_grant_option_select_is_never_granted(self):
        grant = {
            "principal": ANALYST_ARN,
            "permissions": ["DESCRIBE"],
            "permissionsWithGrantOption": ["SELECT"],
        }
        contract = _aws_grants([STEWARD_GRANT, grant], restrictions=[DENY_ID])
        with pytest.raises(UnsupportedBindingError):
            res = _aws_resources(contract)
            # Emitted at all, the analyst's grant must not reach the denied column.
            for g in _table_reads(res, ANALYST_ARN):
                assert "table" not in g

    def test_a_masked_role_in_another_spelling_reads_the_view(self):
        grant = {"principal": ANALYST_SPELLINGS[0], "permissions": ["SELECT", "DESCRIBE"]}
        contract = _aws_grants([STEWARD_GRANT, grant], restrictions=[MASK_MSISDN])
        res = _aws_resources(contract)
        (analyst,) = _table_reads(res, ANALYST_SPELLINGS[0])
        assert "msisdn" in analyst["table_with_columns"][0]["excluded_column_names"]
        views = [t for t in res.get("aws_glue_catalog_table", {}).values() if "masked" in t["name"]]
        assert len(views) == 1

    def test_a_describe_grant_in_another_spelling_gets_no_view_of_its_own(self):
        """Security review: a view is a read, run as its definer; a DESCRIBE grant must
        not get one without the read grant's exclusions and row filter."""
        grants = [
            {"principal": ANALYST_ARN, "permissions": ["SELECT"]},
            {"principal": f"arn:aws:iam::{ACCOUNT}:role/data/Analyst", "permissions": ["DESCRIBE"]},
        ]
        contract = _aws_grants(grants, restrictions=[DENY_ID, MASK_MSISDN], row_filters=[CONSENT])
        with pytest.raises(UnsupportedBindingError) as raised:
            _aws_resources(contract)
        assert raised.value.kind == "lakeformation-grant-columns"

    def test_a_describe_grant_beside_a_masked_read_adds_no_second_view(self):
        grants = [
            {"principal": ANALYST_ARN, "permissions": ["SELECT"]},
            {"principal": STEWARD_ARN, "permissions": ["DESCRIBE"]},
        ]
        contract = _aws_grants(grants, restrictions=[DENY_ID, MASK_MSISDN], row_filters=[CONSENT])
        contract["exposes"][0]["binding"]["principals"][STEWARDS] = ANALYST_ARN
        res = _aws_resources(contract)
        views = [t for t in res.get("aws_glue_catalog_table", {}).values() if "masked" in t["name"]]
        assert len(views) == 1
        sql = views[0]["view_original_text"] if "view_original_text" in views[0] else str(views[0])
        assert "WHERE" in sql and '"customer_id"' not in sql
