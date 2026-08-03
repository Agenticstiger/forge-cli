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

"""Column-name PII pre-classifier — H6 fix.

Pinned cases derived from:

* tokern/piicatcher's ``ColumnNameRegexDetector`` vocabulary (Apache-2.0).
* Microsoft Presidio entity names (MIT).
* GCP DLP InfoType reference (public docs).
* AWS Glue Managed Identifier list (public docs).

If any of these pins start failing, the symptom is "the Judge security
axis under-scores PII-laden contracts" — the regex table must keep
covering the canonical column-name forms the wild produces.
"""

from __future__ import annotations

import re

import pytest

from fluid_build.copilot.pii import (
    PII_CLASSIFIERS,
    PII_SENSITIVITY_MAP,
    apply_pii_tags,
    classifier_enabled,
    classify_column,
    classify_contract_schemas,
)

# ---------------------------------------------------------------------------
# Smoke: every entry in PII_CLASSIFIERS has a sensitivity mapping
# ---------------------------------------------------------------------------


def test_every_classifier_has_a_sensitivity_mapping():
    """Catches the obvious "added a classifier, forgot to map" foot-gun."""
    classifier_classes = set(PII_CLASSIFIERS.keys())
    sensitivity_classes = set(PII_SENSITIVITY_MAP.keys())
    assert classifier_classes == sensitivity_classes, (
        f"missing sensitivity: {classifier_classes - sensitivity_classes} "
        f"orphan sensitivity: {sensitivity_classes - classifier_classes}"
    )


def test_every_sensitivity_is_in_the_schema_enum():
    """Sensitivity values must be drawn from the FLUID schema enum."""
    schema_enum = {
        "none",
        "internal",
        "confidential",
        "restricted",
        "pii",
        "phi",
        "cleartext",
        "treated",
        "anonymized",
        "pseudonymized",
        "tokenized",
        "encrypted",
    }
    assert set(PII_SENSITIVITY_MAP.values()) <= schema_enum


def test_every_classifier_pattern_compiles_and_is_ignorecase():
    """Sanity-check the regex shape."""
    for cls, pat in PII_CLASSIFIERS.items():
        assert pat.flags & re.IGNORECASE, f"{cls}: pattern missing re.IGNORECASE"


# ---------------------------------------------------------------------------
# classify_column — the canonical positive matches the user listed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # Email — the H6 headline case
        ("c_email", ["email"]),
        ("email", ["email"]),
        ("email_address", ["email"]),
        ("EmailAddress", ["email"]),
        ("user_email", ["email"]),
        ("customer_email", ["email"]),
        ("e_mail", ["email"]),
        ("e-mail", ["email"]),
        # Phone
        ("phone_number", ["phone"]),
        ("phone", ["phone"]),
        ("telephone", ["phone"]),
        ("mobile_phone", ["phone"]),
        ("cellphone", ["phone"]),
        ("fax_number", ["phone"]),
        ("contact_phone", ["phone"]),
        # SSN
        ("ssn", ["ssn"]),
        ("SSN", ["ssn"]),
        ("social_security", ["ssn"]),
        ("social_security_number", ["ssn"]),
        # National IDs
        ("passport_number", ["national_id"]),
        ("aadhaar_number", ["national_id"]),
        ("driver_license", ["national_id"]),
        ("driving_license", ["national_id"]),
        # DOB
        ("date_of_birth", ["dob"]),
        ("dob", ["dob"]),
        ("birthday", ["dob"]),
        ("birth_date", ["dob"]),
        # Address
        ("customer_address", ["address"]),
        ("address", ["address"]),
        ("street_address", ["address"]),
        ("postal_code", ["address"]),
        ("zip_code", ["address"]),
        # IP
        ("ip_addr", ["ip_address"]),
        ("ip_address", ["ip_address"]),
        ("client_ip", ["ip_address"]),
        ("ipv4", ["ip_address"]),
        # Payment card
        ("credit_card_num", ["payment_card"]),
        ("credit_card_number", ["payment_card"]),
        ("cc_number", ["payment_card"]),
        ("card_number", ["payment_card"]),
        ("iban", ["payment_card"]),
        # Name
        ("first_name", ["name"]),
        ("last_name", ["name"]),
        ("full_name", ["name"]),
        ("nickname", ["name"]),
        # Credentials
        ("password", ["credentials"]),
        ("api_key", ["credentials"]),
        ("auth_token", ["credentials"]),
        # Medical (PHI)
        ("mrn", ["medical"]),
        ("medical_record_number", ["medical"]),
        ("patient_id", ["medical"]),
        # Special category
        ("ethnicity", ["special_category"]),
        ("religion", ["special_category"]),
    ],
)
def test_classify_positive_matches(name, expected):
    """Headline positive matches. Multi-match cases live in their own test."""
    assert classify_column(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "user_id",
        "id",
        "created_at",
        "updated_at",
        "order_id",
        "amount",
        "quantity",
        "status",
        "is_active",
        "uuid",
        "row_id",
        "row_num",
        "version",
        "currency",
        "total",
        "balance",
        "discount",
        "tax_rate",
        "item_count",
        # voicemail vs mail — bare ``voicemail`` should NOT tag email
        # (it's not the kind of email we're worried about). The regex
        # bounds (word boundary) make this work.
        "voicemail",
        # ``rate_limit`` should NOT match anything PII-ish
        "rate_limit",
    ],
)
def test_classify_negative_cases(name):
    """Non-PII column names must return empty."""
    assert classify_column(name) == []


