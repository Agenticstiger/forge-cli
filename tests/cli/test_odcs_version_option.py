# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--odcs-version``: every door that writes ODCS takes the version, and v3.1.0 stays the default.

``fluid odcs export``, ``fluid generate standard --format odcs`` and ``fluid
datamesh-manager publish --with-contract`` write ODCS documents. Each takes
``--odcs-version 3.1.0|3.2.0``; none changes what it wrote before unless asked, and the
documents the artifact validator reads are held to the schema of the version they declare.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli._common import CLIError
from fluid_build.providers.odcs.provider import OdcsProvider
from tests.test_schema_076_preview_additions import _base_contract

pytestmark = pytest.mark.unit

ICEBERG = {
    "platform": "aws",
    "format": "iceberg",
    "location": {
        "database": "sales",
        "table": "orders",
        "bucket": "acme-lake",
        "path": "gold/orders/",
        "region": "eu-north-1",
    },
}


def _contract() -> Dict[str, Any]:
    contract = _base_contract("0.7.7")
    contract["exposes"][0]["binding"] = copy.deepcopy(ICEBERG)
    return contract


@pytest.fixture
def contract_file(tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(_contract()), encoding="utf-8")
    return path


def _main(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _load(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── fluid odcs export ───────────────────────────────────────────────────


def test_odcs_export_writes_v3_1_0_unless_asked(tmp_path: Path, contract_file: Path) -> None:
    out = tmp_path / "out.yaml"

    assert _main("odcs", "export", str(contract_file), "-o", str(out)) == 0

    doc = _load(out)
    assert doc["apiVersion"] == "v3.1.0" and doc["servers"][0]["type"] == "s3"


def test_odcs_export_writes_v3_2_0_with_an_iceberg_server_when_asked(
    tmp_path: Path, contract_file: Path
) -> None:
    out = tmp_path / "out.yaml"

    assert (
        _main("odcs", "export", str(contract_file), "-o", str(out), "--odcs-version", "3.2.0") == 0
    )

    doc = _load(out)
    assert doc["apiVersion"] == "v3.2.0"
    assert doc["servers"][0]["type"] == "iceberg"
    assert doc["servers"][0]["catalogUrl"] == "https://glue.eu-north-1.amazonaws.com/iceberg"


def test_an_unsupported_version_is_the_typed_error_and_nothing_is_written(
    tmp_path: Path, contract_file: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "out.yaml"

    rc = _main("odcs", "export", str(contract_file), "-o", str(out), "--odcs-version", "4.0.0")

    text = " ".join(capsys.readouterr().out.split())
    assert rc == 2
    assert "ERR_ODCS_VERSION_UNSUPPORTED" in text and "supported: 3.1.0, 3.2.0" in text
    assert not out.exists()


def test_odcs_info_says_which_versions_it_writes(capsys: pytest.CaptureFixture) -> None:
    assert _main("odcs", "info") == 0

    out = capsys.readouterr().out
    assert "Version: v3.1.0 (default)" in out and "Writes: v3.1.0, v3.2.0" in out


# ── fluid odcs validate ─────────────────────────────────────────────────


def test_odcs_validate_holds_a_document_to_the_schema_of_its_declared_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    v32 = OdcsProvider(version="3.2.0").render(_contract())
    ok, wrong = tmp_path / "v32.odcs.yaml", tmp_path / "v31.odcs.yaml"
    ok.write_text(yaml.safe_dump(v32), encoding="utf-8")
    wrong.write_text(yaml.safe_dump({**v32, "apiVersion": "v3.1.0"}), encoding="utf-8")

    assert _main("odcs", "validate", str(ok), "--no-vowl") == 0
    assert "jsonschema: clean" in capsys.readouterr().out
    assert _main("odcs", "validate", str(wrong), "--no-vowl") == 1


def test_odcs_validate_round_trips_a_v3_2_0_document_at_its_own_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    doc = tmp_path / "v32.odcs.yaml"
    doc.write_text(
        yaml.safe_dump(OdcsProvider(version="3.2.0").render(_contract())), encoding="utf-8"
    )

    assert _main("odcs", "validate", str(doc), "--no-vowl", "--roundtrip") == 0
    assert "roundtrip: lossless" in capsys.readouterr().out


# ── fluid odcs export, through click ────────────────────────────────────


def _click_export(*argv: str) -> Any:
    from click.testing import CliRunner

    from fluid_build.cli.odcs import odcs_cli

    return CliRunner().invoke(odcs_cli, ["export", *argv])


def test_the_click_export_writes_v3_1_0_unless_asked(tmp_path: Path, contract_file: Path) -> None:
    out = tmp_path / "out.yaml"

    result = _click_export(str(contract_file), "-o", str(out))

    assert result.exit_code == 0, result.output
    assert _load(out)["apiVersion"] == "v3.1.0"
    assert "Exporting to ODCS v3.1.0" in result.output


def test_the_click_export_writes_v3_2_0_when_asked(tmp_path: Path, contract_file: Path) -> None:
    out = tmp_path / "out.yaml"

    result = _click_export(str(contract_file), "-o", str(out), "--odcs-version", "3.2.0")

    doc = _load(out)
    assert result.exit_code == 0, result.output
    assert doc["apiVersion"] == "v3.2.0" and doc["servers"][0]["type"] == "iceberg"


def test_the_click_export_takes_the_version_for_every_port(
    tmp_path: Path, contract_file: Path
) -> None:
    out = tmp_path / "ports" / "product.odcs.yaml"

    result = _click_export(
        str(contract_file), "--per-port", "-o", str(out), "--odcs-version", "v3.2.0"
    )

    [written] = sorted(out.parent.glob("product.odcs.*.yaml"))
    assert result.exit_code == 0, result.output
    assert _load(written)["apiVersion"] == "v3.2.0"


def test_the_click_export_refuses_an_unsupported_version_with_the_supported_ones(
    tmp_path: Path, contract_file: Path
) -> None:
    out = tmp_path / "out.yaml"

    result = _click_export(str(contract_file), "-o", str(out), "--odcs-version", "4.0.0")

    text = " ".join(result.output.split())
    assert result.exit_code == 2
    assert "--odcs-version" in text and "supported: 3.1.0, 3.2.0" in text
    assert "Traceback" not in text
    assert not out.exists()


# ── fluid generate standard --format odcs ───────────────────────────────


def test_generate_standard_writes_v3_1_0_unless_asked(
    tmp_path: Path, contract_file: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "ports"
    out.mkdir()

    assert (
        _main("generate", "standard", str(contract_file), "--format", "odcs", "--out", str(out))
        == 0
    )

    [written] = sorted(out.glob("product.odcs.*.yaml"))
    assert _load(written)["apiVersion"] == "v3.1.0"
    assert "ODCS v3.1.0 doc(s)" in capsys.readouterr().out


def test_generate_standard_writes_v3_2_0_when_asked(
    tmp_path: Path, contract_file: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "ports"
    out.mkdir()

    rc = _main(
        "generate",
        "standard",
        str(contract_file),
        "--format",
        "odcs",
        "--odcs-version",
        "v3.2.0",
        "--out",
        str(out),
    )

    [written] = sorted(out.glob("product.odcs.*.yaml"))
    doc = _load(written)
    assert rc == 0 and doc["apiVersion"] == "v3.2.0" and doc["servers"][0]["type"] == "iceberg"
    assert "ODCS v3.2.0 doc(s)" in capsys.readouterr().out


def test_the_version_is_for_the_odcs_format_only(
    tmp_path: Path, contract_file: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = _main(
        "generate",
        "standard",
        str(contract_file),
        "--format",
        "odps",
        "--odcs-version",
        "3.2.0",
        "--out",
        str(tmp_path / "p.yaml"),
    )

    assert rc == 1
    assert "--odcs-version applies to --format odcs only" in capsys.readouterr().out
    assert not (tmp_path / "p.yaml").exists()


def test_generate_standard_refuses_an_unsupported_version_as_the_typed_error(
    tmp_path: Path, contract_file: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = _main(
        "generate",
        "standard",
        str(contract_file),
        "--format",
        "odcs",
        "--odcs-version",
        "9.9.9",
        "--out",
        str(tmp_path),
    )

    assert rc == 2 and "ERR_ODCS_VERSION_UNSUPPORTED" in capsys.readouterr().out


# ── fluid datamesh-manager publish ──────────────────────────────────────


def test_the_publish_command_takes_the_version_and_passes_it_on() -> None:
    from fluid_build.cli import build_parser
    from fluid_build.cli.datamesh_manager import _publish_kwargs

    parser = build_parser()
    plain = parser.parse_args(["datamesh-manager", "publish", "c.fluid.yaml"])
    asked = parser.parse_args(
        [
            "datamesh-manager",
            "publish",
            "c.fluid.yaml",
            "--with-contract",
            "--odcs-version",
            "3.2.0",
        ]
    )

    assert plain.odcs_version is None and _publish_kwargs(plain)["odcs_version"] is None
    assert _publish_kwargs(asked)["odcs_version"] == "v3.2.0"


def test_an_unsupported_version_fails_a_publish_before_anything_is_read(tmp_path: Path) -> None:
    from fluid_build.cli.datamesh_manager import _cmd_publish

    args = argparse.Namespace(
        contract=str(tmp_path / "no-such-contract.fluid.yaml"), odcs_version="3.3.0"
    )

    with pytest.raises(CLIError) as raised:
        _cmd_publish(args)

    assert raised.value.event == "odcs_version_unsupported" and raised.value.exit_code == 2


def _dmm_previews(version: Any) -> Any:
    from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

    provider = DataMeshManagerProvider(api_key="k", api_url="https://api.example.test")
    kwargs = {} if version is None else {"odcs_version": version}
    return provider._preview_odcs_per_expose(_contract(), "gold.sales.orders", **kwargs)


def test_a_publish_stays_on_v3_1_0_by_default() -> None:
    from fluid_build.providers.datamesh_manager._publish_flow import DMM_ODCS_VERSION

    [preview] = _dmm_previews(None)

    assert DMM_ODCS_VERSION == "v3.1.0"
    assert preview["payload"]["apiVersion"] == "v3.1.0"
    assert preview["payload"]["servers"][0]["type"] == "s3"


def test_a_publish_asked_for_v3_2_0_sends_it() -> None:
    [preview] = _dmm_previews("v3.2.0")

    assert preview["payload"]["apiVersion"] == "v3.2.0"
    assert preview["payload"]["servers"][0]["type"] == "iceberg"


# ── The artifact validator ──────────────────────────────────────────────


def _issues(document: Dict[str, Any]) -> Any:
    from fluid_build.forge.core.artifact_validators import validate_odcs

    content = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
    return [
        i for i in validate_odcs("odcs/product.odcs.orders.yaml", content) if i.severity == "error"
    ]


def test_an_artifact_is_held_to_the_schema_of_the_version_it_declares() -> None:
    v32 = OdcsProvider(version="3.2.0").render(_contract())
    v31 = OdcsProvider().render(_contract())

    assert _issues(v32) == []
    assert _issues(v31) == []
    assert _issues({**v32, "apiVersion": "v3.1.0"}) != []  # an iceberg server is not a v3.1.0 one


def _issues_at(path: str, content: str) -> Any:
    from fluid_build.forge.core.artifact_validators import validate_odcs

    return [i for i in validate_odcs(path, content.encode("utf-8")) if i.severity == "error"]


def test_an_artifact_is_held_to_its_declared_version_whatever_the_syntax() -> None:
    import json

    v32 = OdcsProvider(version="3.2.0").render(_contract())
    older = {**v32, "apiVersion": "v3.1.0"}

    for document, expect_clean in ((v32, True), (older, False)):
        as_json = json.dumps(document, indent=2)
        compact = json.dumps(document, separators=(",", ":"))
        flow = yaml.safe_dump(document, default_flow_style=True, width=10_000)
        quoted = yaml.safe_dump(document, default_style='"')
        for path, content in (
            ("odcs/product.odcs.orders.json", as_json),
            ("odcs/product.odcs.orders.json", compact),
            ("odcs/product.odcs.orders.yaml", flow),
            ("odcs/product.odcs.orders.yaml", quoted),
            ("odcs/product.odcs.orders.yaml", as_json),  # JSON is YAML, whatever the suffix
        ):
            assert (_issues_at(path, content) == []) is expect_clean, (path, content[:60])


def test_an_artifact_is_parsed_once_whatever_version_it_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.util import safe_yaml

    parsed = []
    real = safe_yaml.load_yaml_safe

    def counting(content: Any, **kwargs: Any) -> Any:
        parsed.append(1)
        return real(content, **kwargs)

    monkeypatch.setattr(safe_yaml, "load_yaml_safe", counting)

    assert _issues(OdcsProvider(version="3.2.0").render(_contract())) == []
    assert len(parsed) == 1


def test_an_artifact_that_does_not_parse_is_reported_as_it_always_was() -> None:
    [issue] = _issues_at("odcs/product.odcs.orders.yaml", "apiVersion: [v3.2.0\n")

    assert "parse failure" in issue.message


def test_an_artifact_that_declares_no_version_is_held_to_v3_1_0() -> None:
    v31 = OdcsProvider().render(_contract())
    without = {k: v for k, v in v31.items() if k != "apiVersion"}

    assert any("apiVersion" in i.message for i in _issues(without))


# ── The registry ────────────────────────────────────────────────────────


def test_the_exporter_registry_says_both_versions() -> None:
    from fluid_build.providers._exporters import list_exporters

    [odcs] = [e for e in list_exporters() if e.name == "odcs"]

    assert "v3.1.0 (default)" in odcs.spec and "v3.2.0" in odcs.spec
