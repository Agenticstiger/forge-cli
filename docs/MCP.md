# MCP Server — `fluid mcp serve`

forge-cli ships an [MCP](https://modelcontextprotocol.io/) stdio
server that exposes the staged forge pipeline as tools for any
MCP-compatible client (Claude Code, Cursor, Continue, etc.). The
server speaks the standard MCP wire format over stdin/stdout — no
HTTP, no auth dance, no extra ports.

## What's exposed

| Tool | Read/Write | What it does |
|---|---|---|
| `read_logical_model` | read | Load a `<name>.fluid.yaml.model.json` sidecar and return the typed Logical IR (DV2 or Dimensional). |
| `validate_contract` | read | Run the FluidContractValidator against a contract path and return the structured `ValidationReport`. |
| `diff_models` | read | Structural diff between two `.model.json` sidecars (added/removed/renamed entities). |
| `search_semantic_memory` | read | Vector-search across `memory/semantic` for similar prior forged models. |
| `update_entity` | **write** | Patch a single entity (hub / link / satellite / fact / dimension) in a logical sidecar. |
| `add_relationship` | **write** | Add a foreign-key relationship between two entities. |
| `regenerate_physical` | **write** | Re-run the physical fan-out (Builder ∥ Readme ∥ Transformation) against a logical draft. |

Path-based read tools are scoped to the server's readable roots. Write
tools are gated by the policy model below.

## Access control

Every `tools/call` is checked against:

1. **`--read-only` flag.** When set, any tool that needs to mutate
   the contract / sidecar / store is rejected with a typed error.
   Default: write tools are visible, but the user has to opt in.
2. **Readable-paths whitelist.** `--readable-paths` accepts a
   comma-separated list of paths that path-based read tools may
   inspect. Default: current working directory.
3. **Writable-paths whitelist.** `--writable-paths` accepts a
   comma-separated list of paths the MCP server is allowed to
   mutate. Any write tool whose target falls outside the list is
   rejected. Defends against an upstream LLM that asks the server
   to overwrite `~/.fluid/ai_config.json` or similar.
4. **Tool-name allow/deny list.** `--allow-tools` / `--deny-tools`
   accept comma-separated tool names. `--deny-tools` wins over
   `--allow-tools` when both name the same tool.
5. **Inline catalog credentials.** The source-catalog tools look up
   secrets by `credentials.credential_id` (a name configured with
   `fluid ai setup --source <catalog> --name <name>`). A raw
   `credentials.inline` block is refused unless the server was started
   with `--allow-inline-credentials`. Turn that on only for a trusted
   in-process harness. That flag does not let an inline block reach
   anything this host holds: its cloud identity (item 6), its files or its
   stored logins. With it alone, a block is accepted only when it carries
   the caller's own credential as values and nothing on this host stands
   in for it:

   | Catalog | Accepted with `--allow-inline-credentials` alone |
   |---|---|
   | Glue | `auth_method: iam_key` with `access_key_id` and `secret_access_key`, and no `profile_name` |
   | BigQuery, Dataplex | Nothing. Their credential models take a key only as a file path, which names this host's file. Use a saved `credential_id`. |
   | Unity | `auth_method: pat` with `token`, or `oauth_m2m` with `oauth_client_id` and `oauth_client_secret`, on a host with no netrc file |
   | Snowflake | `auth_method: password` with `password`, or `oauth` with `oauth_token` |
   | DataHub | `auth_method: pat` with `token` and an `http://` or `https://` `server`, on a host with no netrc file and no `DATAHUB_CLIENT_CERT_PATH` |
   | Data Mesh Manager | `api_key` |

   No model accepts key content inline (a service-account JSON document
   or a PEM private key as text). Where a catalog needs a key file, the
   operator saves the source with `fluid ai setup --source <catalog>
   --name <name>` and the client passes `credentials.credential_id`, or
   the operator starts the server with `--allow-metadata-service`.

   Any other block authenticates as this host and is refused unless the
   server also runs with `--allow-metadata-service`. The refusal names
   the reason and, where one exists, the block to send instead. A block
   is the host's when it has:

   - **A path.** Any path field (`service_account_path`,
     `private_key_path`, `google_service_account_path`), whatever the
     `auth_method` and however the path is written: absolute, relative,
     with `~` or `$VAR`, or through a symlink. The SDK reads that file
     from this host's disk, so the identity in it (gcloud ADC, a mounted
     service-account key, a Snowflake private key) is the host's, not
     the caller's. The block is refused before any adapter is built, so
     the file is never opened.
   - **An `auth_method` that takes the host's identity.** Glue
     `instance_profile`, `iam_role` (the default) and `sso`, and any Glue
     block that sets `profile_name`; BigQuery and Dataplex `adc` (the
     default) and `oidc`; Unity `azure_ad` (this host's `az login`) and
     `google_id` (this host's Google ADC); Snowflake `sso` (a browser
     login on this host); DataHub `none`, and DataHub `oauth`, whose
     client id and secret the adapter does not send, so the DataHub SDK
     falls back to this host's `DATAHUB_SYSTEM_CLIENT_ID` and
     `DATAHUB_SYSTEM_CLIENT_SECRET`.
   - **Missing or blank material** for its auth method (below).
   - **This host's configuration in its place.** The DataHub and
     Databricks SDKs reach the `server` / `host` the block names with
     `requests`, which reads logins from this host's netrc file
     (`$NETRC`, `~/.netrc` or `~/_netrc`). For DataHub it attaches one
     to that endpoint and to any host it redirects to. For Unity it
     depends on the request and on the databricks-sdk version (the
     `databricks` extra accepts 0.20 and later). The SDK sends its API calls, and an
     `oauth_m2m` block's token request, with authentication of its own,
     so `requests` attaches a netrc login to those only when one is
     redirected, for the host it is redirected to. An `oauth_m2m` block's
     endpoint lookup, though, can go to the named host with no
     authentication of its own, and then it carries the named host's
     netrc login: in databricks-sdk 0.20 to 0.35 for a workspace host,
     and in 0.36 to 0.86 when the SDK treats the host as Azure and finds
     an Azure client id in this host's settings (`ARM_CLIENT_ID`, or
     `azure_client_id` in the `~/.databrickscfg` profile that
     `DATABRICKS_CONFIG_PROFILE` names). So on a host with a netrc file,
     DataHub and Unity blocks are refused, whatever the SDK version. A
     DataHub `server` that is not an http(s)
     URL is refused (`__from_env__` makes the SDK use this host's
     `DATAHUB_GMS_*` settings), and so is every DataHub block while
     `DATAHUB_CLIENT_CERT_PATH` is set, because the SDK presents that
     client certificate. Data Mesh Manager's client (`httpx`) reads no
     netrc file unless asked to, so it is unaffected.
   - **A credential type the server does not classify.**

   A saved source is not checked. `credentials.credential_id` names a
   source the operator configured, so it may point at a key file (for
   example a `service_account_path` under `/var/run/secrets`) and
   resolves as before.

   An accepted block also stays the caller's at the SDK. Some catalog
   SDKs read settings from the server's environment and config files
   that choose the credential, so with `--allow-inline-credentials`
   alone the server tells them to use the block's material only:

   - **Unity `pat`.** The server passes `auth_type="pat"` to the
     Databricks SDK. Otherwise `DATABRICKS_AUTH_TYPE` in the server's
     environment, or `auth_type` in the `~/.databrickscfg` profile that
     `DATABRICKS_CONFIG_PROFILE` names (`DATABRICKS_CONFIG_FILE` moves
     the file), replaces the block's token: `oauth-m2m` posts the
     server's own `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`
     to the token endpoint of the workspace the block names, and `basic`
     sends `DATABRICKS_USERNAME` and `DATABRICKS_PASSWORD` to it. A Unity
     `oauth_m2m` block already names its auth type.
   - **Snowflake.** The server passes
     `platform_detection_timeout_seconds=0`, so the connector's login does
     not probe this host's cloud identity (an AWS STS `GetCallerIdentity`
     signed with this host's AWS credentials, the Azure and GCP metadata
     endpoints) and report the result to the account the block names.

   The other SDKs already take the block's material as the whole
   credential. boto3, given keys, takes no credential from the
   environment, an `AWS_PROFILE` profile, a `credential_process`, or the
   container or instance endpoint.
   The DataHub SDK sends the block's token; its `DATAHUB_*` logins apply
   only to blocks refused above. The Data Mesh Manager client sends the
   block's key. BigQuery and Dataplex accept no inline block. A saved
   source, and any block with `--allow-metadata-service`, reads this
   host's settings as before.

   A blank or whitespace-only value is not credential material. An SDK
   reads an empty key as "none supplied" and falls back to its default
   chain, which on a cloud host ends at the instance or container role,
   so the credential models read a blank key id, secret, password,
   token, credential-file path or OAuth client id as absent (a blank
   Data Mesh Manager `api_key`, which is required, fails validation). A
   block whose material is blank is therefore refused with the same
   error. With `--allow-metadata-service` it is still refused, by the
   auth method's own "requires ..." check: the opt-in lets a block ask
   for the host's identity (Glue `instance_profile`, BigQuery `adc`), it
   does not turn blank keys into the host's identity. A blank session
   token beside real keys just means no session token. The same holds
   for a saved source (`credentials.credential_id`, or
   `fluid forge data-model from-source --credential-id`): a blank key in
   the keyring or `sources.yaml` fails that check instead of falling
   back to the host.

   A named AWS profile counts as the host's identity, not the
   caller's, even beside access keys. It is read from this host's
   `~/.aws` files, and a profile with no keys of its own
   (`[default] region = us-east-1` is what `aws configure set region`
   writes) still ends boto3's chain at the container or instance role.