def test_classify_returns_empty_for_falsy_input():
    assert classify_column("") == []
    assert classify_column(None) == []  # type: ignore[arg-type]


def test_classify_multi_match_returns_sorted_list():
    """A column that matches multiple classes returns them alphabetically."""
    # ``contact_email_phone`` matches email + phone
    result = classify_column("contact_email_phone")
    assert result == sorted(result)
    assert "email" in result
    assert "phone" in result


def test_classify_user_name_is_username_not_person_name():
    """``user_name`` is a login field, not a person's name. Should not
    double-tag as both username and name — the name pattern deliberately
    excludes the bare ``user_name`` form."""
    result = classify_column("user_name")
    assert "username" in result
    assert "name" not in result


# ---------------------------------------------------------------------------
# apply_pii_tags — schema-mutation behaviour
# ---------------------------------------------------------------------------


def test_apply_pii_tags_attaches_tags_sensitivity_semantic_type():
    schema = [
        {"name": "email", "type": "string"},
        {"name": "id", "type": "int"},
    ]
    out = apply_pii_tags(schema)
    # The returned object is the same list (mutated in place).
    assert out is schema
    # Email column got the tags + sensitivity + semanticType triple.
    assert "pii-email" in schema[0]["tags"]
    assert schema[0]["sensitivity"] == "pii"
    assert schema[0]["semanticType"] == "email"
    # Non-PII column untouched.
    assert "tags" not in schema[1]
    assert "sensitivity" not in schema[1]
    assert "semanticType" not in schema[1]


def test_apply_pii_tags_preserves_existing_sensitivity_by_default():
    schema = [{"name": "email", "type": "string", "sensitivity": "restricted"}]
    apply_pii_tags(schema)
    # User-set sensitivity is preserved (conservative — never stomp).
    assert schema[0]["sensitivity"] == "restricted"
    # Tags still added.
    assert "pii-email" in schema[0]["tags"]


def test_apply_pii_tags_overwrite_flag_overrides():
    schema = [{"name": "email", "type": "string", "sensitivity": "internal"}]
    apply_pii_tags(schema, overwrite=True)
    assert schema[0]["sensitivity"] == "pii"


def test_apply_pii_tags_merges_existing_tags():
    schema = [{"name": "email", "type": "string", "tags": ["customer-contact"]}]
    apply_pii_tags(schema)
    tags = schema[0]["tags"]
    assert "customer-contact" in tags  # existing user tag preserved
    assert "pii-email" in tags  # PII tag merged in


def test_apply_pii_tags_dedupes_on_rerun():
    """Calling apply_pii_tags twice on the same schema produces no duplicates."""
    schema = [{"name": "email", "type": "string"}]
    apply_pii_tags(schema)
    apply_pii_tags(schema)
    # ``pii-email`` should appear exactly once.
    assert schema[0]["tags"].count("pii-email") == 1


