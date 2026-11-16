# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid plan``'s registry checks, with every registry client faked."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.schema_registry.clients import RegistryError, SubjectVersion
from fluid_build.schema_registry.schemas import avro_schema, json_schema, schema_text
from fluid_build.schema_registry.verify import attach_registry_checks, verify_consumed

from .conftest import consumed_contract, produced_contract

pytestmark = pytest.mark.unit

URL = "https://sr.example.com"
COLUMNS = [
    {"name": "order_id", "type": "string", "required": True},
    {"name": "amount", "type": "long", "required": True},
]
GRANTS = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"


def avro_text(columns=COLUMNS, name="orders", namespace="com.acme"):
    return schema_text(avro_schema(columns, name=name, namespace=namespace))


def json_text(columns=COLUMNS):
    return schema_text(json_schema(columns, title="orders"))


class FakeConfluent:
    def __init__(
        self,
        schema=None,
        data_format="AVRO",
        version=7,
        compatible=True,
        messages=(),
        mode="BACKWARD",
        error=None,
    ):
        self.sv = SubjectVersion(
            "orders-value", version, data_format, schema or avro_text(), schema_id="101"
        )
        self.compatible, self.messages, self.mode, self.error = (
            compatible,
            list(messages),
            mode,
            error,
        )
        self.calls = []
        self.closed = False

    def get_version(self, subject, version="latest"):
        self.calls.append(("get_version", subject, version))
        if self.error:
            raise self.error
        return self.sv

    def check_compatibility(self, subject, version, schema, data_format):
        self.calls.append(("check_compatibility", subject, version, schema, data_format))
        return self.compatible, self.messages

    def compatibility_level(self, subject):
        self.calls.append(("compatibility_level", subject))
        return self.mode

    def close(self):
        self.closed = True


class FakeGlue:
    def __init__(self, schema=None, data_format="AVRO", version=3, error=None):
        self.sv = SubjectVersion(
            "orders-value", version, data_format, schema or avro_text(), schema_id="uuid-1"
        )
        self.error = error
        self.calls = []

    def get_version(self, registry, schema, version="latest"):
        self.calls.append((registry, schema, version))
        if self.error:
            raise self.error
        return self.sv


def run(contract, confluent=None, glue=None, environ=None):
    factories = {}
    if confluent is not None:
        factories["confluent"] = lambda url: confluent
    else:
        factories["confluent"] = lambda url: pytest.fail("a Confluent client was built")
    if glue is not None:
        factories["glue"] = lambda: glue
    else:
        factories["glue"] = lambda: pytest.fail("a Glue client was built")
    return verify_consumed(contract, environ={} if environ is None else environ, **factories)


def confluent_contract(**kw):
    return consumed_contract({"kind": "confluent", "url": URL}, **kw)


def glue_contract(**kw):
    return consumed_contract({"kind": "glue", "registryName": "commerce"}, **kw)


# ---------------------------------------------------------------- Confluent


