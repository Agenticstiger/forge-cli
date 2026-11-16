# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keyless cloud authentication for generated pipelines.

A generated pipeline holds no static cloud key. The CI system mints a
short-lived OIDC ID token for each job, and the cloud (or the secret store)
trusts the CI system's issuer and the job's subject:

* **aws**: the SDKs assume ``AWS_ROLE_ARN`` with the token themselves.
* **gcp**: a workload identity federation credential that names the token.
* **snowflake**: workload identity federation (``authenticator=WORKLOAD_IDENTITY``,
  provider ``OIDC``). forge's connection and the snowflakedb/snowflake OpenTofu
  provider read the same variables (``SNOWFLAKE_AUTHENTICATOR``,
  ``SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER``, ``SNOWFLAKE_TOKEN_FILE_PATH``).
* **databricks**: the SDKs and the provider mint the exchange themselves
  (``github-oidc``) or read the token from the job (``env-oidc``).
* **openbao**: forge's vault resolver logs in with the JWT
  (``FLUID_VAULT_JWT_ROLE`` + ``FLUID_VAULT_JWT_FILE``), for ``vault://`` and
  ``openbao://`` secret references.

Borrowed, not invented (each step below is the vendor's documented one):

* GitHub OIDC: https://docs.github.com/en/actions/reference/security/oidc
* aws-actions/configure-aws-credentials, google-github-actions/auth
* Snowflake WIF: https://docs.snowflake.com/en/user-guide/workload-identity-federation
* Databricks: https://docs.databricks.com/aws/en/dev-tools/auth/provider-github and
  https://docs.databricks.com/aws/en/dev-tools/auth/provider-gitlab
* OpenBao JWT auth: https://openbao.org/docs/auth/jwt/
* GitLab: https://docs.gitlab.com/ci/secrets/id_token_authentication/
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

try:  # PyYAML is a core dependency; the fallback only keeps imports safe.
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None

#: The platforms a pipeline authenticates to, in the order they are wired.
CLOUD_AUTH_PLATFORMS: Tuple[str, ...] = ("aws", "gcp", "snowflake", "databricks", "openbao")

#: ``fluid apply`` and the stages after it reach the cloud; so does ``diff``.
CLOUD_STAGE_NUMBERS = frozenset({5, 7, 8, 9, 10, 11})


class LiteralStr(str):
    """A multi-line ``run:`` script that YAML renders as a literal block."""


if _yaml is not None:

    def _represent_literal(dumper: Any, value: "LiteralStr") -> Any:
        return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style="|")

    _yaml.add_representer(LiteralStr, _represent_literal)


def normalize(cloud_auth: Sequence[str]) -> Tuple[str, ...]:
    """The known platforms in ``cloud_auth``, in wiring order, without repeats."""
    unknown = sorted(set(cloud_auth) - set(CLOUD_AUTH_PLATFORMS))
    if unknown:
        raise ValueError(
            f"unknown cloud_auth platform(s) {unknown}; expected a subset of "
            f"{list(CLOUD_AUTH_PLATFORMS)}"
        )
    return tuple(p for p in CLOUD_AUTH_PLATFORMS if p in set(cloud_auth))


# ---------------------------------------------------------------------------
# Variables (never secrets) each platform needs
# ---------------------------------------------------------------------------

#: Non-secret repository / CI variables, with an example value.
VARIABLES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "aws": (
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/fluid-deploy"),
        ("AWS_REGION", "eu-central-1"),
    ),
    "gcp": (
        ("GCP_PROJECT_NUMBER", "123456789012"),
        ("WIF_POOL", "ci-pool"),
        ("WIF_PROVIDER", "ci-provider"),
        ("GCP_SA_EMAIL", "fluid-deploy@my-project.iam.gserviceaccount.com"),
    ),
    "snowflake": (("SNOWFLAKE_ACCOUNT", "myorg-myaccount"),),
    "databricks": (
        ("DATABRICKS_HOST", "https://my-workspace.cloud.databricks.com"),
        ("DATABRICKS_CLIENT_ID", "00000000-0000-0000-0000-000000000000"),
    ),
    "openbao": (
        ("BAO_ADDR", "https://openbao.example.com:8200"),
        ("OPENBAO_JWT_ROLE", "fluid-ci"),
        ("OPENBAO_JWT_AUDIENCE", "https://openbao.example.com"),
    ),
}

