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

"""A dlt destination whose endpoint the project's dotenv files set gets the project's credentials only.

dlt resolves each credential field of a destination on its own, from the
environment and then from its ``secrets.toml`` and ``config.toml`` files (the
operator's global ``~/.dlt`` ones included) and other config providers. So a
host the project chose was completed with the operator's private key,
authenticator, user name, database, TLS client key or password. While the
project's files set a destination's endpoint, the dlt runner builds that
destination's credentials from the project's own values (its dotenv files and
the contract) and hands them to dlt resolved. A destination's endpoint is a dlt
variable of its credentials (``DESTINATION__<NAME>__CREDENTIALS__HOST``, the
shorter ``DESTINATION__CREDENTIALS__`` and ``CREDENTIALS__`` sections, the
pipeline-scoped ones, the connection string itself), or an endpoint of the
FLUID service the dlt bridge copies into it (``SNOWFLAKE_ACCOUNT``,
``POSTGRES_HOST``, ``REDSHIFT_HOST``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple, Union

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from fluid_build.credentials.protected_env import unpaired_endpoints  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    FakePostgres,
    dlt_builds_in_one_process,
    dlt_destination_credentials,
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
)

_PROJECT_HOST = "pg.project.test"
_OPERATOR_PRIVATE_KEY = "OPERATOR-FAKE-PRIVATE-KEY"  # pragma: allowlist secret
_OPERATOR_TLS_KEY = "/operator/postgres/client.key"
_SNOWFLAKE_FIELDS = (
    "host",
    "username",
    "password",
    "database",
    "private_key",
    "private_key_path",
    "authenticator",
)
_POSTGRES_FIELDS = ("host", "port", "username", "password", "database", "query")
_OPERATOR_SNOWFLAKE_SECRETS = {
    "destination.snowflake.credentials.private_key": _OPERATOR_PRIVATE_KEY,
    "destination.snowflake.credentials.authenticator": "externalbrowser",
    "destination.snowflake.credentials.password": OPERATOR_KEY,
    "destination.snowflake.credentials.username": "operator_user",
}


def _operator_secrets(destination: str) -> Dict[str, Any]:
    """The operator's own dlt secrets for ``destination``, a TLS client key included."""
    section = f"destination.{destination}.credentials."
    return {
        section + "username": "operator_user",
        section + "database": "operator_db",
        section + "password": OPERATOR_KEY,
        section + "query": {"sslmode": "verify-full", "sslkey": _OPERATOR_TLS_KEY},
    }


def _no_operator_material(fields: Dict[str, Any]) -> None:
    flat = repr(fields)
    for value in (OPERATOR_KEY, _OPERATOR_PRIVATE_KEY, _OPERATOR_TLS_KEY, "operator_user"):
        assert value not in flat, fields
    assert fields.get("authenticator") is None, fields


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    refuse_outbound_dns(monkeypatch)
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


# ── which variables are a destination's endpoint ──────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "DESTINATION__POSTGRES__CREDENTIALS",
        "DESTINATION__CREDENTIALS",
        "CREDENTIALS",
        "ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS",
        "SOURCES__SQL_DATABASE__CREDENTIALS",
    ],
)
def test_a_dlt_connection_string_is_an_endpoint_that_pairs_with_its_section_password(
    name: str,
) -> None:
    # dlt reads the whole credentials of a destination as a connection string
    # from these names, and fills what the string leaves out (the password)
    # from the operator's dlt secrets.
    dsn = f"postgresql://operator_user@{_PROJECT_HOST}:5432/db"

    refused = unpaired_endpoints({name: dsn}, {})
    paired = unpaired_endpoints({name: dsn, f"{name}__PASSWORD": PROJECT_KEY}, {})

    assert refused[name].pairing == ((f"{name}__PASSWORD",),)
    assert paired == {}


_PIPELINE = "acme_ingest"

