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

"""Pin the name-based PII pre-classifier wiring in
``LogicalAgent._translate_catalog_table``.

RETEST-6 confirmed that catalog-driven forges (Snowflake / BigQuery /
DataHub / Glue) were emitting contracts with 0/309 columns carrying
PII metadata despite the ``apply_pii_tags`` classifier being available.
RETEST-9 confirmed that ``_translate_catalog_table`` is the single
chokepoint every catalog adapter funnels through, so wiring the
classifier there benefits all four adapters at once.

These tests pin:

* obvious PII column names → classified + stamped onto qualifiers,
* prefixed PII column names (``c_email``) → classified,
* non-PII columns (``id``, ``created_at``) → no PII tag,
* ``FLUID_COPILOT_PII_CLASSIFIER=0`` kill switch disables the pass,
* the classifier is idempotent — a second call doesn't double-tag,
* catalog-already-declared PII (via ``CatalogColumn.sensitivity_tags``)
  is respected and not overwritten,
* the four real catalog adapters (Glue / Snowflake / BigQuery /
  DataHub) all benefit — driven by the same shared mock CatalogTable.
"""

from __future__ import annotations

from fluid_build.copilot.agents.logical_agent import _translate_catalog_table
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogTable,
    SensitivityTag,
)


def _build_catalog_table_with_pii_columns(
    *,
    fqn: str = "sales.individuals",
    name: str = "sat_individual_details",
) -> CatalogTable:
    """Mirror the RETEST-6 Snowflake satellite shape — a real-world
    catalog table where the classifier should fire on EMAIL but not
    on id / created_at / load_date."""
    return CatalogTable(
        fqn=fqn,
        database="HUB_DB",
        name=name,
        description="Satellite holding individual PII attributes.",
        columns=[
            # Obvious PII — both bare and prefixed forms.
            CatalogColumn(name="email", data_type="VARCHAR(255)"),
            CatalogColumn(name="c_email", data_type="VARCHAR(255)"),
            CatalogColumn(name="phone_number", data_type="VARCHAR(32)"),
            CatalogColumn(name="ssn", data_type="VARCHAR(11)"),
            CatalogColumn(name="date_of_birth", data_type="DATE"),
            # Plain non-PII.
            CatalogColumn(
                name="id",
                data_type="NUMBER(38)",
                primary_key=True,
            ),
            CatalogColumn(name="created_at", data_type="TIMESTAMP_LTZ"),
            CatalogColumn(name="load_date", data_type="TIMESTAMP_LTZ"),
        ],
        primary_key_columns=["id"],
    )


# ---------------------------------------------------------------------
# Positive paths — obvious PII columns get classified + stamped
# ---------------------------------------------------------------------


class TestPiiClassifierWiredIntoTranslate:
    def test_email_column_is_tagged(self):
        """Bare ``email`` column gets ``pii-email`` + sensitivity ``pii``."""
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "email")
        assert "pii-email" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"
        assert col.qualifiers["pii_semantic_type"] == "email"

    def test_prefixed_email_column_is_tagged(self):
        """``c_email`` (Data Vault satellite-style prefix) classifies too."""
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "c_email")
        assert "pii-email" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"
        assert col.qualifiers["pii_semantic_type"] == "email"

    def test_phone_column_is_tagged(self):
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "phone_number")
        assert "pii-phone" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"

    def test_ssn_column_is_tagged(self):
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "ssn")
        assert "pii-ssn" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"

    def test_dob_column_is_tagged(self):
        """``date_of_birth`` → classified ``dob``."""
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "date_of_birth")
        assert "pii-dob" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"


# ---------------------------------------------------------------------
# Negative paths — non-PII columns get NO PII tag
# ---------------------------------------------------------------------


class TestPiiClassifierLeavesNonPiiAlone:
    def test_id_column_has_no_pii_tag(self):
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "id")
        assert "pii_tags" not in col.qualifiers
        assert "pii_sensitivity" not in col.qualifiers
        assert "pii_semantic_type" not in col.qualifiers

    def test_created_at_column_has_no_pii_tag(self):
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "created_at")
        assert "pii_tags" not in col.qualifiers

    def test_load_date_column_has_no_pii_tag(self):
        """Audit columns (``load_date``) must not trigger any PII class —
        they include the ``date`` token but not ``birth_date`` / ``dob``."""
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        col = next(c for c in table_def.columns if c.name == "load_date")
        assert "pii_tags" not in col.qualifiers


