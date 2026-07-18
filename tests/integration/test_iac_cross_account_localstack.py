# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Bilateral cross-account LF + S3 live test on a two-account LocalStack Pro.

Closes the *emulator-testable* half of "provision a second AWS sandbox
account and run a bilateral cross-account live test". The producer stack
is applied in account **A** (``000000000000``); every grant it emits names
a consumer IAM role in account **B** (``222222222222``). LocalStack derives
the account id from the access-key id, so two boto3 clients with different
keys are genuinely two accounts sharing one emulator.

WHAT THIS PROVES
----------------
1. forge **emits** correct cross-account configuration — an
   ``aws_lakeformation_permissions`` naming the account-B principal, and
   the companion ``aws_s3_bucket_policy`` (LF alone never authorises
   object-byte reads) — on a *shared pool* bucket, prefix-scoped to the
   binding's ``location.path`` so the grant cannot reach another tenant.
2. forge **applies** it: ``tofu apply`` against the emulator lands every
   resource, and both accounts' APIs read the artifacts back.
3. The emitted **bucket policy actually authorises across the account
   boundary** (``TestCrossAccountS3Authorization``): with a deliberately
   BROAD identity policy on the account-B role, the account-B caller can
   read the granted prefix and is DENIED the sibling tenant's prefix —
   and widening only the bucket policy flips that denial to an allow.
   The causal control is the point: the forge-emitted document is the
   deciding control, not the identity policy.

WHAT THIS DOES **NOT** PROVE
----------------------------
LocalStack emulates the AWS API surface; it is not AWS's authorization
engine. Specifically still open, and only closable on two real AWS
accounts:

* **Lake Formation cross-account authorization.** LF ``GrantPermissions``
  is rejected with ``AccessDeniedException`` whenever LocalStack IAM
  enforcement is on — even with the caller registered in
  ``DataLakeAdmins`` (this module's own contract emits
  ``aws_lakeformation_data_lake_settings`` and it still fails). See
  ``docs/upstream-issues/localstack-lakeformation-grant-auth.md``. So LF
  grants can only be *created* in the non-enforcing mode, where nothing
  evaluates them: the LF permission is verified as **landed**, never as
  **enforced**.
* **Cross-account Glue Data Catalog sharing.** LocalStack gives each
  account an isolated catalog with no AWS RAM: account B gets
  ``EntityNotFoundException`` for account A's database. That is emulator
  state isolation, not an authorization denial, so the catalog-sharing
  path (RAM resource share + catalog resource policy) is untested here.
* **Org-boundary semantics** — SCPs, trust-policy external-ids,
  ``aws:PrincipalOrgID`` conditions.

RUNNING
-------
Needs a *second*, disposable LocalStack Pro with ``lakeformation``
enabled (the common ``SERVICES=s3,iam,sts,glue,lambda,…`` instance does
not carry it), on its own port so the long-lived one is untouched::

    docker run -d --name localstack_xacct -p 4567:4566 \\
      -e LOCALSTACK_AUTH_TOKEN=... \\
      -e SERVICES=s3,iam,sts,glue,lakeformation,athena \\
      localstack/localstack-pro:latest

    export FLUID_IAC_LIVE_XACCT=1
    export FLUID_XACCT_ENDPOINT=http://localhost:4567   # optional
    pytest tests/integration/test_iac_cross_account_localstack.py -v

``TestCrossAccountS3Authorization`` needs that container started with
``-e ENFORCE_IAM=1`` as well, and is skipped otherwise — it detects
enforcement at runtime rather than trusting the env var.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
]

# --- accounts -------------------------------------------------------------
# LocalStack derives the account id from the access-key id, so the key IS
# the account selector. "test" is the default account.
ACCOUNT_A = "000000000000"  # producer — the stack is applied here
ACCOUNT_B = "222222222222"  # consumer — every grant names a principal here
KEY_A = "test"
KEY_B = ACCOUNT_B
CONSUMER_ROLE = "fluid-xacct-consumer"
CONSUMER_ARN = f"arn:aws:iam::{ACCOUNT_B}:role/{CONSUMER_ROLE}"

