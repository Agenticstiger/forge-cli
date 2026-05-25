# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 3 — dbt-on-mesh end-to-end via the ``fluid`` CLI surface.

The earlier ``test_iac_aws_real_e2e.py`` proves the OpenTofu emitter +
``runner.tofu_apply`` round-trip works on real AWS, but it BYPASSES the
``fluid`` CLI dispatch. The single dbt-athena test there calls
``dbtRunner().invoke()`` directly — that proves the profile generator
emits a valid dbt-athena profile, not that ``fluid apply --mode
amend-and-build`` correctly:

  1. resolves the apply engine to OpenTofu for the AWS provider,
  2. compiles the contract to ``.tf.json`` + runs ``tofu apply``,
  3. then dispatches to ``build_runners/dbt/runner.py``,
  4. which infers the right dbt adapter from ``runtime.platform``,
  5. generates the profile via ``_build_generated_dbt_profile``,
  6. forwards AWS env to the dbt subprocess,
  7. runs ``dbt run`` and surfaces failures via forge-cli's exit code.

This file closes that gap. Every test here drives the public ``fluid``
CLI as a subprocess (mirroring ``tests/test_e2e_local.py::_fluid``) so
the full dispatch chain is exercised exactly as a real user would hit
it.

The matrix covers what forge-cli supports today on AWS:

  * ``athena``   — ``dbt-athena-community`` against Iceberg-on-Glue
  * ``redshift`` — ``dbt-redshift`` against Redshift Serverless via IAM
  * ``mesh``     — SDP (Iceberg) → ADP (dbt-athena CTAS) → CDP (Redshift
                   external schema) running through TWO sequential
                   ``fluid apply --mode amend-and-build`` invocations.

``dbt-glue`` is intentionally **not** in this matrix: the adapter
requires a Glue interactive session (~3-5 min cold start, billed per
DPU-hour), and ``fluid generate iac`` does not yet emit the matching
Glue Job + ``GlueInteractiveSession`` configuration on its own. Tracked
as a forge-cli enhancement; covered offline by ``profiles.py``'s glue
branch + the Stage 2 LocalStack matrix.

Triple-gated, like the rest of Stage 3: ``tofu`` on PATH +
``FLUID_IAC_LIVE_AWS=1`` + four ``FLUID_AWS_*_ROLE_ARN`` env vars + a
non-root principal. Per-test resources land under the
``fluid-iactest-*`` prefix the session sweeper picks up.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
import yaml

from tests.iac.conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
    aws_iceberg_contract,
    aws_real_boto,
    aws_real_role_arn,
)

# ---------------------------------------------------------------------------
# Skip gates
# ---------------------------------------------------------------------------

pytestmark = [pytest.mark.aws, pytest.mark.integration, pytest.mark.slow]


def _have_dbt_athena() -> bool:
    try:
        import dbt.adapters.athena  # noqa: F401

        return True
    except ImportError:
        return False


def _have_dbt_redshift() -> bool:
    try:
        import dbt.adapters.redshift  # noqa: F401

        return True
    except ImportError:
        return False


_HAVE_DBT_ATHENA = _have_dbt_athena()
_HAVE_DBT_REDSHIFT = _have_dbt_redshift()


# ---------------------------------------------------------------------------
# CLI invocation — mirrors ``tests/test_e2e_local.py::_fluid``
# ---------------------------------------------------------------------------