# ---------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------


class TestKillSwitchSuppressesClassifier:
    def test_kill_switch_zero_disables_classifier(self, monkeypatch):
        """``FLUID_COPILOT_PII_CLASSIFIER=0`` → no PII tags emitted."""
        monkeypatch.setenv("FLUID_COPILOT_PII_CLASSIFIER", "0")
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        for col in table_def.columns:
            assert "pii_tags" not in col.qualifiers
            assert "pii_sensitivity" not in col.qualifiers
            assert "pii_semantic_type" not in col.qualifiers

    def test_kill_switch_false_disables_classifier(self, monkeypatch):
        """Alternative kill-switch values (``false`` / ``no`` / ``off``)."""
        monkeypatch.setenv("FLUID_COPILOT_PII_CLASSIFIER", "false")
        table_def = _translate_catalog_table(_build_catalog_table_with_pii_columns())
        emails = [c for c in table_def.columns if c.name == "email"]
        assert emails and "pii_tags" not in emails[0].qualifiers


# ---------------------------------------------------------------------
# Idempotency + catalog-pre-classified columns
# ---------------------------------------------------------------------


class TestIdempotencyAndPreClassified:
    def test_classifier_is_idempotent_across_two_translate_calls(self):
        """Translating the same catalog table twice produces stable
        qualifier shape — no duplicated ``pii-email`` tag."""
        catalog_table = _build_catalog_table_with_pii_columns()
        first = _translate_catalog_table(catalog_table)
        second = _translate_catalog_table(catalog_table)
        col_first = next(c for c in first.columns if c.name == "email")
        col_second = next(c for c in second.columns if c.name == "email")
        assert col_first.qualifiers["pii_tags"] == col_second.qualifiers["pii_tags"]
        # And the tag list itself is de-duplicated — one entry per
        # detected class.
        assert col_first.qualifiers["pii_tags"].count("pii-email") == 1

    def test_catalog_pre_classified_pii_is_respected(self):
        """When the catalog already tagged a column ``pii`` via
        :class:`SensitivityTag`, the classifier must not re-stamp
        (idempotent against upstream signal)."""
        catalog_table = CatalogTable(
            fqn="t.foo",
            database="DB",
            name="foo",
            columns=[
                CatalogColumn(
                    name="email",
                    data_type="VARCHAR",
                    sensitivity_tags=[SensitivityTag.PII],
                ),
            ],
        )
        table_def = _translate_catalog_table(catalog_table)
        col = table_def.columns[0]
        # The catalog's typed signal lands as expected …
        assert col.qualifiers["catalog_sensitivity_tags"] == ["pii"]
        # … and the classifier didn't stomp on top of it with its
        # own pii_tags / pii_sensitivity (idempotent against
        # already-classified columns).
        assert "pii_tags" not in col.qualifiers
        assert "pii_sensitivity" not in col.qualifiers
        assert "pii_semantic_type" not in col.qualifiers

    def test_catalog_pre_classified_phi_is_respected(self):
        """PHI sensitivity tags also block re-classification."""
        catalog_table = CatalogTable(
            fqn="t.foo",
            database="DB",
            name="foo",
            columns=[
                CatalogColumn(
                    name="email",
                    data_type="VARCHAR",
                    sensitivity_tags=[SensitivityTag.PHI],
                ),
            ],
        )
        table_def = _translate_catalog_table(catalog_table)
        col = table_def.columns[0]
        assert col.qualifiers["catalog_sensitivity_tags"] == ["phi"]
        assert "pii_tags" not in col.qualifiers


# ---------------------------------------------------------------------
# Adapter-coverage — Glue / Snowflake / BigQuery / DataHub all funnel
# through this single helper (RETEST-9), so wiring once benefits all
# four. We assert by simulating each adapter's CatalogTable shape.
# ---------------------------------------------------------------------