POOL_BUCKET = "fluid-iactest-xacct-pool"
# The authorization class uses its OWN pool so it never races the
# module-scoped apply below (which resets the emulator once, up front).
AUTHZ_BUCKET = "fluid-iactest-xacct-authz"
OWN_PREFIX = "silver/orders/"
OWN_KEY = f"{OWN_PREFIX}part-0.parquet"
OTHER_TENANT_KEY = "silver/other_tenant/secret.parquet"

ENDPOINT = os.environ.get("FLUID_XACCT_ENDPOINT", "http://localhost:4567")

_TRUE = {"1", "true", "yes", "on"}


# ── gate ─────────────────────────────────────────────────────────────────


def _gate() -> tuple[bool, str]:
    """``(enabled, skip_reason)`` — quadruple-gated, self-skipping.

    Deliberately checks that ``lakeformation`` is *available* rather than
    just that something answers on the port: pointing this at the common
    ``SERVICES=s3,iam,sts,glue,…`` instance must skip, not fail.
    """
    if os.environ.get("FLUID_IAC_LIVE_XACCT", "").strip().lower() not in _TRUE:
        return False, "cross-account LocalStack tests are opt-in — set FLUID_IAC_LIVE_XACCT=1"
    try:
        from fluid_build.iac import runner
    except ImportError as exc:  # pragma: no cover - import guard
        return False, f"fluid_build.iac unavailable: {exc}"
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    for mod in ("boto3", "requests"):
        try:
            __import__(mod)
        except ImportError:
            return False, f"the `{mod}` package is not installed"
    import requests

    try:
        resp = requests.get(f"{ENDPOINT}/_localstack/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        return False, f"LocalStack not reachable at {ENDPOINT}: {exc}"
    if resp.status_code != 200:
        return False, f"LocalStack health at {ENDPOINT} returned {resp.status_code}"
    services = (resp.json() or {}).get("services") or {}
    # "available" = enabled but not yet touched; "running" = already started.
    # Anything else (including "disabled" or a missing key) means this
    # instance was not started with the lakeformation service.
    if services.get("lakeformation") not in {"available", "running"}:
        return False, (
            f"the LocalStack at {ENDPOINT} has no `lakeformation` service — start a "
            "second instance with SERVICES=...,lakeformation (see this module's docstring)"
        )
    return True, ""


ENABLED, SKIP_REASON = _gate()

pytestmark.append(pytest.mark.skipif(not ENABLED, reason=SKIP_REASON))


# ── helpers ──────────────────────────────────────────────────────────────


def _client(service: str, key: str = KEY_A):
    """A boto3 client bound to LocalStack *as a specific account*."""
    import boto3

    return boto3.client(
        service,
        endpoint_url=ENDPOINT,
        aws_access_key_id=key,
        aws_secret_access_key="test",  # noqa: S106 — dummy, emulator only
        region_name="us-east-1",
    )


def _reset() -> None:
    """Wipe every emulator backend so each test starts from empty."""
    import requests

    try:
        requests.post(f"{ENDPOINT}/_localstack/state/reset", timeout=20)
    except Exception:  # noqa: BLE001 - best-effort isolation
        pass


def _cross_account_contract(
    *, database: str, table: str, bucket: str = POOL_BUCKET
) -> Dict[str, Any]:
    """Producer contract whose LF grant + bucket policy name account B.

    The bucket is a **shared pool** (``packaging.containers.bucket:
    shared``), which is the interesting case: the emit references it via
    ``data.aws_s3_bucket`` instead of creating it, and every bucket-level
    control must narrow to ``location.path`` or it would reach the other
    tenants in the pool.
    """
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "iac.aws.xacct.orders",
        "name": "Cross-Account Orders",
        "metadata": {"layer": "Silver", "productType": "ADP", "owner": {"team": "data-platform"}},
        "packaging": {
            "mode": "isolated",
            "pool": "platform-lake-pool",
            "containers": {"bucket": "shared"},
        },
        # Registers account A's caller as an LF data-lake admin. On real
        # AWS this is what authorises the GrantPermissions below.
        "governance": {"lakeFormation": {"admins": [f"arn:aws:iam::{ACCOUNT_A}:root"]}},
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": database,
                        "table": table,
                        "bucket": bucket,
                        "path": OWN_PREFIX,
                    },
                    "governance": {
                        "lakeFormation": {
                            "registerLocation": True,
                            "grants": [
                                {
                                    "principal": CONSUMER_ARN,
                                    "permissions": ["SELECT", "DESCRIBE"],
                                }
                            ],
                        }
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string", "required": True},
                        {"name": "occurred_at", "type": "timestamp"},
                        {"name": "amount", "type": "decimal(12,2)"},
                    ]
                },
            }
        ],
    }