class TestConfluent:
    def test_compatible_is_recorded_with_version_and_result(self):
        client = FakeConfluent()
        records, errors, warnings = run(confluent_contract(), confluent=client)
        assert errors == [] and warnings == []
        (record,) = records
        assert record == {
            "build": "ingest",
            "topic": "orders",
            "kind": "confluent",
            "registry": URL,
            "subject": "orders-value",
            "requested": "latest",
            "version": 7,
            "schemaId": "101",
            "mode": "BACKWARD",
            "result": "compatible",
        }

    def test_the_registry_is_asked_about_the_pinned_version_not_latest(self):
        client = FakeConfluent(version=7)
        run(confluent_contract(), confluent=client)
        assert client.calls[0] == ("get_version", "orders-value", "latest")
        check = next(c for c in client.calls if c[0] == "check_compatibility")
        assert check[1:3] == ("orders-value", 7) and check[4] == "AVRO"

    def test_the_client_is_closed(self):
        client = FakeConfluent()
        run(confluent_contract(), confluent=client)
        assert client.closed

    def test_the_client_is_closed_on_error(self):
        client = FakeConfluent(error=RegistryError("boom"))
        run(confluent_contract(), confluent=client)
        assert client.closed

    def test_the_declared_schema_is_named_after_the_producers_records(self):
        client = FakeConfluent()
        run(confluent_contract(), confluent=client)
        sent = json.loads(next(c for c in client.calls if c[0] == "check_compatibility")[3])
        assert (sent["name"], sent["namespace"]) == ("orders", "com.acme")

    def test_a_pinned_version_is_requested(self):
        contract = consumed_contract({"kind": "confluent", "url": URL, "version": 4})
        client = FakeConfluent(version=4)
        records, errors, _ = run(contract, confluent=client)
        assert client.calls[0][2] == 4 and records[0]["requested"] == 4 and errors == []

    def test_registry_says_incompatible_is_an_error_quoting_messages(self):
        client = FakeConfluent(compatible=False, messages=["Field amount removed", "other reason"])
        records, errors, _ = run(confluent_contract(), confluent=client)
        (error,) = errors
        assert "not compatible with version 7" in error
        assert "Field amount removed; other reason" in error
        assert records[0]["result"] == "incompatible"

    def test_incompatible_without_messages_says_so(self):
        _, errors, _ = run(confluent_contract(), confluent=FakeConfluent(compatible=False))
        assert "no reason given" in errors[0]

    def test_none_mode_registry_accepts_but_forge_comparison_is_breaking(self):
        # The subject holds amount as int; the stream reads it as long (a widening)...
        # ... so flip it: the subject holds a string, the stream reads a long.
        writer = avro_text([COLUMNS[0], {"name": "amount", "type": "string", "required": True}])
        client = FakeConfluent(schema=writer, compatible=True, mode="NONE")
        records, errors, _ = run(confluent_contract(), confluent=client)
        (error,) = errors
        assert "cannot read version 7" in error and "amount" in error
        assert records[0]["result"] == "incompatible" and records[0]["mode"] == "NONE"

    def test_new_required_reader_field_is_caught_when_the_registry_is_lenient(self):
        client = FakeConfluent(schema=avro_text(COLUMNS[:1]), mode="NONE")
        _, errors, _ = run(confluent_contract(), confluent=client)
        assert errors and "amount" in errors[0]

    def test_widening_is_accepted(self):
        writer = avro_text([COLUMNS[0], {"name": "amount", "type": "int", "required": True}])
        records, errors, _ = run(confluent_contract(), confluent=FakeConfluent(schema=writer))
        assert errors == [] and records[0]["result"] == "compatible"

    def test_format_mismatch_is_an_error(self):
        client = FakeConfluent(schema=json_text(), data_format="JSON")
        _, errors, _ = run(confluent_contract(), confluent=client)
        (error,) = errors
        assert "holds a JSON schema" in error and "reads AVRO" in error
        assert not any(c[0] == "check_compatibility" for c in client.calls)

    def test_json_stream_against_an_avro_subject_is_an_error(self):
        _, errors, _ = run(confluent_contract(fmt="json"), confluent=FakeConfluent())
        assert errors and "holds a AVRO schema" in errors[0] and "reads JSON" in errors[0]

    def test_json_subject_compatible(self):
        client = FakeConfluent(schema=json_text(), data_format="JSON")
        records, errors, _ = run(confluent_contract(fmt="json"), confluent=client)
        assert errors == [] and records[0]["result"] == "compatible"

    def test_json_int_vs_long_not_distinguished(self):
        writer = json_text([COLUMNS[0], {"name": "amount", "type": "int", "required": True}])
        client = FakeConfluent(schema=writer, data_format="JSON")
        _, errors, _ = run(confluent_contract(fmt="json"), confluent=client)
        assert errors == []

    def test_subject_not_found_is_an_error_with_the_registrys_message(self):
        error = RegistryError("HTTP 404 (40401): Subject 'orders-value' not found.", code=40401)
        records, errors, _ = run(confluent_contract(), confluent=FakeConfluent(error=error))
        (message,) = errors
        assert "could not be read" in message and "40401" in message and "not found" in message
        assert records[0]["result"] == "incompatible"

    def test_unsupported_union_in_the_subject_is_an_error(self):
        schema = json.dumps(
            {
                "type": "record",
                "name": "orders",
                "fields": [{"name": "a", "type": ["int", "string"]}],
            }
        )
        _, errors, _ = run(confluent_contract(), confluent=FakeConfluent(schema=schema))
        assert errors and "union" in errors[0]

    def test_a_subject_whose_schema_is_not_json_is_an_error(self):
        _, errors, _ = run(confluent_contract(), confluent=FakeConfluent(schema="syntax"))
        assert errors and "not JSON" in errors[0]

    def test_a_failing_mode_lookup_does_not_fail_the_check(self):
        class NoMode(FakeConfluent):
            def compatibility_level(self, subject):
                raise RegistryError("denied")

        records, errors, _ = run(confluent_contract(), confluent=NoMode())
        assert errors == [] and "mode" not in records[0]

    def test_with_auth_the_subject_is_not_verified_and_no_client_is_called(self):
        contract = consumed_contract(
            {
                "kind": "confluent",
                "url": URL,
                "auth": {"secretRef": "env://SR_AUTH"},  # pragma: allowlist secret
            }
        )
        env = {GRANTS: "SR_AUTH@sr.example.com"}
        records, errors, warnings = run(contract, environ=env)  # a built client fails the test
        assert errors == []
        (warning,) = warnings
        assert "not verified at plan" in warning
        assert records == [
            {
                "build": "ingest",
                "topic": "orders",
                "kind": "confluent",
                "registry": URL,
                "subject": "orders-value",
                "requested": "latest",
                "result": "not-verified",
            }
        ]

    def test_with_auth_but_no_grant_is_a_plan_error_and_no_client_call(self):
        contract = consumed_contract(
            {
                "kind": "confluent",
                "url": URL,
                "auth": {"secretRef": "env://SR_AUTH"},  # pragma: allowlist secret
            }
        )
        records, errors, _ = run(contract)
        assert errors and records == []

    def test_link_local_registry_is_refused_before_any_client(self):
        contract = consumed_contract({"kind": "confluent", "url": "http://169.254.169.254"})
        records, errors, _ = run(contract)
        assert records == [] and "link-local" in errors[0]

    def test_public_unlisted_host_is_refused_at_plan(self, public_dns):
        records, errors, _ = run(confluent_contract())
        assert records == [] and "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST" in errors[0]

    def test_two_builds_are_both_checked(self):
        contract = confluent_contract()
        second = copy.deepcopy(contract["builds"][0])
        second["id"] = "second"
        contract["builds"].append(second)
        records, errors, _ = run(contract, confluent=FakeConfluent())
        assert [r["build"] for r in records] == ["ingest", "second"] and errors == []