6. **Metadata-service credential fallback.** The source-catalog tools
   (`list_source_tables`, `inspect_source_table`, `list_source_lineage`,
   `list_source_glossary`, and `forge_from_source` with a catalog
   `source`) take an `allow_metadata_service` argument that requests the
   resolver's fallback to this host's ambient cloud identity (instance
   role, workload identity, gcloud ADC); a JDBC `source` ignores it
   (item 7). That identity usually carries broad scopes, so using it is
   the operator's decision, not the client's:

   | Server started with | Client sends `allow_metadata_service: true` | Client omits it / sends `false` |
   |---|---|---|
   | neither opt-in (default) | tool error naming `--allow-metadata-service` | fallback off |
   | `--allow-metadata-service` | fallback on for that call | fallback off |
   | `FLUID_ALLOW_METADATA_SERVICE=1` in the server's environment | fallback on | fallback on (the env var enables it process-wide, as for the CLI) |

   "Fallback off" holds at the SDK too. Without the opt-in, a region or
   project variable alone (`AWS_REGION`, `GOOGLE_CLOUD_PROJECT`) is not
   treated as a credential, so when nothing else resolves the call fails
   with "No credentials resolved" rather than using the host identity.
   Credentials the operator put in the server's environment
   (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`,
   `GOOGLE_APPLICATION_CREDENTIALS`) still resolve.

   The same opt-in covers inline credentials that would authenticate as
   the host (item 5): through their `auth_method`, a named AWS profile, a
   path to a file on this host, this host's netrc or DataHub settings, or
   material that is missing or blank. So without it an inline block never
   draws credentials from the metadata service, this host's files or its
   stored logins either. With it, such a block behaves as it did before
   these checks: its path is read, a netrc login is attached, and the
   SDK reads this host's settings (a `DATABRICKS_AUTH_TYPE` over a Unity
   token, the Snowflake login's cloud-identity probe). It still
   reaches the host's identity only by asking for it, never through blank
   keys. The client's `allow_metadata_service` argument never stands in
   for the operator there.

   The refusal is an error, not a silent downgrade, so an agent can tell
   the user why the call failed. Prefer a saved `credential_id`; enable the
   fallback only on a host whose ambient identity is scoped to what the
   MCP client should be able to read.

7. **JDBC sources (`forge_from_source`).** A `forge_from_source` call
   whose `source` is `postgres`, `postgresql`, `mysql` or `sqlite`
   reaches a database — and this host's database credentials, files and
   identity — directly from the `uri` the client sends, with no
   credential resolver in between. It is classified like an inline
   credential block (item 5) before any connection is attempted:

   | URI the client sends | Would reach | Needs |
   |---|---|---|
   | `postgres` / `mysql` with a password | the caller's own credential (inline material) | `--allow-inline-credentials` |
   | `postgres` / `mysql` with no password | this host's own DB credentials (`PGPASSWORD`, `~/.pgpass`, a libpq service, `MYSQL_PWD`), sent to the host the client named | `--allow-metadata-service` |
   | `sqlite:///…` | a file on this host's disk the client chose | `--allow-metadata-service` |
   | `postgres` / `mysql` with no user name | this host's login name (`PGUSER` or the OS user) | `--allow-metadata-service` (plus `--allow-inline-credentials` for a password) |
   | `mysql://…@localhost` | this host's unix socket, where socket authentication identifies the server's OS user | `--allow-metadata-service` (plus `--allow-inline-credentials` for a password); use `127.0.0.1` for TCP |
   | `postgres` host that is empty, blank or a socket path: no host (`@:5432/db`), an empty element of a host list (`db,`, `,db`, `a,,b`, `a:5432,,b:5433`, `[::1],`, also with the comma percent-encoded: `db%2C`, `%2Cdb`), a percent-encoded path (`%2Fvar%2Frun%2Fpostgresql`), an abstract socket (`%40name`), or a `host=` / `hostaddr=` query parameter that is empty or a path | this host's unix socket, where peer or trust authentication identifies the server's OS user | `--allow-metadata-service` (plus `--allow-inline-credentials` for a password) |

   The URI must be a string whose scheme matches `source`:
   `postgres://` or `postgresql://`, `mysql://`, `sqlite://`. An empty
   password (`user:@host`) counts as no password. A postgres host is the
   one the connection uses. The attach reads the URI with Python's
   urllib, whose user name and password run to the *last* `@` before the
   host, and builds the connection from those fields, so the URI itself
   never reaches libpq: `app_user:Summer2024@@db.example` connects to
   `db.example` with the password `Summer2024@` and needs only
   `--allow-inline-credentials`. That host part is also read the way
   libpq would read it (a host list of `host[:port]` items, IPv6 in
   brackets, percent-decoded and then split on commas, query parameters
   overriding the host part), and a socket under either reading needs
   `--allow-metadata-service`. A list of named hosts — `a:5432,b:5433`,
   `[::1],[::2]` — needs only `--allow-inline-credentials` (the attach
   itself cannot read per-host ports or several bracketed hosts, so such
   a URI then fails before any connection is made). A URI urllib cannot
   read never connects; it is classified on libpq's own reading, where
   the password ends at the *first* `@`. A mysql URI with no usable host
   and port (no host, a port that is not a number from 0 to 65535, or a
   netloc urllib cannot read) is refused whatever the server's flags,
   since none would make it connect; for an empty host the refusal adds
   that a mysql client reads it as `localhost`, the row above. Query
   parameters are not passed to the connection.

   Refused otherwise with a typed error naming the exact flag, before
   the database is contacted or the file opened. A JDBC call connects
   with `uri` alone and never reads `credentials.credential_id`, so the
   way past a refusal is the CLI (`fluid forge data-model from-source`)
   or the flag the refusal names. Unlike item 6, the client's own
   `allow_metadata_service` argument plays no part here: a JDBC source
   ignores it, and only the operator's `--allow-metadata-service` or
   `FLUID_ALLOW_METADATA_SERVICE=1` in the server's environment opens
   the rows above that need it.

   An allowed inline connection (a password in a URI the MCP client sent)
   is pinned to that password: it is set explicitly and, for libpq,
   `passfile` is redirected to `/dev/null` and `sslcert` / `sslkey` name
   a path that cannot exist, so this host's default client certificate
   (`~/.postgresql/postgresql.crt`) is never presented; this host's
   ambient DB credential environment (`PGPASSWORD`, `PGPASSFILE`,
   `PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGSSLCERT`, `PGSSLKEY`,
   `PGOAUTHTOKEN`, `PGGSSDELEGATION`, `MYSQL_PWD`, …) is suppressed for
   the duration of the connection. So neither libpq nor the duckdb mysql
   client falls back to a password in this host's environment, libpq
   does not fall back to a service definition, a `~/.pgpass`, a client
   certificate or an OAuth bearer token that belongs to this host rather
   than to the caller, nor forward this host's Kerberos credentials, and
   a client cannot borrow this host's TLS identity for a server it chose.
   A server that requires a client certificate (`clientcert=verify-ca`)
   therefore refuses such a connection. No tool argument turns the pin
   off.

   The pin applies only to connections whose URI an MCP client (or a
   model's tool call) supplied. A URI the operator typed on the command
   line — `fluid init --discover <uri>` and
   `fluid forge data-model from-source --uri <uri>` — authenticates as it
   always did, with this host's client certificate and libpq settings.
   So does the copilot's opt-in `fetch_sample_rows` tool: its URI comes
   from the operator's own `FLUID_FORGE_DB_URI` / `FLUID_FORGE_DB_URI_<NAME>`
   environment, and the model only names a table and a connection alias.
   A JDBC discoverer added with `register_discoverer` keeps the
   one-argument `discover(uri)` interface and runs the same way under
   `fluid init --discover`, also when it calls its own `_introspect` in
   threads it starts: the command counts that discoverer instance as the
   operator's for the length of the call. A discoverer that hands the URI
   to a different discoverer instance in a thread it starts should make
   that call with `contextvars.copy_context().run`, or that thread gets
   the pin. Called from anywhere else, a `JdbcDiscoverer` gets the whole
   pin, the environment window included.

   Every field interpolated into the duckdb `ATTACH '<dsn>' …` statement
   is quoted at both layers — libpq conninfo quoting on each postgres
   value, each mysql value in double quotes with `\` and `"`
   backslash-escaped (DuckDB's mysql DSN syntax), and a single SQL string literal around
   the whole DSN — and control characters are rejected, so a crafted URI
   cannot inject a second keyword (`host=`, `passfile=`, `sslcert=`,
   `socket=`, `ssl_key=`) or break out of the SQL literal into DuckDB
   SQL (`read_csv`, `COPY … TO`). A mysql value containing `?` is
   refused, because DuckDB's mysql extension reads everything after a `?`
   as URI options whatever the quoting. A connect error that quotes the
   DSN has the password masked, and so does the error for a URI that
   cannot be used (no host, a bad port, a missing `//`, or a reserved
   character such as `/`, `?`, `#` or `[` typed in the password), which
   the server prints to its console and `fetch_sample_rows` returns to
   the model. That error masks the password where it sits in the URI, so
   a user name, database or scheme spelled like the password stays
   readable. The same quoting covers the operator's own
   `fluid init --discover` and `fluid forge data-model from-source`
   paths. The duckdb build runner's `ATTACH` of a contract's mysql or
   mariadb source uses the same mysql quoting, `?` refusal and
   control-character refusal, and masks the password in its connect
   error; its `postgres_scan` DSN for a postgres source uses the same
   libpq quoting and control-character refusal.

   The operator's own CLI does not pass through this gate: it runs the
   operator's input, not an MCP client's, so a URI with no password
   continues to use this host's credentials as before, and a URI with a
   password is not pinned.

The policy is built once at startup and applied to every
`tools/call`. The 41-test suite at
`tests/test_mcp_permission_policy.py` pins every branch of the
matrix; `tests/test_mcp_jdbc_source_gate.py` pins the JDBC gate above
and `tests/cli/discover/test_jdbc_attach_safety.py` the ATTACH-string
quoting and credential pinning.

## Sample Claude Code config

Drop this into Claude Code's MCP settings (typically
`~/.config/claude-code/mcp_servers.json` or the IDE's MCP panel):

```json
{
  "mcpServers": {
    "fluid-forge": {
      "command": "fluid",
      "args": [
        "mcp",
        "serve",
        "--read-only",
        "--readable-paths",
        "${WORKSPACE}/forge-output",
        "--writable-paths",
        "${WORKSPACE}/forge-output"
      ],
      "env": {
        "FLUID_LLM_PROVIDER": "anthropic",
        "FLUID_QUIET": "1"
      }
    }
  }
}
```

* `--read-only` blocks every write tool by default; remove the flag
  if you want Claude Code to actually patch your contracts.
* `--readable-paths` scopes model/contract reads to a single
  directory under your workspace.
* `--writable-paths` scopes any allowed writes to a single
  directory under your workspace — defence-in-depth even when the
  read-only flag is dropped.
* `FLUID_QUIET=1` silences the v2-preview banner so it doesn't
  pollute the MCP wire format.

## Sample Cursor config

```json
{
  "mcp.servers": {
    "fluid-forge": {
      "command": "fluid",
      "args": ["mcp", "serve"],
      "env": { "FLUID_LLM_PROVIDER": "openai", "FLUID_QUIET": "1" }
    }
  }
}
```

Cursor reads the same config shape; the only difference is the
top-level key (`mcp.servers` vs `mcpServers`).

## Client certification

Before cutting a release, run the real-client certification script:

```bash
PYTHONPATH=. python scripts/mcp_client_certify.py
```

It performs three checks:

1. Direct JSON-RPC lifecycle smoke (`initialize`, `tools/list`,
   `tools/call`).
2. MCP Inspector CLI `tools/list` and `tools/call` against the stdio
   server, when `npx` is installed.
3. Claude Code project-config health check via `claude mcp get`, when
   the `claude` CLI is installed.

Use `--json` for machine-readable CI output. Optional clients are
reported as `skip`, not `fail`; protocol failures from installed
clients fail the script.

## Versioning + audit trail

Every write tool that mutates an artefact bumps the artefact's
`meta.version` and stores the previous version under
`~/.fluid/store/history/<contract_hash>/<n>.json`. This is the
file-based audit trail the plan promises — no DB, no separate
service, just lazy versioned snapshots. List with:

```bash
fluid memory show history --contract customer_orders
```

The retention policy is `keep last N` (default `N=10`); override
via `FLUID_HISTORY_KEEP=N` or per-call.

## Tool schemas

Every tool's input schema is the corresponding Pydantic model from
`fluid_build/copilot/schemas/`. So `update_entity`'s schema is
literally `EntityPatch.model_json_schema()` — the MCP client gets
the same validation guarantees as the staged pipeline. A future
v2.x bump will rename the Pydantic models with backward-compat
shims so existing MCP clients keep working.

## Wire-format quirks

* **stdio only.** No HTTP transport in v1.0. SSE/HTTP comes in
  v1.6+ when we have a clear use case (today's MCP clients all
  speak stdio).
* **Logging goes to stderr.** stdout is reserved for MCP frames; a
  stray `print()` in any imported module would corrupt the wire.
  All logging in `cli/mcp.py` and the tool-call paths uses
  `logging.getLogger(...)` configured at warning level by default.
* **Tools are idempotent.** Calling `update_entity` twice with the
  same patch produces the same final state. The repair-loop logic
  inside the staged pipeline already handles this — we don't add a
  second idempotency layer at the MCP boundary.

## Disabling the server

Don't run `fluid mcp serve` and the server doesn't exist. There's
no daemon, no socket, no startup hook. The server is a foreground
process that exits when stdin closes — kill the parent process
(your IDE) and the server vanishes with it.