#: GitLab needs the Databricks account id as the token audience.
_GITLAB_EXTRA_VARIABLES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "databricks": (("DATABRICKS_ACCOUNT_ID", "00000000-0000-0000-0000-000000000000"),),
}

_WHAT = {
    "aws": "an IAM role the job assumes (no access key)",
    "gcp": "workload identity federation to a service account (no key file)",
    "snowflake": "workload identity federation to a service user (no password, no key pair)",
    "databricks": "OAuth token federation to a service principal (no token, no client secret)",
    "openbao": "a JWT login to OpenBao or Vault for vault:// and openbao:// secret references",
}


def variables(cloud_auth: Sequence[str], system: str = "github") -> List[Tuple[str, str, str]]:
    """``(platform, name, example)`` for every variable the detected platforms need."""
    rows: List[Tuple[str, str, str]] = []
    for platform in normalize(cloud_auth):
        pairs = VARIABLES[platform]
        if system == "gitlab":
            pairs = pairs + _GITLAB_EXTRA_VARIABLES.get(platform, ())
        rows.extend((platform, name, example) for name, example in pairs)
    return rows


# ---------------------------------------------------------------------------
# Comments: what the cloud side must trust
# ---------------------------------------------------------------------------

_GITHUB_SUBJECT = [
    "The subject (sub) to bind every trust to is immutable by design. GitHub repositories",
    "created after 2026-07-15 use repo:OWNER@OWNER-ID/REPO@REPO-ID:<context> by default;",
    "older repositories keep repo:OWNER/REPO:<context> until they opt in to the immutable",
    "form, which makes a renamed or re-created repository unable to inherit the trust.",
    "Opt in, then bind to it (https://docs.github.com/en/actions/reference/security/oidc).",
    "<context> is environment:<name> for a job that declares an environment (the deploy",
    "jobs do), pull_request for a job a pull request runs, and ref:refs/heads/<branch>",
    "otherwise.",
]

_GITLAB_SUBJECT = [
    "Bind every trust to the job's subject: project_path:GROUP/PROJECT:ref_type:branch:ref:main",
    "(a protected branch or environment), not to the whole group. The issuer is your GitLab",
    "instance URL (https://gitlab.com), and its discovery document is public.",
]