# --------------------------------------------------------------------- Glue


class TestGlue:
    def test_compatible(self):
        client = FakeGlue()
        records, errors, _ = run(glue_contract(), glue=client)
        assert errors == []
        assert client.calls == [("commerce", "orders-value", "latest")]
        assert records[0]["kind"] == "glue" and records[0]["registry"] == "commerce"
        assert records[0]["version"] == 3 and records[0]["schemaId"] == "uuid-1"
        assert records[0]["result"] == "compatible"

    def test_default_registry_name(self):
        client = FakeGlue()
        run(consumed_contract({"kind": "glue"}), glue=client)
        assert client.calls[0][0] == "default-registry"

    def test_breaking_comparison_is_an_error(self):
        writer = avro_text([COLUMNS[0], {"name": "amount", "type": "string", "required": True}])
        _, errors, _ = run(glue_contract(), glue=FakeGlue(schema=writer))
        assert errors and "cannot read version 3" in errors[0]

    def test_format_mismatch(self):
        _, errors, _ = run(glue_contract(), glue=FakeGlue(schema=json_text(), data_format="JSON"))
        assert errors and "holds a JSON schema" in errors[0]

    def test_json_subject(self):
        client = FakeGlue(schema=json_text(), data_format="JSON")
        records, errors, _ = run(glue_contract(fmt="json"), glue=client)
        assert errors == [] and records[0]["result"] == "compatible"

    def test_entity_not_found(self):
        error = RegistryError(
            "EntityNotFoundException: Schema is not found.", code="EntityNotFoundException"
        )
        _, errors, _ = run(glue_contract(), glue=FakeGlue(error=error))
        assert errors and "EntityNotFoundException" in errors[0]

    def test_glue_needs_no_credential_findings(self):
        records, errors, warnings = run(glue_contract(), glue=FakeGlue())
        assert warnings == [] and errors == []


# ---------------------------------------------------------------- produced