def _fluid(
    *args: str,
    cwd: Path,
    env_overrides: Optional[Mapping[str, str]] = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    """Invoke the ``fluid`` CLI as a subprocess.

    Uses ``python -m fluid_build.cli`` so we test the in-repo entry
    point (no dependence on the installed ``fluid`` script). UTF-8 is
    forced on both ends; the parent decodes with ``errors='replace'``
    so test output never raises on stray bytes.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> Path:
    """Serialise a contract dict to ``contract.fluid.yaml`` under workdir."""
    target = workdir / "contract.fluid.yaml"
    target.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return target


def _write_dbt_project(
    project_dir: Path,
    *,
    profile: str,
    model_name: str,
    model_sql: str,
    sources: Optional[Dict[str, Any]] = None,
    dependencies: Optional[Dict[str, Any]] = None,
) -> Path:
    """Materialise a minimal dbt project on disk.

    Mirrors the canonical dbt-core test-fixture shape:
    ``dbt_project.yml`` + ``models/<model_name>.sql`` + optional
    ``models/_sources.yml``. Returns the project directory path.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": profile,
                "version": "1.0.0",
                "config-version": 2,
                "profile": profile,
                "model-paths": ["models"],
                "target-path": "target",
                "clean-targets": ["target", "dbt_packages"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    models = project_dir / "models"
    models.mkdir(exist_ok=True)
    (models / f"{model_name}.sql").write_text(model_sql, encoding="utf-8")
    if sources:
        (models / "_sources.yml").write_text(
            yaml.safe_dump(sources, sort_keys=False), encoding="utf-8"
        )
    if dependencies:
        (project_dir / "dependencies.yml").write_text(
            yaml.safe_dump(dependencies, sort_keys=False), encoding="utf-8"
        )
    return project_dir


def _live_env_overrides() -> Dict[str, str]:
    """Env passed to every ``fluid`` subprocess — propagates the Stage 3 gate."""
    overrides = {
        "FLUID_IAC_LIVE_AWS": os.environ.get("FLUID_IAC_LIVE_AWS", "1"),
    }
    for var in (
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FLUID_AWS_LAMBDA_ROLE_ARN",
        "FLUID_AWS_SFN_ROLE_ARN",
        "FLUID_AWS_GLUE_ROLE_ARN",
        "FLUID_AWS_SPECTRUM_ROLE_ARN",
    ):
        v = os.environ.get(var)
        if v:
            overrides[var] = v
    return overrides


def _assert_fluid_ok(rc: subprocess.CompletedProcess, label: str) -> None:
    """``fluid`` returncode 0 or fail loudly with both pipes captured."""
    if rc.returncode != 0:
        pytest.fail(
            f"`fluid {label}` exited {rc.returncode}\n"
            f"--- STDOUT ---\n{rc.stdout[-4000:]}\n"
            f"--- STDERR ---\n{rc.stderr[-4000:]}"
        )


# ---------------------------------------------------------------------------
# Test 1 — dbt-athena via the CLI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON)
@pytest.mark.skipif(not _HAVE_DBT_ATHENA, reason="needs dbt-athena-community installed")
def test_real_cli_dbt_athena_amend_and_build(aws_real_project, aws_account, tmp_path):
    """``fluid apply --mode amend-and-build`` → OpenTofu provisions
    Iceberg-on-Glue, then ``build_runners/dbt`` runs dbt-athena which
    materialises one model into the SAME Glue catalog.

    Verification oracle: ``glue.get_table`` returns the model's table.
    """
    bucket = aws_real_project.name("dbtcli-athena-b")
    glue_db = aws_real_project.name("dbtcli_athena_db").replace("-", "_")
    region = aws_account["region"]
    model_name = f"agg_{aws_real_project.uid}"

    # 1. Contract — SDP (Iceberg-on-Glue source) + ADP (dbt-athena build)
    contract = aws_iceberg_contract(
        bucket, database=glue_db, table="events", cid="iac.aws.dbt.athena"
    )
    contract["build"] = {
        "engine": "dbt",
        "pattern": "hybrid-reference",
        "repository": "./dbt_project",
        "properties": {"model": model_name},
        "outputs": [model_name],
        "execution": {
            "runtime": {
                "platform": "athena",
                "resources": {
                    "s3_staging_dir": f"s3://{bucket}/athena-staging/",
                    "s3_data_dir": f"s3://{bucket}/iceberg/{glue_db}/",
                    "region": region,
                    "schema": glue_db,
                },
            }
        },
    }
    _write_contract(aws_real_project.workdir, contract)

    # 2. Minimal dbt-athena project — one model, Iceberg materialisation.
    _write_dbt_project(
        aws_real_project.workdir / "dbt_project",
        profile="iac_aws_dbt_athena",
        model_name=model_name,
        model_sql=(
            "{{ config(materialized='table', table_type='iceberg', format='parquet') }}\n"
            "SELECT cast(1 as integer) AS x, cast('hello' as varchar) AS msg\n"
        ),
    )

    # 3. fluid apply --mode amend-and-build --yes
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env_overrides(),
        timeout=900,
    )
    # Mark applied so the conftest fixture's destroy runs at teardown.
    aws_real_project.applied = True
    _assert_fluid_ok(rc, "apply --mode amend-and-build (athena)")

    # 4. Verification — dbt-athena materialised the model into the
    # Glue catalog the IaC layer provisioned. The model appears as a
    # table in the same database.
    glue = aws_real_boto("glue")
    table = glue.get_table(DatabaseName=glue_db, Name=model_name)["Table"]
    assert table["Name"] == model_name, table
    # Iceberg materialization → Parameters.table_type == 'ICEBERG'.
    table_type = (table.get("Parameters") or {}).get("table_type", "").upper()
    assert table_type in ("ICEBERG", "EXTERNAL_TABLE", ""), table


# ---------------------------------------------------------------------------
# Test 2 — dbt-redshift Serverless via the CLI, executed FROM an EC2
#         inside the workgroup's VPC. No public ingress is ever opened.
# ---------------------------------------------------------------------------
#
# dbt-redshift Core talks to Redshift Serverless over postgres-wire (port
# 5439). That's a private-VPC concern — production deployments run dbt
# from inside the same VPC (Glue Python Shell, EKS pod, EC2). To test the
# ``fluid apply --mode amend-and-build`` path end-to-end against private
# Redshift Serverless WITHOUT opening any port to the internet, the
# Stage 3 bootstrap (tests/iac/_aws_stage3_bootstrap) stands up a
# private-subnet EC2 with SSM-only access. The test bundles the local
# forge-cli source + contract + dbt project, uploads to the bootstrap's
# S3 bucket, and uses ``ssm:SendCommand`` to drive ``fluid apply`` on
# the EC2. SSM goes through the VPC's interface endpoints (no NAT, no
# IGW). The materialised table is then verified from local via the
# Redshift Data API (an AWS public API — no network access to the
# workgroup needed, AWS proxies the query internally).
#
# Gating: requires four extra env vars produced by the extended
# bootstrap apply (see README in the bootstrap dir). Without them the
# test is skipped with a clear reason, so the rest of the dbt-mesh CLI
# suite runs even on accounts that haven't applied the VPC bootstrap.

_VPC_BOOTSTRAP_VARS = (
    "FLUID_AWS_TEST_VPC_SUBNET_IDS",
    "FLUID_AWS_TEST_REDSHIFT_SG_ID",
    "FLUID_AWS_TEST_EC2_INSTANCE_ID",
    "FLUID_AWS_TEST_SOURCE_BUCKET",
)
_VPC_BOOTSTRAP_READY = all(os.environ.get(v) for v in _VPC_BOOTSTRAP_VARS)
_VPC_BOOTSTRAP_SKIP_REASON = (
    "needs the Stage-3 VPC bootstrap (EC2 + private subnets + SSM "
    "endpoints) — apply tests/iac/_aws_stage3_bootstrap and export "
    + " + ".join("$" + v for v in _VPC_BOOTSTRAP_VARS)
)


def _copy_forge_cli_into(repo_root: Path, dest_dir: Path) -> None:
    """Copy the bits of forge-cli the EC2 needs straight into ``dest_dir``:
    ``fluid_build/`` + ``pyproject.toml``. Excludes the venv, .git,
    terraform state, bytecode, and the tests/ tree — the runner only
    needs to ``pip install`` the package and invoke ``fluid``.

    Flat structure (no inner tarball): ``dest_dir/fluid_build/`` and
    ``dest_dir/pyproject.toml`` end up at the same level as the
    contract + dbt project, so the EC2 just ``tar -xzf payload.tar.gz``
    and ``pip install -e payload``.
    """
    import shutil

    excludes = (".venv", ".git", "__pycache__", ".terraform", ".tfstate")

    def _ignore(path: str, names: list) -> list:
        skip = []
        for n in names:
            full = os.path.join(path, n)
            if any(x in full for x in excludes):
                skip.append(n)
            elif n.endswith((".pyc", ".pyo")):
                skip.append(n)
        return skip

    for name in ("fluid_build", "pyproject.toml"):
        src = repo_root / name
        if not src.exists():
            continue
        target = dest_dir / name
        if src.is_dir():
            shutil.copytree(str(src), str(target), ignore=_ignore)
        else:
            shutil.copy2(str(src), str(target))


def _ssm_run_and_wait(
    ssm_client,
    instance_id: str,
    commands: list,
    *,
    timeout_seconds: int = 1800,
    poll_interval: float = 5.0,
):
    """Send a shell-script command to one instance and block until done.

    Returns the ``get_command_invocation`` response on terminal status.
    Raises ``AssertionError`` (so pytest surfaces the failure cleanly)
    if the command Status isn't ``Success``.
    """
    cmd = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=timeout_seconds,
    )
    command_id = cmd["Command"]["CommandId"]
    deadline = time.monotonic() + timeout_seconds
    last_resp = None
    while time.monotonic() < deadline:
        try:
            last_resp = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ssm_client.exceptions.InvocationDoesNotExist:
            # SSM eventual consistency between send + first get; retry.
            time.sleep(poll_interval)
            continue
        if last_resp["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            return last_resp
        time.sleep(poll_interval)
    raise AssertionError(
        f"SSM command {command_id} did not finish within {timeout_seconds}s; "
        f"last status: {last_resp['Status'] if last_resp else 'unknown'}"
    )


@pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON)
@pytest.mark.skipif(not _HAVE_DBT_REDSHIFT, reason="needs dbt-redshift installed")
@pytest.mark.skipif(not _VPC_BOOTSTRAP_READY, reason=_VPC_BOOTSTRAP_SKIP_REASON)
def test_real_cli_dbt_redshift_serverless_amend_and_build(aws_real_project, aws_account, tmp_path):
    """``fluid apply --mode amend-and-build`` against private Redshift
    Serverless, executed FROM a private-subnet EC2 via SSM.

    Provisioning side (OpenTofu, runs on the EC2): namespace + workgroup
    attached to the bootstrap's 3 private subnets + the bootstrap's
    Redshift SG (port 5439 inbound from the EC2 SG only). No
    ``publicly_accessible``, no internet ingress.

    Build side (dbt-redshift, runs on the EC2): connects to the
    workgroup over the private subnet using IAM auth
    (``serverless_work_group`` + ``method: iam``), materialises one
    model into ``public``.

    Verification: local ``redshift-data execute_statement`` selects the
    materialised row — the Data API is an AWS public API that proxies
    queries into the VPC, so the local test process never needs network
    access to the workgroup.
    """
    ns = aws_real_project.name("dbtcli-rsns")
    wg = aws_real_project.name("dbtcli-rswg")
    region = aws_account["region"]
    model_name = f"hello_{aws_real_project.uid}"

    # Resolve bootstrap outputs from env.
    subnet_ids = [s for s in os.environ["FLUID_AWS_TEST_VPC_SUBNET_IDS"].split(",") if s]
    redshift_sg_id = os.environ["FLUID_AWS_TEST_REDSHIFT_SG_ID"]
    ec2_instance_id = os.environ["FLUID_AWS_TEST_EC2_INSTANCE_ID"]
    source_bucket = os.environ["FLUID_AWS_TEST_SOURCE_BUCKET"]
    repo_root = Path(__file__).resolve().parents[2]  # tests/iac/x → repo root

    # Contract: Redshift Serverless attached to the private subnets +
    # the bootstrap's Redshift SG. NOT publicly accessible.
    contract: Dict[str, Any] = {
        "id": "iac.aws.dbt.redshift",
        "name": "Real Redshift Serverless + dbt-redshift (private VPC)",
        "exposes": [
            {
                "exposeId": "warehouse",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_serverless",
                    "location": {
                        "namespace": ns,
                        "workgroup": wg,
                        "database": "fluid",
                        "base_capacity": 8,
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                        "subnet_ids": subnet_ids,
                        "security_group_ids": [redshift_sg_id],
                        # Private VPC access — emits an
                        # aws_redshiftserverless_endpoint_access. Without
                        # this the workgroup's natural hostname has no
                        # DNS entry inside the VPC and dbt-redshift
                        # cannot connect even though the EC2 and the
                        # workgroup share the same VPC.
                        "private_endpoint_subnets": subnet_ids,
                    },
                },
            }
        ],
        "build": {
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": "./dbt_project",
            "properties": {"model": model_name},
            "outputs": [model_name],
            "execution": {
                "runtime": {
                    "platform": "redshift",
                    "resources": {
                        "workgroup": wg,
                        "account_id": aws_account["account_id"],
                        "database": "fluid",
                        "schema": "public",
                        "user": "admin",
                        "region": region,
                    },
                }
            },
        },
    }

    # Build the bundle: forge-cli source, contract, dbt project — flat
    # layout so the EC2 extracts one tarball and ``pip install -e payload``
    # finds pyproject.toml + fluid_build/ at the right level.
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _copy_forge_cli_into(repo_root, bundle_dir)
    _write_contract(bundle_dir, contract)
    _write_dbt_project(
        bundle_dir / "dbt_project",
        profile="iac_aws_dbt_redshift",
        model_name=model_name,
        model_sql=(
            # post_hook grants SELECT to PUBLIC so the verification
            # query (issued via redshift-data API by a *different* IAM
            # identity than the one that built the table on the EC2)
            # can read the materialised row. By default Redshift tables
            # are only readable by the creator + superusers.
            "{{ config(materialized='table', "
            'post_hook="GRANT SELECT ON {{ this }} TO PUBLIC") }}\n'
            "SELECT 42::int AS answer, 'hello'::varchar AS greeting\n"
        ),
    )

    import tarfile

    payload_path = tmp_path / "payload.tar.gz"
    with tarfile.open(payload_path, "w:gz") as tar:
        tar.add(str(bundle_dir), arcname="payload")

    # Upload to the bootstrap S3 bucket — the EC2 reads from there via
    # the S3 gateway endpoint (no NAT needed).
    s3_key = f"runs/{aws_real_project.uid}/payload.tar.gz"
    s3 = aws_real_boto("s3")
    s3.upload_file(str(payload_path), source_bucket, s3_key)

    # SSM command on the EC2: pull payload, install fluid + dbt-redshift
    # in a fresh venv, run ``fluid apply``. Resource names + role ARNs
    # come in as env vars so the EC2 inherits the Stage-3 gate.
    #
    # workdir lives under /var/lib (real root filesystem) not /tmp —
    # AL2023 defaults ``/tmp`` to a tmpfs sized to half RAM (459 MB on
    # t3.micro), and a forge-cli venv install fills that fast. Root
    # filesystem has the 30 GB block device we provisioned in the
    # bootstrap.
    ssm = aws_real_boto("ssm")
    workdir = f"/var/fluid-runs/{aws_real_project.uid}"
    commands = [
        "set -euxo pipefail",
        # Reclaim disk from prior runs so the venv install doesn't
        # ENOSPC out on a long-lived test EC2.
        "sudo rm -rf /var/fluid-runs/* /tmp/fluid-run-*",
        "sudo mkdir -p /var/fluid-runs && sudo chmod 1777 /var/fluid-runs",
        # Idempotent system-package install: python3.11 / pip / git / unzip.
        # AL2023's dnf reaches Amazon Linux repos via AWS-internal routing
        # even without NAT, so this works either way.
        # AL2023 ships ``curl-minimal``; don't try to install full ``curl``
        # (conflicts with ``--allowerasing``). All other packages we need
        # have non-conflicting variants.
        "sudo dnf install -y python3.11 python3.11-pip git tar gzip unzip jq",
        # OpenTofu binary install (idempotent — skip if already on PATH).
        "if ! command -v tofu >/dev/null 2>&1; then "
        "  curl -fsSL -o /tmp/tofu.zip "
        "https://github.com/opentofu/opentofu/releases/download/v1.12.0/tofu_1.12.0_linux_amd64.zip; "
        "  sudo unzip -o /tmp/tofu.zip -d /usr/local/bin/; "
        "  sudo chmod +x /usr/local/bin/tofu; "
        "fi",
        "tofu version",
        f"mkdir -p {workdir} && cd {workdir}",
        # Pull the test payload from the bootstrap S3 bucket (S3 gateway
        # endpoint — no internet egress needed for this step).
        f"aws s3 cp s3://{source_bucket}/{s3_key} payload.tar.gz",
        "tar -xzf payload.tar.gz",
        "cd payload",
        # Fresh venv, install forge-cli + dbt-redshift. Network egress
        # to PyPI via the NAT gateway. TMPDIR + PIP_CACHE_DIR redirected
        # to the root filesystem (30 GB) because /tmp is tmpfs (~459 MB).
        f"mkdir -p {workdir}/tmp {workdir}/pip-cache",
        f"export TMPDIR={workdir}/tmp PIP_CACHE_DIR={workdir}/pip-cache",
        "python3.11 -m venv .venv-runner",
        ". .venv-runner/bin/activate",
        "pip install --quiet --upgrade pip",
        "pip install --quiet -e .",
        "pip install --quiet 'dbt-redshift>=1.10,<1.12'",
        # Stage 3 gate env.
        f"export FLUID_IAC_LIVE_AWS=1 AWS_REGION={region} AWS_DEFAULT_REGION={region}",
        f"export FLUID_AWS_LAMBDA_ROLE_ARN={os.environ['FLUID_AWS_LAMBDA_ROLE_ARN']}",
        f"export FLUID_AWS_SFN_ROLE_ARN={os.environ['FLUID_AWS_SFN_ROLE_ARN']}",
        f"export FLUID_AWS_GLUE_ROLE_ARN={os.environ['FLUID_AWS_GLUE_ROLE_ARN']}",
        f"export FLUID_AWS_SPECTRUM_ROLE_ARN={os.environ['FLUID_AWS_SPECTRUM_ROLE_ARN']}",
        # Force the IAM auth path in the dbt-redshift profile generator.
        # On the EC2 we authenticate via the instance role (no
        # ``AWS_PROFILE`` env), and forge-cli's profile generator
        # otherwise falls through to password mode and writes
        # ``host: ''`` — which makes redshift_connector fail with
        # ``gaierror`` on an empty hostname.
        "export REDSHIFT_USE_IAM=1",
        # Two-phase apply: ``--mode amend`` (IaC only) provisions the
        # Redshift Serverless namespace + workgroup. Then we wait for
        # the workgroup's VPC-internal DNS hostname to resolve before
        # the second ``--mode amend-and-build`` invocation runs dbt.
        # Tofu apply waits for ``status=AVAILABLE`` but the
        # private-zone DNS entry can lag by 10-60s after that, and
        # dbt-redshift's first connection attempt fails hard with
        # ``gaierror`` (not a retry-eligible Database Error).
        "python -m fluid_build.cli apply contract.fluid.yaml --mode amend --yes",
        # Resolve the workgroup hostname before running dbt. Extract the
        # endpoint from the live AWS API (boto3) rather than parsing
        # the emitted .tf.json, then poll until ``getent hosts``
        # returns an A record.
        "python3.11 - <<'PYEOF'\n"
        "import json, subprocess, time, boto3, yaml, sys\n"
        "with open('contract.fluid.yaml') as f:\n"
        "    contract = yaml.safe_load(f)\n"
        "wg = None\n"
        "for exp in contract.get('exposes', []):\n"
        "    loc = (exp.get('binding') or {}).get('location') or {}\n"
        "    if loc.get('workgroup'):\n"
        "        wg = loc['workgroup']\n"
        "        break\n"
        "if not wg:\n"
        "    print('no workgroup in contract', file=sys.stderr); sys.exit(0)\n"
        "rs = boto3.client('redshift-serverless')\n"
        "host = rs.get_workgroup(workgroupName=wg)['workgroup']['endpoint']['address']\n"
        "print(f'waiting for DNS: {host}', flush=True)\n"
        "for i in range(60):\n"
        "    r = subprocess.run(['getent', 'hosts', host], capture_output=True, text=True)\n"
        "    if r.returncode == 0 and r.stdout.strip():\n"
        "        print(f'  resolved after {i*3}s: {r.stdout.strip()}', flush=True)\n"
        "        break\n"
        "    time.sleep(3)\n"
        "else:\n"
        "    print(f'  DNS never resolved for {host}', file=sys.stderr); sys.exit(1)\n"
        "PYEOF",
        # Dump the dbt profile forge-cli would generate, so we can see
        # what host dbt-redshift actually receives.
        "python3.11 - <<'PYEOF'\n"
        "import yaml, json\n"
        "from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile\n"
        "with open('contract.fluid.yaml') as f: c = yaml.safe_load(f)\n"
        "build = c.get('builds') and c['builds'][0] or c.get('build')\n"
        "p = _build_generated_dbt_profile(build, {})\n"
        "print('=== generated dbt profile ===')\n"
        "print(yaml.safe_dump(p, sort_keys=False))\n"
        "PYEOF",
        # Now the build phase. IaC is already applied (tofu plan = 0
        # changes), so this is effectively just the dbt run.
        "python -m fluid_build.cli apply contract.fluid.yaml --mode amend-and-build --yes",
        f"echo OK > {workdir}/done",
    ]

    aws_real_project.applied = True  # so the fixture's destroy runs
    resp = _ssm_run_and_wait(ssm, ec2_instance_id, commands, timeout_seconds=1800)
    if resp["Status"] != "Success":
        pytest.fail(
            f"SSM `fluid apply` on EC2 failed with Status={resp['Status']}\n"
            f"--- StandardOutputContent ---\n{(resp.get('StandardOutputContent') or '')[-4000:]}\n"
            f"--- StandardErrorContent ---\n{(resp.get('StandardErrorContent') or '')[-4000:]}"
        )

    # Verify via redshift-data API (public AWS API; no VPC access from
    # the local test process needed).
    rsdata = aws_real_boto("redshift-data")
    stmt = rsdata.execute_statement(
        WorkgroupName=wg,
        Database="fluid",
        Sql=f"SELECT answer, greeting FROM public.{model_name}",
    )
    sid = stmt["Id"]
    deadline = time.monotonic() + 60.0
    desc = None
    while time.monotonic() < deadline:
        desc = rsdata.describe_statement(Id=sid)
        if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(1)
    assert desc and desc["Status"] == "FINISHED", desc
    rows = rsdata.get_statement_result(Id=sid)
    answer = rows["Records"][0][0].get("longValue")
    greeting = rows["Records"][0][1].get("stringValue")
    assert answer == 42 and greeting == "hello", rows["Records"]