def trust_comments(cloud_auth: Sequence[str], system: str) -> List[str]:
    """Comment lines (no prefix) saying what to configure on the cloud side."""
    platforms = normalize(cloud_auth)
    if not platforms:
        return []
    github = system == "github"
    issuer = "https://token.actions.githubusercontent.com" if github else "https://gitlab.com"
    sub = "<sub>"
    lines = (_GITHUB_SUBJECT if github else _GITLAB_SUBJECT) + [""]
    if "aws" in platforms:
        lines += [
            "aws: create the IAM OIDC provider for the issuer, and a role whose trust policy",
            f"  allows sts:AssumeRoleWithWebIdentity with  aud = sts.amazonaws.com  and  sub = {sub}.",
        ]
    if "gcp" in platforms:
        lines += [
            "gcp: create a workload identity pool and an OIDC provider for the issuer; map",
            "  google.subject=assertion.sub; allow the service account to be impersonated by",
            f"  principal://iam.googleapis.com/projects/<NUMBER>/locations/global/workloadIdentityPools/<POOL>/subject/{sub}.",
        ]
    if "snowflake" in platforms:
        lines += [
            "snowflake: a service user trusts the job's token (the audience is snowflakecomputing.com):",
            "  CREATE USER FLUID_CI TYPE = SERVICE",
            f"    WORKLOAD_IDENTITY = (TYPE = OIDC ISSUER = '{issuer}' SUBJECT = '{sub}');",
            "  Grant that user the role the contract's bindings use.",
        ]
    if "databricks" in platforms:
        if github:
            lines += [
                "databricks: create a service principal and an account federation policy for it:",
                f"  issuer {issuer}, audience <your Databricks account ID>, subject {sub}.",
                "  DATABRICKS_CLIENT_ID is the service principal's application (client) ID.",
            ]
        else:
            lines += [
                "databricks: create a service principal and an account federation policy for it:",
                f"  issuer {issuer}, audience <your Databricks account ID>, subject {sub}.",
                "  DATABRICKS_CLIENT_ID is the service principal's application (client) ID;",
                "  the job's DATABRICKS_OIDC_TOKEN carries the account ID as its audience.",
            ]
    if "openbao" in platforms:
        lines += [
            "openbao: enable the JWT auth method and trust the issuer, then bind a role to the",
            "  audience and the subject:",
            f"  bao write auth/jwt/config oidc_discovery_url={issuer} bound_issuer={issuer}",
            "  bao write auth/jwt/role/fluid-ci role_type=jwt user_claim=sub \\",
            f"    bound_audiences=<OPENBAO_JWT_AUDIENCE> bound_subject={sub} token_policies=fluid-read",
        ]
    return lines


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


def github_job_permissions(cloud_auth: Sequence[str]) -> Dict[str, str]:
    """Permissions of a job that reaches the cloud: it may ask for an ID token."""
    perms = {"contents": "read"}
    if normalize(cloud_auth):
        perms["id-token"] = "write"
    return perms


def _github_token_script(file_name: str, audience: str, exports: List[str]) -> "LiteralStr":
    """A step that fetches the job's ID token for ``audience`` into a mode-600
    file under ``$RUNNER_TEMP``, masks it, and exports ``exports`` (``NAME=value``
    shell snippets) to the later steps."""
    # -H @- reads the header from stdin, so the request token is in no process's
    # argv (another user of a shared self-hosted runner could read that).
    header = '<<<"Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN"'
    if audience == "snowflakecomputing.com":
        fetch = (
            'curl -sSf -H @- "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=snowflakecomputing.com" '
            + header
        )
    else:
        # -G puts the audience in the query string, URL-encoded, after the
        # runner's own query (curl joins it with &).
        fetch = (
            'curl -sSfG -H @- --data-urlencode "audience=$OIDC_AUDIENCE" '
            '"$ACTIONS_ID_TOKEN_REQUEST_URL" ' + header
        )
    # -I (isolated): the job's working directory is the checked-out repository, and a
    # json.py committed there would otherwise run while the token request is in reach.
    extract = "python3 -I -c 'import json, sys; print(json.load(sys.stdin)[\"value\"])'"
    lines = [
        "set -euo pipefail",
        "umask 077",
        f'token_file="$RUNNER_TEMP/{file_name}"',
        "mint() {",
        f'  token="$({fetch} | {extract})"',
        '  [ -n "$token" ] || return 1',
        '  echo "::add-mask::$token"',
        '  printf \'%s\' "$token" > "$token_file.new" && mv -f "$token_file.new" "$token_file"',
        "}",
        "mint",
        "# GitHub's ID tokens expire after about five minutes and the request URL stays",
        "# valid for the whole job, so a fresh one replaces the file every four minutes",
        "# for the later steps that read it (GitHub ends the loop with the job). A failed",
        "# refresh keeps the last token. Only the first token is masked in the log, so",
        "# no step should print this file.",
        "( while sleep 240; do mint || true; done ) >/dev/null 2>&1 &",
        "{",
        *[f"  {line}" for line in exports],
        '} >> "$GITHUB_ENV"',
    ]
    return LiteralStr("\n".join(lines) + "\n")


