# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import schema-registry``: the flags, and the contract a subject becomes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli import build_parser
from fluid_build.cli.import_cmd import run
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.schema_registry import clients
from fluid_build.schema_registry.clients import RegistryError, SubjectVersion
from fluid_build.schema_registry.schemas import avro_schema, json_schema, schema_text

pytestmark = pytest.mark.unit

COLUMNS = [
    {"name": "order_id", "type": "string", "required": True},
    {"name": "amount", "type": "long", "required": False},
]
URL = "https://sr.example.com"
LOG = logging.getLogger("test_import_schema_registry")


def parse(*argv: str) -> Any:
    return build_parser().parse_args(["import", *argv])


class FakeRegistry:
    """Stands in for ConfluentRegistry and GlueRegistry; records how it was built."""

    built: List[Any] = []
    versions: List[Any] = []
    data_format = "AVRO"
    text = ""
    error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).built.append((args, kwargs))
        self.closed = False

    def get_version(self, *args: Any) -> SubjectVersion:
        type(self).versions.append(args)
        if type(self).error:
            raise type(self).error
        number = args[-1]
        return SubjectVersion(
            "orders-value",
            7 if number == "latest" else int(number),
            type(self).data_format,
            type(self).text,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    class Fake(FakeRegistry):
        built: List[Any] = []
        versions: List[Any] = []
        data_format = "AVRO"
        text = schema_text(avro_schema(COLUMNS, name="orders"))
        error = None

    monkeypatch.setattr(clients, "ConfluentRegistry", Fake)
    monkeypatch.setattr(clients, "GlueRegistry", Fake)
    return Fake


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def validate(path: Path):
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return FluidSchemaManager().validate_contract(
        contract, schema_version="0.7.7", offline_only=True
    )


class TestParser:
    def test_the_tool_and_flags_parse(self):
        args = parse(
            "schema-registry",
            "orders-value",
            "--registry",
            "confluent",
            "--url",
            URL,
            "--version",
            "3",
        )
        assert (args.tool, args.source, args.registry, args.url, args.registry_version) == (
            "schema-registry",
            "orders-value",
            "confluent",
            URL,
            3,
        )
        glue = parse(
            "schema-registry",
            "orders-value",
            "--registry",
            "glue",
            "--registry-name",
            "r",
        )
        assert (glue.registry_name, glue.registry_version) == ("r", None)

    def test_the_flags_default_to_none(self):
        args = parse("dbt", "x")
        assert [getattr(args, d) for d in ("registry", "url", "registry_name")] == [None] * 3

    @pytest.mark.parametrize("version", ["0", "-1", "latest"])
    def test_a_bad_version_is_refused(self, version, capsys):
        with pytest.raises(SystemExit) as exc:
            parse("schema-registry", "s", "--registry", "glue", "--version", version)
        assert exc.value.code != 0

    def test_an_unknown_registry_is_refused(self):
        with pytest.raises(SystemExit):
            parse("schema-registry", "s", "--registry", "apicurio")

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["schema-registry", "s", "--url", URL], "requires --registry"),
            (["schema-registry", "--registry", "glue"], "requires the subject"),
            (["schema-registry", "s", "--registry", "confluent"], "requires --url"),
            (
                [
                    "schema-registry",
                    "s",
                    "--registry",
                    "confluent",
                    "--url",
                    URL,
                    "--registry-name",
                    "r",
                ],
                "--registry-name does not apply with --registry confluent",
            ),
            (
                ["schema-registry", "s", "--registry", "glue", "--url", URL],
                "--url does not apply with --registry glue",
            ),
            (["dbt", "x", "--registry", "glue"], "--registry only applies to"),
            (["meltano", "x", "--url", URL, "--version", "2"], "--url, --version only applies"),
            (["--registry-name", "r"], "--registry-name only applies to"),
        ],
    )
    def test_refused_combinations(self, argv, message, fake):
        with pytest.raises(FluidUserError) as exc:
            run(parse(*argv), LOG)
        assert message in exc.value.what
        assert fake.built == []

    def test_refusals_exit_non_zero_through_main(self, capsys):
        from fluid_build.cli import main

        code = main(["import", "schema-registry", "s", "--registry", "glue", "--url", URL])
        assert code != 0
        assert "--url does not apply with --registry glue" in capsys.readouterr().err

    def test_help_builds_and_names_the_tool(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["import", "--help"])
        out = capsys.readouterr().out
        assert "schema-registry" in out and "--registry-name" in out and "AWS_REGION" in out