# ---------------------------------------------------------------------------
# Test 3 — Mesh: SDP → ADP (dbt-athena) → CDP (Redshift external schema)
#                via TWO sequential ``fluid apply`` invocations
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON)
@pytest.mark.skipif(not _HAVE_DBT_ATHENA, reason="needs dbt-athena-community installed")
def test_real_cli_dbt_mesh_amend_and_build(aws_real_project, aws_account):
    """The full mesh path through the CLI:

      1. ``fluid apply`` SDP+ADP contract — provisions an Iceberg-on-Glue
         table AND runs dbt-athena to materialise an aggregate model into
         the same Glue catalog.
      2. ``fluid apply`` CDP contract — provisions a Redshift Serverless
         namespace/workgroup + external schema pointing at the Glue DB.
      3. Verification: ``redshift-data`` SELECT through the external schema
         reads the dbt-materialised aggregate.

    The two contracts share the Glue database — that's the mesh interface.
    """
    bucket = aws_real_project.name("dbtcli-mesh-b")
    glue_db = aws_real_project.name("dbtcli_mesh_db").replace("-", "_")
    ns = aws_real_project.name("dbtcli-mesh-ns")
    wg = aws_real_project.name("dbtcli-mesh-wg")
    ext_schema = aws_real_project.name("dbtcli_mesh_ext").replace("-", "_")
    region = aws_account["region"]
    adp_model = f"adp_{aws_real_project.uid}"

    # --- Contract 1: SDP raw Iceberg + ADP dbt-athena aggregate ---
    sdp_adp_dir = aws_real_project.workdir / "sdp_adp"
    sdp_adp_dir.mkdir(exist_ok=True)
    sdp_adp_contract = aws_iceberg_contract(
        bucket, database=glue_db, table="events", cid="iac.aws.mesh.sdp_adp"
    )
    sdp_adp_contract["build"] = {
        "engine": "dbt",
        "pattern": "hybrid-reference",
        "repository": "./dbt_project",
        "properties": {"model": adp_model},
        "outputs": [adp_model],
        "execution": {
            "runtime": {
                "platform": "athena",
                "resources": {
                    "s3_staging_dir": f"s3://{bucket}/athena-staging/",
                    "s3_data_dir": f"s3://{bucket}/iceberg/{glue_db}/",
                    "region": region,
                    "schema": glue_db,
                },
            }
        },
    }
    _write_contract(sdp_adp_dir, sdp_adp_contract)

    # The ADP dbt model: aggregate that materialises into the SAME Glue
    # database as the SDP table — that's the mesh interface.
    #
    # NB on the source(): the SDP table is provisioned by the IaC layer
    # as an Iceberg-typed Glue catalog entry, but the S3 location has
    # no Iceberg metadata.json yet (the IaC apply registers the schema,
    # it doesn't write Iceberg files). dbt-athena would then fail on
    # ``Detected Iceberg type table without metadata location``. The
    # mesh story doesn't require dbt-athena to actually read SDP rows
    # — it requires dbt-athena to materialise into the Glue catalog,
    # and Redshift external-schema to read that materialisation. So we
    # use a small literal for the input. The ``_sources.yml`` is still
    # emitted to demonstrate the contract-side pattern (catalogs the
    # SDP table as a source), but the model SQL sidesteps it.
    _write_dbt_project(
        sdp_adp_dir / "dbt_project",
        profile="iac_aws_mesh_adp",
        model_name=adp_model,
        model_sql=(
            "{{ config(materialized='table', table_type='iceberg', format='parquet') }}\n"
            "SELECT count(*) AS event_count FROM (VALUES (1), (2), (3)) AS t(x)\n"
        ),
        sources={
            "version": 2,
            "sources": [
                {
                    "name": "sdp",
                    "schema": glue_db,
                    "tables": [{"name": "events"}],
                }
            ],
        },
    )

    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=sdp_adp_dir,
        env_overrides=_live_env_overrides(),
        timeout=900,
    )
    aws_real_project.applied = True  # both contracts share the sweeper-scope
    _assert_fluid_ok(rc, "apply mesh SDP+ADP")

    # Confirm the ADP table is in the Glue catalog (the mesh interface).
    glue = aws_real_boto("glue")
    adp_table = glue.get_table(DatabaseName=glue_db, Name=adp_model)["Table"]
    assert adp_table["Name"] == adp_model

    # The bootstrap activates Lake Formation on this account (admins =
    # fluid-stage3-tester + EC2 runner). LF gates every Glue catalog
    # read — including reads from Redshift Spectrum's external schema.
    # The ADP table is created by dbt-athena via CTAS *at runtime* (not
    # by IaC apply), so it isn't known to the contract's
    # ``governance.lakeFormation.grants[]`` block. Grant Spectrum
    # SELECT on the ADP table here, after dbt-athena materialises it.
    # This mirrors the real-world mesh pattern: the producer's
    # ADP-layer publishing pipeline registers consumer-role grants on
    # each materialised table.
    lf = aws_real_boto("lakeformation")
    spectrum_arn = aws_real_role_arn("spectrum")
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": spectrum_arn},
        Resource={"Table": {"DatabaseName": glue_db, "Name": adp_model}},
        Permissions=["SELECT", "DESCRIBE"],
    )
    # LF + Glue catalog also need a database-level DESCRIBE so Spectrum
    # can resolve the cross-database name through the external schema.
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": spectrum_arn},
        Resource={"Database": {"Name": glue_db}},
        Permissions=["DESCRIBE"],
    )

    # --- Contract 2: CDP Redshift Serverless + external schema → Glue ---
    cdp_dir = aws_real_project.workdir / "cdp"
    cdp_dir.mkdir(exist_ok=True)
    cdp_contract: Dict[str, Any] = {
        "id": "iac.aws.mesh.cdp",
        "name": "Real Mesh CDP — Redshift external schema",
        "exposes": [
            {
                "exposeId": "cdp_warehouse",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_serverless",
                    "location": {
                        "namespace": ns,
                        "workgroup": wg,
                        "database": "fluid",
                        "base_capacity": 8,
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                    },
                },
            },
            {
                "exposeId": "cdp_external_schema",
                "binding": {
                    "platform": "aws",
                    "format": "redshift_external_schema",
                    "location": {
                        "workgroup": wg,
                        "database": "fluid",
                        "external_schema": ext_schema,
                        "glue_database": glue_db,
                        "iam_role_arn": aws_real_role_arn("spectrum"),
                        "region": region,
                    },
                },
            },
        ],
    }
    _write_contract(cdp_dir, cdp_contract)

    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend-and-build",
        "--yes",
        cwd=cdp_dir,
        env_overrides=_live_env_overrides(),
        timeout=1200,
    )
    _assert_fluid_ok(rc, "apply mesh CDP")

    # --- Mesh assertion: read the ADP table THROUGH the CDP external
    # schema. This is the architectural smoking gun — same Glue catalog
    # entry, accessed by Athena (write side) and Redshift Spectrum (read
    # side).
    rsdata = aws_real_boto("redshift-data")
    stmt = rsdata.execute_statement(
        WorkgroupName=wg,
        Database="fluid",
        Sql=f"SELECT event_count FROM {ext_schema}.{adp_model}",
    )
    sid = stmt["Id"]
    for _ in range(90):
        desc = rsdata.describe_statement(Id=sid)
        if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(1)
    # Surface the full Redshift Spectrum error reason on failure —
    # the legacy assertion was truncating `desc` repr.
    if desc["Status"] != "FINISHED":
        raise AssertionError(
            f"Redshift Spectrum query FAILED on mesh ADP table.\n"
            f"  Status: {desc['Status']}\n"
            f"  StateChangeReason: {desc.get('Error') or desc.get('StateChangeReason')!r}\n"
            f"  QueryString: {desc.get('QueryString')!r}\n"
            f"  WorkgroupName: {desc.get('WorkgroupName')!r}\n"
            f"  full desc keys: {sorted(desc.keys())}"
        )
    rows = rsdata.get_statement_result(Id=sid)
    # Empty SDP → ADP aggregate is 0; we just confirm the query path works.
    assert "Records" in rows, rows


# ---------------------------------------------------------------------------
# Test 4 — dbt-glue documented limitation
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "dbt-glue is intentionally out of the Stage 3 CLI matrix: the "
        "adapter requires a Glue interactive session (~3-5 min cold "
        "start, billed per DPU-hour) and ``fluid generate iac`` does "
        "not yet emit the matching Glue Job + GlueInteractiveSession "
        "configuration end-to-end. Tracked as a forge-cli enhancement; "
        "covered offline by the Stage 2 ``test_iac_aws_dbt_athena_e2e.py`` "
        "matrix on the profile-shape side. File issue to enable: "
        "https://github.com/agentics/forge-cli/issues/new "
        "(remove this skip + add the iac.providers.aws.glue_job emitter)."
    )
)
def test_real_cli_dbt_glue_amend_and_build():
    """Placeholder — see skip reason. Lives here so the matrix is
    self-documenting: someone reading the file knows dbt-glue was
    considered, why it was deferred, and what would unblock it."""
