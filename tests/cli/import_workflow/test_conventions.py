# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The conventions file: an organisation's layout as data, read strictly."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fluid_build._errors import FluidUserError
from fluid_build.cli.import_workflow import _conventions as C
from fluid_build.cli.import_workflow._spark_io import HelperSpec

FULL = """
version: 1
script_roots:
  "s3://code/jobs/": jobs
  "s3://code/jobs/special/": special
paths:
  - prefix: "s3://lake/curated/"
    database: curated
    platform: aws
    format: iceberg
  - prefix: "s3://lake/curated/fixed/"
    table: one_table
helpers:
  read_table: {reads: table, arg: 0}
  write_table: {writes: table, arg: 1, format: Iceberg}
  read_topic: {reads: topic, arg: topic}
operators:
  AcmeSparkOperator: spark_submit
session_names: [spark, session]
config_modules:
  - {module: acme_config.settings, path: libs/settings.py}
environment: prod
environments:
  prod: {ENV: prod, N: 3}
  dev: {ENV: dev}
defaults: {database: Retail, domain: retail, platform: aws}
catalogs:
  Glue: {platform: aws, format: iceberg, catalog: glue}
layers: {raw: bronze, curated: Silver, marts: GOLD}
owners:
  data-eng: {team: data-engineering, email: data-eng@example.com, slack: "#data-eng"}
ignore:
  tables: ["^tmp_", "_staging$"]
  paths: ["^s3://lake/tmp/"]
  files: ["jobs/legacy/*"]
products: {id: "{catalog}.{database}.{table}"}
"""