def _bootstrap_two_accounts(bucket: str = POOL_BUCKET) -> None:
    """Pre-create the platform-owned pool (account A) + consumer role (B).

    Seeds two objects: one under this product's prefix and one belonging
    to a *different* tenant of the same pool — the negative control that
    makes prefix-scoping meaningful.
    """
    s3_a = _client("s3", KEY_A)
    try:
        s3_a.create_bucket(Bucket=bucket)
    except s3_a.exceptions.BucketAlreadyOwnedByYou:  # pragma: no cover
        pass
    s3_a.put_object(Bucket=bucket, Key=OWN_KEY, Body=b"ORDERS-DATA")
    s3_a.put_object(Bucket=bucket, Key=OTHER_TENANT_KEY, Body=b"OTHER-TENANT")

    iam_b = _client("iam", KEY_B)
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_A}:root"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam_b.create_role(RoleName=CONSUMER_ROLE, AssumeRolePolicyDocument=json.dumps(trust))
    except iam_b.exceptions.EntityAlreadyExistsException:  # pragma: no cover
        pass


def _provider_sidecar() -> Dict[str, Any]:
    """Aim the AWS provider at LocalStack.

    A sidecar, never part of the emit: ``tofu`` merges every ``*.tf.json``
    in the workdir, so the plugin's ``main.tf.json`` stays portable and
    credential-free while only the test rig knows about the emulator.
    """
    services = ("s3", "glue", "sts", "iam", "lakeformation", "athena")
    return {
        "provider": {
            "aws": {
                "region": "us-east-1",
                "access_key": KEY_A,
                "secret_key": "test",
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: ENDPOINT for svc in services},
            }
        }
    }


@pytest.fixture(scope="module")
def applied_stack(tmp_path_factory):
    """Emit the producer contract through the AWS plugin and ``tofu apply``
    it into account A. Yields ``(contract, emitted_resources)``.

    Module-scoped: one ``tofu init`` + ``apply`` serves every assertion
    below (they are all read-only), which turns a ~6-minute run into
    roughly one minute.
    """
    from fluid_build.iac import build_module, get_iac_plugin, runner
    from fluid_build.iac.credentials import build_tofu_env

    workdir = tmp_path_factory.mktemp("xacct")
    _reset()
    _bootstrap_two_accounts()

    suffix = uuid.uuid4().hex[:8]
    contract = _cross_account_contract(database=f"mesh_silver_{suffix}", table="orders")
    plugin = get_iac_plugin("aws")

    (workdir / "main.tf.json").write_text(build_module(plugin, contract), encoding="utf-8")
    (workdir / "provider.tf.json").write_text(json.dumps(_provider_sidecar()), encoding="utf-8")

    env = build_tofu_env(os.environ)
    init = runner.tofu_init(str(workdir), backend=False, env=env)
    assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
    planned = runner.tofu_plan(str(workdir), env=env)
    assert planned.ok, f"tofu plan failed:\n{planned.stderr or planned.stdout}"
    applied = runner.tofu_apply(str(workdir), env=env)
    if not applied.ok:
        blob = f"{applied.stderr}\n{applied.stdout}"
        # LocalStack rejects LF GrantPermissions under IAM enforcement even
        # for a registered DataLakeAdmin — a known emulator gap, not a
        # forge bug. Skip rather than red-flag a correct emit.
        if "GrantPermissions" in blob and "AccessDenied" in blob:
            pytest.skip(
                "LocalStack rejects Lake Formation GrantPermissions under IAM "
                "enforcement even for a registered DataLakeAdmin — see "
                "docs/upstream-issues/localstack-lakeformation-grant-auth.md. "
                "Run this class against an instance without ENFORCE_IAM=1."
            )
        pytest.fail(f"tofu apply failed:\n{blob}")

    yield contract, plugin.emit(contract)

    # No `tofu destroy`: LocalStack's Glue API can hang for minutes on
    # delete (the same emulator bug the tests/iac LocalStack tier documents).
    # `_reset()` at the head of the next test is the isolation mechanism.


