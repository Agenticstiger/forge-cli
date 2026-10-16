# Environment variables and project `.env` files

`fluid apply`, `fluid verify` and `fluid publish` read the project's dotenv
files before they run, and so does every credential lookup that falls back to
them:

1. `.env`
2. `.env.<environment>` (`--env`, else `FLUID_ENV`, else `dev`)
3. `.env.local`
4. the file named by `FLUID_SECRETS_FILE`, when you exported it

A variable you already set in the shell or CI environment always wins over
files 1-3, whatever its case: a project `postgres_host` does not load next to
your `POSTGRES_HOST` (Windows treats them as one variable, and so do
case-insensitive readers such as pydantic-settings).

## Credentials load from `.env`; security settings do not

Files 1-3 belong to the project, and the project may be written by someone
other than the person running `fluid`. So they can hold credentials (API keys,
tokens, passwords), but not the settings that:

- widen a sandbox or confinement root;
- move where FLUID reads trusted configuration or writes state, audit records
  and logs;
- change an authentication or trust mode;
- change where data or credentials are sent;
- choose which program or library runs.

```dotenv
# .env in the project
FLUID_COMMAND_CENTER_API_KEY=cc_test_key        # loads
FLUID_DUCKDB_ALLOWED_DIRS=/srv/shared/reference # ignored
```

```console
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
Ignored 1 setting from project dotenv files: FLUID_DUCKDB_ALLOWED_DIRS (.env). A project file may hold credentials, but not settings that widen a sandbox, change authentication or trust, or change where data and credentials are sent. If you meant to set it, export it in your shell or CI environment instead.
Loaded 1 credentials from .env files
```

The warning (logged at `WARNING`) names the variables and the file, never
their values, and appears once per run for each file. A variable you also set in the shell
or in your `FLUID_SECRETS_FILE` is not reported: your value is the one in effect. A
blank one is reported, since readers take a blank variable as unset.

To use such a setting, set it where the operator controls it:

```console
$ export FLUID_DUCKDB_ALLOWED_DIRS=/srv/shared/reference
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
```

or in the CI job's `environment {}` / `env:` block, or in your
`FLUID_SECRETS_FILE`.

### `FLUID_SECRETS_FILE`

`FLUID_SECRETS_FILE` counts only when it is set in the real environment; a
project `.env` cannot name one. A leading `~` in it is resolved against your
home directory before any project file is read. The file it names is yours, so every key in it
loads, protected or not, and it overrides values from the shell (it is loaded
with `override=True`, the launchpad convention).

### Which variables are protected

The list lives in one place,
[`fluid_build/credentials/protected_env.py`](../fluid_build/credentials/protected_env.py),
and matching ignores case (on Windows `fluid_ref_root` and `FLUID_REF_ROOT` are
the same variable).

