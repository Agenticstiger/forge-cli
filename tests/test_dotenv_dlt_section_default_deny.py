# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A project may set no dlt setting that chooses where the data goes or which
account it uses, beyond the destination's credentials (default-deny).

``test_dotenv_dlt_default_deny.py`` covers the destination's credentials. dlt
reads more than that from the same environment: the destination's other
configuration (lance's ``storage`` and ``embeddings``, the filesystem's
``bucket_url`` and ``kwargs``), the type and name the destination is resolved
under (``DESTINATION__<NAME>__DESTINATION_TYPE``), and the pipeline's staging
destination (``staging_type``, ``staging_name``, then that destination's own
configuration). A value a project dotenv file supplied for one of them is
layered on the operator's own configuration, so the runner refuses the build
(``runner._project_dlt_settings``). The exceptions are the boolean and integer
fields of a destination, ``environment``, and the named destination's own type
while the project's credentials pin it. What the operator sets themselves
applies as before. Real dlt is the oracle for which names it reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest

pytest.importorskip("dotenv")
pytest.importorskip("dlt")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    PROJECT_KEY,
    _dlt_contract,
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
)

_PIPELINE = "acme_ingest"


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


def _settings(
    project: Path,
    dotenv: str,
    destination: str,
    *,
    pinned: bool = False,
    staging: Optional[Tuple[str, str]] = None,
) -> List[str]:
    """What the runner refuses of a project whose ``.env`` is ``dotenv``."""
    from fluid_build.build_runners.dlt.runner import _project_dlt_settings

    (project / ".env").write_text(dotenv)
    hydrate_dotenv(project)
    return _project_dlt_settings(destination, _PIPELINE, pinned=pinned, staging=staging)


# ── real dlt as the oracle: the names it reads, and the runner refuses ─────


def _dlt_reads(
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    name: str,
    value: str,
    path: Sequence[str],
    expected: Any,
) -> bool:
    """Whether real dlt, resolving ``reference``'s configuration in the run's
    pipeline with ``name`` set to ``value``, ends up with ``expected`` at
    ``path`` of it."""
    from dlt.common.configuration.inject import inject_section
    from dlt.common.configuration.specs import ConfigSectionContext
    from dlt.common.destination import Destination

    if "STORAGE__CREDENTIALS" in name:
        # the operator's own S3 bucket selects the nested credentials' type
        monkeypatch.setenv("DESTINATION__LANCE__STORAGE__BUCKET_URL", "s3://operator-bucket/lake")
        monkeypatch.setenv("DESTINATION__LANCE__STORAGE__CREDENTIALS__REGION_NAME", "eu-west-1")
    if "EMBEDDINGS" in name:
        # dlt builds the embeddings configuration once it has a provider and a model
        monkeypatch.setenv("DESTINATION__LANCE__EMBEDDINGS__PROVIDER", "cohere")
        monkeypatch.setenv("DESTINATION__LANCE__EMBEDDINGS__NAME", "embed-model")
    monkeypatch.setenv(name, value)
    try:
        destination = Destination.from_reference(reference)
        with inject_section(ConfigSectionContext(pipeline_name=_PIPELINE)):
            config: Any = destination.configuration(
                destination.spec()._bind_dataset_name(dataset_name="x"), accept_partial=True
            )
    finally:
        monkeypatch.delenv(name, raising=False)
    for attribute in path:
        config = getattr(config, attribute, None)
    return bool(config == expected)


_ENDPOINT = "https://203.0.113.9"

