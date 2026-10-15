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

"""``fluid publish`` with a catalog endpoint from the checkout's project config,
against a real loopback listener.

The project config only gets credentials it carries itself, on every path
``fluid publish`` takes: a ``--target`` catalog and the catalogs an acquisition
contract lists under ``properties.catalog.register``. An endpoint from the
checkout is also not sent values read from the runner's environment through
the contract's ``{{ env.* }}`` placeholders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

_ENV_KEY = "runner-key-not-a-secret"  # pragma: allowlist secret
_PROJECT_KEY = "project-key-not-a-secret"  # pragma: allowlist secret
_ORG = "0f5e3c1a-telco"
_REPO_URL = (
    "https://ci-token-not-a-secret@git.example.com/team/repo.git"  # pragma: allowlist secret
)
_DB_URL = "postgres://app:db-password-not-a-secret@db.example.com/app"  # pragma: allowlist secret

_CLEARED = (
    "FLUID_CC_ENDPOINT",
    "FLUID_CATALOG_FLUID_CC_URL",
    "FLUID_API_KEY",
    "FLUID_CATALOG_FLUID_CC_TOKEN",
    "FLUID_BEARER_TOKEN",
    "FLUID_CC_ORG_ID",
    "FLUID_SECRETS_FILE",
    "DMM_API_URL",
    "DMM_API_KEY",
    "FLUID_CATALOG_DMM_URL",
    "FLUID_CATALOG_DMM_TOKEN",
    "DMM_AUTO_APPROVE_ACCESS",
)


class _Recorder:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    def carrying(self, *needles: str) -> List[Dict[str, Any]]:
        return [
            r
            for r in self.requests
            if any(n in r["path"] or n in str(r["headers"]) or n in r["raw"] for n in needles)
        ]


@pytest.fixture
def listener() -> Iterator[Tuple[str, _Recorder]]:
    """Any-method HTTP listener on loopback: ``(base_url, recorder)``."""
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            recorder.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "raw": raw.decode("utf-8", "replace"),
                }
            )
            if self.command == "GET" and self.path.startswith("/api/v1/organizations"):
                payload: Any = [{"id": _ORG, "slug": "telco", "name": "Telco"}]
            else:
                payload = {"id": "asset-1", "ok": True}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = _answer  # noqa: N815

        def log_message(self, *_a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    for var in _CLEARED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def _write(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _contract(description: str = "fixture", **extra: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.trust_probe",
        "name": "Trust Probe",
        "version": "1.0.0",
        "description": description,
        "domain": "Customer",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
        "exposes": [
            {
                "exposeId": "orders_raw",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "data/orders.parquet"},
                },
                "contract": {"schema": [{"name": "id", "type": "STRING"}]},
            }
        ],
        **extra,
    }


_ACQUISITION_BUILDS = [
    {
        "id": "ingest_orders",
        "pattern": "acquisition",
        "engine": "duckdb",
        "properties": {"catalog": {"register": ["datamesh_manager"]}},
        "outputs": ["orders_raw"],
    }
]


def _fluid_publish(*argv: str) -> int:
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["publish", *argv])
    return args.func(args, logging.getLogger("test.publish"))


# ── properties.catalog.register ───────────────────────────────────────────


def test_acquisition_register_sends_nothing_to_a_project_endpoint(
    checkout, listener, monkeypatch, capsys
):
    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(
        checkout / "fluid.config.yaml",
        {"catalogs": {"datamesh_manager": {"endpoint": url}}},
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract(builds=_ACQUISITION_BUILDS))

    _fluid_publish(str(contract), "--target", "datamesh_manager")

    assert recorder.carrying(_ENV_KEY) == []
    assert recorder.requests == []
    out = " ".join(capsys.readouterr().out.split())
    assert "acquisition publish bronze.trust_probe/orders_raw → datamesh_manager" in out
    assert "DMM_API_URL" in out


def test_acquisition_register_names_the_refused_target(checkout, listener, monkeypatch):
    from fluid_build.cli._acquisition_stage_ext import publish_acquisition

    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datamesh_manager": {"endpoint": url}}})

    results = publish_acquisition(_contract(builds=_ACQUISITION_BUILDS), checkout)

    assert recorder.requests == []
    (result,) = results
    assert result.succeeded is False
    assert result.target == "datamesh_manager"
    assert url in result.error  # the origin, so the operator sees which endpoint
    assert "fluid.config.yaml" in result.error
    assert _ENV_KEY not in result.error


def test_acquisition_register_still_uses_the_operators_endpoint(checkout, listener, monkeypatch):
    from fluid_build.cli._acquisition_stage_ext import publish_acquisition

    url, recorder = listener
    monkeypatch.setenv("DMM_API_URL", url)
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datamesh_manager": {"timeout": 5}}})

    publish_acquisition(_contract(builds=_ACQUISITION_BUILDS), checkout)

    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, "the operator's own endpoint is still registered with"
    assert all(_ENV_KEY in str(r["headers"]) for r in puts)


_BLANK_CREDENTIALS = [
    pytest.param("dmm", {"auth": {"api_key": ""}}, id="dmm-auth-empty"),
    pytest.param("dmm", {"auth": {"api_key": None}}, id="dmm-auth-null"),
    pytest.param("datamesh_manager", {"api_token": ""}, id="registrar-empty"),
    pytest.param("datamesh_manager", {"api_token": None}, id="registrar-null"),
    pytest.param("datamesh_manager", {"api_token": "   "}, id="registrar-whitespace"),
]


@pytest.mark.parametrize("acquisition", [False, True], ids=["target", "target+register"])
@pytest.mark.parametrize(("name", "blank"), _BLANK_CREDENTIALS)
def test_fluid_publish_sends_no_env_key_when_the_project_blanks_its_credential(
    checkout, listener, monkeypatch, capsys, name, blank, acquisition
):
    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(checkout / "fluid.config.yaml", {"catalogs": {name: {"endpoint": url, **blank}}})
    builds = [dict(_ACQUISITION_BUILDS[0], properties={"catalog": {"register": [name]}})]
    contract = _write(
        checkout / "contract.fluid.yaml", _contract(**({"builds": builds} if acquisition else {}))
    )

    rc = _fluid_publish(str(contract), "--target", name)

    assert recorder.carrying(_ENV_KEY) == []
    assert recorder.requests == []
    assert rc != 0
    out = " ".join(capsys.readouterr().out.split())
    assert "DMM_API_URL" in out
    assert _ENV_KEY not in out


@pytest.mark.parametrize(
    ("name", "own"),
    [
        ("datamesh-manager", {"api_token": _PROJECT_KEY}),
        ("dmm", {"token": _PROJECT_KEY}),
        ("datamesh_manager", {"auth": {"api_key": _PROJECT_KEY}}),
    ],
    ids=["datamesh-manager-api_token", "dmm-token", "registrar-auth.api_key"],
)
def test_fluid_publish_sends_a_self_contained_project_block_its_own_key(
    checkout, listener, monkeypatch, capsys, name, own
):
    # The runner exports DMM_API_KEY; the checkout's block names its endpoint
    # and carries its own key in a slot other than the one the env fills.
    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(checkout / "fluid.config.yaml", {"catalogs": {name: {"endpoint": url, **own}}})
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", name)

    out = " ".join(capsys.readouterr().out.split())
    assert recorder.carrying(_ENV_KEY) == []
    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, out[-600:]
    assert all(_PROJECT_KEY in str(r["headers"]) for r in puts)
    assert rc == 0, out[-600:]


@pytest.mark.parametrize("name", ["datamesh-manager", "datamesh_manager"])
def test_fluid_publish_sends_a_self_contained_block_its_key_beside_the_cc_key(
    checkout, listener, monkeypatch, capsys, name
):
    # The runner exports the Command Center key and the DMM key; the block's
    # own key sits under ``auth``, where pick_token reads it.
    url, recorder = listener
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    _write(
        checkout / "fluid.config.yaml",
        {"catalogs": {name: {"endpoint": url, "auth": {"token": _PROJECT_KEY}}}},
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", name)

    out = " ".join(capsys.readouterr().out.split())
    assert recorder.carrying(_ENV_KEY) == []
    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, out[-600:]
    assert all(_PROJECT_KEY in str(r["headers"]) for r in puts)
    assert rc == 0, out[-600:]


@pytest.mark.parametrize("readme_key", [False, True], ids=["settings-only", "readme-block"])
def test_fluid_publish_sends_a_self_contained_block_beside_the_dmm_readme_settings(
    checkout, listener, monkeypatch, capsys, readme_key
):
    # The operator keeps the provider README's DMM block in ~/.fluid/config.yaml
    # and exports DMM_API_KEY; the checkout names its own DMM and its own key.
    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    operator: Dict[str, Any] = {
        "endpoint": "https://api.entropy-data.example.test",
        "odps_lineage_mode": "contract",
        "auto_approve_access": False,
        "enabled": True,
    }
    if readme_key:
        operator["auth"] = {"api_key": "${DMM_API_KEY}"}
    _write(Path.home() / ".fluid" / "config.yaml", {"catalogs": {"datamesh-manager": operator}})
    _write(
        checkout / "fluid.config.yaml",
        {"catalogs": {"datamesh-manager": {"endpoint": url, "api_token": _PROJECT_KEY}}},
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "datamesh-manager")

    out = " ".join(capsys.readouterr().out.split())
    assert recorder.carrying(_ENV_KEY) == []
    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, out[-600:]
    assert all(r["headers"].get("Authorization") == f"Bearer {_PROJECT_KEY}" for r in puts)
    assert rc == 0, out[-600:]


@pytest.mark.parametrize("dmm_api_key", [None, ""], ids=["unset", "empty"])
def test_fluid_publish_sends_a_self_contained_block_beside_the_readme_block_without_dmm_api_key(
    checkout, listener, monkeypatch, capsys, dmm_api_key
):
    # The README block stays in ~/.fluid/config.yaml, but this shell has no
    # DMM_API_KEY, or CI hands a fork PR an empty secret: the placeholder
    # carries no key, so the checkout's own block publishes with its own.
    url, recorder = listener
    if dmm_api_key is not None:
        monkeypatch.setenv("DMM_API_KEY", dmm_api_key)
    operator: Dict[str, Any] = {
        "endpoint": "https://api.entropy-data.example.test",
        "auth": {"api_key": "${DMM_API_KEY}"},
        "odps_lineage_mode": "contract",
        "auto_approve_access": False,
        "enabled": True,
    }
    _write(Path.home() / ".fluid" / "config.yaml", {"catalogs": {"datamesh-manager": operator}})
    _write(
        checkout / "fluid.config.yaml",
        {"catalogs": {"datamesh-manager": {"endpoint": url, "api_token": _PROJECT_KEY}}},
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "datamesh-manager")

    out = " ".join(capsys.readouterr().out.split())
    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, out[-600:]
    assert all(r["headers"].get("Authorization") == f"Bearer {_PROJECT_KEY}" for r in puts)
    assert recorder.carrying("${DMM_API_KEY}") == []
    assert rc == 0, out[-600:]


def test_fluid_publish_sends_a_self_contained_bearer_block_its_own_token(
    checkout, listener, monkeypatch, capsys
):
    url, recorder = listener
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    _write(
        checkout / "fluid.config.yaml",
        {
            "catalogs": {
                "fluid-command-center": {
                    "endpoint": url,
                    "organization_id": _ORG,
                    "auth": {"type": "bearer", "token": _PROJECT_KEY},
                }
            }
        },
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    out = " ".join(capsys.readouterr().out.split())
    assert recorder.carrying(_ENV_KEY) == []
    assert rc == 0, out[-600:]
    assert recorder.requests
    assert all(
        r["headers"].get("Authorization") == f"Bearer {_PROJECT_KEY}" for r in recorder.requests
    )


@pytest.mark.parametrize(
    ("literal", "token"),
    [("12345", "12345"), ("1.5", "1.5"), ("2024-01-31", "2024-01-31")],
    ids=["int", "float", "date"],
)
def test_fluid_publish_sends_an_unquoted_numeric_operator_token(
    checkout, listener, capsys, literal, token
):
    url, recorder = listener
    path = Path.home() / ".fluid" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "catalogs:\n"
        "  fluid-command-center:\n"
        f"    endpoint: {url}\n"
        f"    organization_id: {_ORG}\n"
        f"    auth:\n      type: bearer\n      token: {literal}\n",
        encoding="utf-8",
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out[-600:]
    sent = [r["headers"].get("Authorization") for r in recorder.requests]
    assert sent and all(v == f"Bearer {token}" for v in sent), sent


def test_fluid_publish_sends_dmm_api_key_for_an_operator_boolean_token(
    checkout, listener, monkeypatch, capsys
):
    # ``api_token: true`` is no key, so DMM_API_KEY applies, never "Bearer True".
    url, recorder = listener
    monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
    path = Path.home() / ".fluid" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"catalogs:\n  datamesh-manager:\n    endpoint: {url}\n    api_token: true\n",
        encoding="utf-8",
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "datamesh-manager")

    out = " ".join(capsys.readouterr().out.split())
    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, out[-600:]
    assert all(r["headers"].get("Authorization") == f"Bearer {_ENV_KEY}" for r in puts)
    assert rc == 0, out[-600:]


def test_fluid_publish_says_why_a_committed_numeric_token_is_not_sent(
    checkout, listener, monkeypatch, capsys, caplog
):
    # The checkout commits an unquoted number as its Command Center token; CI
    # names the endpoint and exports its own bearer token. The number is no
    # key (a project file is strict), the runner's token goes to the
    # operator's endpoint, and the log says why the file's was not sent.
    url, recorder = listener
    monkeypatch.setenv("FLUID_CC_ENDPOINT", url)
    monkeypatch.setenv("FLUID_CC_ORG_ID", _ORG)
    monkeypatch.setenv("FLUID_BEARER_TOKEN", _ENV_KEY)
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n    auth:\n      type: bearer\n      token: 98765\n",
        encoding="utf-8",
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out[-600:]
    sent = [r["headers"].get("Authorization") for r in recorder.requests]
    assert sent and all(v == f"Bearer {_ENV_KEY}" for v in sent), sent
    assert "auth.token in the project config fluid.config.yaml is a int" in caplog.text
    assert "quote it" in caplog.text and "98765" not in caplog.text


def test_fluid_publish_says_why_a_committed_numeric_api_key_is_not_sent(
    checkout, listener, monkeypatch, capsys, caplog
):
    # The same with an api_key: FLUID_API_KEY is written over the number,
    # and the log still says why the file's key is not the one sent.
    url, recorder = listener
    monkeypatch.setenv("FLUID_CC_ENDPOINT", url)
    monkeypatch.setenv("FLUID_CC_ORG_ID", _ORG)
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    (checkout / "fluid.config.yaml").write_text(
        "catalogs:\n  fluid-command-center:\n    auth:\n      type: api_key\n      api_key: 98765\n",
        encoding="utf-8",
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    with caplog.at_level(logging.WARNING, logger="fluid.config"):
        rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0, out[-600:]
    sent = [r["headers"].get("X-API-Key") for r in recorder.requests]
    assert sent and all(v == _ENV_KEY for v in sent), sent
    assert "auth.api_key in the project config fluid.config.yaml is a int" in caplog.text
    assert "quote it" in caplog.text and "98765" not in caplog.text
    assert caplog.text.count("not a string, so it is no credential") == 1


def test_a_registrar_for_a_project_endpoint_sends_no_env_key_set_after_resolution(
    checkout, listener, monkeypatch
):
    # The registrar itself must not fill the key: the environment can change
    # between resolving the config and building the registrar.
    from fluid_build.cli._acquisition_stage_ext import publish_acquisition

    url, recorder = listener
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datamesh_manager": {"endpoint": url}}})
    from fluid_build.cli import _acquisition_stage_ext as stage

    real_collect = stage._collect_target_configs

    def _collect_then_export(contract):
        resolved = real_collect(contract)
        monkeypatch.setenv("DMM_API_KEY", _ENV_KEY)
        return resolved

    monkeypatch.setattr(stage, "_collect_target_configs", _collect_then_export)

    (result,) = publish_acquisition(_contract(builds=_ACQUISITION_BUILDS), checkout)

    assert recorder.carrying(_ENV_KEY) == []
    assert recorder.requests == []
    assert result.succeeded is False
    assert _ENV_KEY not in (result.error or "")


# ── a Command Center credential that is not a non-empty string ────────────
#
# ``api_key: false`` (or ``0``, ``no``, ``[]`` ...) in the checkout's config is
# no credential of its own; before the fix the provider's ``value or
# os.getenv(...)`` fallback sent the runner's key to the project's endpoint.

_NOT_A_CREDENTIAL = ["false", "0", "no", "off", "[]", '!!binary ""', "{}", "true", "12345"]


@pytest.mark.parametrize(
    ("auth_type", "slot", "env"),
    [
        ("api_key", "api_key", "FLUID_API_KEY"),
        ("bearer", "token", "FLUID_BEARER_TOKEN"),
    ],
    ids=["api_key", "bearer"],
)
@pytest.mark.parametrize("literal", _NOT_A_CREDENTIAL)
def test_fluid_publish_sends_no_env_key_when_the_project_credential_is_not_a_string(
    checkout, listener, monkeypatch, capsys, auth_type, slot, env, literal
):
    url, recorder = listener
    monkeypatch.setenv(env, _ENV_KEY)
    (checkout / ".fluidrc.yaml").write_text(
        "catalogs:\n"
        "  fluid-command-center:\n"
        f"    endpoint: {url}\n"
        f"    organization_id: {_ORG}\n"
        f"    auth:\n      type: {auth_type}\n      {slot}: {literal}\n",
        encoding="utf-8",
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    assert recorder.carrying(_ENV_KEY) == []
    assert recorder.requests == []
    assert rc != 0
    out = " ".join(capsys.readouterr().out.split())
    assert "not sending" in out
    assert _ENV_KEY not in out


def test_fluid_publish_sends_no_user_value_to_a_project_endpoint_whatever_its_name(
    checkout, listener, monkeypatch, capsys
):
    # Names no credential recogniser knew, and a secret inside a list: the
    # operator's layers give a checkout-chosen endpoint only allow-listed settings.
    url, recorder = listener
    _write(
        Path.home() / ".fluid" / "config.yaml",
        {
            "catalogs": {
                "datahub": {
                    "endpoint": "https://datahub.operator.test",
                    "jwt": "USER-JWT-SECRET",
                    "passphrase": "USER-PASSPHRASE",
                    "extra": [{"token": "USER-TOKEN-IN-LIST"}],
                }
            }
        },
    )
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datahub": {"endpoint": url}}})
    contract = _write(checkout / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "datahub")

    assert recorder.requests == []
    assert rc != 0
    out = " ".join(capsys.readouterr().out.split())
    assert "not sending" in out and "jwt" in out
    assert "USER-" not in out


def test_fluid_publish_from_home_uses_the_operators_own_config(
    checkout, listener, monkeypatch, capsys
):
    # cwd == $HOME: ~/.fluid/config.yaml is the user layer, not a project file.
    url, recorder = listener
    monkeypatch.setenv("HOME", str(checkout))
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    _write(
        checkout / ".fluid" / "config.yaml",
        {"catalogs": {"fluid-command-center": {"endpoint": url, "organization_id": _ORG}}},
    )
    contract = _write(checkout / "proj" / "contract.fluid.yaml", _contract())

    rc = _fluid_publish(str(contract), "--target", "fluid-command-center")

    assert rc == 0, capsys.readouterr().out
    assert recorder.requests, "the operator's endpoint is published to"
    assert all(r["headers"].get("X-API-Key") == _ENV_KEY for r in recorder.requests)
    assert "not sending" not in capsys.readouterr().out


def _dmm_block(url: str) -> Dict[str, Any]:
    """A self-contained project block: the endpoint and the key it carries itself."""
    return {"endpoint": url, "api_token": _PROJECT_KEY}


@pytest.mark.parametrize(
    "endpoint, reason",
    [
        ("http://catalog.example.com", "https://"),
        ("https://169.254.169.254", "private or metadata"),
        ("ftp://catalog.example.com", "scheme is ftp"),
    ],
)
def test_acquisition_register_applies_the_host_gate_to_a_project_endpoint(
    checkout, monkeypatch, endpoint, reason
):
    from fluid_build.build_runners import _catalog
    from fluid_build.cli._acquisition_stage_ext import publish_acquisition
    from fluid_build.observability import reporter

    # No DNS in the test: the metadata address is private, the public name is not.
    monkeypatch.setattr(reporter, "_hostname_is_private", lambda h: h.startswith("169.254."))
    built: List[str] = []
    real_build = _catalog.build_registrar

    def _recording_build(target, config=None):
        built.append(target)
        return real_build(target, config)

    monkeypatch.setattr(_catalog, "build_registrar", _recording_build)
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datamesh_manager": _dmm_block(endpoint)}})

    results = publish_acquisition(_contract(builds=_ACQUISITION_BUILDS), checkout)

    assert built == [], "no registrar is built for a refused endpoint"
    (result,) = results
    assert result.succeeded is False
    assert result.target == "datamesh_manager"
    assert reason in result.error
    assert "project config" in result.error
    assert _PROJECT_KEY not in result.error


def test_acquisition_register_honours_the_operators_allow_list(checkout, monkeypatch):
    from fluid_build.cli._acquisition_stage_ext import _collect_target_configs

    monkeypatch.setenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", "catalog.example.com")
    _write(
        checkout / "fluid.config.yaml",
        {"catalogs": {"datamesh_manager": _dmm_block("http://catalog.example.com")}},
    )

    configs, refused = _collect_target_configs(_contract(builds=_ACQUISITION_BUILDS))

    assert refused == {}
    assert configs["datamesh_manager"]["endpoint"] == "http://catalog.example.com"


def test_acquisition_register_uses_a_self_contained_project_endpoint_without_env_values(
    checkout, listener, monkeypatch
):
    from fluid_build.cli._acquisition_stage_ext import publish_acquisition

    url, recorder = listener
    monkeypatch.setenv("DATABASE_URL", _DB_URL)
    _write(checkout / "fluid.config.yaml", {"catalogs": {"datamesh_manager": _dmm_block(url)}})

    publish_acquisition(_contract("{{ env.DATABASE_URL }}", builds=_ACQUISITION_BUILDS), checkout)

    puts = [r for r in recorder.requests if r["method"] == "PUT"]
    assert puts, "a project endpoint with its own key on loopback is registered with"
    assert all(_PROJECT_KEY in str(r["headers"]) for r in puts)
    assert recorder.carrying(_DB_URL, "db-password-not-a-secret") == []


# ── {{ env.* }} in the contract ───────────────────────────────────────────


def _publish_to_cc(contract: Path):
    from fluid_build.cli.publish import publish_contract
    from fluid_build.config_manager import FluidConfig

    return asyncio.run(
        publish_contract(contract, "fluid-command-center", FluidConfig(), skip_health_check=True)
    )


def _cc_block(url: str) -> Dict[str, Any]:
    return {
        "endpoint": url,
        "organization_id": _ORG,
        "auth": {"type": "api_key", "api_key": _PROJECT_KEY},
    }


def test_a_project_endpoint_gets_env_placeholders_as_written(
    checkout, listener, monkeypatch, caplog
):
    url, recorder = listener
    monkeypatch.setenv("CI_REPOSITORY_URL", _REPO_URL)
    monkeypatch.setenv("DATABASE_URL", _DB_URL)
    _write(checkout / "fluid.config.yaml", {"catalogs": {"fluid-command-center": _cc_block(url)}})
    contract = _write(
        checkout / "contract.fluid.yaml",
        _contract("{{ env.CI_REPOSITORY_URL }} {{ env.DATABASE_URL }}"),
    )

    with caplog.at_level(logging.WARNING):
        _publish_to_cc(contract)

    assert recorder.carrying("{{ env.CI_REPOSITORY_URL }}"), "the contract was sent"
    assert recorder.carrying(_REPO_URL, _DB_URL, "ci-token-not-a-secret") == []
    assert "{{ env.DATABASE_URL }}" in caplog.text
    assert "fluid.config.yaml" in caplog.text
    assert _DB_URL not in caplog.text


def test_the_operators_endpoint_still_gets_env_placeholders_filled(checkout, listener, monkeypatch):
    url, recorder = listener
    monkeypatch.setenv("TEAM_CHANNEL", "orders-team-channel")
    _write(
        Path.home() / ".fluid" / "config.yaml",
        {"catalogs": {"fluid-command-center": _cc_block(url)}},
    )
    contract = _write(checkout / "contract.fluid.yaml", _contract("{{ env.TEAM_CHANNEL }}"))

    _publish_to_cc(contract)

    assert recorder.carrying("orders-team-channel")