# ── the artifacts actually land, and both accounts can see them ──────────


class TestCrossAccountArtifactsLand:
    """Emit → ``tofu apply`` → read back through the live AWS APIs."""

    def test_bucket_policy_names_the_account_b_principal(self, applied_stack):
        """The companion bucket policy — LF alone never authorises object
        reads, so a cross-account consumer is broken without this."""
        _contract, _resources = applied_stack
        policy = json.loads(_client("s3", KEY_A).get_bucket_policy(Bucket=POOL_BUCKET)["Policy"])
        sids = {s["Sid"]: s for s in policy["Statement"]}

        get_stmt = sids["FluidLfBucketGet0"]
        assert get_stmt["Principal"]["AWS"] == CONSUMER_ARN
        assert get_stmt["Effect"] == "Allow"
        assert get_stmt["Action"] == ["s3:GetObject"]
        # Prefix-scoped, NOT `/*` — a pool grant must not reach other tenants.
        assert get_stmt["Resource"] == f"arn:aws:s3:::{POOL_BUCKET}/{OWN_PREFIX}*"

        list_stmt = sids["FluidLfBucketList0"]
        assert list_stmt["Principal"]["AWS"] == CONSUMER_ARN
        # ListBucket is inherently bucket-wide, so it must carry the
        # s3:prefix condition or account B could enumerate every tenant.
        assert list_stmt["Condition"]["StringLike"]["s3:prefix"] == [f"{OWN_PREFIX}*"]

    def test_lake_formation_permission_granted_to_account_b(self, applied_stack):
        contract, _resources = applied_stack
        database = contract["exposes"][0]["binding"]["location"]["database"]

        perms = _client("lakeformation", KEY_A).list_permissions(
            Principal={"DataLakePrincipalIdentifier": CONSUMER_ARN}
        )["PrincipalResourcePermissions"]
        assert perms, "no LF permissions returned for the account-B principal"

        grant = perms[0]
        assert grant["Principal"]["DataLakePrincipalIdentifier"] == CONSUMER_ARN
        assert set(grant["Permissions"]) == {"SELECT", "DESCRIBE"}
        assert grant["Resource"]["Table"]["DatabaseName"] == database
        assert grant["Resource"]["Table"]["Name"] == "orders"

    def test_lf_location_registered_at_the_prefix_not_the_pool_root(self, applied_stack):
        """Registering a *pool* bucket at its root would hand this product's
        LF service role access to every other tenant's data."""
        _contract, _resources = applied_stack
        arns = {
            r["ResourceArn"]
            for r in _client("lakeformation", KEY_A).list_resources()["ResourceInfoList"]
        }
        assert f"arn:aws:s3:::{POOL_BUCKET}/{OWN_PREFIX}" in arns
        assert f"arn:aws:s3:::{POOL_BUCKET}" not in arns

    def test_pool_bucket_is_referenced_not_created(self, applied_stack):
        """``packaging.containers.bucket: shared`` must produce a
        ``data.aws_s3_bucket`` lookup — a product that does not own the
        pool must never emit a resource that could replace it."""
        contract, resources = applied_stack
        from fluid_build.iac import get_iac_plugin

        assert "aws_s3_bucket" not in resources
        data = get_iac_plugin("aws").emit_data(contract)
        assert POOL_BUCKET in {block["bucket"] for block in data.get("aws_s3_bucket", {}).values()}

    def test_consumer_account_is_a_distinct_account(self, applied_stack):
        """Bilateral sanity: the two credential sets really are two accounts."""
        assert _client("sts", KEY_A).get_caller_identity()["Account"] == ACCOUNT_A
        assert _client("sts", KEY_B).get_caller_identity()["Account"] == ACCOUNT_B

    def test_account_b_catalog_is_isolated_on_the_emulator(self, applied_stack):
        """Documents an emulator LIMIT rather than a forge behaviour.

        On real AWS, cross-account catalog reads are brokered by AWS RAM +
        a catalog resource policy. LocalStack has neither, so account B
        simply cannot see account A's database. Pinned so nobody later
        mistakes this ``EntityNotFound`` for an authorization denial — or
        writes a "consumer can query" assertion that would silently pass
        for the wrong reason.
        """
        contract, _resources = applied_stack
        database = contract["exposes"][0]["binding"]["location"]["database"]
        glue_b = _client("glue", KEY_B)
        with pytest.raises(glue_b.exceptions.EntityNotFoundException):
            glue_b.get_table(DatabaseName=database, Name="orders")