#: GitHub gives a pull request from a fork, or from Dependabot, no ID token, so the
#: login steps are skipped there instead of failing the job before validate runs.
GITHUB_HAS_ID_TOKEN = (
    "github.event.pull_request.head.repo.fork != true && github.actor != 'dependabot[bot]'"
)


def github_auth_steps(cloud_auth: Sequence[str], pin: Any) -> List[Dict[str, Any]]:
    """Steps that authenticate a job to every detected platform. ``pin`` is
    ``_pin_action``: every action is referenced by commit SHA. Each runs only where
    GitHub hands out an ID token (:data:`GITHUB_HAS_ID_TOKEN`)."""
    platforms = normalize(cloud_auth)
    steps: List[Dict[str, Any]] = []
    if "aws" in platforms:
        steps.append(
            {
                "name": "Authenticate to AWS (OIDC)",
                "uses": pin("aws-actions/configure-aws-credentials@v6"),
                "with": {
                    "role-to-assume": "${{ vars.AWS_ROLE_ARN }}",
                    "aws-region": "${{ vars.AWS_REGION }}",
                },
            }
        )
    if "gcp" in platforms:
        steps.append(
            {
                "name": "Authenticate to Google Cloud (OIDC)",
                "id": "auth",
                "uses": pin("google-github-actions/auth@v3"),
                "with": {
                    "workload_identity_provider": (
                        "projects/${{ vars.GCP_PROJECT_NUMBER }}/locations/global/"
                        "workloadIdentityPools/${{ vars.WIF_POOL }}/providers/"
                        "${{ vars.WIF_PROVIDER }}"
                    ),
                    "service_account": "${{ vars.GCP_SA_EMAIL }}",
                },
            }
        )
    if "snowflake" in platforms:
        steps.append(
            {
                "name": "Authenticate to Snowflake (workload identity federation)",
                "shell": "bash",
                "run": _github_token_script(
                    "snowflake-id-token",
                    "snowflakecomputing.com",
                    [
                        'echo "SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY"',
                        'echo "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER=OIDC"',
                        'echo "SNOWFLAKE_TOKEN_FILE_PATH=$token_file"',
                    ],
                ),
            }
        )
    if "openbao" in platforms:
        steps.append(
            {
                "name": "Authenticate to OpenBao (JWT login token)",
                "shell": "bash",
                "env": {"OIDC_AUDIENCE": "${{ vars.OPENBAO_JWT_AUDIENCE }}"},
                "run": _github_token_script(
                    "openbao-id-token",
                    "$OIDC_AUDIENCE",
                    ['echo "FLUID_VAULT_JWT_FILE=$token_file"'],
                ),
            }
        )
    for step in steps:
        step["if"] = GITHUB_HAS_ID_TOKEN
    return steps


def github_auth_env(cloud_auth: Sequence[str]) -> Dict[str, str]:
    """Job environment the detected platforms read (variables, never secrets)."""
    platforms = normalize(cloud_auth)
    env: Dict[str, str] = {}
    if "snowflake" in platforms:
        env["SNOWFLAKE_ACCOUNT"] = "${{ vars.SNOWFLAKE_ACCOUNT }}"
    if "databricks" in platforms:
        # The SDKs and the provider mint the token exchange themselves from
        # ACTIONS_ID_TOKEN_REQUEST_URL / _TOKEN.
        env["DATABRICKS_AUTH_TYPE"] = "github-oidc"
        env["DATABRICKS_HOST"] = "${{ vars.DATABRICKS_HOST }}"
        env["DATABRICKS_CLIENT_ID"] = "${{ vars.DATABRICKS_CLIENT_ID }}"
    if "openbao" in platforms:
        env["FLUID_VAULT_JWT_ROLE"] = "${{ vars.OPENBAO_JWT_ROLE }}"
        env["BAO_ADDR"] = "${{ vars.BAO_ADDR }}"
    return env