# (destination, name, value, where dlt puts it)
_READ_BY_DLT: List[Tuple[str, str, str, Tuple[str, ...]]] = [
    ("filesystem", "DESTINATION__FILESYSTEM__BUCKET_URL", "file:///project/x", ("bucket_url",)),
    ("filesystem", "DESTINATION__BUCKET_URL", "file:///project/x", ("bucket_url",)),
    ("filesystem", "BUCKET_URL", "file:///project/x", ("bucket_url",)),
    ("filesystem", f"{_PIPELINE.upper()}__BUCKET_URL", "file:///project/x", ("bucket_url",)),
    (
        "filesystem",
        f"{_PIPELINE.upper()}__DESTINATION__FILESYSTEM__BUCKET_URL",
        "file:///project/x",
        ("bucket_url",),
    ),
    (
        "lance",
        "DESTINATION__LANCE__STORAGE__BUCKET_URL",
        "file:///project/x",
        ("storage", "bucket_url"),
    ),
    ("lance", "DESTINATION__STORAGE__BUCKET_URL", "file:///project/x", ("storage", "bucket_url")),
    ("lance", "STORAGE__BUCKET_URL", "file:///project/x", ("storage", "bucket_url")),
    (
        "lance",
        f"{_PIPELINE.upper()}__DESTINATION__LANCE__STORAGE__BUCKET_URL",
        "file:///project/x",
        ("storage", "bucket_url"),
    ),
    (
        "lance",
        "DESTINATION__LANCE__STORAGE__CREDENTIALS__ENDPOINT_URL",
        _ENDPOINT,
        ("storage", "credentials", "endpoint_url"),
    ),
    (
        "lance",
        "DESTINATION__LANCE__STORAGE__NAMESPACE_NAME",
        "project-ns",
        ("storage", "namespace_name"),
    ),
    (
        "lance",
        "DESTINATION__LANCE__EMBEDDINGS__CREDENTIALS__API_KEY",
        "project-embeddings-key",
        ("embeddings", "credentials", "api_key"),
    ),
    ("lance", "DESTINATION__LANCE__CATALOG_TYPE", "rest", ("catalog_type",)),
    ("lance", "DESTINATION__LANCE__BRANCH_NAME", "project-branch", ("branch_name",)),
    ("snowflake", "DESTINATION__SNOWFLAKE__STAGE_NAME", "project_stage", ("stage_name",)),
    ("snowflake", "DESTINATION__SNOWFLAKE__QUERY_TAG", "project-tag", ("query_tag",)),
    ("snowflake", "DESTINATION__SNOWFLAKE__DESTINATION_NAME", "evil", ("destination_name",)),
]


@pytest.mark.parametrize(
    "destination,name,value,path",
    _READ_BY_DLT,
    ids=[f"{d}-{n}" for d, n, _, _ in _READ_BY_DLT],
)
def test_every_name_dlt_reads_a_destination_setting_under_is_refused(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    name: str,
    value: str,
    path: Tuple[str, ...],
) -> None:
    from fluid_build.build_runners.dlt.runner import _dlt_destination

    assert _dlt_reads(monkeypatch, destination, name, value, path, value), "dlt does not read it"

    section, _ = _dlt_destination(destination)
    assert section == destination
    assert _settings(project, f"{name}={value}\n", destination) == [name]


@pytest.mark.parametrize(
    "name",
    [
        "STAGING_TYPE",
        "PIPELINES__STAGING_TYPE",
        f"PIPELINES__{_PIPELINE.upper()}__STAGING_TYPE",
        f"{_PIPELINE.upper()}__STAGING_TYPE",
        f"{_PIPELINE.upper()}__PIPELINES__{_PIPELINE.upper()}__STAGING_TYPE",
    ],
)
def test_every_name_dlt_reads_the_staging_type_under_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # real dlt builds the pipeline's staging destination from each of them
    import dlt

    monkeypatch.setenv("DLT_DATA_DIR", str(project / "dlt-data"))
    monkeypatch.setenv(name, "filesystem")
    built = dlt.pipeline(pipeline_name=_PIPELINE, destination="snowflake")
    monkeypatch.delenv(name)
    assert built.staging is not None, "dlt does not read it"

    assert _settings(project, f"{name}=filesystem\n", "snowflake") == [name]


@pytest.mark.parametrize(
    "name",
    [
        "STAGING_NAME",
        "DESTINATION_TYPE",
        "DESTINATION_NAME",
        "PIPELINES__DESTINATION_NAME",
        f"{_PIPELINE.upper()}__PIPELINES__STAGING_NAME",
        f"PIPELINES__{_PIPELINE.upper()}__DESTINATION_TYPE",
    ],
)
def test_the_other_pipeline_settings_that_choose_a_destination_are_refused(
    project: Path, name: str
) -> None:
    assert _settings(project, f"{name}=x\n", "snowflake") == [name]