class TestConfluent:
    def test_an_avro_subject_writes_a_valid_consumer_contract(self, fake, capsys):
        assert (
            run(
                parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL),
                LOG,
            )
            == 0
        )
        path = Path("contract.orders.fluid.yaml")
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        stream = contract["builds"][0]["properties"]["stream"]
        assert stream == {
            "kind": "kafka",
            "topic": "orders",
            "format": "avro-confluent",
            "schema": COLUMNS,
            "registry": {"kind": "confluent", "url": URL, "version": 7},
        }
        assert contract["id"] == "orders" and contract["fluidVersion"] == "0.7.7"
        assert validate(path).is_valid
        assert fake.versions == [("orders-value", "latest")]
        assert fake.built == [((URL,), {})]
        out = capsys.readouterr().out
        assert "contract.orders.fluid.yaml" in out
        assert "topic orders" in out and "schema version 7 read (2 columns" in out

    def test_fluid_validate_accepts_it_offline(self, fake):
        from fluid_build.cli import main

        run(parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL), LOG)
        assert main(["validate", "contract.orders.fluid.yaml", "--offline"]) == 0

    def test_version_n_pins_the_stream(self, fake):
        run(
            parse(
                "schema-registry",
                "orders-value",
                "--registry",
                "confluent",
                "--url",
                URL,
                "--version",
                "3",
            ),
            LOG,
        )
        contract = yaml.safe_load(Path("contract.orders.fluid.yaml").read_text(encoding="utf-8"))
        assert contract["builds"][0]["properties"]["stream"]["registry"]["version"] == 3
        assert fake.versions == [("orders-value", 3)]
        assert validate(Path("contract.orders.fluid.yaml")).is_valid

    def test_out_is_honoured(self, fake):
        run(
            parse(
                "schema-registry",
                "orders-value",
                "--registry",
                "confluent",
                "--url",
                URL,
                "--out",
                "sub/dir/c.fluid.yaml",
            ),
            LOG,
        )
        assert not Path("contract.orders.fluid.yaml").exists()
        assert validate(Path("sub/dir/c.fluid.yaml")).is_valid

    def test_an_existing_file_is_overwritten_like_the_other_importers(self, fake):
        Path("contract.orders.fluid.yaml").write_text("stale", encoding="utf-8")
        run(parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL), LOG)
        assert "fluidVersion" in Path("contract.orders.fluid.yaml").read_text(encoding="utf-8")

    def test_a_json_schema_subject_imports_as_json(self, fake):
        fake.data_format = "JSON"
        fake.text = schema_text(json_schema(COLUMNS, title="orders"))
        run(parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL), LOG)
        path = Path("contract.orders.fluid.yaml")
        stream = yaml.safe_load(path.read_text(encoding="utf-8"))["builds"][0]["properties"][
            "stream"
        ]
        assert stream["format"] == "json" and validate(path).is_valid

    def test_the_topic_is_sanitised_into_the_id(self, fake):
        run(parse("schema-registry", "Orders.V1_Raw", "--registry", "confluent", "--url", URL), LOG)
        path = Path("contract.orders-v1-raw.fluid.yaml")
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["id"] == "orders-v1-raw"
        assert validate(path).is_valid

    def test_a_protobuf_subject_is_refused(self, fake):
        fake.data_format = "PROTOBUF"
        fake.text = 'syntax = "proto3"; message O { string id = 1; }'
        with pytest.raises(FluidUserError) as exc:
            run(
                parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL),
                LOG,
            )
        assert "PROTOBUF" in exc.value.what and "orders-value" in exc.value.what
        assert "no consumed stream format for Protobuf" in exc.value.why
        assert not Path("contract.orders.fluid.yaml").exists()

    def test_an_unsupported_schema_names_the_subject_and_version(self, fake):
        fake.text = schema_text(
            {"type": "record", "name": "o", "fields": [{"name": "x", "type": ["string", "long"]}]}
        )
        with pytest.raises(FluidUserError) as exc:
            run(
                parse(
                    "schema-registry",
                    "orders-value",
                    "--registry",
                    "confluent",
                    "--url",
                    URL,
                    "--version",
                    "4",
                ),
                LOG,
            )
        assert "orders-value" in exc.value.what and "version 4" in exc.value.what
        assert "union" in exc.value.why
        assert not Path("contract.orders.fluid.yaml").exists()

    def test_the_registrys_own_refusal_is_surfaced(self, fake):
        fake.error = RegistryError("HTTP 401: Unauthorized", code=401)
        with pytest.raises(FluidUserError) as exc:
            run(
                parse("schema-registry", "orders-value", "--registry", "confluent", "--url", URL),
                LOG,
            )
        assert "HTTP 401: Unauthorized" in exc.value.why
        assert "sends no credential" in exc.value.fix

    def test_a_refused_host_is_refused_before_any_client(self, fake):
        with pytest.raises(FluidUserError) as exc:
            run(
                parse(
                    "schema-registry",
                    "orders-value",
                    "--registry",
                    "confluent",
                    "--url",
                    "https://169.254.169.254",
                ),
                LOG,
            )
        assert exc.value.why
        assert fake.built == [] and fake.versions == []

    def test_a_malformed_url_is_refused_before_any_client(self, fake):
        with pytest.raises(FluidUserError):
            run(
                parse(
                    "schema-registry", "orders-value", "--registry", "confluent", "--url", "ftp://x"
                ),
                LOG,
            )
        assert fake.built == []