| Kind | Examples |
|---|---|
| Sandbox and confinement roots | `FLUID_DUCKDB_ALLOWED_DIRS`, `FLUID_REF_ROOT`, `FLUID_UPSTREAM_CONTRACTS`, `FLUID_WORKSPACE_ROOT`, `FLUID_FORGE_AGENT_CWD`, `FLUID_LOCAL_DUCKDB_PATH` |
| Where trusted config, state, audit and logs live | `HOME`, `USERPROFILE`, `HOMEDRIVE` / `HOMEPATH`, `XDG_CONFIG_HOME` and the other `XDG_*` base directories (where `~` and git's and the cloud SDKs' own config resolve), their Windows counterparts `APPDATA` (Python's user site-packages), `LOCALAPPDATA` (the keyring's `keyringrc.cfg`), `ProgramData` (FLUID's system config, Git for Windows' config) and `ALLUSERSPROFILE`, the temporary directory `TMPDIR` / `TEMP` / `TMP` (where the coding agents run), `FLUID_SECRETS_FILE`, `FLUID_ENCRYPTION_KEY` / `_PASSPHRASE` (the key of your own `~/.fluid/credentials.enc`), `FLUID_USER_HOME`, `FLUID_HOME`, `FLUID_CONFIG`, `FLUID_CACHE_DIR`, `FLUID_LOG_FILE`, `FLUID_PRICES_JSON`, `FLUID_STATE_BACKEND`, `FLUID_STORE_BACKEND` / `_DSN` / `_PATH` / `_ROOT`, `FLUID_AUDIT_ROOT`, `FLUID_AUDIT_MAX_*`, `FLUID_ATHENA_OUTPUT_LOCATION` |
| Authentication and trust | `FLUID_SAFE_MODE`, `FLUID_AI_READY`, `FLUID_ALLOW_*`, `FLUID_OPENLINEAGE_ALLOW_PRIVATE`, `FLUID_DBT_FORWARD_ENV`, `FLUID_OVERLAY_*`, `FLUID_PROMPT_OVERLAYS`, `FLUID_PLUGIN_STRICT_COMPAT`, `FLUID_PLUGINS_ALLOWLIST` / `_BLOCKLIST`, `FLUID_PROVIDERS`, `FLUID_SECRETS_INMEMORY`, `FLUID_TELEMETRY`, `FLUID_MCP_AUTH_MODE`, `FLUID_MCP_JWT_*`, `FLUID_MCP_TRUSTED_PROXIES`, the MCP rate limits, every `*_ALLOWLIST`, `HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `SSL_CERT_FILE` / `_DIR`, `SSLKEYLOGFILE` (each in either case, so `https_proxy` too), `PGSSLMODE`, `DMM_ALLOW_INSECURE_HTTP`, `DMM_AUTO_APPROVE_ACCESS`, libpq's CA and revocation lists `PGSSLROOTCERT`, `PGSSLCRL`, `PGSSLCRLDIR` |
| Where data and credentials go | `FLUID_COMMAND_CENTER_URL` / `_ENABLED`, `FLUID_API_URL`, `FLUID_CC_ENDPOINT`, `FLUID_CATALOG_*_URL`, `FLUID_OPENLINEAGE_URL` / `_ENDPOINT`, `FLUID_LLM_ENDPOINT`, `FLUID_LLM_PROVIDER` / `_MODEL`, `FLUID_RUNNER_HOST_OVERRIDE`, `FLUID_PUBLIC_REGISTRY`, `FLUID_<NAME>_MCP`, `DMM_API_URL`, `DATAHUB_GMS_URL` / `_HOST`, `OPENMETADATA_SERVER_URL` / `OPENMETADATA_HOST`, `SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL`, `BIGQUERY_EMULATOR_HOST`, `OPENLINEAGE_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `AWS_ENDPOINT_URL*`, botocore's credential-provider locations (`AWS_CONTAINER_CREDENTIALS_FULL_URI` / `_RELATIVE_URI`, `AWS_CONTAINER_AUTHORIZATION_TOKEN` / `_FILE`, `AWS_EC2_METADATA_SERVICE_ENDPOINT` / `_MODE`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_ROLE_ARN`, `AWS_ROLE_SESSION_NAME`), libpq's host address `PGHOSTADDR` (libpq connects to it whatever host it was given, and sends the password meant for that host, your `~/.pgpass` entry for it included) and its service definitions (`PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`), `VAULT_ADDR`, `OLLAMA_HOST`, `OLLAMA_API_BASE`, `USE_LITELLM_PROXY` and `LITELLM_PROXY_API_BASE` (litellm sends the proxy whichever provider key FLUID resolved, so no project file can pair it with a key of its own), litellm's GitHub Copilot settings `GITHUB_COPILOT_API_BASE`, `GITHUB_COPILOT_API_KEY_URL`, `GITHUB_COPILOT_ACCESS_TOKEN_URL`, `GITHUB_COPILOT_DEVICE_CODE_URL`, `GITHUB_COPILOT_TOKEN_DIR`, `GITHUB_COPILOT_ACCESS_TOKEN_FILE` and `GITHUB_COPILOT_API_KEY_FILE` (that route sends the GitHub login and the Copilot key litellm stored, never the key FLUID passes; it sends that key to `GITHUB_COPILOT_API_BASE` when the stored key names no endpoint, as on a first call, and keeps that login and the key where these say) |
| What runs | `FLUID_<NAME>_MCP_COMMAND` / `_ARGS` / `_ENV`, `FLUID_OPENTOFU_VERSION`, `DBT_EXECUTABLE`, `PATH`, `ComSpec` / `SystemRoot` / `windir` (the shell Windows runs for `shell=True`), `PYTHON_DOTENV_DISABLED` and `LITELLM_MODE` (see below), `PYTHONPATH`, `LD_*`, `DYLD_*`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `GIT_CONFIG_*`, `TF_CLI_CONFIG_FILE`, `TF_CLI_ARGS*`, and SDK and agent settings that can name a credential helper program or an endpoint: `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `KUBECONFIG`, `CLOUDSDK_CONFIG`, `DOCKER_CONFIG`, `DOCKER_HOST`, `GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` (the coding-agent CLIs `fluid forge` can run read their endpoint, key name and Claude Code's `apiKeyHelper` program from there) |
| git | `GIT_SSL_*` (TLS verification, CA, client certificate), `GIT_PROXY_*` (proxy command, proxy TLS), `GIT_TRACE*` and `GIT_CURL_*` (they write request headers to a file or stderr), `GIT_SSH` / `_COMMAND`, `GIT_CONFIG*`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_TEMPLATE_DIR`, `GIT_ALLOW_PROTOCOL`, `GIT_EXTERNAL_DIFF`, `GIT_EDITOR`, `GIT_PAGER`. `GIT_TERMINAL_PROMPT` and identity settings stay loadable. |