@pytest.mark.parametrize("pipeline_name", ["acme_ingest", "sources", "a__destination__postgres"])
def test_the_pipeline_names_are_the_ones_dlt_looks_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pipeline_name: str
) -> None:
    import dlt
    from dlt.common.configuration.providers.environ import EnvironProvider

    from fluid_build.build_runners.dlt.runner import _dlt_pipeline_setting_names

    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt-data"))
    looked_up: List[str] = []
    real_get_value = EnvironProvider.get_value

    def recording(self: Any, key: str, hint: Any, pipeline: Any, *sections: str) -> Any:
        value, name = real_get_value(self, key, hint, pipeline, *sections)
        looked_up.append(name)
        return value, name

    monkeypatch.setattr(EnvironProvider, "get_value", recording)
    dlt.pipeline(pipeline_name=pipeline_name, destination="snowflake")

    wanted = _dlt_pipeline_setting_names(pipeline_name)
    seen = {
        name
        for name in looked_up
        if name.endswith(("STAGING_TYPE", "STAGING_NAME"))
        and not name.startswith("DESTINATION__")
        and "CONFIG__" not in name
    }
    assert seen and seen <= wanted, sorted(seen - wanted)


# ── the probes, by their names ──────────────────────────────────────────────


def test_a_project_lance_storage_and_embeddings_are_refused(project: Path) -> None:
    dotenv = (
        "DESTINATION__LANCE__STORAGE__BUCKET_URL=file:///project-bucket/lake\n"
        f"DESTINATION__LANCE__STORAGE__CREDENTIALS__ENDPOINT_URL={_ENDPOINT}\n"
        f"DESTINATION__LANCE__EMBEDDINGS__CREDENTIALS__API_KEY={PROJECT_KEY}\n"
    )

    found = _settings(project, dotenv, "lance")

    assert found == [
        "DESTINATION__LANCE__EMBEDDINGS__CREDENTIALS__API_KEY",
        "DESTINATION__LANCE__STORAGE__BUCKET_URL",
        "DESTINATION__LANCE__STORAGE__CREDENTIALS__ENDPOINT_URL",
    ]


def test_a_project_staging_config_of_the_destination_is_refused(project: Path) -> None:
    # the destination's own staging configuration (the names dlt looks up while
    # it resolves it, as the recorded lookups of every destination show)
    found = _settings(
        project,
        "DESTINATION__POSTGRES__STAGING_CONFIG__BUCKET_URL=file:///project/x\n"
        "DESTINATION__STAGING_CONFIG__LAYOUT=x\n",
        "postgres",
    )

    assert found == [
        "DESTINATION__POSTGRES__STAGING_CONFIG__BUCKET_URL",
        "DESTINATION__STAGING_CONFIG__LAYOUT",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "RUNTIME__SENTRY_DSN",
        "RUNTIME__SLACK_INCOMING_HOOK",
        "RUNTIME__DLTHUB_TELEMETRY_ENDPOINT",
        f"{_PIPELINE.upper()}__RUNTIME__SENTRY_DSN",
        "PIPELINES__RUNTIME__SLACK_INCOMING_HOOK",
        f"PIPELINES__{_PIPELINE.upper()}__RUNTIME__DLTHUB_TELEMETRY_ENDPOINT",
    ],
)
def test_a_project_url_the_run_reports_to_is_refused(project: Path, name: str) -> None:
    """Exception reports, load notices and telemetry go to the URL these name."""
    assert _settings(project, f"{name}=https://collector.example/x\n", "snowflake") == [name]


def test_a_url_the_operator_exports_for_the_run_is_not_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNTIME__SENTRY_DSN", "https://operator.example/1")
    assert _settings(project, "", "snowflake") == []


def test_a_project_staging_type_is_refused(project: Path) -> None:
    found = _settings(project, "STAGING_TYPE=filesystem\n", "snowflake")

    assert found == ["STAGING_TYPE"]