class TestAllAdaptersBenefit:
    """RETEST-9 confirmed every catalog adapter routes through
    ``_translate_catalog_table``. These tests simulate the shape each
    adapter actually emits (case, FQN style, owner field) and assert
    the classifier still fires."""

    def _table_with_email_column(self, *, fqn: str, owner: str | None) -> CatalogTable:
        return CatalogTable(
            fqn=fqn,
            database="DB",
            name="customers",
            owner=owner,
            columns=[
                CatalogColumn(name="EMAIL", data_type="VARCHAR(255)"),
                CatalogColumn(name="id", data_type="INTEGER", primary_key=True),
            ],
            primary_key_columns=["id"],
        )

    def test_snowflake_uppercase_email_is_tagged(self):
        """Snowflake adapter emits uppercase column names. Classifier
        is ``re.IGNORECASE`` so the EMAIL column still tags."""
        table_def = _translate_catalog_table(
            self._table_with_email_column(
                fqn="DB.SCH.CUSTOMERS",
                owner="ACCOUNTADMIN",
            )
        )
        col = next(c for c in table_def.columns if c.name == "EMAIL")
        assert "pii-email" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"

    def test_bigquery_dotted_fqn_email_is_tagged(self):
        """BigQuery adapter uses ``project.dataset.table`` FQNs."""
        table_def = _translate_catalog_table(
            self._table_with_email_column(
                fqn="my-project.dataset.customers",
                owner="data-team@example.com",
            )
        )
        col = next(c for c in table_def.columns if c.name == "EMAIL")
        assert "pii-email" in col.qualifiers["pii_tags"]

    def test_glue_lowercase_email_is_tagged(self):
        """Glue Data Catalog emits lowercase column names by default."""
        cat_table = CatalogTable(
            fqn="aws_glue_db.customers",
            database="aws_glue_db",
            name="customers",
            owner="root",
            columns=[
                CatalogColumn(name="email", data_type="string"),
                CatalogColumn(name="id", data_type="bigint", primary_key=True),
            ],
            primary_key_columns=["id"],
        )
        table_def = _translate_catalog_table(cat_table)
        col = next(c for c in table_def.columns if c.name == "email")
        assert "pii-email" in col.qualifiers["pii_tags"]

    def test_datahub_mixed_case_email_is_tagged(self):
        """DataHub adapter normalises to the column's native casing —
        sometimes mixed case like ``Email_Address``."""
        cat_table = CatalogTable(
            fqn="urn:li:dataset:(urn:li:dataPlatform:snowflake,DB.SCH.CUSTOMERS,PROD)",
            database="DB",
            name="customers",
            columns=[
                CatalogColumn(name="Email_Address", data_type="VARCHAR"),
                CatalogColumn(name="id", data_type="BIGINT", primary_key=True),
            ],
            primary_key_columns=["id"],
        )
        table_def = _translate_catalog_table(cat_table)
        col = next(c for c in table_def.columns if c.name == "Email_Address")
        assert "pii-email" in col.qualifiers["pii_tags"]
        assert col.qualifiers["pii_sensitivity"] == "pii"


# ---------------------------------------------------------------------
# RETEST-6 regression pin — the original bug
# ---------------------------------------------------------------------


def test_retest6_email_column_now_carries_pii_metadata():
    """RETEST-6: EMAIL column in ``sat_individual_details`` (Snowflake
    catalog forge) ended up with 0/309 columns carrying PII metadata.
    This pins the fix — at least the obvious PII columns in the sat
    now carry a non-empty ``pii_tags`` qualifier."""
    catalog_table = _build_catalog_table_with_pii_columns()
    table_def = _translate_catalog_table(catalog_table)

    pii_tagged_count = sum(1 for col in table_def.columns if col.qualifiers.get("pii_tags"))
    # The catalog has 5 obvious PII columns (email, c_email,
    # phone_number, ssn, date_of_birth) and 3 audit columns. All 5
    # should classify; none of the 3 audit columns should.
    assert (
        pii_tagged_count == 5
    ), f"expected 5 PII-tagged columns, got {pii_tagged_count}: " + ", ".join(
        f"{c.name}={c.qualifiers.get('pii_tags')}" for c in table_def.columns
    )