class TestProducedEgressAndEndpoint:
    SPEC = {"kind": "confluent", "url": URL}
    ENV_OK = {
        "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
        GRANTS: "SCHEMA_REGISTRY_API_SECRET@sr.example.com",
    }

    def check(self, endpoint, **extra):
        env = dict(self.ENV_OK)
        if endpoint is not None:
            env["SCHEMA_REGISTRY_REST_ENDPOINT"] = endpoint
        return run(produced_contract(self.SPEC), environ=env, **extra)

    def test_unset_endpoint_is_a_warning(self):
        records, errors, warnings = self.check(None)
        assert errors == [] and records == []
        (warning,) = warnings
        assert "SCHEMA_REGISTRY_REST_ENDPOINT" in warning and "unset" in warning

    def test_a_different_endpoint_is_an_error(self):
        _, errors, _ = self.check("https://other.example.com")
        (error,) = errors
        assert "points OpenTofu at another registry" in error

    def test_the_same_endpoint_is_ok(self):
        assert self.check(URL)[1:] == ([], [])

    @pytest.mark.parametrize("variant", [URL + "/", URL.upper(), " " + URL + " ", URL + "//"])
    def test_trailing_slash_and_case_differences_are_the_same_endpoint(self, variant):
        assert self.check(variant)[1:] == ([], [])

    def test_a_different_port_is_another_registry(self):
        assert self.check(URL + ":8443")[1]

    def test_a_different_path_is_another_registry(self):
        assert self.check(URL + "/v2")[1]

    def test_a_different_scheme_is_another_registry(self):
        assert self.check("http://sr.example.com")[1]

    def test_missing_grant_is_a_plan_error(self):
        env = {
            "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
            "SCHEMA_REGISTRY_REST_ENDPOINT": URL,
        }
        _, errors, _ = run(produced_contract(self.SPEC), environ=env)
        assert errors and "SCHEMA_REGISTRY_API_SECRET" in errors[0]

    def test_glue_produced_subject_needs_no_endpoint(self):
        records, errors, warnings = run(produced_contract({"kind": "glue"}))
        assert (records, errors, warnings) == ([], [], [])

    def test_malformed_produced_registry_is_reported(self):
        _, errors, _ = run(produced_contract({"kind": "confluent", "url": "nope"}))
        assert errors and "properties.registry.url" in errors[0]

    def test_a_registry_on_a_non_kafka_expose_is_reported(self):
        _, errors, _ = run(produced_contract({"kind": "glue"}, platform="aws", fmt="iceberg"))
        assert errors and "belongs on a Kafka topic expose" in errors[0]


# -------------------------------------------------------- attach_registry_checks