def test_a_project_destination_type_of_a_named_destination_is_refused(project: Path) -> None:
    found = _settings(project, "DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n", "my_wh")

    assert found == ["DESTINATION__MY_WH__DESTINATION_TYPE"]


def test_a_project_destination_type_is_the_project_s_while_its_credentials_pin_the_destination(
    project: Path,
) -> None:
    # The destination is then the project's wholly: its credentials are built
    # from the project's values only (_destination_with_project_credentials).
    found = _settings(
        project,
        f"DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n"
        f"{_PIPELINE.upper()}__DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n",
        "my_wh",
        pinned=True,
    )

    assert found == []


def test_pinning_does_not_free_the_staging_or_the_rest(project: Path) -> None:
    found = _settings(
        project,
        "DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n"
        "STAGING_TYPE=filesystem\n"
        "DESTINATION__MY_WH__STAGE_NAME=project_stage\n",
        "my_wh",
        pinned=True,
    )

    assert found == ["DESTINATION__MY_WH__STAGE_NAME", "STAGING_TYPE"]


def test_the_staging_destination_s_own_settings_and_credentials_are_refused(
    project: Path,
) -> None:
    # Once dlt has resolved the operator's staging destination, a project value
    # for its bucket or its credentials would be layered on the operator's.
    dotenv = (
        "DESTINATION__FILESYSTEM__BUCKET_URL=s3://project-bucket/stage\n"
        f"DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL={_ENDPOINT}\n"
        "DESTINATION__CREDENTIALS__AWS_ACCESS_KEY_ID=project-key-id\n"  # pragma: allowlist secret
        "DESTINATION__SNOWFLAKE__STAGE_NAME=not_the_staging_destination\n"
    )

    found = _settings(project, dotenv, "snowflake", staging=("filesystem", "filesystem"))

    assert found == [
        "DESTINATION__CREDENTIALS__AWS_ACCESS_KEY_ID",
        "DESTINATION__FILESYSTEM__BUCKET_URL",
        "DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL",
    ]


# ── the allow-list ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        # a label dlt never reads again, and a common variable of its own
        "ENVIRONMENT=production",
        "DESTINATION__POSTGRES__ENVIRONMENT=production",
        # booleans and integers cannot name a place or a credential
        "DESTINATION__POSTGRES__CREATE_INDEXES=true",
        "DESTINATION__POSTGRES__ENABLE_DATASET_NAME_NORMALIZATION=false",
        "DESTINATION__POSTGRES__INFO_TABLES_QUERY_THRESHOLD=10",
        "DESTINATION__POSTGRES__TRUNCATE_TABLES_ON_STAGING_DESTINATION_BEFORE_LOAD=true",
        # another destination's settings are not read in this run
        "DESTINATION__BIGQUERY__LOCATION=EU",
        "DESTINATION__SNOWFLAKE__STAGE_NAME=snowflake_stage",
        # the credentials have their own rule (test_dotenv_dlt_default_deny.py)
        "DESTINATION__POSTGRES__CREDENTIALS__DATABASE=db",
        "DESTINATION__POSTGRES__CREDENTIALS__HOST=db.project.test",
        # settings that are not about the destination or staging
        "SOURCES__SQL_DATABASE__CREDENTIALS=project",
        "NORMALIZE__WORKERS=2",
        "BUCKET=my-project-bucket",
    ],
)
def test_the_settings_a_project_may_still_set(project: Path, line: str) -> None:
    assert _settings(project, f"{line}\n", "postgres") == []


def test_the_allowed_fields_are_a_label_and_the_flags_and_counts_of_dlt_s_destinations() -> None:
    # beside its credentials, a destination's own fields a project may set: no
    # field of a type that can name a place or a credential
    import dlt

    from fluid_build.build_runners.dlt.runner import (
        _DLT_PROJECT_SETTINGS_ALLOWED,
        _dlt_is_flag_or_count,
    )

    assert _DLT_PROJECT_SETTINGS_ALLOWED == {"environment"}
    flags = set()
    for kind in ("postgres", "snowflake", "filesystem", "lance", "duckdb", "bigquery"):
        spec = getattr(dlt.destinations, kind)().spec()
        for field, hint in spec.get_resolvable_fields().items():
            if _dlt_is_flag_or_count(hint):
                assert str(hint) in ("<class 'bool'>", "<class 'int'>"), (kind, field, hint)
                flags.add(field)
    assert {"create_indexes", "info_tables_query_threshold"} <= flags
    assert not flags & {"bucket_url", "destination_type", "destination_name", "kwargs"}


