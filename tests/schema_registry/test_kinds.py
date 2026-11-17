# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The registry kinds are plugins: the built-ins' attributes, and the seam itself."""

from __future__ import annotations

import re

import pytest

from fluid_build.schema_registry import kinds
from fluid_build.schema_registry.clients import SubjectVersion
from fluid_build.schema_registry.kinds import RegistryKind, get_kind, register_kind
from fluid_build.schema_registry.produced import cloud_bound, produced_set_errors
from fluid_build.schema_registry.reference import (
    RegistryRef,
    Subject,
    from_registry,
    shape_errors,
)
from fluid_build.schema_registry.schemas import avro_schema, schema_text
from fluid_build.schema_registry.verify import _for_provider, verify_at_apply, verify_consumed

from .conftest import consumed_contract

pytestmark = pytest.mark.unit

COLUMNS = [
    {"name": "order_id", "type": "string", "required": True},
    {"name": "amount", "type": "long", "required": True},
]


def test_names_are_in_registration_order():
    assert kinds.names()[:2] == ("glue", "confluent")
    assert [k.name for k in kinds.all_kinds()] == list(kinds.names())


def test_an_unknown_kind_names_the_known_ones():
    with pytest.raises(ValueError, match="unknown registry kind 'nope'; known: glue, confluent"):
        get_kind("nope")


def test_the_shape_message_lists_the_kinds_in_order():
    assert shape_errors({"kind": "nope"}, "w") == ["w.kind: expected glue or confluent"]


def test_glue_attributes_match_the_old_constants():
    glue = get_kind("glue")
    assert glue.modes == (
        "NONE",
        "DISABLED",
        "BACKWARD",
        "BACKWARD_ALL",
        "FORWARD",
        "FORWARD_ALL",
        "FULL",
        "FULL_ALL",
    )
    assert glue.default_compatibility == "BACKWARD_ALL"
    assert glue.registering_cloud == "aws"
    assert glue.fields == {"registryName"}
    assert glue.checks_compatibility is False


def test_confluent_attributes_match_the_old_constants():
    confluent = get_kind("confluent")
    assert confluent.modes == (
        "NONE",
        "BACKWARD",
        "BACKWARD_TRANSITIVE",
        "FORWARD",
        "FORWARD_TRANSITIVE",
        "FULL",
        "FULL_TRANSITIVE",
    )
    assert confluent.default_compatibility == "BACKWARD_TRANSITIVE"
    assert confluent.registering_cloud == "confluent"
    assert confluent.fields == {"url", "auth", "rules"}
    assert confluent.checks_compatibility is True


def test_a_misspelled_keyword_is_refused_not_swallowed():
    # **clients takes factories keyed by kind name; a typo must not vanish into it.
    with pytest.raises(TypeError, match="enviorn"):
        verify_consumed({}, enviorn={})


def test_a_name_is_registered_once():
    with pytest.raises(ValueError, match="already registered"):
        register_kind(get_kind("glue"))


# ------------------------------------------------------------ a third kind


class Tidal(RegistryKind):
    """A throwaway kind: no core module knows it."""

    name = "tidal"
    operator_credential_env = None
    title = "Tidal"
    modes = ("OFF", "ON")
    default_compatibility = "ON"
    registering_cloud = "gcp"
    bind_hint = "bind a GCP expose"
    subject_pattern = re.compile(r"^[a-z-]{1,40}$")
    fields = frozenset({"tide"})

    def egress_findings(self, subject, *, stage, environ=None):
        return []

    def location(self, ref):
        return "the-sea"

    def produced_key(self, ref):
        return str(ref.subject).lower()

    def client(self, ref, *, contract=None, environ=None, credential=None, factory=None):
        return factory(ref)

    def read(self, client, ref):
        return client.get_version(ref.subject, ref.version)


class FakeTidal:
    def __init__(self):
        self.calls = []
        text = schema_text(avro_schema(COLUMNS, name="orders", namespace="com.acme"))
        self.sv = SubjectVersion("orders-value", 5, "AVRO", text, schema_id="9")

    def get_version(self, subject, version="latest"):
        self.calls.append((subject, version))
        return self.sv


@pytest.fixture
def tidal():
    kind = register_kind(Tidal())
    yield kind
    kinds._base._KINDS.pop("tidal", None)


def test_the_registry_is_restored_after_the_fixture():
    assert "tidal" not in kinds.names()