A `FLUID_*` name that is not listed is still protected when its shape says it
is an endpoint, location, allowlist or program: it ends in `_URL`,
`_ENDPOINT`, `_HOST`, `_ALLOWLIST`, `_BLOCKLIST`, `_ROOT`, `_DIR(S)`, `_PATH`,
`_FILE`, `_DSN`, `_HOME`, `_BACKEND`, `_PROXIES`, `_MCP`, `_COMMAND`, `_ARGS`,
`_MCP_ENV` or `_EXECUTABLE`, or contains `_ALLOW_`, `_TRUSTED_`, `_AUTH_MODE`,
`_JWT_` or `_INSECURE`.

`FLUID_DBZ_*` is protected too. The Debezium runner sets these variables
itself: it writes each credential of Debezium Server's generated
`application.properties` as `${FLUID_DBZ_<PROPERTY>}` and passes the value in
the server's environment.

Matching is by variable name, never by file name: on a case-insensitive
filesystem `.Env` or `.ENV.local` is the `.env` / `.env.local` FLUID opens, and
it is filtered the same way.

### Tools that read `.env` themselves

dbt loads the nearest `.env` when it starts, and litellm loads one when it is
imported (found by walking up from its install directory, so a virtualenv inside
the project finds the project's). Both use python-dotenv and neither applies
this filter. Every `fluid` process, and any process once FLUID has read a
project's dotenv files, therefore sets `PYTHON_DOTENV_DISABLED=1`, which
python-dotenv honours from 1.2.0, the oldest release FLUID installs. The dbt it
runs gets the switch too, on the host and in the dbt container. A project file
cannot change it. A python build step is the project's own code, so it runs
without the switch FLUID set and its own `load_dotenv()` works; a value you
exported yourself reaches it unchanged.

**dbt no longer finds a `.env` above the contract.** dbt-core looks for the
nearest `.env` upward from the directory it runs in
(`load_dotenv(find_dotenv(usecwd=True))`). In a monorepo it used to find one at
the repository root even when the contract's directory had none. With the
switch on, that lookup is off, and a model that reads such a value with
`env_var()` fails with `Env var required but not provided`. FLUID reads the
dotenv files of the directory you run it from (`.env`, `.env.<environment>`,
`.env.local`), filtered. Any one of these brings the value back:

- Export the variable in your shell or CI job, or put it in your
  `FLUID_SECRETS_FILE`. The dbt FLUID runs on the host inherits it. The dbt
  container gets only the adapter-prefixed names (`SNOWFLAKE_`, `DBT_`,
  `GOOGLE_`, ...), libpq's connection variables (`PGHOST`, `PGUSER`, ...) and
  what the project's dotenv files exported, so also list the name (or a prefix
  ending in `_`) in `FLUID_DBT_FORWARD_ENV`, a comma-separated list you export
  yourself; a project file cannot set it.
- Let FLUID's own filtered read load it: put the value in a `.env` in the
  directory you run `fluid` from (next to the contract), or run `fluid` from the
  repository root (`fluid apply products/p/contract.fluid.yaml`) so that it
  reads the root `.env`.
- Export `PYTHON_DOTENV_DISABLED=0`. dbt then loads the nearest `.env` itself
  again. That has a cost: when the project has a `.env` next to the contract,
  dbt reads it without this filter, and every setting FLUID refused from it
  comes back inside dbt (an endpoint next to your credential, a sandbox root).
  Use it only for a checkout you trust. The dbt container sees only the
  mounted dbt project, so it never finds a `.env` above it either way.

### Values are literal

Files 1-3 are read without `${VAR}` expansion: `LABEL=${HOME}` sets `LABEL`
to the text `${HOME}`. Expansion would read the operator's environment, so a
project file could copy a secret into a loadable variable that a contract's
`{{ env.LABEL }}` then ships to a catalog. If you need expansion, put the
variable in your `FLUID_SECRETS_FILE` (it is yours, so it keeps it) or export
it in the shell.

### What stays loadable, and why

- **Credentials**: `*_API_KEY`, `*_TOKEN`, `*_PASSWORD`, `FLUID_PII_*` keys,
  `FLUID_FORGE_DB_URI`, `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_PROFILE`. A key
  from `.env` cannot be paired with a FLUID endpoint, a proxy or an SDK config
  file from `.env`, because those are protected. `AWS_PROFILE` picks a profile
  from your own AWS config; a project cannot point that config elsewhere.