class TestGlue:
    def test_a_glue_subject_writes_a_valid_contract(self, fake):
        run(
            parse(
                "schema-registry",
                "orders-value",
                "--registry",
                "glue",
                "--registry-name",
                "commerce",
                "--version",
                "2",
            ),
            LOG,
        )
        path = Path("contract.orders.fluid.yaml")
        stream = yaml.safe_load(path.read_text(encoding="utf-8"))["builds"][0]["properties"][
            "stream"
        ]
        assert stream["registry"] == {
            "kind": "glue",
            "registryName": "commerce",
            "version": 2,
        }
        assert fake.versions == [("commerce", "orders-value", 2)]
        assert fake.built == [((), {"region": None})]
        assert validate(path).is_valid

    def test_the_default_registry_is_the_sdks_and_latest_is_pinned(self, fake):
        run(parse("schema-registry", "orders-value", "--registry", "glue"), LOG)
        stream = yaml.safe_load(Path("contract.orders.fluid.yaml").read_text(encoding="utf-8"))[
            "builds"
        ][0]["properties"]["stream"]
        assert stream["registry"] == {"kind": "glue", "version": 7}
        assert fake.built == [((), {"region": None})]

    def test_a_protobuf_subject_is_refused(self, fake):
        fake.data_format = "PROTOBUF"
        fake.text = "message O {}"
        with pytest.raises(FluidUserError):
            run(parse("schema-registry", "orders-value", "--registry", "glue"), LOG)


# --------------------------------------------- the Glue region (live finding, 2026-11-17)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"AWS_REGION": "us-west-2"}, "us-west-2"),
        ({"AWS_DEFAULT_REGION": "eu-west-1"}, "eu-west-1"),
        ({"AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "eu-west-1"}, "us-west-2"),
        ({}, None),
    ],
)
def test_a_glue_read_without_a_contract_takes_the_environments_region(monkeypatch, env, expected):
    # boto3 1.x reads only AWS_DEFAULT_REGION: with AWS_REGION alone, an import read the
    # profile's region (us-east-1) and found nothing. The kind passes the region itself.
    from fluid_build.schema_registry.kinds import get_kind
    from fluid_build.schema_registry.reference import RegistryRef

    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    ref = RegistryRef(kind="glue", origin="import", where="import", subject="orders-value")
    assert get_kind("glue").client(ref).region == expected