# ── does the emitted policy actually authorise across the boundary? ──────


def _iam_enforced() -> bool:
    """Probe whether this LocalStack evaluates IAM at all.

    Detected by behaviour, not by reading the env var the container was
    started with: an unauthorised principal must actually be denied.
    """
    probe = f"fluid-iactest-enforce-probe-{uuid.uuid4().hex[:8]}"
    s3_a = _client("s3", KEY_A)
    try:
        s3_a.create_bucket(Bucket=probe)
        s3_a.put_object(Bucket=probe, Key="k", Body=b"x")
        try:
            # Account B has no grant of any kind here — with enforcement
            # on this must fail; without it, it succeeds.
            _client("s3", KEY_B).get_object(Bucket=probe, Key="k")
            return False
        except Exception:  # noqa: BLE001
            return True
    finally:
        try:
            s3_a.delete_object(Bucket=probe, Key="k")
            s3_a.delete_bucket(Bucket=probe)
        except Exception:  # noqa: BLE001
            pass


class TestCrossAccountS3Authorization:
    """The forge-emitted bucket policy is the deciding access control.

    Applies the emitter's own policy document to the pool and drives real
    cross-account ``GetObject`` calls as the account-B role. The account-B
    role is given a deliberately BROAD identity policy (the whole pool) so
    that any narrowing observed can only come from the resource policy —
    which is precisely the artifact under test.

    Skipped unless LocalStack is running with ``ENFORCE_IAM=1``; without
    it the emulator authorises everything and the assertions below would
    pass or fail for reasons unrelated to forge.
    """

    @pytest.fixture
    def enforced_pool(self):
        """Self-contained: its own pool bucket, rebuilt per test, so it
        never collides with the module-scoped apply above whatever order
        the tests run in."""
        if not _iam_enforced():
            pytest.skip(
                "this LocalStack does not evaluate IAM — start the disposable "
                "instance with -e ENFORCE_IAM=1 to run the authorization checks"
            )
        _bootstrap_two_accounts(AUTHZ_BUCKET)

        from fluid_build.iac import get_iac_plugin

        contract = _cross_account_contract(
            database="mesh_silver_authz", table="orders", bucket=AUTHZ_BUCKET
        )
        resources = get_iac_plugin("aws").emit(contract)
        policy_doc = next(iter(resources["aws_s3_bucket_policy"].values()))["policy"]

        # Apply the emitter's document directly. `tofu apply` of the whole
        # module cannot be used here: its sibling LF grant is rejected
        # under IAM enforcement (see the module docstring + the upstream
        # issue), and this class is about the S3 control specifically.
        _client("s3", KEY_A).put_bucket_policy(Bucket=AUTHZ_BUCKET, Policy=policy_doc)

        # BROAD identity policy on the consumer role — whole pool. Any
        # narrowing observed can therefore only come from the resource
        # policy, which is the artifact under test.
        _client("iam", KEY_B).put_role_policy(
            RoleName=CONSUMER_ROLE,
            PolicyName="broad-pool-read",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:ListBucket"],
                            "Resource": [
                                f"arn:aws:s3:::{AUTHZ_BUCKET}",
                                f"arn:aws:s3:::{AUTHZ_BUCKET}/*",
                            ],
                        }
                    ],
                }
            ),
        )
        return policy_doc

    @staticmethod
    def _consumer_s3():
        """An S3 client acting AS the account-B role (STS assume-role)."""
        import boto3

        creds = _client("sts", KEY_A).assume_role(
            RoleArn=CONSUMER_ARN, RoleSessionName="fluid-xacct"
        )["Credentials"]
        return boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            region_name="us-east-1",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    @classmethod
    def _can_read(cls, key: str) -> bool:
        try:
            cls._consumer_s3().get_object(Bucket=AUTHZ_BUCKET, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def test_granted_prefix_is_readable_across_the_account_boundary(self, enforced_pool):
        assert self._can_read(
            OWN_KEY
        ), "account B could not read the prefix the emitted bucket policy grants"

    def test_other_tenants_prefix_is_denied(self, enforced_pool):
        """The identity policy allows this object; only the emitted bucket
        policy withholds it."""
        assert not self._can_read(
            OTHER_TENANT_KEY
        ), "account B read another tenant's object — the pool grant is not prefix-scoped"

    def test_widening_only_the_bucket_policy_flips_the_denial(self, enforced_pool):
        """Causal control.

        Without this, the denial above could be an artefact of the
        emulator rather than a consequence of the emitted document. Widen
        the bucket policy alone — identity policy untouched — and the same
        call must succeed; restore it and the denial must come back.
        """
        assert not self._can_read(OTHER_TENANT_KEY)

        wide = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "WideControl",
                        "Effect": "Allow",
                        "Principal": {"AWS": CONSUMER_ARN},
                        "Action": ["s3:GetObject"],
                        "Resource": f"arn:aws:s3:::{AUTHZ_BUCKET}/*",
                    }
                ],
            }
        )
        s3_a = _client("s3", KEY_A)
        s3_a.put_bucket_policy(Bucket=AUTHZ_BUCKET, Policy=wide)
        assert self._can_read(OTHER_TENANT_KEY), (
            "widening the bucket policy did not grant access — this emulator is not "
            "evaluating resource policies, so the sibling denial proves nothing"
        )

        s3_a.put_bucket_policy(Bucket=AUTHZ_BUCKET, Policy=enforced_pool)
        assert not self._can_read(OTHER_TENANT_KEY)


# ── the guard that makes a pool grant safe in the first place ────────────


def test_pool_grant_without_a_path_is_rejected():
    """A bucket-level cross-account grant on a shared pool with no
    ``location.path`` has nothing to scope to and would reach every other
    tenant. It must fail closed rather than silently widen the pool."""
    from fluid_build.iac import get_iac_plugin
    from fluid_build.iac.packaging import PackagingError

    contract = _cross_account_contract(database="mesh_silver_nopath", table="orders")
    del contract["exposes"][0]["binding"]["location"]["path"]

    with pytest.raises(PackagingError) as excinfo:
        get_iac_plugin("aws").emit(contract)
    assert excinfo.value.kind == "shared-bucket-requires-path"