def write(tmp_path: Path, text: str, name: str = "conventions.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def refusal(tmp_path: Path, text: str) -> FluidUserError:
    with pytest.raises(FluidUserError) as refused:
        C.load_conventions(write(tmp_path, text))
    return refused.value


def test_the_empty_conventions_and_an_empty_file(tmp_path):
    assert C.load_conventions(None) == C.Conventions()
    empty = C.load_conventions(write(tmp_path, ""))
    assert empty.helpers == {} and empty.id_template == "{database}.{table}"


def test_a_full_file(tmp_path):
    conv = C.load_conventions(write(tmp_path, FULL))
    assert conv.source == (tmp_path / "conventions.yaml").resolve()
    # longest prefix first; local paths are relative to the file
    assert [p for p, _ in conv.script_roots] == ["s3://code/jobs/special/", "s3://code/jobs/"]
    assert conv.script_roots[1][1] == (tmp_path / "jobs").resolve()
    assert [r.prefix for r in conv.paths] == ["s3://lake/curated/fixed/", "s3://lake/curated/"]
    assert conv.paths[1] == C.PathRule(
        prefix="s3://lake/curated/", database="curated", platform="aws", format="iceberg"
    )
    assert conv.helpers == {
        "read_table": HelperSpec("reads", "table", 0),
        "write_table": HelperSpec("writes", "table", 1, "iceberg"),
        "read_topic": HelperSpec("reads", "topic", "topic"),
    }
    assert conv.operators == {"AcmeSparkOperator": "spark_submit"}
    assert conv.session_names == ("spark", "session")
    assert conv.config_modules == {
        "acme_config.settings": (tmp_path / "libs/settings.py").resolve()
    }
    assert (conv.environment, dict(conv.variables)) == ("prod", {"ENV": "prod", "N": "3"})
    assert (conv.default_database, conv.domain, conv.runtime_platform) == (
        "retail",
        "retail",
        "aws",
    )
    assert conv.catalogs == {"glue": C.CatalogRule("aws", "iceberg", "glue")}
    assert conv.layers == {"raw": "Bronze", "curated": "Silver", "marts": "Gold"}
    assert conv.owners["data-eng"]["team"] == "data-engineering"
    assert conv.ignore_files == ("jobs/legacy/*",)
    assert conv.id_template == "{catalog}.{database}.{table}"
    assert conv.ignores_table(["retail", "tmp_scratch"]) and conv.ignores_table(
        ["x", "orders_staging"]
    )
    assert not conv.ignores_table(["retail", "orders"]) and not conv.ignores_table([])
    assert conv.ignores_path("s3://lake/tmp/x") and not conv.ignores_path("s3://lake/final/x")


@pytest.mark.parametrize(
    "text, expect",
    [
        ("bogus: 1", "unknown key"),
        ("script_roots: [a]", "expected a mapping"),
        ("version: 2", "is not a version"),
        ("paths: [{database: x}]", "the prefix is required"),
        ("paths: [{prefix: p, nope: 1}]", "unknown key"),
        ("paths: [{prefix: p, platform: mars}]", "is not one of"),
        ("paths: [{prefix: p, format: nonsense}]", "is not one of"),
        ("paths: [{prefix: p, database: 'a b'}]", "not an acceptable name"),
        ("helpers: {f: {reads: table, writes: table}}", "exactly one of reads and writes"),
        ("helpers: {f: {arg: 0}}", "exactly one of reads and writes"),
        ("helpers: {f: {reads: view}}", "expected table, path or topic"),
        ("helpers: {f: {reads: table, arg: -1}}", "position"),
        ("helpers: {f: {reads: table, arg: true}}", "position"),
        ("helpers: {f: {reads: table, arg: 'not an identifier'}}", "position"),
        ("helpers: {'bad name': {reads: table}}", "not an acceptable name"),
        ("operators: {X: flink}", "is not one of"),
        ("session_names: [a-b]", "not an acceptable name"),
        ("config_modules: [{module: a.b}]", "give both module and path"),
        ("config_modules: [{module: 'a b', path: x}]", "not an acceptable name"),
        ("environment: nope", "is not named under environments"),
        ("environments: {a: {X: 1}, b: {X: 2}}", "name the one to use"),
        ("defaults: {unknown: 1}", "unknown key"),
        ("defaults: {platform: mars}", "is not one of"),
        ("catalogs: {x: {platform: mars}}", "is not one of"),
        ("layers: {raw: platinum}", "expected Bronze, Silver or Gold"),
        ("owners: {x: {}}", "at least a team or an email"),
        ("owners: {x: {team: t, extra: y}}", "unknown key"),
        ("owners: {x: {email: not-an-email}}", "not an email address"),
        ("ignore: {tables: ['(']}", "not a regular expression"),
        ("ignore: {unknown: []}", "unknown key"),
        ("products: {id: '{nope}'}", "may be used"),
        ("products: {id: '{table!r}'}", "may be used"),
        ("products: {id: '{table:>9}'}", "may be used"),
        ("products: {id: '{table'}", "not a template"),
    ],
)
def test_a_bad_file_is_refused_with_the_key_named(tmp_path, text, expect):
    error = refusal(tmp_path, text)
    assert expect in error.why, error.why
    assert error.extras == {"conventions": "conventions.yaml"}
    assert "conventions.yaml" in error.what


def test_a_single_environment_is_selected_without_naming_it(tmp_path):
    conv = C.load_conventions(write(tmp_path, "environments: {only: {ENV: x}}"))
    assert (conv.environment, dict(conv.variables)) == ("only", {"ENV": "x"})


def test_sizes_are_bounded(tmp_path, monkeypatch):
    big = write(tmp_path, "version: 1\n" + "# pad\n" * 10)
    monkeypatch.setattr(C, "MAX_BYTES", 20)
    with pytest.raises(FluidUserError) as refused:
        C.load_conventions(big)
    assert "too large" in refused.value.what
    monkeypatch.setattr(C, "MAX_BYTES", 256 * 1024)
    monkeypatch.setattr(C, "MAX_ENTRIES", 2)
    assert "more than 2 entries" in refusal(tmp_path, "session_names: [a, b, c]").why
    monkeypatch.setattr(C, "MAX_ENTRIES", 500)
    monkeypatch.setattr(C, "MAX_TEXT", 5)
    assert "longer than 5 characters" in refusal(tmp_path, "defaults: {domain: toolongvalue}").why
    monkeypatch.setattr(C, "MAX_TEXT", 500)
    monkeypatch.setattr(C, "MAX_PATTERN", 4)
    assert "longer than 4 characters" in refusal(tmp_path, "ignore: {tables: ['abcdefgh']}").why


def test_a_missing_file_and_a_directory_and_bad_yaml_are_refused(tmp_path):
    with pytest.raises(FluidUserError) as missing:
        C.load_conventions(tmp_path / "nope.yaml")
    assert "cannot read the conventions file" in missing.value.what
    with pytest.raises(FluidUserError) as directory:
        C.load_conventions(tmp_path)
    assert "cannot read" in directory.value.what
    assert "not valid YAML" in refusal(tmp_path, "a: [unclosed").what
    binary = tmp_path / "b.yaml"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(FluidUserError) as bad:
        C.load_conventions(binary)
    assert "not valid YAML" in bad.value.what


def test_an_alias_bomb_is_refused_not_expanded(tmp_path):
    levels = ["l0: &l0 [a, a]"] + [f"l{i}: &l{i} [*l{i-1}, *l{i-1}]" for i in range(1, 25)]
    bomb = "owners:\n  x:\n    team: t\n" + "\n".join(levels)
    assert "not valid YAML" in refusal(tmp_path, bomb).what


def test_the_file_is_data_only(tmp_path):
    marker = tmp_path / "ran"
    text = f"!!python/object/apply:os.system ['touch {marker}']\n"
    with pytest.raises(FluidUserError):
        C.load_conventions(write(tmp_path, text))
    assert not marker.exists()


def test_the_binding_enums_come_from_the_bundled_schema():
    platforms, formats = C._binding_enums()
    assert {"aws", "kafka", "other"} <= platforms and {
        "iceberg",
        "parquet",
        "kafka_topic",
    } <= formats