- **Third-party endpoints, with their own credential**: `OPENAI_BASE_URL`,
  `PGHOST`, `SNOWFLAKE_ACCOUNT`, `DATABRICKS_HOST` and the like load when the
  project files also supply the credential that goes with them; see
  [Third-party endpoints](#third-party-endpoints-load-only-with-their-own-credential).
- **Non-secret targets**: `FLUID_PROJECT`, `FLUID_REGION`, `SNOWFLAKE_WAREHOUSE`,
  `SNOWFLAKE_ROLE`, `PGDATABASE` and the like. They choose within the account
  your credential already opens, not where it is sent.
- **Budgets with no default limit** (`FLUID_COST_LIMIT_USD*`): a project file
  can only add a limit; one you set in the shell wins.
- **Output, logging verbosity and feature switches** that neither widen access
  nor redirect data (`FLUID_ENV`, `FLUID_LOG_LEVEL`, `FLUID_QUIET`, ...).

## Third-party endpoints load only with their own credential

An SDK reads where to connect and what to send from the same environment. If
a project `.env` could set the endpoint while the credential is yours, your
credential would go to the project's host:

```dotenv
# .env in the project
OPENAI_BASE_URL=https://llm.project.example/v1
PGHOST=pg.project.example
```

```console
$ export OPENAI_API_KEY=... PGPASSWORD=...     # yours
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
Ignored 2 endpoint settings from project dotenv files: OPENAI_BASE_URL (.env, would receive OPENAI_API_KEY); PGHOST (.env, would receive PGPASSWORD). A project file may not point a credential you hold outside the project at a host the project chose. Export them in your shell or CI environment.
```

So a project file may set a service's endpoint only for a credential it
supplies itself. The endpoint is refused when:

1. a credential of the same service is set in your shell, CI environment or
   `FLUID_SECRETS_FILE` (with a value other than the project file's own); or
2. the service also finds your credential outside the environment (the
   keyring `fluid ai setup` and `fluid auth` save to, a stored CLI login, an SDK
   config file, cloud default credentials) and the project files do not supply
   a credential that every reader of that endpoint sends instead of yours.

The second point is about what is actually sent. FLUID hands litellm the key it
resolved (`FLUID_LLM_API_KEY`, then the provider's own variable for OpenAI,
Anthropic, Gemini and GitHub Models, then the keyring), and that key beats any
other variable litellm would read, so a project `GROQ_API_KEY` next to
`GROQ_API_BASE` is a decoy while your keyring holds a Groq key. The same goes
for a credential no reader of that endpoint looks at (`CLAUDE_API_KEY` for
litellm, `SNOWFLAKE_PAT` for the Snowflake provider). The column below names
what does pair. A project credential counts
only when it is the value readers see: not blank, not replaced by a variable of
yours with the same name (even an empty one), and, outside Windows, spelled in
upper case (`openai_api_key` is a different variable that no SDK reads). The
one exception is Snowflake's, below. A project setting that is ignored is
reported even when your variable of that name is blank, since readers take a
blank variable as unset.

```console
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes     # Groq key in the keyring
Ignored 1 endpoint setting from project dotenv files: GROQ_API_BASE (.env, would receive your Groq login or keyring credential; it pairs only with FLUID_LLM_API_KEY). ... Export it in your shell or CI environment, or put the credential in the same project file.
```

The warning offers the project-file remedy only where it works. While you
hold a credential of the service yourself, nothing the project adds pairs the
endpoint. Nor does it while a switch is set, and the warning names the switch
(`REDSHIFT_HOST (.env, would receive your Amazon Redshift login or keyring
credential while AWS_PROFILE is set)`). In both cases the only remedy is to
export the endpoint yourself.

A project that brings its own service keeps working, and an endpoint you
exported always wins:

```dotenv
# .env: the project's own database and its own password -- both load
PGHOST=localhost
PGPASSWORD=postgres
```

To use a project's endpoint with your credential, decide that yourself: export
the endpoint in your shell or CI job (or put it in your `FLUID_SECRETS_FILE`).
The warning names the variables, the file and the credentials involved, never
their values.

| Service | Endpoint variables | A project file pairs it with | Your credential can also come from |
|---|---|---|---|
| OpenAI | `OPENAI_BASE_URL`, `OPENAI_API_BASE` | `OPENAI_API_KEY` or `FLUID_LLM_API_KEY` (codex: `CODEX_API_KEY`, see below) | keyring, codex login |
| Anthropic | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_BASE` | `ANTHROPIC_API_KEY` (the one key both litellm and the claude CLI send before your keyring key or Claude login) | keyring, Claude login, `apiKeyHelper` |
| Azure OpenAI | `AZURE_OPENAI_ENDPOINT`, `AZURE_API_BASE`, `AZURE_AI_API_BASE` | `FLUID_LLM_API_KEY` | keyring |
| Google Gemini | `GEMINI_API_BASE`, `GOOGLE_GEMINI_BASE_URL` | `FLUID_LLM_API_KEY`, `GOOGLE_API_KEY` or `GEMINI_API_KEY` | keyring |
| Groq, Mistral, Cohere | `GROQ_API_BASE`; `MISTRAL_API_BASE`, `MISTRAL_AZURE_API_BASE`; `COHERE_API_BASE`, `CO_API_URL` | `FLUID_LLM_API_KEY` | keyring |
| GitHub Models | `GITHUB_API_BASE` | `FLUID_LLM_API_KEY` or `GITHUB_API_KEY` | keyring |
| Amazon Bedrock | `AWS_BEDROCK_RUNTIME_ENDPOINT`, `BEDROCK_API_BASE`, `ANTHROPIC_BEDROCK_BASE_URL` | `FLUID_LLM_API_KEY` | keyring |
| Vertex AI | `VERTEXAI_API_BASE`, `VERTEX_API_BASE` | `FLUID_LLM_API_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` (or `VERTEXAI_CREDENTIALS`) | keyring, application default credentials |
| Google Cloud | `GOOGLE_VERTEX_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`, `GOOGLE_*_CUSTOM_ENDPOINT`, `GOOGLE_CLOUD_UNIVERSE_DOMAIN`, `GCE_METADATA_HOST` / `_IP` / `_ROOT` | `GOOGLE_APPLICATION_CREDENTIALS` | application default credentials |
| Google Cloud CLI | `CLOUDSDK_API_ENDPOINT_OVERRIDES_*` | `CLOUDSDK_AUTH_ACCESS_TOKEN_FILE` or `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` | gcloud login |
| Hugging Face Hub | `HF_ENDPOINT`, `HF_INFERENCE_ENDPOINT` | `HF_TOKEN` | `hf auth login` token |
| PostgreSQL | `PGHOST`, `PGPORT`, `POSTGRES_HOST`, `POSTGRES_PORT`, `PG_HOST`, `PG_PORT` | not needed: refused while you hold `PGPASSWORD`, `PGPASSFILE`, `PGSSLKEY`, `POSTGRES_PASSWORD` or `PG_PASSWORD`. While your libpq passfile (named by `PGPASSFILE`, else `~/.pgpass`, else `%APPDATA%\postgresql\pgpass.conf`) has an entry for any host (`*`), `PGHOST`/`PGPORT` pair only with `PGPASSWORD`, `POSTGRES_*` only with `POSTGRES_PASSWORD`, and `PG_*` with `PG_PASSWORD` or `POSTGRES_PASSWORD` | `~/.pgpass` `*` entry |
| MySQL | `MYSQL_HOST`, `MYSQL_TCP_PORT`, `MYSQL_UNIX_PORT` | not needed: refused only while you hold `MYSQL_PWD` or `MYSQL_PASSWORD` | |
| Snowflake | `SNOWFLAKE_ACCOUNT`, `SF_ACCOUNT`, `SNOWFLAKE__ACCOUNT`, `ACCOUNT`, `SNOWFLAKE_ACCOUNT_NAME`, `SNOWFLAKE_ORGANIZATION_NAME`, `SNOWFLAKE_HOST`, `SNOWFLAKE_PORT`, `SNOWFLAKE_PROTOCOL`, `SNOWFLAKE_PROD_ACCOUNT` | `SNOWFLAKE_PASSWORD` (or `SF_PASSWORD`), `SNOWFLAKE_OAUTH_TOKEN` or `SNOWFLAKE_PRIVATE_KEY_PATH`, or the resolver's other spellings of them (`SNOWFLAKE__PASSWORD`, `PASSWORD`, `SNOWFLAKE__OAUTH_TOKEN`, `OAUTH_TOKEN`, `SNOWFLAKE__PRIVATE_KEY_PATH`, `PRIVATE_KEY_PATH`); see below | for your own account only (none is read for a project's): keyring, ~/.fluid/credentials.enc, ~/.fluidrc.yaml, Vault, a secret manager, the `~/.snowflake/` config and connection files, `~/.dlt/secrets.toml` |
| Amazon Redshift | `REDSHIFT_HOST`, `REDSHIFT_PORT` | `REDSHIFT_PASSWORD`, but never while `REDSHIFT_USE_IAM`, `REDSHIFT_IAM_PROFILE` or `AWS_PROFILE` is set (IAM auth mints a database password from your AWS credentials) | AWS profile |
| Databricks | `DATABRICKS_HOST`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_CONFIG_FILE` | `DATABRICKS_TOKEN` | keyring, ~/.databrickscfg |
| Databricks model serving | `DATABRICKS_API_BASE` | `FLUID_LLM_API_KEY` | keyring |
| IBM watsonx | `WATSONX_API_BASE`, `WATSONX_URL`, `WX_URL`, `WML_URL`, `WATSONX_IAM_URL` | `FLUID_LLM_API_KEY`, but never while you hold `WATSONX_TOKEN` or `WATSONX_ZENAPIKEY` (litellm sends them ahead of the key FLUID passes) | keyring |
| dlt | `*CREDENTIALS__HOST`, `*CREDENTIALS__PORT`, `*CREDENTIALS__SERVER_HOSTNAME`, `*CREDENTIALS___SNOWFLAKE_HOST`, and the connection string `*__CREDENTIALS` / `CREDENTIALS` (`DESTINATION__POSTGRES__CREDENTIALS`, `DESTINATION__CREDENTIALS` ...) | the password of the same section (`DESTINATION__POSTGRES__CREDENTIALS__PASSWORD` for `DESTINATION__POSTGRES__CREDENTIALS__HOST`, `DESTINATION__CREDENTIALS__PASSWORD` for `DESTINATION__CREDENTIALS`), or for a connection string a password inside it (in the URL's user info, or MotherDuck's `md:///db?token=...`) | the dlt bridge, dlt's `secrets.toml` / `config.toml` and other config providers, for your own destination only; see below |
| Azure identity | `AZURE_AUTHORITY_HOST`, `AZURE_POD_IDENTITY_AUTHORITY_HOST`, `IDENTITY_ENDPOINT`, `MSI_ENDPOINT`, `IMDS_ENDPOINT` | `AZURE_CLIENT_SECRET` or `AZURE_CLIENT_CERTIFICATE_PATH`, with `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` | shared token cache |
| GitHub | `GITHUB_HOST`, `GH_HOST`, `GITHUB_API_URL` | not needed: refused only while you hold `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN` or `GH_ENTERPRISE_TOKEN` | |

Any credential of a row that you hold in your environment refuses the row's
endpoints, whatever the project supplies.

**The coding agents.** codex sends its stored login to `OPENAI_BASE_URL`
unless `CODEX_API_KEY` is set, so a project's `OPENAI_BASE_URL` paired with its
`OPENAI_API_KEY` (a local OpenAI-compatible server) serves FLUID's own OpenAI
calls but is not passed to codex unless the project files supply
`CODEX_API_KEY` too.

**Snowflake.** While the Snowflake account or host in effect came from the
project's files, FLUID's Snowflake readers take credentials only from the
environment and those files, so the account goes only with the project's own
password, OAuth token or key path. The credential resolver skips the keyring,
`~/.fluid/credentials.enc`, `~/.fluidrc.yaml`, Vault, the secret managers, the
values it cached and the interactive prompt. OpenTofu's Snowflake provider gets
an empty config file (`SNOWFLAKE_CONFIG_PATH`) instead of `~/.snowflake/config`.
The hosted Snowflake MCP server gets an empty `SNOWFLAKE_HOME` and no
`SNOWFLAKE_CONNECTIONS*` or connection-name variables. dlt's Snowflake
destination gets the project's credentials only (see **dlt** below). So the
quickstart `.env` (`SNOWFLAKE_ACCOUNT`,
`SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, as `.env.example` and the forge scaffold
write it) works as it is. So does it behind blank variables of yours, as a
docker compose `environment:` list or a CI job passes variables you have not
set: the credential resolver reads a blank variable as unset and uses the
project's file instead, for the account and the password alike. So while your
`SNOWFLAKE_ACCOUNT` is blank, a project `SNOWFLAKE_PASSWORD` (or `SF_PASSWORD`,
`SNOWFLAKE_OAUTH_TOKEN`, `SNOWFLAKE_PRIVATE_KEY_PATH`, or the bare `PASSWORD`,
`OAUTH_TOKEN`, `PRIVATE_KEY_PATH`) still pairs the project's account behind your
blank variable of the same name; next to an account the
project's file exports, it does not, since the readers of the environment would
see the account without it. An account you export yourself or set in your
`FLUID_SECRETS_FILE` is yours, even when the project's file has the same value,
and your stores serve it as before. The project's account counts in any
spelling: `snowflake_account` is the account to pydantic-settings readers such
as the dlt bridge, as it is to Windows. The same holds for every endpoint in
the table above.

The authenticator counts as a credential. FLUID uses it ahead of the password,
and values other than `snowflake` can sign in with something the project's
files do not hold: your browser session (`externalbrowser`), an identity
provider login (an Okta URL), an OAuth or workload identity, a key. So a
`SNOWFLAKE_AUTHENTICATOR` you set in your shell, CI environment or
`FLUID_SECRETS_FILE`, with any value, refuses the project's account, and so do
its other spellings (`SF_AUTHENTICATOR`, `SNOWFLAKE__AUTHENTICATOR`,
`AUTHENTICATOR`, and dlt's `CREDENTIALS__AUTHENTICATOR` under any section, such
as `DESTINATION__SNOWFLAKE__CREDENTIALS__AUTHENTICATOR`). Next to its own
account, or its own dlt Snowflake endpoint, a project file may set the
authenticator only to `snowflake`.
Any other value is ignored, under any of those names, `oauth` and
`snowflake_jwt` included. A project's `SNOWFLAKE_OAUTH_TOKEN` or
`SNOWFLAKE_PRIVATE_KEY_PATH` does not need them: FLUID's connection settings
and the dbt profile choose OAuth or key-pair sign-in from the token or key
path itself.

The warning names the variable and its file:

```console
Ignored 1 sign-in setting from project dotenv files: SNOWFLAKE_AUTHENTICATOR (.env). Next to a Snowflake endpoint from the project's files, a project file may set it only to 'snowflake', ... If you mean to sign in to that endpoint that way, export it in your shell or CI environment.
```

With an account you exported, a project file's authenticator loads as before:
you chose the account.

**dlt.** dlt fills every credential field of a destination that the
environment leaves unset from its other config providers: `.dlt/secrets.toml`
and `config.toml` in the directory `fluid` runs from, your global `~/.dlt`
ones, and Airflow's or a vault's when configured. FLUID's dlt bridge also copies
your `SNOWFLAKE_*`, `POSTGRES_*` and `REDSHIFT_*` variables into dlt's names.
So while an endpoint from the project's files reaches a destination, that
destination's credentials come only from the project's files and the contract
(`binding.location`). Such an endpoint is one of dlt's own variables for the
destination (`DESTINATION__<NAME>__CREDENTIALS__HOST`, the shorter
`DESTINATION__CREDENTIALS__` and `CREDENTIALS__` sections, the same behind the
run's pipeline name, the connection string, or Snowflake's host override
`DESTINATION__SNOWFLAKE__CREDENTIALS___SNOWFLAKE_HOST`), in any case. It is
also every other field of the destination's dlt `credentials` configuration
that the project's files set, at any depth (default-deny): the `query` map
(`DESTINATION__<NAME>__CREDENTIALS__QUERY`, the whole map or a single key under
it, such as `…__QUERY__HOSTADDR`), which libpq reads `hostaddr`, `host` and
`options` from; the user name and the password; a field of a configuration
nested in the credentials, such as ducklake's `catalog` connection and
`storage` filesystem or lance's `credentials`, under every name dlt reads it
under (`DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG__HOST` and the shorter
`DESTINATION__DUCKLAKE__CATALOG__HOST`, `DESTINATION__CATALOG__HOST` and
`CATALOG__HOST`); and any other field. dlt layers each of these on top of the
operator's credentials, so a project that set one would move or reshape the
operator's connection, or choose its credential, with the operator's password
still on it; it pins the destination the same way a host does. If FLUID cannot
read dlt's description of a destination's credentials, every name under the
destination's credentials sections and under `DESTINATION__<NAME>__` pins it.
The only exceptions are these top-level fields, which select inside your own
account or tune its session and cannot move the connection or choose a
credential: `database`, `connect_timeout`, `client_encoding` and
`session_timezone` for `postgres` and `redshift`, and `database`, `warehouse`
and `role` for `snowflake`. A project may set those for your destination. The
names are matched exactly as dlt looks them up, for the destination as dlt
resolves the contract's `dlt.destination` (`dlt.destinations.snowflake` reads
the `DESTINATION__SNOWFLAKE__` section, a named destination its own), so a
contract's `dlt.pipeline_name` that looks like a section (`sources`,
`a__destination__postgres`) changes nothing. It can also be an
endpoint FLUID copies in (`SNOWFLAKE_ACCOUNT`, `POSTGRES_HOST`, `PG_HOST`,
`REDSHIFT_HOST`), when the contract's `binding.platform` is that destination,
since the bridge runs only then, and while you have not exported the
destination's own dlt host (`DESTINATION__POSTGRES__CREDENTIALS__HOST`, or one
dlt reads ahead of it behind the pipeline name:
`<PIPELINE>__DESTINATION__POSTGRES__CREDENTIALS__HOST`,
`<PIPELINE>__DESTINATION__CREDENTIALS__HOST`, `<PIPELINE>__CREDENTIALS__HOST`)
or a connection string that names a host: dlt uses those ahead of FLUID's copy,
so the destination stays yours. A host in your `secrets.toml` or a vault does
not count: dlt reads the environment first, and finds FLUID's copy of the
project's host there. What you exported is read for each build from the
environment as it was before FLUID's bridge ran for that build: the copies the
bridge made for an earlier build of the same `fluid apply` are put back before
the next one starts and never count as yours. libpq's `PGHOST` and `PGPORT` and
the Snowflake resolver's `SF_ACCOUNT` never pin a destination: dlt's connection
string sets the host and port, which libpq then uses instead of its variables,
and neither dlt nor the bridge reads `SF_ACCOUNT`. libpq's `PGHOSTADDR` is
yours alone: a project file cannot set it (see
[Which variables are protected](#which-variables-are-protected)). dlt never
sets a host address, and libpq connects to the address `PGHOSTADDR` names
whatever host it was given, sending the password meant for that host, so a
project's `PGHOSTADDR` would move every destination that connects through
libpq (`postgres`, `redshift`, `sqlalchemy` with a PostgreSQL URL, `ducklake`
with a PostgreSQL catalog), and dbt-postgres alike. If the project's values
leave out a field the destination requires, the run fails and names it:

```console
dlt destination 'postgres': the project's dotenv files set its endpoint (POSTGRES_HOST), so its credentials come only from those files and the contract, which leave out database, password, username. Add them there, or export the endpoint in your shell or CI environment to use your own dlt credentials.
```

A destination whose endpoint you set yourself, or that no project file names,
reads your dlt configuration as before.

The exact lists, with where each variable is read, are
`ENDPOINT_CREDENTIAL_GROUPS` in
[`fluid_build/credentials/protected_env.py`](../fluid_build/credentials/protected_env.py).

**Services not listed** follow the same rule by name shape: a variable ending
in `_ENDPOINT_URL`, `_API_BASE`, `_BASE_URL`, `_API_URL`, `_HOSTNAME`,
`_ENDPOINT`, `_SERVER`, `_HOST`, `_ADDR`, `_URL` or `_URI` is refused while a
variable with the same prefix ending in `_KEY`, `_TOKEN`, `_PASSWORD`,
`_PASSWD`, `_PWD`, `_PASS`, `_SECRET`, `_PASSPHRASE`, `_CREDENTIALS` or `_PAT`
is set in your environment. For example `ACME_HOST` from `.env` is refused
while you have `ACME_API_KEY` or `ACME_CLIENT_SECRET` exported. The name-shape
rule cannot know about a credential kept outside the environment, which is why
the services above are listed. The endpoints litellm reads for its providers
are listed too: `<PROVIDER>_API_BASE` for every provider litellm knows
(`DEEPSEEK_API_BASE`, `OPENROUTER_API_BASE`, `LM_STUDIO_API_BASE` ...), the
names its registry of OpenAI-compatible providers gives
(`TENSORMESH_SERVERLESS_BASE_URL` ...), and every other host, URL, endpoint
or account id its provider code reads to choose where a request goes
(`DATAROBOT_ENDPOINT`, `GRADIENT_AI_AGENT_ENDPOINT`, `SNOWFLAKE_ACCOUNT_ID`,
`GIGACHAT_AUTH_URL` ...). FLUID sends the key it resolved there, so they pair
only with `FLUID_LLM_API_KEY`, except litellm's proxy and GitHub Copilot
settings, which only you can set (see the table above). The names are in
[`fluid_build/credentials/litellm_endpoints.py`](../fluid_build/credentials/litellm_endpoints.py),
with the few names litellm reads that look like an endpoint but choose no
provider host (`LITELLM_MODEL_COST_MAP_URL`, `REDIS_HOST`,
`CLOUDFLARE_ACCOUNT_ID` ...) and why; any other `<NAME>_API_BASE`, such as
`MYAPP_API_BASE` next to `MYAPP_API_KEY` for a service of the project's own,
follows the name-shape rule.

This is the same binding npm uses for registry tokens (`//host/:_authToken`,
so a project `.npmrc` that changes `registry=` does not receive your token) and
git's credential helpers use (a stored credential is matched to the URL's host).

## Adding a new environment variable

Every `FLUID_*` name in the package must be classified in
`fluid_build/credentials/protected_env.py`: protected
(`PROTECTED_ENV_KEYS`, a prefix, or the name-shape rule), deliberately
loadable (`DOTENV_LOADABLE`), or not read from the CLI's environment
(`NOT_PROCESS_ENV`). `tests/test_protected_env_classification.py` fails on an
unclassified name. If the variable widens access, changes trust, or decides
where data goes or what runs, protect it.

A third-party endpoint FLUID or an SDK it drives reads (a base URL, host,
account or port) goes in the service's `EndpointGroup` with the credentials
sent there, and `ambient=True` when that credential can also come from outside
the environment. An ambient group lists in `paired_by`, for each code path
that reads the endpoint, the credentials that path sends before the ambient
one, and in `switches` any setting that makes a path send the ambient one
regardless. `tests/test_dotenv_endpoint_pairing.py` checks every group in both
directions, and `tests/test_dotenv_pairing_matrix.py` drives the readers.