# ── the operator's own environment applies as before ───────────────────────


def test_what_the_operator_exports_is_not_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAGING_TYPE", "filesystem")
    monkeypatch.setenv("DESTINATION__FILESYSTEM__BUCKET_URL", "s3://operator-bucket/stage")
    monkeypatch.setenv("DESTINATION__LANCE__STORAGE__BUCKET_URL", "s3://operator-bucket/lake")

    # the project's file is loaded behind the operator's, which keeps its own value
    found = _settings(
        project,
        "STAGING_TYPE=filesystem\n"
        "DESTINATION__FILESYSTEM__BUCKET_URL=s3://project-bucket/stage\n"
        "DESTINATION__LANCE__STORAGE__BUCKET_URL=file:///project-bucket/lake\n",
        "lance",
    )

    assert found == []
    assert os.environ["DESTINATION__LANCE__STORAGE__BUCKET_URL"] == "s3://operator-bucket/lake"


def test_a_destination_dlt_cannot_introspect_refuses_every_name_under_its_section(
    project: Path,
) -> None:
    found = _settings(
        project,
        "DESTINATION__NOT_A_DESTINATION__ANYTHING=1\nDESTINATION__POSTGRES__ANYTHING=1\n",
        "not_a_destination",
    )

    assert found == ["DESTINATION__NOT_A_DESTINATION__ANYTHING"]


# ── through the runner ──────────────────────────────────────────────────────

_DRIVER = """
import json, os, sys
from pathlib import Path

import dlt

project = Path(sys.argv[1])
contract = json.loads((project / "contract.json").read_text())

from fluid_build.cli._common import hydrate_dotenv

hydrate_dotenv(project)
import fluid_build.build_runners.dlt.runner as runner


class Done(BaseException):
    pass


real_pipeline = dlt.pipeline


def pipeline(**kwargs):
    built = real_pipeline(**kwargs)
    destination = built.destination
    staging = built.staging
    seen = {
        "built": True,
        "destination": destination.destination_name + ":" + destination.destination_type.rsplit(".", 1)[-1],
        "staging": staging.destination_name if staging is not None else None,
    }

    def run(*args, **kw):
        raise Done(seen)

    built.run = run
    return built


def failed(ctx, started_at, err, t_start):
    raise Done({"failed": err})


dlt.pipeline = pipeline
runner._failed_result = failed
try:
    runner.execute_dlt_build(contract["builds"][0], contract, project)
except Done as done:
    print(json.dumps(done.args[0]))
else:
    raise SystemExit("the dlt runner neither built its pipeline nor failed")
"""