class TestAttach:
    def attach(self, plan, contract, **kw):
        kw.setdefault("environ", {})
        return attach_registry_checks(plan, contract, **kw)

    def test_no_registry_returns_the_plan_unchanged(self):
        plan = {"actions": [], "digest": "abc"}
        before = copy.deepcopy(plan)
        result = self.attach(plan, {"id": "x", "builds": []})
        assert result is plan and result == before and "schemaRegistry" not in result

    def test_a_connector_block_only_changes_nothing(self):
        contract = {
            "builds": [
                {
                    "id": "a",
                    "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
                }
            ]
        }
        plan = {"actions": []}
        assert self.attach(plan, contract) == {"actions": []}

    def test_records_land_under_plan_schema_registry_checked(self, capsys):
        plan = {"actions": []}
        result = self.attach(
            plan,
            confluent_contract(),
            confluent=lambda url: FakeConfluent(),
        )
        (record,) = result["schemaRegistry"]["checked"]
        assert record["result"] == "compatible" and record["version"] == 7
        assert result["actions"] == []

    def test_errors_raise_a_cli_error(self):
        client = FakeConfluent(compatible=False, messages=["nope"])
        with pytest.raises(CLIError) as caught:
            self.attach({}, confluent_contract(), confluent=lambda url: client)
        assert caught.value.event == "plan_schema_registry_failed"
        assert caught.value.exit_code == 1
        assert any("nope" in e for e in caught.value.context["errors"])
        assert "1 schema registry check(s) failed" in caught.value.context["message"]

    def test_every_error_is_listed(self):
        contract = confluent_contract()
        second = copy.deepcopy(contract["builds"][0])
        second["id"] = "second"
        contract["builds"].append(second)
        client = FakeConfluent(error=RegistryError("down"))
        with pytest.raises(CLIError) as caught:
            self.attach({}, contract, confluent=lambda url: client)
        assert len(caught.value.context["errors"]) == 2

    def test_the_plan_is_not_touched_when_it_fails(self):
        plan = {"actions": []}
        with pytest.raises(CLIError):
            self.attach(plan, glue_contract(), glue=lambda: FakeGlue(error=RegistryError("x")))
        assert plan == {"actions": []}

    def test_not_verified_records_are_attached_with_a_warning(self, monkeypatch):
        seen = []
        import fluid_build.cli.console as console

        monkeypatch.setattr(console, "warning", lambda message: seen.append(message))
        contract = consumed_contract(
            {
                "kind": "confluent",
                "url": URL,
                "auth": {"secretRef": "env://SR_AUTH"},  # pragma: allowlist secret
            }
        )
        env = {GRANTS: "SR_AUTH@sr.example.com"}
        result = self.attach({}, contract, environ=env)
        assert result["schemaRegistry"]["checked"][0]["result"] == "not-verified"
        assert any("not verified at plan" in m for m in seen)

    def test_glue_record_attached(self):
        result = self.attach({}, glue_contract(), glue=lambda: FakeGlue())
        assert result["schemaRegistry"]["checked"][0]["kind"] == "glue"

    def test_plan_stays_json_serialisable(self):
        result = self.attach({}, confluent_contract(), confluent=lambda url: FakeConfluent())
        assert json.loads(json.dumps(result)) == result

    def test_produced_confluent_with_different_endpoint_refuses_the_plan(self):
        env = {
            "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
            GRANTS: "SCHEMA_REGISTRY_API_SECRET@sr.example.com",
            "SCHEMA_REGISTRY_REST_ENDPOINT": "https://other.example.com",
        }
        with pytest.raises(CLIError) as caught:
            self.attach({}, produced_contract({"kind": "confluent", "url": URL}), environ=env)
        assert caught.value.event == "plan_schema_registry_failed"

    def test_produced_confluent_with_the_same_endpoint_leaves_the_plan_alone(self):
        env = {
            "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
            GRANTS: "SCHEMA_REGISTRY_API_SECRET@sr.example.com",
            "SCHEMA_REGISTRY_REST_ENDPOINT": URL + "/",
        }
        plan = {"actions": []}
        assert self.attach(
            plan, produced_contract({"kind": "confluent", "url": URL}), environ=env
        ) == {"actions": []}


# ------------------------------------------- the Confluent REST client, no network