_PROJECT_ENDPOINTS = [
    # dlt's own variables of the destination's credentials, every section path
    # its environment provider tries (dlt/common/configuration/resolve.py)
    ("DESTINATION__POSTGRES__CREDENTIALS__HOST", "postgres", _PIPELINE, True),
    ("DESTINATION__CREDENTIALS__HOST", "postgres", _PIPELINE, True),
    ("CREDENTIALS__HOST", "postgres", _PIPELINE, True),
    ("ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS__HOST", "postgres", _PIPELINE, True),
    ("ACME_INGEST__CREDENTIALS__HOST", "postgres", _PIPELINE, True),
    ("DESTINATION__POSTGRES__CREDENTIALS", "postgres", _PIPELINE, True),
    ("DESTINATION__CREDENTIALS", "postgres", _PIPELINE, True),
    # the connector's host override, a field of dlt's Snowflake credentials
    ("DESTINATION__SNOWFLAKE__CREDENTIALS___SNOWFLAKE_HOST", "snowflake", _PIPELINE, True),
    # dlt reads upper-case names only, but names compare in any case, as for
    # every endpoint (Windows has one variable for both): pinned, failing closed
    ("destination__postgres__credentials__host", "postgres", _PIPELINE, True),
    ("DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "postgres", _PIPELINE, False),
    ("SOURCES__SQL_DATABASE__CREDENTIALS__HOST", "postgres", _PIPELINE, False),
    # behind another pipeline's name: dlt never reads it in this one
    ("OTHER_INGEST__DESTINATION__POSTGRES__CREDENTIALS__HOST", "postgres", _PIPELINE, False),
    # The pipeline name is the contract's: one that looks like a section is
    # still the first section dlt tries, so what follows it is read as the
    # destination's credentials (dlt.pipeline_name 'sources' reads
    # SOURCES__DESTINATION__SNOWFLAKE__CREDENTIALS__HOST for snowflake).
    ("SOURCES__DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "snowflake", "sources", True),
    ("SOURCES__DESTINATION__CREDENTIALS__HOST", "snowflake", "sources", True),
    ("SOURCES__CREDENTIALS__HOST", "snowflake", "sources", True),
    ("SOURCES__DESTINATION__CREDENTIALS", "snowflake", "sources", True),
    ("SOURCES__SQL_DATABASE__CREDENTIALS__HOST", "postgres", "sources__sql_database", True),
    ("A__DESTINATION__POSTGRES__CREDENTIALS__HOST", "snowflake", "a__destination__postgres", True),
    ("A__DESTINATION__POSTGRES__CREDENTIALS", "snowflake", "a__destination__postgres", True),
    ("DESTINATION__POSTGRES__CREDENTIALS__HOST", "snowflake", "destination__postgres", True),
    ("DESTINATION__DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "snowflake", "destination", True),
    ("DESTINATION__DESTINATION__CREDENTIALS__HOST", "snowflake", "destination", True),
    ("CREDENTIALS__DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "snowflake", "credentials", True),
    ("CREDENTIALS__CREDENTIALS__HOST", "snowflake", "credentials", True),
    ("A__DESTINATION__POSTGRES__CREDENTIALS__HOST", "snowflake", _PIPELINE, False),
    # FLUID's names that the dlt bridge copies into them
    ("POSTGRES_HOST", "postgres", _PIPELINE, True),
    ("PG_PORT", "postgres", _PIPELINE, True),
    ("SNOWFLAKE_ACCOUNT", "snowflake", _PIPELINE, True),
    ("snowflake_account", "snowflake", _PIPELINE, True),
    ("REDSHIFT_HOST", "redshift", _PIPELINE, True),
    ("POSTGRES_HOST", "snowflake", _PIPELINE, False),
]


def _paired_line(name: str) -> str:
    upper = name.upper()
    if "CREDENTIALS" in upper:
        at = upper.rfind("CREDENTIALS")
        return f"{upper[: at + len('CREDENTIALS')]}__PASSWORD={PROJECT_KEY}"
    if upper.startswith("SNOWFLAKE"):
        return f"SNOWFLAKE_PASSWORD={PROJECT_KEY}"
    if upper.startswith("REDSHIFT"):
        return f"REDSHIFT_PASSWORD={PROJECT_KEY}"
    return f"POSTGRES_PASSWORD={PROJECT_KEY}"


@pytest.mark.parametrize("name,destination,pipeline_name,pinned", _PROJECT_ENDPOINTS)
def test_the_destinations_a_project_endpoint_pins(
    project: Path, name: str, destination: str, pipeline_name: str, pinned: bool
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(f"{name}=project-value\n{_paired_line(name)}\n")
    hydrate_dotenv(project)
    assert os.environ.get(name) == "project-value"

    found = _project_endpoints_for_destination(destination, pipeline_name)

    assert (name in found) is pinned, found


def test_an_operator_endpoint_pins_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    monkeypatch.setenv("DESTINATION__POSTGRES__CREDENTIALS__HOST", "pg.operator.test")
    monkeypatch.setenv("POSTGRES_HOST", "pg.operator.test")
    (project / ".env").write_text("POSTGRES_USER=project_user\n")
    hydrate_dotenv(project)

    assert _project_endpoints_for_destination("postgres", _PIPELINE) == []


# The contract's dlt.destination, as dlt.pipeline resolves it
# (Destination.from_reference): a destination type, its full module reference,
# or a named destination whose type is configured
# (https://dlthub.com/docs/general-usage/destination#configure-multiple-destinations-of-the-same-type).
_NAMED_DESTINATION_TYPE = ("DESTINATION__MY_WH__DESTINATION_TYPE", "snowflake")


@pytest.mark.parametrize("pipeline_name", ["acme_ingest", "sources", "a__destination__postgres"])
@pytest.mark.parametrize(
    "destination", ["snowflake", "postgres", "dlt.destinations.snowflake", "my_wh"]
)
def test_the_runner_reads_the_names_dlt_looks_up(
    monkeypatch: pytest.MonkeyPatch, destination: str, pipeline_name: str
) -> None:
    # The names the runner matches a project endpoint against and reads the
    # project's values from are the ones dlt's environment provider looks up,
    # in its order, while it resolves the contract's destination in this
    # pipeline.
    pytest.importorskip("dlt")
    from dlt.common.configuration import inject_section
    from dlt.common.configuration.providers.environ import EnvironProvider
    from dlt.common.configuration.specs.config_section_context import ConfigSectionContext
    from dlt.common.destination import Destination

    from fluid_build.build_runners.dlt.runner import _dlt_destination, _dlt_names

    monkeypatch.setenv(*_NAMED_DESTINATION_TYPE)
    looked_up = []
    real_get_value = EnvironProvider.get_value

    def recording(self: Any, key: str, hint: Any, pipeline: Any, *sections: str) -> Any:
        value, name = real_get_value(self, key, hint, pipeline, *sections)
        looked_up.append(name)
        return value, name

    monkeypatch.setattr(EnvironProvider, "get_value", recording)
    factory = Destination.from_reference(destination)
    with inject_section(ConfigSectionContext(pipeline_name=pipeline_name)):
        factory.configuration(
            factory.spec()._bind_dataset_name(dataset_name="x"), accept_partial=True
        )

    section, _ = _dlt_destination(destination)
    for field in (None, "host", "password"):
        wanted = _dlt_names(section, pipeline_name, field)
        suffix = f"CREDENTIALS__{field.upper()}" if field else "CREDENTIALS"
        seen = [n for n in looked_up if n.endswith(suffix) and "STAGING_CONFIG" not in n]
        assert seen == wanted, (field, seen)


@pytest.mark.parametrize(
    "destination,named",
    [
        ("snowflake", ("snowflake", "snowflake")),
        ("dlt.destinations.snowflake", ("snowflake", "snowflake")),
        ("dlt.destinations.postgres", ("postgres", "postgres")),
        ("my_wh", ("my_wh", "snowflake")),
    ],
)
def test_the_destination_is_named_as_dlt_names_it(
    monkeypatch: pytest.MonkeyPatch, destination: str, named: tuple
) -> None:
    # (section dlt reads its configuration under, its type)
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _dlt_destination

    monkeypatch.setenv(*_NAMED_DESTINATION_TYPE)

    assert _dlt_destination(destination) == named


@pytest.mark.parametrize(
    "name,destination,pinned",
    [
        # a full module reference reads the type's section, and the bridge's
        # copies of FLUID's variables there
        ("DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "dlt.destinations.snowflake", True),
        (
            "ACME_INGEST__DESTINATION__SNOWFLAKE__CREDENTIALS__HOST",
            "dlt.destinations.snowflake",
            True,
        ),
        ("SNOWFLAKE_ACCOUNT", "dlt.destinations.snowflake", True),
        ("DESTINATION__POSTGRES__CREDENTIALS__HOST", "dlt.destinations.snowflake", False),
        # a named destination reads its own section only
        ("DESTINATION__MY_WH__CREDENTIALS__HOST", "my_wh", True),
        ("DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "my_wh", False),
    ],
)
def test_a_destination_reference_pins_the_names_dlt_reads_for_it(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str, destination: str, pinned: bool
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    monkeypatch.setenv(*_NAMED_DESTINATION_TYPE)
    (project / ".env").write_text(f"{name}=project-value\n{_paired_line(name)}\n")
    hydrate_dotenv(project)
    assert os.environ.get(name) == "project-value"

    found = _project_endpoints_for_destination(destination, _PIPELINE)

    assert (name in found) is pinned, found


@pytest.mark.parametrize(
    "endpoint_line",
    [
        "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=xy12345",
        "SNOWFLAKE_ACCOUNT=xy12345",
    ],
)
@pytest.mark.parametrize(
    "name",
    [
        "DESTINATION__SNOWFLAKE__CREDENTIALS__AUTHENTICATOR",
        "DESTINATION__CREDENTIALS__AUTHENTICATOR",
        "CREDENTIALS__AUTHENTICATOR",
        "ACME_INGEST__DESTINATION__SNOWFLAKE__CREDENTIALS__AUTHENTICATOR",
    ],
)
def test_a_project_dlt_authenticator_is_dropped_next_to_the_project_s_endpoint(
    project: Path, endpoint_line: str, name: str
) -> None:
    # dlt reads the authenticator under every section path; any value but
    # 'snowflake' can sign in with the operator's browser session or identity.
    password = (
        f"SNOWFLAKE_PASSWORD={PROJECT_KEY}"
        if endpoint_line.startswith("SNOWFLAKE")
        else f"DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD={PROJECT_KEY}"
    )
    (project / ".env").write_text(f"{endpoint_line}\n{password}\n{name}=externalbrowser\n")

    hydrate_dotenv(project)

    assert endpoint_line.split("=")[0] in os.environ
    assert name not in os.environ


def test_a_project_password_authenticator_loads_next_to_its_dlt_endpoint(project: Path) -> None:
    (project / ".env").write_text(
        "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=xy12345\n"
        f"DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
        "CREDENTIALS__AUTHENTICATOR=snowflake\n"
    )

    hydrate_dotenv(project)

    assert os.environ["CREDENTIALS__AUTHENTICATOR"] == "snowflake"


# ── end to end: what the destination resolves ─────────────────────────────


# A section behind each pipeline name dlt reads it under; the contract chooses
# the name, and one that looks like a section ('sources', 'destination',
# 'credentials', 'a__destination__postgres') must pin the destination all the same.
_PIPELINE_SECTIONS = [
    (f"{pipeline.upper()}__{section}", pipeline)
    for pipeline in (
        "acme_ingest",
        "sources",
        "destination",
        "credentials",
        "a__destination__postgres",
    )
    for section in (
        "DESTINATION__SNOWFLAKE__CREDENTIALS__",
        "DESTINATION__CREDENTIALS__",
        "CREDENTIALS__",
    )
]


@pytest.mark.parametrize(
    "section,pipeline_name",
    [
        ("DESTINATION__SNOWFLAKE__CREDENTIALS__", None),
        ("DESTINATION__CREDENTIALS__", None),
        ("CREDENTIALS__", None),
        *_PIPELINE_SECTIONS,
    ],
)
def test_a_project_dlt_section_endpoint_gets_no_operator_dlt_secret(
    tmp_path: Path, section: str, pipeline_name: Optional[str]
) -> None:
    dotenv = (
        f"{section}HOST=xy12345\n"
        f"{section}USERNAME=project_user\n"
        f"{section}PASSWORD={PROJECT_KEY}\n"
        f"{section}DATABASE=DB\n"
    )

    fields = dlt_destination_credentials(
        tmp_path,
        "snowflake",
        dotenv,
        _SNOWFLAKE_FIELDS,
        operator_secrets=_OPERATOR_SNOWFLAKE_SECRETS,
        pipeline_name=pipeline_name,
    )

    assert (fields["host"], fields["username"]) == ("xy12345", "project_user")
    assert (fields["password"], fields["database"]) == (PROJECT_KEY, "DB")
    _no_operator_material(fields)


@pytest.mark.parametrize(
    "dotenv",
    [
        (
            "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=xy12345\n"
            "DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME=project_user\n"  # pragma: allowlist secret
            f"DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
            "DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE=DB\n"
        ),
        (
            "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n"  # pragma: allowlist secret
            f"SNOWFLAKE_PASSWORD={PROJECT_KEY}\nSNOWFLAKE_DATABASE=DB\n"
        ),
    ],
)
def test_a_destination_given_by_its_module_reference_gets_no_operator_dlt_secret(
    tmp_path: Path, dotenv: str
) -> None:
    # dlt.destination 'dlt.destinations.snowflake' is dlt's Snowflake
    # destination under its usual section, DESTINATION__SNOWFLAKE__.
    fields = dlt_destination_credentials(
        tmp_path,
        "dlt.destinations.snowflake",
        dotenv,
        _SNOWFLAKE_FIELDS,
        operator_secrets=_OPERATOR_SNOWFLAKE_SECRETS,
        platform="snowflake",
    )

    assert (fields["host"], fields["username"]) == ("xy12345", "project_user")
    assert (fields["password"], fields["database"]) == (PROJECT_KEY, "DB")
    _no_operator_material(fields)


def test_a_named_destination_gets_the_project_s_credentials_only(tmp_path: Path) -> None:
    # A named destination of a configured type reads its own section; the
    # credentials are built for its type, from the project's values only.
    section = "DESTINATION__MY_WH__CREDENTIALS__"
    fields = dlt_destination_credentials(
        tmp_path,
        "my_wh",
        (
            "DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n"
            f"{section}HOST=xy12345\n{section}USERNAME=project_user\n"
            f"{section}PASSWORD={PROJECT_KEY}\n{section}DATABASE=DB\n"
        ),
        _SNOWFLAKE_FIELDS,
        operator_secrets={
            key.replace("destination.snowflake.", "destination.my_wh."): value
            for key, value in _OPERATOR_SNOWFLAKE_SECRETS.items()
        },
    )

    assert (fields["host"], fields["username"]) == ("xy12345", "project_user")
    assert (fields["password"], fields["database"]) == (PROJECT_KEY, "DB")
    _no_operator_material(fields)


def test_a_project_dlt_section_endpoint_gets_no_operator_snowflake_variable(
    tmp_path: Path,
) -> None:
    # The dlt bridge copies FLUID's SNOWFLAKE_* variables into the
    # destination's credentials; the operator's key path and authenticator
    # are not credentials the dlt section's pairing rule looks at.
    fields = dlt_destination_credentials(
        tmp_path,
        "snowflake",
        (
            "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST=xy12345\n"
            "DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME=project_user\n"  # pragma: allowlist secret
            f"DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
            "DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE=DB\n"
        ),
        _SNOWFLAKE_FIELDS,
        operator_env={
            "SNOWFLAKE_PRIVATE_KEY_PATH": "/operator/snowflake/rsa_key.p8",
            "SNOWFLAKE_AUTHENTICATOR": "externalbrowser",
            "SNOWFLAKE_USER": "operator_user",
        },
    )

    assert (fields["host"], fields["password"]) == ("xy12345", PROJECT_KEY)
    assert fields["private_key_path"] is None
    _no_operator_material(fields)


def test_a_project_snowflake_account_gets_no_operator_dlt_variable(tmp_path: Path) -> None:
    # The Snowflake pairing rule does not count dlt's own variable names, so
    # an operator who keeps their key there exported it next to the project's
    # account and password.
    fields = dlt_destination_credentials(
        tmp_path,
        "snowflake",
        (
            "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n"  # pragma: allowlist secret
            f"SNOWFLAKE_PASSWORD={PROJECT_KEY}\nSNOWFLAKE_DATABASE=DB\n"
        ),
        _SNOWFLAKE_FIELDS,
        operator_env={"DESTINATION__SNOWFLAKE__CREDENTIALS__PRIVATE_KEY": _OPERATOR_PRIVATE_KEY},
    )

    assert (fields["host"], fields["password"]) == ("xy12345", PROJECT_KEY)
    _no_operator_material(fields)


@pytest.mark.parametrize("name", ["DESTINATION__CREDENTIALS", "CREDENTIALS"])
@pytest.mark.parametrize("destination", ["postgres", "snowflake"])
def test_a_project_connection_string_never_gets_the_operator_password(
    tmp_path: Path, name: str, destination: str
) -> None:
    scheme = "postgresql" if destination == "postgres" else "snowflake"
    fields = dlt_destination_credentials(
        tmp_path,
        destination,
        f"{name}={scheme}://operator_user@{_PROJECT_HOST}:5432/db\n",
        ("host", "password"),
        operator_secrets={f"destination.{destination}.credentials.password": OPERATOR_KEY},
    )

    # Refused: it names no password of its own. So the destination is the
    # operator's own configuration, their password with no host of the project's.
    assert fields.get("host") != _PROJECT_HOST, fields
    assert (fields.get("host"), fields.get("password")) == (None, OPERATOR_KEY), fields


def test_the_connector_s_host_override_never_gets_the_operator_s_dlt_secrets(
    tmp_path: Path,
) -> None:
    # dlt's Snowflake credentials have a field that the connector connects to
    # in place of the account's host (_snowflake_host, from
    # DESTINATION__SNOWFLAKE__CREDENTIALS___SNOWFLAKE_HOST): a project value of
    # it is an endpoint like the account itself.
    fields = dlt_destination_credentials(
        tmp_path,
        "snowflake",
        f"DESTINATION__SNOWFLAKE__CREDENTIALS___SNOWFLAKE_HOST={_PROJECT_HOST}\n",
        ("host", "password", "_snowflake_host"),
        operator_secrets={
            **_OPERATOR_SNOWFLAKE_SECRETS,
            "destination.snowflake.credentials.host": "operator-account",
        },
    )

    assert fields.get("_snowflake_host") != _PROJECT_HOST, fields
    assert (fields["host"], fields["password"]) == ("operator-account", OPERATOR_KEY), fields


def test_a_project_connection_string_with_its_password_is_the_project_s(tmp_path: Path) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        (
            f"DESTINATION__CREDENTIALS=postgresql://project_user@{_PROJECT_HOST}:5433/db\n"
            f"DESTINATION__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
        ),
        _POSTGRES_FIELDS,
        operator_secrets=_operator_secrets("postgres"),
    )

    assert (fields["host"], fields["port"]) == (_PROJECT_HOST, 5433)
    assert (fields["username"], fields["password"]) == ("project_user", PROJECT_KEY)
    _no_operator_material(fields)


def test_a_project_postgres_dlt_section_gets_no_operator_dlt_secret(tmp_path: Path) -> None:
    section = "DESTINATION__POSTGRES__CREDENTIALS__"
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        (
            f"{section}HOST={_PROJECT_HOST}\n{section}PASSWORD={PROJECT_KEY}\n"
            f"{section}USERNAME=project_user\n{section}DATABASE=project_db\n"
        ),
        _POSTGRES_FIELDS,
        operator_secrets=_operator_secrets("postgres"),
    )

    assert (fields["host"], fields["password"]) == (_PROJECT_HOST, PROJECT_KEY)
    assert (fields["username"], fields["database"]) == ("project_user", "project_db")
    _no_operator_material(fields)


@pytest.mark.parametrize(
    "destination,dotenv",
    [
        ("postgres", f"POSTGRES_HOST={_PROJECT_HOST}\n"),
        ("postgres", f"PG_HOST={_PROJECT_HOST}\nPG_PASSWORD={PROJECT_KEY}\n"),
        (
            "postgres",
            f"DESTINATION__POSTGRES__CREDENTIALS__HOST={_PROJECT_HOST}\n"
            f"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD={PROJECT_KEY}\n",
        ),
        ("redshift", f"REDSHIFT_HOST={_PROJECT_HOST}\nREDSHIFT_PASSWORD={PROJECT_KEY}\n"),
    ],
)
def test_what_the_project_leaves_out_is_not_filled_from_the_operator(
    tmp_path: Path, destination: str, dotenv: str
) -> None:
    # A project PostgreSQL host loads on its own (no ambient credential in
    # the pairing rule), and dlt filled the password, user and database of
    # the operator's dlt secrets next to it.
    fields = dlt_destination_credentials(
        tmp_path,
        destination,
        dotenv,
        _POSTGRES_FIELDS,
        operator_secrets=_operator_secrets(destination),
    )

    assert set(fields) == {"failed"}, fields
    assert "username" in fields["failed"] and "database" in fields["failed"]
    assert "export" in fields["failed"]
    _no_operator_material(fields)


@pytest.mark.parametrize(
    "destination,dotenv",
    [
        (
            "postgres",
            f"POSTGRES_HOST={_PROJECT_HOST}\nPOSTGRES_USER=project_user\n"
            f"POSTGRES_PASSWORD={PROJECT_KEY}\nPOSTGRES_DB=project_db\n",
        ),
        (
            "redshift",
            f"REDSHIFT_HOST={_PROJECT_HOST}\nREDSHIFT_USER=project_user\n"
            f"REDSHIFT_PASSWORD={PROJECT_KEY}\nREDSHIFT_DATABASE=project_db\n",
        ),
    ],
)
def test_a_whole_project_setup_through_fluid_s_names_still_works(
    tmp_path: Path, destination: str, dotenv: str
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        destination,
        dotenv,
        _POSTGRES_FIELDS,
        operator_secrets=_operator_secrets(destination),
    )

    assert (fields["host"], fields["password"]) == (_PROJECT_HOST, PROJECT_KEY)
    assert (fields["username"], fields["database"]) == ("project_user", "project_db")
    _no_operator_material(fields)


def test_the_operator_s_own_destination_still_uses_their_dlt_secrets(tmp_path: Path) -> None:
    secrets = {
        **_operator_secrets("postgres"),
        "destination.postgres.credentials.host": "pg.operator.test",
    }

    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        "POSTGRES_SCHEMA=project_schema\n",  # pragma: allowlist secret
        _POSTGRES_FIELDS,
        operator_secrets=secrets,
    )

    assert (fields["host"], fields["password"]) == ("pg.operator.test", OPERATOR_KEY)
    assert fields["query"]["sslkey"] == _OPERATOR_TLS_KEY


def test_exporting_the_endpoint_as_the_failure_advises_uses_the_operator_s_dlt_secrets(
    tmp_path: Path,
) -> None:
    # The advice in the failure: an endpoint the operator exports is theirs,
    # so their dlt configuration completes it.
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        f"POSTGRES_HOST={_PROJECT_HOST}\n",
        _POSTGRES_FIELDS,
        operator_env={"POSTGRES_HOST": _PROJECT_HOST},
        operator_secrets=_operator_secrets("postgres"),
    )

    assert (fields["host"], fields["password"]) == (_PROJECT_HOST, OPERATOR_KEY)
    assert fields["username"] == "operator_user"


def test_a_destination_fluid_cannot_pin_fails_closed(tmp_path: Path) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "acme",
        (
            "DESTINATION__ACME__CREDENTIALS__HOST=acme.project.test\n"
            f"DESTINATION__ACME__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
        ),
        ("host",),
    )

    assert set(fields) == {"failed"}, fields
    assert "DESTINATION__ACME__CREDENTIALS__HOST" in fields["failed"]


# ── a connection string that carries its own password ─────────────────────

_PROJECT_DSN = f"postgresql://project_user:{PROJECT_KEY}@{_PROJECT_HOST}:5433/project_db"
_DSN_NAMES = [
    "DESTINATION__POSTGRES__CREDENTIALS",
    "DESTINATION__CREDENTIALS",
    "CREDENTIALS",
    "ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS",
]


@pytest.mark.parametrize("name", _DSN_NAMES)
@pytest.mark.parametrize(
    "dsn",
    [
        _PROJECT_DSN,
        # MotherDuck's access token, which dlt reads as the password
        # (dlt/destinations/impl/motherduck/configuration.py, _token_to_password)
        f"md:///project_db?token={PROJECT_KEY}",
        f"md:project_db?motherduck_token={PROJECT_KEY}",
    ],
)
def test_a_connection_string_with_its_own_password_pairs_itself(name: str, dsn: str) -> None:
    # The password is the project's own, in the same file as the host.
    assert unpaired_endpoints({name: dsn}, {}) == {}
    assert unpaired_endpoints({name.lower(): dsn}, {}, case_insensitive_env=True) == {}


@pytest.mark.parametrize("name", _DSN_NAMES)
@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://project_user@{_PROJECT_HOST}:5433/project_db",
        f"postgresql://project_user:@{_PROJECT_HOST}:5433/project_db",
        f"postgresql://project_user: @{_PROJECT_HOST}:5433/project_db",
        f"postgresql://project_user@{_PROJECT_HOST}:5433/project_db?sslmode=require",
        # a libpq parameter, not the password field dlt fills from its secrets
        f"postgresql://project_user@{_PROJECT_HOST}:5433/project_db?password={PROJECT_KEY}",
        "md:///project_db?token=",
    ],
)
def test_a_connection_string_without_a_password_of_its_own_still_needs_one(
    name: str, dsn: str
) -> None:
    refused = unpaired_endpoints({name: dsn}, {})

    assert refused[name].pairing == ((f"{name}__PASSWORD",),)
    assert refused[name].pairs_inside


def test_a_connection_string_with_a_password_still_refuses_the_operator_s_dlt_password() -> None:
    # A credential of the group the operator holds refuses every project
    # endpoint of the group, a connection string with a password included.
    operator = {"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD": OPERATOR_KEY}

    refused = unpaired_endpoints({"DESTINATION__POSTGRES__CREDENTIALS": _PROJECT_DSN}, operator)

    assert refused["DESTINATION__POSTGRES__CREDENTIALS"].operator_credentials == (
        "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD",
    )


def test_only_a_connection_string_pairs_itself() -> None:
    # A host field holds a host: a password-shaped value of it pairs nothing.
    name = "DESTINATION__POSTGRES__CREDENTIALS__HOST"

    refused = unpaired_endpoints({name: _PROJECT_DSN}, {})

    assert refused[name].pairing == (("DESTINATION__POSTGRES__CREDENTIALS__PASSWORD",),)
    assert not refused[name].pairs_inside


def test_the_refusal_names_a_password_inside_the_connection_string(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    (project / ".env").write_text(f"DESTINATION__CREDENTIALS=postgresql://u@{_PROJECT_HOST}/db\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(project)

    assert "DESTINATION__CREDENTIALS" not in os.environ
    (message,) = [
        r.getMessage() for r in caplog.records if "DESTINATION__CREDENTIALS" in r.getMessage()
    ]
    assert (
        "it pairs only with DESTINATION__CREDENTIALS__PASSWORD or a password inside the "
        "connection string" in message
    ), message


@pytest.mark.parametrize("name", _DSN_NAMES)
def test_a_project_connection_string_with_its_password_is_the_whole_destination(
    tmp_path: Path, name: str
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        f"{name}={_PROJECT_DSN}\n",
        _POSTGRES_FIELDS,
        operator_secrets=_operator_secrets("postgres"),
        pipeline_name=_PIPELINE,
    )

    assert (fields["host"], fields["port"]) == (_PROJECT_HOST, 5433), fields
    assert (fields["username"], fields["password"]) == ("project_user", PROJECT_KEY)
    assert fields["database"] == "project_db"
    _no_operator_material(fields)


def test_a_project_motherduck_connection_string_with_its_token(tmp_path: Path) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "motherduck",
        f"DESTINATION__MOTHERDUCK__CREDENTIALS=md:///project_db?token={PROJECT_KEY}\n",
        ("database", "password"),
        operator_secrets={"destination.motherduck.credentials.password": OPERATOR_KEY},
    )

    assert (fields["database"], fields["password"]) == ("project_db", PROJECT_KEY), fields


# ── the operator's own destination next to a project's FLUID names ────────
#
# The dlt bridge (destinations.py) runs only for binding.platform, writes under
# that platform's section, reads only its credential class's variables
# (_credentials.env_names_for) and keeps a dlt variable already set. So a FLUID
# endpoint of the project's reaches the destination only through it, and dlt
# uses a host or connection string the operator exported ahead of its copy.

_OPERATOR_HOST = "pg.operator.test"
_OPERATOR_DSN = f"postgresql://operator_user:{OPERATOR_KEY}@{_OPERATOR_HOST}:5432/operator_db"
_OPERATOR_PG_ENV = {
    "DESTINATION__POSTGRES__CREDENTIALS__HOST": _OPERATOR_HOST,
    "DESTINATION__POSTGRES__CREDENTIALS__PORT": "5432",
    "DESTINATION__POSTGRES__CREDENTIALS__USERNAME": "operator_user",
    "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD": OPERATOR_KEY,
    "DESTINATION__POSTGRES__CREDENTIALS__DATABASE": "operator_db",
}
_OPERATOR_PG = {
    "host": _OPERATOR_HOST,
    "port": 5432,
    "username": "operator_user",
    "password": OPERATOR_KEY,
    "database": "operator_db",
}
# the app's own database, docker-compose style
_COMPOSE_ENV = (
    "POSTGRES_HOST=db\nPOSTGRES_PORT=5432\nPOSTGRES_USER=app\n"
    f"POSTGRES_PASSWORD={PROJECT_KEY}\nPOSTGRES_DB=app\n"
)
# examples/source-aligned-postgres-duckdb/.env, for the source through libpq
_LIBPQ_ENV = (
    "PGHOST=localhost\nPGPORT=5432\nPGDATABASE=sales\nPGUSER=analyst\n"  # pragma: allowlist secret
    f"PGPASSWORD={PROJECT_KEY}\n"
)


@pytest.mark.parametrize(
    "dotenv,destination,platform,pinned",
    [
        (f"POSTGRES_HOST={_PROJECT_HOST}\n", "postgres", "postgres", "POSTGRES_HOST"),
        (f"postgres_host={_PROJECT_HOST}\n", "postgres", "postgres", "postgres_host"),
        (
            f"PG_HOST={_PROJECT_HOST}\nPG_PASSWORD={PROJECT_KEY}\n",
            "postgres",
            "postgres",
            "PG_HOST",
        ),
        (
            f"SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_PASSWORD={PROJECT_KEY}\n",
            "snowflake",
            "Snowflake",
            "SNOWFLAKE_ACCOUNT",
        ),
        # the bridge runs only for binding.platform, under its section
        (f"POSTGRES_HOST={_PROJECT_HOST}\n", "postgres", None, None),
        (f"POSTGRES_HOST={_PROJECT_HOST}\n", "postgres", "snowflake", None),
        (f"SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_PASSWORD={PROJECT_KEY}\n", "snowflake", None, None),
        (
            f"REDSHIFT_HOST=rs.project.test\nREDSHIFT_PASSWORD={PROJECT_KEY}\n",
            "redshift",
            None,
            None,
        ),
        # names the bridge never reads: libpq's, which dlt's connection string
        # overrides (PGHOSTADDR it does not: see below), the Snowflake resolver's
        (f"PGHOST={_PROJECT_HOST}\nPGPORT=5433\n", "postgres", "postgres", None),
        ("PGPORT=5433\nPGDATABASE=dev\n", "postgres", "postgres", None),
        (f"SF_ACCOUNT=xy12345\nSF_PASSWORD={PROJECT_KEY}\n", "snowflake", "snowflake", None),
        (f"ACCOUNT=xy12345\nPASSWORD={PROJECT_KEY}\n", "snowflake", "snowflake", None),
        (
            f"SNOWFLAKE_PORT=8443\nSNOWFLAKE_PASSWORD={PROJECT_KEY}\n",
            "snowflake",
            "snowflake",
            None,
        ),
        # dlt's own names reach the destination whatever binding.platform says;
        # the project's password is one of them too (default-deny: every field
        # of the destination's dlt credentials but the in-account ones)
        (
            f"DESTINATION__POSTGRES__CREDENTIALS__HOST={_PROJECT_HOST}\n"
            f"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD={PROJECT_KEY}\n",
            "postgres",
            None,
            (
                "DESTINATION__POSTGRES__CREDENTIALS__HOST",
                "DESTINATION__POSTGRES__CREDENTIALS__PASSWORD",
            ),
        ),
    ],
)
def test_a_fluid_endpoint_pins_the_destination_only_where_the_bridge_copies_it(
    project: Path,
    dotenv: str,
    destination: str,
    platform: Optional[str],
    pinned: Union[None, str, Tuple[str, ...]],
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(dotenv)
    hydrate_dotenv(project)
    assert dotenv.split("=", 1)[0] in os.environ  # it loaded

    binding = {"platform": platform} if platform else {}
    found = _project_endpoints_for_destination(destination, _PIPELINE, binding, dict(os.environ))

    expected = [pinned] if isinstance(pinned, str) else list(pinned or ())
    assert found == expected, found


@pytest.mark.parametrize(
    "name,value,pinned",
    [
        # dlt reads these ahead of the bridge's DESTINATION__POSTGRES__CREDENTIALS__HOST
        ("DESTINATION__POSTGRES__CREDENTIALS__HOST", _OPERATOR_HOST, False),
        ("ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS__HOST", _OPERATOR_HOST, False),
        ("ACME_INGEST__DESTINATION__CREDENTIALS__HOST", _OPERATOR_HOST, False),
        ("ACME_INGEST__CREDENTIALS__HOST", _OPERATOR_HOST, False),
        # a connection string's own fields come before every field variable
        ("DESTINATION__POSTGRES__CREDENTIALS", _OPERATOR_DSN, False),
        ("DESTINATION__CREDENTIALS", _OPERATOR_DSN, False),
        ("CREDENTIALS", _OPERATOR_DSN, False),
        ("ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS", _OPERATOR_DSN, False),
        # dlt reads the bridge's copy first, or these choose no host
        ("DESTINATION__CREDENTIALS__HOST", _OPERATOR_HOST, True),
        ("CREDENTIALS__HOST", _OPERATOR_HOST, True),
        ("DESTINATION__POSTGRES__CREDENTIALS__PORT", "5432", True),
        (
            "DESTINATION__POSTGRES__CREDENTIALS",
            f"postgresql://operator_user:{OPERATOR_KEY}@/db",
            True,
        ),
        ("DESTINATION__POSTGRES__CREDENTIALS__HOST", " ", True),
        ("OTHER_INGEST__DESTINATION__POSTGRES__CREDENTIALS__HOST", _OPERATOR_HOST, True),
        ("DESTINATION__SNOWFLAKE__CREDENTIALS__HOST", "operator-acct", True),
    ],
)
def test_a_dlt_host_the_operator_exported_keeps_the_destination_theirs(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, pinned: bool
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    monkeypatch.setenv(name, value)
    (project / ".env").write_text(_COMPOSE_ENV)
    hydrate_dotenv(project)
    assert "POSTGRES_HOST" in os.environ

    found = _project_endpoints_for_destination(
        "postgres", _PIPELINE, {"platform": "postgres"}, dict(os.environ)
    )

    assert ("POSTGRES_HOST" in found) is pinned, found


@pytest.mark.parametrize(
    "platform,dotenv,operator_env,operator_secrets",
    [
        pytest.param("", _COMPOSE_ENV, _OPERATOR_PG_ENV, None, id="compose-env"),
        pytest.param(None, _COMPOSE_ENV, _OPERATOR_PG_ENV, None, id="compose-env-bridged"),
        pytest.param(
            "",
            _COMPOSE_ENV,
            {"DESTINATION__POSTGRES__CREDENTIALS": _OPERATOR_DSN},
            None,
            id="connection-string",
        ),
        pytest.param(
            None,
            _COMPOSE_ENV,
            {"DESTINATION__POSTGRES__CREDENTIALS": _OPERATOR_DSN},
            None,
            id="connection-string-bridged",
        ),
        pytest.param(None, "PGPORT=5433\nPGDATABASE=dev\n", _OPERATOR_PG_ENV, None, id="pgport"),
        pytest.param(None, _LIBPQ_ENV, _OPERATOR_PG_ENV, None, id="libpq-env"),
        pytest.param(
            None,
            _LIBPQ_ENV,
            None,
            {f"destination.postgres.credentials.{k}": v for k, v in _OPERATOR_PG.items()},
            id="libpq-env-dlt-secrets",
        ),
    ],
)
def test_the_operator_s_own_postgres_destination_next_to_a_project_s_postgres_names(
    tmp_path: Path,
    platform: Optional[str],
    dotenv: str,
    operator_env: Optional[Dict[str, str]],
    operator_secrets: Optional[Dict[str, Any]],
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        dotenv,
        tuple(_OPERATOR_PG),
        operator_env=operator_env,
        operator_secrets=operator_secrets,
        platform=platform,
        pipeline_name=_PIPELINE,
    )

    assert fields == _OPERATOR_PG, fields


_OPERATOR_SF_ENV = {
    "DESTINATION__SNOWFLAKE__CREDENTIALS__HOST": "operator-acct",
    "DESTINATION__SNOWFLAKE__CREDENTIALS__USERNAME": "operator_user",
    "DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD": OPERATOR_KEY,
    "DESTINATION__SNOWFLAKE__CREDENTIALS__DATABASE": "OPERATOR_DB",
}


@pytest.mark.parametrize(
    "dotenv",
    [
        # the forge Snowflake scaffold, for dbt on the team's account
        (
            "SNOWFLAKE_ACCOUNT=team-dev\nSNOWFLAKE_USER=dev_user\n"  # pragma: allowlist secret
            f"SNOWFLAKE_PASSWORD={PROJECT_KEY}\nSNOWFLAKE_WAREHOUSE=DEV_WH\nSNOWFLAKE_DATABASE=DEV\n"
        ),
        # resolve_snowflake_settings' names, which the bridge never reads
        f"SF_ACCOUNT=team-dev\nSF_USER=dev_user\nSF_PASSWORD={PROJECT_KEY}\n",
    ],
)
def test_the_operator_s_own_snowflake_destination_next_to_a_team_snowflake_env(
    tmp_path: Path, dotenv: str
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "snowflake",
        dotenv,
        ("host", "username", "password", "database"),
        operator_env=_OPERATOR_SF_ENV,
    )

    assert fields == {
        "host": "operator-acct",
        "username": "operator_user",
        "password": OPERATOR_KEY,
        "database": "OPERATOR_DB",
    }, fields


def test_the_operator_s_own_redshift_destination_next_to_a_team_redshift_env(
    tmp_path: Path,
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "redshift",
        (
            "REDSHIFT_HOST=rs.team-dev.test\nREDSHIFT_USER=dev_user\n"
            f"REDSHIFT_PASSWORD={PROJECT_KEY}\nREDSHIFT_DATABASE=dev\n"
        ),
        ("host", "username", "password", "database"),
        operator_env={
            "DESTINATION__REDSHIFT__CREDENTIALS__HOST": "rs.operator.test",
            "DESTINATION__REDSHIFT__CREDENTIALS__USERNAME": "operator_user",
            "DESTINATION__REDSHIFT__CREDENTIALS__PASSWORD": OPERATOR_KEY,
            "DESTINATION__REDSHIFT__CREDENTIALS__DATABASE": "warehouse",
        },
    )

    assert fields == {
        "host": "rs.operator.test",
        "username": "operator_user",
        "password": OPERATOR_KEY,
        "database": "warehouse",
    }, fields


@pytest.mark.parametrize(
    "operator_env",
    [
        # dlt reads the bridge's DESTINATION__POSTGRES__CREDENTIALS__HOST first
        {
            "DESTINATION__CREDENTIALS__HOST": _OPERATOR_HOST,
            "DESTINATION__CREDENTIALS__USERNAME": "operator_user",
            "DESTINATION__CREDENTIALS__PASSWORD": OPERATOR_KEY,
            "DESTINATION__CREDENTIALS__DATABASE": "operator_db",
        },
        # a connection string with no host: dlt takes the bridge's copy for it
        {
            "DESTINATION__POSTGRES__CREDENTIALS": f"postgresql://operator_user:{OPERATOR_KEY}@/operator_db"
        },
    ],
)
def test_a_project_host_the_bridge_copies_in_still_gets_no_operator_dlt_secret(
    tmp_path: Path, operator_env: Dict[str, str]
) -> None:
    fields = dlt_destination_credentials(
        tmp_path,
        "postgres",
        f"POSTGRES_HOST={_PROJECT_HOST}\n",
        _POSTGRES_FIELDS,
        operator_env=operator_env,
    )

    assert set(fields) == {"failed"}, fields
    assert "POSTGRES_HOST" in fields["failed"]
    _no_operator_material(fields)


# ── libpq's own variables ──────────────────────────────────────────────────
#
# libpq takes a connection parameter from its environment variable only where
# the connection string leaves it out, and with ``hostaddr`` it connects to
# that address whatever ``host`` says. dlt's string for its postgres and
# redshift destinations sets host and port but never hostaddr, so a project's
# PGHOST and PGPORT change nothing there, and its PGHOSTADDR would move the
# connection, the operator's password with it. No project file can set
# PGHOSTADDR (protected_env.PROTECTED_ENV_KEYS); the runner still pins a libpq
# destination to the project's values if one ever loaded.


@pytest.fixture
def pghostaddr_unfiltered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader as if it let a project's PGHOSTADDR through, which it does not."""
    from fluid_build.credentials import dotenv_store

    protected = dotenv_store.is_protected_env_key
    monkeypatch.setattr(
        dotenv_store,
        "is_protected_env_key",
        lambda key: key.strip().upper() != "PGHOSTADDR" and protected(key),
    )


@pytest.mark.parametrize("spelling", ["PGHOSTADDR", "pghostaddr"])
@pytest.mark.parametrize("destination", ["postgres", "redshift"])
@pytest.mark.parametrize("platform", ["postgres", None, "snowflake"])
def test_a_project_host_address_never_loads_and_pins_nothing(
    project: Path, spelling: str, destination: str, platform: Optional[str]
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(f"{spelling}=10.0.0.1\n")
    hydrate_dotenv(project)
    assert not [k for k in os.environ if k.upper() == "PGHOSTADDR"]

    binding = {"platform": platform} if platform else {}
    found = _project_endpoints_for_destination(destination, _PIPELINE, binding, dict(os.environ))

    assert found == [], found


@pytest.mark.usefixtures("pghostaddr_unfiltered")
@pytest.mark.parametrize(
    "dotenv,destination,platform,pinned",
    [
        ("PGHOSTADDR=10.0.0.1\n", "postgres", "postgres", ["PGHOSTADDR"]),
        ("pghostaddr=10.0.0.1\n", "postgres", "postgres", ["pghostaddr"]),
        # libpq reads it whatever the binding says: the bridge plays no part
        ("PGHOSTADDR=10.0.0.1\n", "postgres", None, ["PGHOSTADDR"]),
        ("PGHOSTADDR=10.0.0.1\n", "postgres", "snowflake", ["PGHOSTADDR"]),
        ("PGHOSTADDR=10.0.0.1\n", "redshift", "redshift", ["PGHOSTADDR"]),
        ("PGHOSTADDR=10.0.0.1\n", "redshift", None, ["PGHOSTADDR"]),
        (
            f"PGHOSTADDR=10.0.0.1\nPGHOST={_PROJECT_HOST}\nPGPORT=5433\n",
            "postgres",
            "postgres",
            ["PGHOSTADDR"],
        ),
        (
            f"POSTGRES_HOST={_PROJECT_HOST}\nPGHOSTADDR=10.0.0.1\n",
            "postgres",
            "postgres",
            ["PGHOSTADDR", "POSTGRES_HOST"],
        ),
        # the names dlt's connection string sets
        (f"PGHOST={_PROJECT_HOST}\nPGPORT=5433\n", "redshift", "redshift", []),
        (f"PGHOST={_PROJECT_HOST}\nPGPORT=5433\n", "redshift", None, []),
        # destinations that do not connect through libpq
        ("PGHOSTADDR=10.0.0.1\n", "snowflake", "snowflake", []),
        ("PGHOSTADDR=10.0.0.1\n", "bigquery", None, []),
    ],
)
def test_a_loaded_host_address_would_pin_every_destination_that_connects_through_libpq(
    project: Path, dotenv: str, destination: str, platform: Optional[str], pinned: list
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(dotenv)
    hydrate_dotenv(project)
    assert dotenv.split("=", 1)[0] in os.environ  # it loaded

    binding = {"platform": platform} if platform else {}
    found = _project_endpoints_for_destination(destination, _PIPELINE, binding, dict(os.environ))

    assert found == pinned, found


@pytest.mark.usefixtures("pghostaddr_unfiltered")
@pytest.mark.parametrize(
    "operator",
    [
        _OPERATOR_PG_ENV,
        {"DESTINATION__POSTGRES__CREDENTIALS": _OPERATOR_DSN},
        {"ACME_INGEST__DESTINATION__POSTGRES__CREDENTIALS__HOST": _OPERATOR_HOST},
    ],
)
def test_a_loaded_host_address_would_pin_a_destination_the_operator_exported(
    project: Path, monkeypatch: pytest.MonkeyPatch, operator: Dict[str, str]
) -> None:
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    for name, value in operator.items():
        monkeypatch.setenv(name, value)
    (project / ".env").write_text(f"PGHOSTADDR=10.0.0.1\nPOSTGRES_HOST={_PROJECT_HOST}\n")
    hydrate_dotenv(project)
    assert "PGHOSTADDR" in os.environ

    found = _project_endpoints_for_destination(
        "postgres", _PIPELINE, {"platform": "postgres"}, dict(os.environ)
    )

    # the operator's host keeps POSTGRES_HOST from reaching it, never PGHOSTADDR
    assert found == ["PGHOSTADDR"], found


@pytest.mark.usefixtures("pghostaddr_unfiltered")
def test_a_loaded_host_address_would_pin_a_named_postgres_destination(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    monkeypatch.setenv("DESTINATION__MY_PG__DESTINATION_TYPE", "postgres")
    (project / ".env").write_text("PGHOSTADDR=10.0.0.1\n")
    hydrate_dotenv(project)
    assert "PGHOSTADDR" in os.environ

    found = _project_endpoints_for_destination("my_pg", _PIPELINE, {}, dict(os.environ))

    assert found == ["PGHOSTADDR"], found


@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_dlt_s_connection_string_leaves_only_the_host_address_to_libpq(destination: str) -> None:
    # what dlt hands psycopg2.connect (destinations/impl/postgres/sql_client.py),
    # as libpq reads it
    pytest.importorskip("dlt")
    extensions = pytest.importorskip("psycopg2.extensions")
    from fluid_build.build_runners.dlt.destinations import _credentials_class
    from fluid_build.build_runners.dlt.runner import _DLT_LIBPQ_PARAMETERS, _LIBPQ_ENDPOINTS

    credentials = _credentials_class(destination)()
    credentials.host = "db.example"
    credentials.username = "user"
    credentials.password = "secret"  # pragma: allowlist secret
    credentials.database = "db"
    credentials.resolve()

    sent = set(extensions.parse_dsn(credentials.to_native_representation()))

    assert _DLT_LIBPQ_PARAMETERS <= sent, sent
    assert {name for name, keyword in _LIBPQ_ENDPOINTS.items() if keyword not in sent} == {
        "PGHOSTADDR"
    }


def _operator_pg_secrets(port: int, host: str = _OPERATOR_HOST) -> Dict[str, Any]:
    return {
        "destination.postgres.credentials.host": host,
        "destination.postgres.credentials.port": port,
        "destination.postgres.credentials.username": "operator_user",
        "destination.postgres.credentials.password": OPERATOR_KEY,
        "destination.postgres.credentials.database": "operator_db",
    }


@pytest.mark.parametrize(
    "dotenv",
    ["PGHOSTADDR=127.0.0.1\n", f"PGHOSTADDR=127.0.0.1\nPGPASSWORD={PROJECT_KEY}\n"],
)
@pytest.mark.parametrize("where", ["secrets", "env"])
@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_a_project_host_address_never_receives_the_operator_s_dlt_password(
    tmp_path: Path, dotenv: str, where: str, destination: str
) -> None:
    # a real load: dlt, psycopg2 and libpq against a loopback stand-in at the
    # project's address, on the port of the operator's own destination
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        secrets = {
            key.replace(".postgres.", f".{destination}."): value
            for key, value in _operator_pg_secrets(server.port).items()
        }
        env = {key.upper().replace(".", "__"): str(value) for key, value in secrets.items()}
        builds = dlt_builds_in_one_process(
            tmp_path,
            destination,
            dotenv,
            (),
            operator_env=env if where == "env" else None,
            operator_secrets=secrets if where == "secrets" else None,
            real=True,
        )

    assert server.passwords == [], (builds, server.passwords)
    assert server.connections == 0, builds
    assert builds == [{"rc": 1}], builds


@pytest.mark.parametrize("destination", ["postgres", "redshift"])
def test_a_project_s_own_destination_names_its_address_as_its_host(
    tmp_path: Path, destination: str
) -> None:
    # A project that brings its whole destination still reaches it, through
    # its own dlt host; its PGHOSTADDR (here pointing nowhere) is ignored, so
    # it can route neither its own connection nor the operator's.
    pytest.importorskip("psycopg2")
    section = destination.upper()
    with FakePostgres() as server:
        dotenv = (
            "PGHOSTADDR=127.0.0.2\n"
            f"DESTINATION__{section}__CREDENTIALS__HOST=127.0.0.1\n"
            f"DESTINATION__{section}__CREDENTIALS__PORT={server.port}\n"
            f"DESTINATION__{section}__CREDENTIALS__USERNAME=project_user\n"
            f"DESTINATION__{section}__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
            f"DESTINATION__{section}__CREDENTIALS__DATABASE=project_db\n"
        )
        secrets = {
            key.replace(".postgres.", f".{destination}."): value
            for key, value in _operator_pg_secrets(5432).items()
        }
        builds = dlt_builds_in_one_process(
            tmp_path, destination, dotenv, (), operator_secrets=secrets, real=True
        )

    assert server.passwords and set(server.passwords) == {PROJECT_KEY}, (builds, server.passwords)


@pytest.mark.parametrize("project_address", [True, False])
def test_the_sqlalchemy_destination_s_postgres_url_never_moves_to_a_project_address(
    tmp_path: Path, project_address: bool
) -> None:
    # dlt's sqlalchemy destination hands its URL to psycopg2 as it is, and
    # the runner cannot tell from the destination type that libpq reads
    # PGHOSTADDR there: that the project cannot set it is what keeps the
    # operator's password at the operator's server (127.0.0.2 has no server)
    pytest.importorskip("psycopg2")
    pytest.importorskip("sqlalchemy")
    with FakePostgres() as server:
        host = "127.0.0.2" if project_address else "127.0.0.1"
        url = f"postgresql+psycopg2://operator_user:{OPERATOR_KEY}@{host}:{server.port}/operator_db"
        builds = dlt_builds_in_one_process(
            tmp_path,
            "sqlalchemy",
            "PGHOSTADDR=127.0.0.1\n" if project_address else None,
            (),
            platforms=("",),
            operator_secrets={"destination.sqlalchemy.credentials": url},
            real=True,
        )

    if project_address:
        assert server.passwords == [], (builds, server.passwords)
        assert server.connections == 0, builds
    else:  # not vacuous: at the stand-in's own address the operator's password arrives
        assert server.passwords and set(server.passwords) == {OPERATOR_KEY}, builds


@pytest.mark.parametrize("name", ["PGHOST", "PGPORT"])
def test_libpq_s_host_and_port_never_move_the_operator_s_destination(
    tmp_path: Path, name: str
) -> None:
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        if name == "PGHOST":  # the operator's destination is elsewhere, on the stand-in's port
            dotenv, operator = "PGHOST=127.0.0.1\n", _operator_pg_secrets(server.port)
        else:  # the operator's destination is on loopback, on its own port
            dotenv, operator = f"PGPORT={server.port}\n", _operator_pg_secrets(5432, "127.0.0.1")
        builds = dlt_builds_in_one_process(
            tmp_path, "postgres", dotenv, (), operator_secrets=operator, real=True
        )

    assert server.connections == 0, (builds, server.passwords)


# ── one process, several builds ────────────────────────────────────────────
#
# ``fluid apply`` runs its builds one after another in one process, and goes on
# after a failed one. What the dlt bridge copied into the environment for one
# build goes back before the next takes its snapshot, so that one never takes
# the bridge's copy of a project's host for one the operator exported.

_PROJECT_ONLY_HOST = {
    "POSTGRES_HOST": f"POSTGRES_HOST={_PROJECT_HOST}\n",
    "PG_HOST": f"PG_HOST={_PROJECT_HOST}\n",
}
_PROJECT_WHOLE = {
    "POSTGRES_HOST": (
        f"POSTGRES_HOST={_PROJECT_HOST}\nPOSTGRES_USER=project_user\n"
        f"POSTGRES_PASSWORD={PROJECT_KEY}\nPOSTGRES_DB=project_db\n"
    ),
    "PG_HOST": (
        f"PG_HOST={_PROJECT_HOST}\nPG_USER=project_user\n"
        f"PG_PASSWORD={PROJECT_KEY}\nPG_DB=project_db\n"
    ),
}
_PROJECT_PG = {
    "host": _PROJECT_HOST,
    "port": 5432,
    "username": "project_user",
    "password": PROJECT_KEY,
    "database": "project_db",
}


@pytest.mark.parametrize("name", ["POSTGRES_HOST", "PG_HOST"])
@pytest.mark.parametrize("first", ["fails", "succeeds"])
@pytest.mark.parametrize("builds", [2, 3])
def test_every_build_in_one_process_resolves_as_the_first(
    tmp_path: Path, name: str, first: str, builds: int
) -> None:
    dotenv = (_PROJECT_ONLY_HOST if first == "fails" else _PROJECT_WHOLE)[name]

    runs = dlt_builds_in_one_process(
        tmp_path,
        "postgres",
        dotenv,
        _POSTGRES_FIELDS,
        platforms=("postgres",) * builds,
        operator_secrets=_operator_secrets("postgres"),
    )

    if first == "fails":
        assert set(runs[0]) == {"failed"} and name in runs[0]["failed"], runs
    else:
        assert {field: runs[0][field] for field in _PROJECT_PG} == _PROJECT_PG, runs
    for run in runs:
        _no_operator_material(run)
    assert runs == [runs[0]] * builds, runs


@pytest.mark.parametrize("name", ["POSTGRES_HOST", "PG_HOST"])
@pytest.mark.parametrize("later", ["", "snowflake"])
def test_a_later_build_the_bridge_does_not_serve_gets_no_project_host(
    tmp_path: Path, name: str, later: str
) -> None:
    # the first build's bridge copies the project's host; the second's runs
    # for another platform or not at all, so dlt reads the operator's alone
    runs = dlt_builds_in_one_process(
        tmp_path,
        "postgres",
        _PROJECT_ONLY_HOST[name],
        _POSTGRES_FIELDS,
        platforms=("postgres", later),
        operator_secrets=_operator_secrets("postgres"),
    )

    assert set(runs[0]) == {"failed"}, runs
    assert runs[1].get("host") is None, runs
    assert runs[1].get("password") == OPERATOR_KEY, runs


@pytest.mark.parametrize("names", [("POSTGRES_HOST", "POSTGRES_PORT"), ("PG_HOST", "PG_PORT")])
@pytest.mark.parametrize(
    "platforms", [("postgres", "postgres"), ("postgres",) * 3, ("postgres", "")]
)
def test_a_later_real_build_never_sends_the_operator_s_dlt_password_to_the_project_host(
    tmp_path: Path, names: tuple, platforms: tuple
) -> None:
    pytest.importorskip("psycopg2")
    host, port = names
    with FakePostgres() as server:
        builds = dlt_builds_in_one_process(
            tmp_path,
            "postgres",
            f"{host}=127.0.0.1\n{port}={server.port}\n",
            (),
            platforms=platforms,
            operator_secrets={
                key: value
                for key, value in _operator_pg_secrets(5432).items()
                if not key.endswith((".host", ".port"))
            },
            real=True,
        )

    assert server.passwords == [], (builds, server.passwords)
    assert server.connections == 0, builds
    assert builds == [{"rc": 1}] * len(platforms), builds


def test_a_later_real_build_of_the_project_s_whole_destination_stays_the_project_s(
    tmp_path: Path,
) -> None:
    pytest.importorskip("psycopg2")
    with FakePostgres() as server:
        dotenv = (
            f"POSTGRES_HOST=127.0.0.1\nPOSTGRES_PORT={server.port}\nPOSTGRES_USER=project_user\n"
            f"POSTGRES_PASSWORD={PROJECT_KEY}\nPOSTGRES_DB=project_db\n"
        )
        builds = dlt_builds_in_one_process(
            tmp_path,
            "postgres",
            dotenv,
            (),
            platforms=("postgres",) * 3,
            operator_secrets=_operator_pg_secrets(5432),
            real=True,
        )

    assert OPERATOR_KEY not in server.passwords, (builds, server.passwords)
    assert server.passwords and set(server.passwords) == {PROJECT_KEY}, server.passwords


def test_the_bridge_s_copies_are_put_back(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners._credentials import make_destination
    from fluid_build.build_runners.dlt.destinations import undo_bridge_writes

    monkeypatch.setenv("DESTINATION__POSTGRES__CREDENTIALS__HOST", "")
    (project / ".env").write_text(_COMPOSE_ENV)
    hydrate_dotenv(project)

    make_destination("dlt", "postgres", binding={"platform": "postgres"})
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__HOST"] == "db"
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__PORT"] == "5432"
    os.environ["DESTINATION__POSTGRES__CREDENTIALS__DATABASE"] = "set-since"
    undo_bridge_writes()

    # blank as before, unset as before, and a value set since stays
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__HOST"] == ""
    assert "DESTINATION__POSTGRES__CREDENTIALS__PORT" not in os.environ
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__DATABASE"] == "set-since"
    undo_bridge_writes()  # nothing left to undo
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__DATABASE"] == "set-since"


@pytest.mark.parametrize("name", ["POSTGRES_HOST", "PG_HOST"])
def test_a_copy_the_bridge_left_behind_is_never_the_operator_s_host(
    project: Path, name: str
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners._credentials import make_destination
    from fluid_build.build_runners.dlt import destinations
    from fluid_build.build_runners.dlt.runner import _project_endpoints_for_destination

    (project / ".env").write_text(_PROJECT_ONLY_HOST[name])
    hydrate_dotenv(project)
    make_destination("dlt", "postgres", binding={"platform": "postgres"})
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__HOST"] == _PROJECT_HOST

    try:
        found = _project_endpoints_for_destination(
            "postgres", _PIPELINE, {"platform": "postgres"}, dict(os.environ)
        )
        assert found == [name], found
    finally:
        destinations.undo_bridge_writes()


def test_the_bridge_puts_back_what_was_there_when_it_last_wrote(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("dlt")
    from fluid_build.build_runners._credentials import make_destination
    from fluid_build.build_runners.dlt.destinations import undo_bridge_writes

    (project / ".env").write_text(_COMPOSE_ENV)
    hydrate_dotenv(project)
    make_destination("dlt", "postgres", binding={"platform": "postgres"})
    # something else drops the bridge's copy (a caller's clean-up, a test's
    # teardown), and the operator's blank variable takes its place
    del os.environ["DESTINATION__POSTGRES__CREDENTIALS__HOST"]
    monkeypatch.setenv("DESTINATION__POSTGRES__CREDENTIALS__HOST", "")
    make_destination("dlt", "postgres", binding={"platform": "postgres"})
    assert os.environ["DESTINATION__POSTGRES__CREDENTIALS__HOST"] == "db"

    undo_bridge_writes()

    assert os.environ.get("DESTINATION__POSTGRES__CREDENTIALS__HOST") == ""