# ---------------------------------------------------------------------------
# GitLab CI
# ---------------------------------------------------------------------------

_GCP_AUDIENCE = (
    "//iam.googleapis.com/projects/$GCP_PROJECT_NUMBER/locations/global/"
    "workloadIdentityPools/$WIF_POOL/providers/$WIF_PROVIDER"
)

#: An ``external_account`` credential configuration (google-github-actions/auth
#: writes the same shape): the SDK reads the job's token from ``credential_source.file``,
#: exchanges it at the STS endpoint and impersonates the service account.
_GCP_CREDENTIAL_JSON = (
    "printf '"
    '{"type":"external_account","audience":"%s",'
    '"subject_token_type":"urn:ietf:params:oauth:token-type:jwt",'
    '"token_url":"https://sts.googleapis.com/v1/token",'
    '"service_account_impersonation_url":"https://iamcredentials.googleapis.com/v1/'
    'projects/-/serviceAccounts/%s:generateAccessToken",'
    '"credential_source":{"file":"%s"}}'
    '\' "$GCP_WIF_AUDIENCE" "$GCP_SA_EMAIL" "$FLUID_AUTH_DIR/gcp-token"'
)


def gitlab_id_tokens(cloud_auth: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """``id_tokens:`` of a job that reaches the cloud, one token per platform."""
    platforms = normalize(cloud_auth)
    tokens: Dict[str, Dict[str, str]] = {}
    if "aws" in platforms:
        tokens["AWS_OIDC_TOKEN"] = {"aud": "sts.amazonaws.com"}
    if "gcp" in platforms:
        tokens["GCP_OIDC_TOKEN"] = {"aud": _GCP_AUDIENCE}
    if "snowflake" in platforms:
        tokens["SNOWFLAKE_OIDC_TOKEN"] = {"aud": "snowflakecomputing.com"}
    if "databricks" in platforms:
        # env-oidc reads DATABRICKS_OIDC_TOKEN; Databricks recommends the account ID as aud.
        tokens["DATABRICKS_OIDC_TOKEN"] = {"aud": "$DATABRICKS_ACCOUNT_ID"}
    if "openbao" in platforms:
        tokens["OPENBAO_OIDC_TOKEN"] = {"aud": "$OPENBAO_JWT_AUDIENCE"}
    return tokens


def gitlab_auth_variables(cloud_auth: Sequence[str]) -> Dict[str, str]:
    """Job ``variables:`` the detected platforms read."""
    return {"DATABRICKS_AUTH_TYPE": "env-oidc"} if "databricks" in normalize(cloud_auth) else {}


def gitlab_auth_script(cloud_auth: Sequence[str]) -> List[str]:
    """Script lines (one shell: GitLab runs a job's lines in one session) that
    write each token to a mode-600 file and point the SDKs at it. Nothing
    prints a token or a credential."""
    platforms = normalize(cloud_auth)
    needs_file = [p for p in platforms if p != "databricks"]
    if not platforms:
        return []
    lines: List[str] = []
    if needs_file:
        lines += [
            "umask 077",
            # SECURITY_REVIEW S-005: never a predictable /tmp path; a private
            # directory that exists only for this job's shell.
            'FLUID_AUTH_DIR="$(mktemp -d)" && trap \'rm -rf "$FLUID_AUTH_DIR"\' EXIT',
        ]
    if "aws" in platforms:
        lines += [
            'printf "%s" "$AWS_OIDC_TOKEN" > "$FLUID_AUTH_DIR/aws-token"',
            # The SDKs assume AWS_ROLE_ARN (a CI/CD variable) with the token themselves.
            'export AWS_WEB_IDENTITY_TOKEN_FILE="$FLUID_AUTH_DIR/aws-token" '
            'AWS_ROLE_SESSION_NAME="fluid-ci-$CI_PIPELINE_ID"',
        ]
    if "gcp" in platforms:
        lines += [
            'printf "%s" "$GCP_OIDC_TOKEN" > "$FLUID_AUTH_DIR/gcp-token"',
            f'GCP_WIF_AUDIENCE="{_GCP_AUDIENCE}"',
            _GCP_CREDENTIAL_JSON + ' > "$FLUID_AUTH_DIR/gcp-credentials.json"',
            'export GOOGLE_APPLICATION_CREDENTIALS="$FLUID_AUTH_DIR/gcp-credentials.json"',
        ]
    if "snowflake" in platforms:
        lines += [
            'printf "%s" "$SNOWFLAKE_OIDC_TOKEN" > "$FLUID_AUTH_DIR/snowflake-token"',
            "export SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY "
            "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER=OIDC "
            'SNOWFLAKE_TOKEN_FILE_PATH="$FLUID_AUTH_DIR/snowflake-token"',
        ]
    if "openbao" in platforms:
        lines += [
            'printf "%s" "$OPENBAO_OIDC_TOKEN" > "$FLUID_AUTH_DIR/openbao-token"',
            'export FLUID_VAULT_JWT_FILE="$FLUID_AUTH_DIR/openbao-token" '
            'FLUID_VAULT_JWT_ROLE="$OPENBAO_JWT_ROLE"',
        ]
    return lines


# ---------------------------------------------------------------------------
# The banner every CI system shares
# ---------------------------------------------------------------------------

#: What the CI systems without keyless wiring here use. Each names the system's
#: own mechanism and nothing else; no static key is suggested.
_OTHER_SYSTEMS: Dict[str, List[str]] = {
    "azure": [
        "Azure DevOps: use a service connection with workload identity federation",
        "  (no secret), and the pipeline's own OIDC token for the other clouds.",
    ],
    "bitbucket": [
        "Bitbucket Pipelines: set `oidc: true` on the step; BITBUCKET_STEP_OIDC_TOKEN is",
        "  the ID token (issuer https://api.bitbucket.org/2.0/workspaces/<workspace>/pipelines-config/identity/oidc).",
    ],
    "circle": [
        "CircleCI: CIRCLE_OIDC_TOKEN_V2 is the job's ID token (issuer",
        "  https://oidc.circleci.com/org/<ORG_ID>).",
    ],
    "tekton": [
        "Tekton: use the task's Kubernetes service account token (projected, short-lived) or",
        "  SPIFFE/SPIRE as the identity, and the cloud's workload identity for that.",
    ],
    "jenkins": [
        "Jenkins has no native OIDC. It stays on credentials binding: prefer credentials that",
        "  expire (a role session, a short-lived token from your vault) over long-lived ones,",
        "  keep them in the Jenkins credential store or the agent's environment, and never",
        "  commit them. An OIDC plugin that vends ID tokens would let you use the setup below.",
    ],
}


#: The header's first line, by CI system: only GitHub Actions and GitLab are wired
#: for keyless auth here.
_HEADLINES: Dict[str, str] = {
    "github": "Credential model: keyless. No static cloud key lives in this file or in CI.",
    "gitlab": "Credential model: keyless. No static cloud key lives in this file or in CI.",
    "jenkins": (
        "Credential model: credentials binding (Jenkins has no native OIDC). No cloud key "
        "lives in this file."
    ),
    "": (
        "Credential model: this CI system's own OIDC or workload identity (not wired here). "
        "No cloud key lives in this file."
    ),
}

#: What forge reads once a job holds an ID token in a file, per platform.
_TOKEN_READERS: Dict[str, List[str]] = {
    "aws": ["    aws         AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE (the SDK's web identity)"],
    "gcp": ["    gcp         GOOGLE_APPLICATION_CREDENTIALS naming an external_account file"],
    "snowflake": [
        "    snowflake   SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY, SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER=OIDC,",
        "                SNOWFLAKE_TOKEN_FILE_PATH, SNOWFLAKE_ACCOUNT",
    ],
    "databricks": [
        "    databricks  DATABRICKS_AUTH_TYPE=env-oidc or file-oidc, DATABRICKS_OIDC_TOKEN (or",
        "                DATABRICKS_OIDC_TOKEN_FILEPATH), DATABRICKS_HOST, DATABRICKS_CLIENT_ID",
    ],
    "openbao": ["    openbao     FLUID_VAULT_JWT_FILE, FLUID_VAULT_JWT_ROLE, BAO_ADDR"],
}


def banner_lines(cloud_auth: Sequence[str], ci_system_name: str) -> List[str]:
    """The credential section of a pipeline's header comment (no prefix)."""
    platforms = normalize(cloud_auth)
    key = ci_system_name.lower()
    system = next(
        (
            name
            for name in ("github", "gitlab", "azure", "bitbucket", "circle", "tekton", "jenkins")
            if name in key
        ),
        "",
    )
    lines = [_HEADLINES.get(system, _HEADLINES[""])]
    if not platforms:
        lines += [
            "  No cloud platform was detected: no contract was read, or it binds none and",
            "  names no vault:// or openbao:// secret, so no cloud credential is configured.",
            "  Run `fluid generate ci <contract>` to get the contract's keyless setup.",
        ]
    else:
        lines += ["  Detected from the contract and its overlays:"]
        lines += [f"    {platform:<10} {_WHAT[platform]}" for platform in platforms]
    if system in ("github", "gitlab"):
        if platforms:
            where = (
                "Settings → Secrets and variables → Actions → Variables"
                if system == "github"
                else "Settings → CI/CD → Variables"
            )
            lines += [
                "",
                f"  The jobs that reach the cloud ask for an OIDC ID token ({where}",
                "  holds the non-secret values below):",
            ]
            lines += [
                f"    {name}  (e.g. {example})" for _, name, example in variables(platforms, system)
            ]
            lines += [""] + [
                f"  {line}" if line else "" for line in trust_comments(platforms, system)
            ]
    else:
        lines += [
            "",
            "  This CI system's pipeline is not wired for OIDC here; use its own mechanism:",
            *[f"  {line}" for line in _OTHER_SYSTEMS.get(system, [])],
        ]
        readers = [line for p in platforms for line in _TOKEN_READERS[p]]
        if readers:
            lines += [
                "  Once a job has an ID token in a file, forge reads it from these variables:",
                *readers,
            ]
    lines += [
        "",
        "  Not cloud keys, but still credentials: `fluid publish` to a catalog reads DMM_API_URL /",
        "  DMM_API_KEY, and --target fluid-command-center reads FLUID_CC_ENDPOINT / FLUID_API_KEY",
        "  (sent as X-API-Key) / FLUID_CC_ORG_ID. Supply those from your CI's secret store.",
        "  See ``fluid_build.credentials.resolver`` for the full resolver chain.",
    ]
    return lines


def env_example_lines(cloud_auth: Sequence[str]) -> List[str]:
    """``.env.ci.example`` body: the non-secret variables, commented."""
    platforms = normalize(cloud_auth)
    lines = [
        "# Variables for the FLUID pipeline. None of them is a secret:",
        "# the pipeline holds no static cloud key (keyless OIDC).",
        "#",
        "# FLUID Configuration",
        "FLUID_LOG_LEVEL=INFO",
        "# CONTRACT=contract.fluid.yaml",
        "",
    ]
    if not platforms:
        lines += [
            "# The contract binds no cloud platform, so no cloud variable is needed.",
            "# Re-run `fluid generate ci` after adding a binding.",
        ]
    for platform in platforms:
        lines += [f"# {platform}: {_WHAT[platform]}"]
        lines += [f"# {name}={example}" for name, example in VARIABLES[platform]]
        lines += [
            f"# {name}={example}  # GitLab only"
            for name, example in _GITLAB_EXTRA_VARIABLES.get(platform, ())
        ]
        lines += [""]
    return lines