def test_apply_pii_tags_multi_match_uses_strongest_sensitivity():
    """A column matching ``medical`` (phi) + ``national_id`` (pii) lands at phi."""
    # ``patient_id_passport_number`` matches both ``medical`` (patient_id
    # token) and ``national_id`` (passport_number token) — a contrived
    # but valid multi-class column name.
    schema = [{"name": "patient_id_passport_number", "type": "string"}]
    apply_pii_tags(schema)
    # Strongest priority wins → phi.
    assert schema[0]["sensitivity"] == "phi"
    # Both tags present.
    assert "pii-medical" in schema[0]["tags"]
    assert "pii-national-id" in schema[0]["tags"]


def test_apply_pii_tags_kill_switch(monkeypatch):
    """Setting FLUID_COPILOT_PII_CLASSIFIER=0 disables tagging."""
    monkeypatch.setenv("FLUID_COPILOT_PII_CLASSIFIER", "0")
    assert classifier_enabled() is False
    schema = [{"name": "email", "type": "string"}]
    apply_pii_tags(schema)
    # No tags attached.
    assert "tags" not in schema[0]
    assert "sensitivity" not in schema[0]


def test_apply_pii_tags_handles_non_list_input():
    """A schema that isn't a list returns unchanged (defensive)."""
    assert apply_pii_tags("not a list") == "not a list"  # type: ignore[arg-type]
    assert apply_pii_tags(None) is None  # type: ignore[arg-type]


def test_apply_pii_tags_skips_non_dict_items():
    """Malformed schema (non-dict items) is silently skipped."""
    schema = [{"name": "email", "type": "string"}, "garbage", 42, None]
    apply_pii_tags(schema)  # type: ignore[arg-type]
    assert "pii-email" in schema[0]["tags"]


def test_apply_pii_tags_uses_kebab_case_tags_matching_schema_pattern():
    """Tag pattern in the schema is [a-z0-9][a-z0-9-]*[a-z0-9].
    Underscores in PII class names (``ip_address``, ``payment_card``) must
    become hyphens in the emitted tags."""
    schema = [
        {"name": "ip_address", "type": "string"},
        {"name": "credit_card", "type": "string"},
        {"name": "patient_id", "type": "string"},
    ]
    apply_pii_tags(schema)
    # No underscores in any tag.
    for col in schema:
        for tag in col.get("tags", []):
            assert "_" not in tag, f"underscore in tag {tag!r} for col {col['name']!r}"
            assert re.match(
                r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", tag
            ), f"tag {tag!r} doesn't match FLUID schema pattern"


# ---------------------------------------------------------------------------
# classify_contract_schemas — contract-level walk
# ---------------------------------------------------------------------------


def test_classify_contract_schemas_walks_every_expose():
    contract = {
        "fluidVersion": "0.7.3",
        "exposes": [
            {
                "exposeId": "customer",
                "contract": {
                    "schema": [
                        {"name": "id", "type": "int"},
                        {"name": "c_email", "type": "string"},
                        {"name": "c_phone", "type": "string"},
                    ],
                },
            },
            {
                "exposeId": "orders",
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "int"},
                        {"name": "amount", "type": "decimal"},
                    ],
                },
            },
        ],
    }
    summary = classify_contract_schemas(contract)
    assert summary["totals"] == {"email": 1, "phone": 1}
    # First model has both PII columns; second has none.
    assert summary["models"] == [
        {"model_name": "customer", "tagged": {"email": 1, "phone": 1}},
    ]
    # And the schema was actually mutated.
    customer_email = contract["exposes"][0]["contract"]["schema"][1]
    assert "pii-email" in customer_email["tags"]


def test_classify_contract_schemas_empty_contract():
    summary = classify_contract_schemas({})
    assert summary == {"models": [], "totals": {}}


def test_classify_contract_schemas_handles_no_pii_contract():
    """An entirely non-PII contract reports empty totals — no false positives."""
    contract = {
        "exposes": [
            {
                "exposeId": "orders",
                "contract": {
                    "schema": [
                        {"name": "id", "type": "int"},
                        {"name": "amount", "type": "decimal"},
                        {"name": "created_at", "type": "timestamp"},
                    ],
                },
            }
        ],
    }
    summary = classify_contract_schemas(contract)
    assert summary["totals"] == {}
    assert summary["models"] == []