class TestConfluentRegistryHttp:
    @pytest.fixture
    def rest(self, monkeypatch):
        import httpx

        from fluid_build.schema_registry.clients import ConfluentRegistry

        seen = []
        responder = {}

        def handler(request):
            seen.append(request)
            return responder["fn"](request)

        def _http(self):
            if self._client is None:
                self._client = httpx.Client(
                    transport=httpx.MockTransport(handler), base_url=self.url.rstrip("/") + "/"
                )
            return self._client

        monkeypatch.setattr(ConfluentRegistry, "_http", _http)

        def make(fn):
            responder["fn"] = fn
            return ConfluentRegistry(URL), seen

        return make

    def test_get_version_path_and_mapping(self, rest):
        import httpx

        client, seen = rest(
            lambda r: httpx.Response(
                200,
                json={
                    "subject": "a b/c-value",
                    "version": 5,
                    "id": 42,
                    "schemaType": "JSON",
                    "schema": "{}",
                },
            )
        )
        sv = client.get_version("a b/c-value", "latest")
        (request,) = seen
        assert request.method == "GET"
        assert request.url.raw_path == b"/subjects/a%20b%2Fc-value/versions/latest"
        assert (sv.subject, sv.version, sv.data_format, sv.schema, sv.schema_id) == (
            "a b/c-value",
            5,
            "JSON",
            "{}",
            "42",
        )

    def test_schema_type_defaults_to_avro(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(200, json={"version": 1, "schema": "{}"}))
        sv = client.get_version("orders-value", 1)
        assert sv.data_format == "AVRO" and sv.schema_id is None and sv.subject == "orders-value"

    def test_pinned_version_in_the_path(self, rest):
        import httpx

        client, seen = rest(lambda r: httpx.Response(200, json={"version": 3, "schema": "{}"}))
        client.get_version("s", 3)
        assert seen[0].url.path.endswith("/subjects/s/versions/3")

    def test_check_compatibility_request(self, rest):
        import httpx

        client, seen = rest(
            lambda r: httpx.Response(200, json={"is_compatible": False, "messages": ["bad", 2]})
        )
        ok, messages = client.check_compatibility("orders-value", 7, '{"type":"record"}', "AVRO")
        (request,) = seen
        assert request.method == "POST"
        assert request.url.path == "/compatibility/subjects/orders-value/versions/7"
        assert dict(request.url.params) == {"verbose": "true"}
        assert json.loads(request.content) == {"schema": '{"type":"record"}', "schemaType": "AVRO"}
        assert request.headers["content-type"] == "application/vnd.schemaregistry.v1+json"
        assert ok is False and messages == ["bad", "2"]

    def test_compatible_answer(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(200, json={"is_compatible": True}))
        assert client.check_compatibility("s", 1, "{}", "JSON") == (True, [])

    def test_404_body_becomes_a_registry_error_with_code_and_message(self, rest):
        import httpx

        client, _ = rest(
            lambda r: httpx.Response(
                404, json={"error_code": 40401, "message": "Subject 'orders-value' not found."}
            )
        )
        with pytest.raises(RegistryError) as caught:
            client.get_version("orders-value")
        assert caught.value.code == 40401
        assert "Subject 'orders-value' not found." in str(caught.value)
        assert "HTTP 404" in str(caught.value)

    def test_a_non_json_error_body_falls_back_to_the_status(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(502, text="Bad Gateway"))
        with pytest.raises(RegistryError) as caught:
            client.get_version("s")
        assert caught.value.code == 502 and "Bad Gateway" in str(caught.value)

    def test_error_messages_are_truncated(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(500, text="x" * 5000))
        with pytest.raises(RegistryError) as caught:
            client.get_version("s")
        assert len(str(caught.value)) < 600

    def test_a_non_json_success_body_is_an_error(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(200, text="<html>"))
        with pytest.raises(RegistryError, match="no JSON"):
            client.get_version("s")

    def test_a_transport_failure_is_a_registry_error(self, rest):
        import httpx

        def boom(request):
            raise httpx.ConnectError("refused")

        client, _ = rest(boom)
        with pytest.raises(RegistryError, match="could not be reached"):
            client.get_version("s")

    def test_compatibility_level_prefers_the_subject_config(self, rest):
        import httpx

        client, seen = rest(lambda r: httpx.Response(200, json={"compatibilityLevel": "FULL"}))
        assert client.compatibility_level("orders-value") == "FULL"
        assert seen[0].url.path == "/config/orders-value"
        assert dict(seen[0].url.params) == {"defaultToGlobal": "true"}

    def test_compatibility_level_falls_back_to_the_global_config(self, rest):
        import httpx

        def answer(request):
            if request.url.path == "/config/orders-value":
                return httpx.Response(404, json={"error_code": 40408, "message": "none"})
            return httpx.Response(200, json={"compatibility": "NONE"})

        client, seen = rest(answer)
        assert client.compatibility_level("orders-value") == "NONE"
        assert [r.url.path for r in seen] == ["/config/orders-value", "/config"]

    def test_compatibility_level_none_when_nothing_answers(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(404, json={"error_code": 40401, "message": "m"}))
        assert client.compatibility_level("s") is None

    def test_compatibility_level_other_errors_propagate(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(500, json={"error_code": 50001, "message": "m"}))
        with pytest.raises(RegistryError):
            client.compatibility_level("s")

    def test_subjects(self, rest):
        import httpx

        client, seen = rest(lambda r: httpx.Response(200, json=["a-value", "b-value"]))
        assert client.subjects() == ["a-value", "b-value"]
        assert seen[0].url.path == "/subjects"

    def test_context_manager_closes_the_client(self, rest):
        import httpx

        client, _ = rest(lambda r: httpx.Response(200, json=[]))
        with client as opened:
            opened.subjects()
            http = opened._client
        assert http.is_closed and client._client is None