def test_shape_errors_accepts_the_new_kind(tidal):
    assert kinds.names() == ("glue", "confluent", "tidal")
    spec = {"kind": "tidal", "tide": "high", "subject": "orders-value", "compatibility": "OFF"}
    assert shape_errors(spec, "w") == []
    assert shape_errors({"kind": "tidal", "subject": "NOT OK"}, "w") == [
        "w.subject: not a tidal subject name"
    ]
    assert shape_errors({"kind": "tidal", "compatibility": "FULL"}, "w") == [
        "w.compatibility: tidal accepts OFF, ON"
    ]
    assert shape_errors({"kind": "nope"}, "w") == ["w.kind: expected glue or confluent or tidal"]
    # Another kind's field is refused, never silently ignored.
    assert shape_errors({"kind": "glue", "tide": "x"}, "w") == ["w.tide is for kind: tidal"]
    assert shape_errors({"kind": "confluent", "url": "https://x", "tide": "x"}, "w") == [
        "w.tide is for kind: tidal"
    ]


def test_the_new_kinds_own_field_reaches_its_ref(tidal):
    ref = from_registry({"kind": "tidal", "tide": "high"}, topic="t", origin="o", where="w")
    assert ref.options == {"tide": "high"}
    # The two built-in kinds model their fields on the ref itself.
    glue = from_registry({"kind": "glue", "registryName": "r"}, topic="t", origin="o", where="w")
    assert glue.options == {} and glue.registry_name == "r"


def test_a_kind_that_states_no_egress_rules_is_refused():
    class Silent(RegistryKind):
        name = "silent"

    with pytest.raises(TypeError, match="states no egress rules"):
        register_kind(Silent())
    assert "silent" not in kinds.names()


def test_apply_takes_factories_only_for_kinds_that_take_auth():
    # Apply reads only credentialed subjects; a Glue factory there is a caller's mistake.
    with pytest.raises(TypeError, match=r"\['glue'\].*\(confluent\)"):
        verify_at_apply({}, dry_run=True, glue=lambda: None)


def test_ref_compatibility_and_label_use_the_new_kind(tidal):
    ref = RegistryRef(kind="tidal", origin="o", where="w", subject="orders-value")
    assert ref.effective_compatibility == "ON"
    assert ref.label == "tidal:the-sea/orders-value@latest"
    assert (
        RegistryRef(
            kind="tidal", origin="o", where="w", compatibility="OFF"
        ).effective_compatibility
        == "OFF"
    )


def test_verify_consumed_reads_through_a_factory_named_by_the_kind(tidal):
    client = FakeTidal()
    contract = consumed_contract({"kind": "tidal", "subject": "orders-value"}, columns=COLUMNS)
    records, errors, warnings = verify_consumed(contract, environ={}, tidal=lambda ref: client)
    assert errors == [] and warnings == []
    assert client.calls == [("orders-value", "latest")]
    (record,) = records
    assert record["kind"] == "tidal" and record["registry"] == "the-sea"
    assert record["version"] == 5 and record["result"] == "compatible"


def test_verify_consumed_compares_the_new_kinds_schema(tidal):
    client = FakeTidal()
    wider = [*COLUMNS, {"name": "extra", "type": "string", "required": True}]
    contract = consumed_contract({"kind": "tidal", "subject": "orders-value"}, columns=wider)
    records, errors, _ = verify_consumed(contract, environ={}, tidal=lambda ref: client)
    assert len(errors) == 1 and "cannot read version 5" in errors[0]
    assert records[0]["result"] == "incompatible"


def test_produced_duplicates_are_keyed_by_the_new_kind(tidal):
    # The refs are built directly: "EVT-VALUE" fails the kind's own subject pattern.
    refs = [
        Subject(
            RegistryRef("tidal", "o", f"w{i}", subject=name), "produced", "evt", "avro", (), f"e{i}"
        )
        for i, name in enumerate(["evt-value", "EVT-VALUE"])
    ]
    errors = produced_set_errors({}, refs)
    assert len(errors) == 1 and "also produced by w0" in errors[0]


def test_produced_distinct_keys_do_not_collide(tidal):
    refs = [
        Subject(
            RegistryRef("tidal", "o", f"w{i}", subject=name), "produced", "evt", "avro", (), f"e{i}"
        )
        for i, name in enumerate(["a-value", "b-value"])
    ]
    assert produced_set_errors({}, refs) == []


def test_cloud_bound_uses_the_registering_cloud(tidal):
    contract = {"exposes": [{"binding": {"platform": "gcp", "format": "bigquery"}}]}
    assert cloud_bound(contract, "tidal") is True
    assert cloud_bound(contract, "glue") is False


def test_for_provider_filters_by_the_registering_cloud(tidal):
    def subject(kind, **kw):
        return Subject(RegistryRef(kind, "o", "w", subject="s", **kw), "produced", "t", "avro")

    glue, confluent, third = (
        subject("glue"),
        subject("confluent", url="https://x"),
        subject("tidal"),
    )
    every = [glue, confluent, third]
    assert _for_provider(every, "gcp") == [third]
    assert _for_provider(every, "aws") == [glue]
    assert _for_provider(every, "confluent") == [confluent]
    assert _for_provider(every, None) == every
    assert _for_provider(every, "no-such-cloud") == every