def _run(
    tmp_path: Path,
    destination: str,
    dotenv: str,
    operator_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """``fluid``'s dlt runner on a CSV source into ``destination``, in a fresh
    interpreter, with ``dotenv`` as the project's ``.env`` and ``operator_env``
    as the whole environment (besides what Python needs): what it built its
    pipeline with, or why it failed."""
    root = tmp_path / "dlt-project"
    root.mkdir()
    (root / "in.csv").write_text("id\n1\n")
    (root / "contract.json").write_text(json.dumps(_dlt_contract(root, destination, None, "")))
    (root / ".env").write_text(dotenv)
    os.chmod(root / ".env", 0o600)
    home = tmp_path / "dlt-home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DLT_DATA_DIR": str(root / "dlt-data"),
        **(operator_env or {}),
    }
    done = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return dict(json.loads(done.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(
    "destination,dotenv,names",
    [
        ("snowflake", "STAGING_TYPE=filesystem\n", ["STAGING_TYPE"]),
        (
            "lance",
            "DESTINATION__LANCE__STORAGE__BUCKET_URL=file:///project-bucket/lake\n"
            f"DESTINATION__LANCE__EMBEDDINGS__CREDENTIALS__API_KEY={PROJECT_KEY}\n",
            [
                "DESTINATION__LANCE__EMBEDDINGS__CREDENTIALS__API_KEY",
                "DESTINATION__LANCE__STORAGE__BUCKET_URL",
            ],
        ),
        ("my_wh", "DESTINATION__MY_WH__DESTINATION_TYPE=postgres\n", None),
        (
            "filesystem",
            f'DESTINATION__FILESYSTEM__KWARGS={{"endpoint_url": "{_ENDPOINT}"}}\n',
            None,
        ),
        ("duckdb", "DESTINATION__DUCKDB__LOCAL_DIR=/tmp\nSTAGING_NAME=x\n", ["STAGING_NAME"]),
        ("duckdb", "RUNTIME__SENTRY_DSN=https://collector.example/1\n", None),
        ("duckdb", "RUNTIME__SLACK_INCOMING_HOOK=https://collector.example/2\n", None),
        ("duckdb", "RUNTIME__DLTHUB_TELEMETRY_ENDPOINT=https://collector.example/3\n", None),
    ],
)
def test_the_runner_refuses_a_build_whose_project_sets_such_a_setting(
    tmp_path: Path, destination: str, dotenv: str, names: Optional[List[str]]
) -> None:
    operator = {
        "DESTINATION__MY_WH__CREDENTIALS__HOST": "operator-db.example.test",
        "DESTINATION__MY_WH__CREDENTIALS__USERNAME": "operator",
        "DESTINATION__MY_WH__CREDENTIALS__PASSWORD": "operator-secret",  # pragma: allowlist secret
        "DESTINATION__MY_WH__CREDENTIALS__DATABASE": "ops",
    }

    result = _run(tmp_path, destination, dotenv, operator)

    assert "failed" in result, result
    message = result["failed"]
    assert "the project's dotenv files set dlt settings" in message
    for name in names or [dotenv.split("=", 1)[0]]:
        assert name in message
    # a refusal names the settings, never a value
    assert PROJECT_KEY not in message and "operator-secret" not in message


def test_the_runner_builds_when_the_operator_sets_the_staging_destination(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "snowflake",
        "NORMALIZE__WORKERS=2\n",
        {
            "STAGING_TYPE": "filesystem",
            "DESTINATION__FILESYSTEM__BUCKET_URL": "file:///operator-bucket",
        },
    )

    assert result == {"built": True, "destination": "snowflake:snowflake", "staging": "filesystem"}


def test_the_runner_refuses_a_project_value_for_the_operator_s_staging_destination(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "snowflake",
        f"DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL={_ENDPOINT}\n",
        {
            "STAGING_TYPE": "filesystem",
            "DESTINATION__FILESYSTEM__BUCKET_URL": "file:///operator-bucket",
        },
    )

    assert "failed" in result, result
    assert "DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL" in result["failed"]
    assert "(staging)" in result["failed"]


def test_the_runner_builds_a_named_destination_the_project_defines_whole(tmp_path: Path) -> None:
    section = "DESTINATION__MY_WH__CREDENTIALS__"
    result = _run(
        tmp_path,
        "my_wh",
        "DESTINATION__MY_WH__DESTINATION_TYPE=snowflake\n"
        f"{section}HOST=xy12345\n{section}USERNAME=project_user\n"
        f"{section}PASSWORD={PROJECT_KEY}\n{section}DATABASE=DB\n",
    )

    assert result == {"built": True, "destination": "my_wh:snowflake", "staging": None}


def test_the_runner_builds_with_what_the_operator_exports(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "my_wh",
        "",
        {
            "DESTINATION__MY_WH__DESTINATION_TYPE": "postgres",
            "DESTINATION__MY_WH__CREDENTIALS__HOST": "operator-db.example.test",
            "DESTINATION__MY_WH__CREDENTIALS__USERNAME": "operator",
            "DESTINATION__MY_WH__CREDENTIALS__PASSWORD": "operator-secret",  # pragma: allowlist secret
            "DESTINATION__MY_WH__CREDENTIALS__DATABASE": "ops",
        },
    )

    assert result == {"built": True, "destination": "my_wh:postgres", "staging": None}