class TestGlueRegistryClient:
    def test_latest_and_numbered_version_requests(self):
        from fluid_build.schema_registry.clients import GlueRegistry

        calls = []

        class Boto:
            def get_schema_version(self, **kwargs):
                calls.append(kwargs)
                return {
                    "VersionNumber": 2,
                    "DataFormat": "JSON",
                    "SchemaDefinition": "{}",
                    "SchemaVersionId": "id-1",
                    "Status": "AVAILABLE",
                }

        registry = GlueRegistry(client=Boto())
        sv = registry.get_version("r", "s")
        registry.get_version("r", "s", 2)
        assert calls[0] == {
            "SchemaId": {"RegistryName": "r", "SchemaName": "s"},
            "SchemaVersionNumber": {"LatestVersion": True},
        }
        assert calls[1]["SchemaVersionNumber"] == {"VersionNumber": 2}
        assert (sv.version, sv.data_format, sv.schema_id) == (2, "JSON", "id-1")

    def test_a_pending_version_is_refused(self):
        from fluid_build.schema_registry.clients import GlueRegistry

        class Boto:
            def get_schema_version(self, **kwargs):
                return {"VersionNumber": 2, "SchemaDefinition": "{}", "Status": "PENDING"}

        with pytest.raises(RegistryError, match="PENDING"):
            GlueRegistry(client=Boto()).get_version("r", "s")

    def test_a_client_error_becomes_a_registry_error_with_its_code(self):
        from fluid_build.schema_registry.clients import GlueRegistry

        class Denied(Exception):
            response = {"Error": {"Code": "AccessDeniedException", "Message": "no glue:Get"}}

        class Boto:
            def get_schema_version(self, **kwargs):
                raise Denied()

        with pytest.raises(RegistryError) as caught:
            GlueRegistry(client=Boto()).get_version("r", "s")
        assert caught.value.code == "AccessDeniedException"
        assert "no glue:Get" in str(caught.value)

    def test_schemas_paginates(self):
        from fluid_build.schema_registry.clients import GlueRegistry

        pages = [
            {"Schemas": [{"SchemaName": "a"}], "NextToken": "t"},
            {"Schemas": [{"SchemaName": "b"}]},
        ]
        seen = []

        class Boto:
            def list_schemas(self, **kwargs):
                seen.append(kwargs)
                return pages.pop(0)

        assert GlueRegistry(client=Boto()).schemas("r") == ["a", "b"]
        assert seen[1]["NextToken"] == "t"


class TestProducedOnlyWarnings:
    def test_an_unset_endpoint_warning_reaches_the_user_for_a_produced_only_contract(
        self, monkeypatch
    ):
        import fluid_build.cli.console as console

        seen = []
        monkeypatch.setattr(console, "warning", lambda message: seen.append(message))
        env = {
            "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
            GRANTS: "SCHEMA_REGISTRY_API_SECRET@sr.example.com",
        }
        attach_registry_checks(
            {}, produced_contract({"kind": "confluent", "url": URL}), environ=env
        )
        assert any("SCHEMA_REGISTRY_REST_ENDPOINT" in m for m in seen)


class TestFlinkOnlyIsNotChecked:
    def contract(self):
        contract = consumed_contract({"kind": "glue"})
        stream = contract["builds"][0]["properties"]["stream"]
        del stream["registry"]
        contract["builds"][0]["properties"]["parameters"] = {
            "flink": {"source": {"schemaRegistryUrl": "https://registry.example"}}
        }
        return contract

    def test_resolved_but_not_checked(self):
        from fluid_build.schema_registry.reference import checked_subjects, consumed_subjects

        contract = self.contract()
        assert len(consumed_subjects(contract)) == 1
        assert checked_subjects(contract) == []

    def test_verify_consumed_never_calls_a_client(self):
        assert run(self.contract()) == ([], [], [])

    def test_validation_findings_ignore_it(self):
        from fluid_build.schema_registry.policy import validation_findings

        contract = self.contract()
        contract["builds"][0]["properties"]["parameters"]["flink"]["source"][
            "schemaRegistryUrl"
        ] = "http://169.254.169.254"
        assert validation_findings(contract, environ={}) == ([], [])

    def test_attach_leaves_the_plan_alone(self):
        assert attach_registry_checks({"a": 1}, self.contract(), environ={}) == {"a": 1}
