# Federation: upstream credentials

`fluid apply` verifies every federated `consumes[]` pin against the upstream
workspace declared in `federation/upstreams.yaml`. When that upstream is
private, the manifest names an environment variable that holds the
credential. This page covers which variables may be named, where each one
may be sent, and how it travels.

## Quick start

A private GitHub repo needs nothing beyond the token:

```yaml
# federation/upstreams.yaml
workspaces:
  - id: telco-billing
    kind: git_registry
    endpoint: https://github.com/acme/telco-billing-mesh
    auth:
      mode: github_token
      secret_ref: GITHUB_TOKEN
```

```bash
export GITHUB_TOKEN=...        # or GH_TOKEN
fluid apply contract.fluid.yaml
```

Any other registry needs its variable allow-listed by whoever runs
`fluid apply`, bound to the host it is for:

```yaml
  - id: marketing-cdp
    kind: catalog
    endpoint: https://catalog.acme.com/api
    auth:
      mode: bearer
      secret_ref: CATALOG_TOKEN
```

```bash
export CATALOG_TOKEN=...
export FLUID_FEDERATION_SECRET_REF_ALLOWLIST=CATALOG_TOKEN@catalog.acme.com
fluid apply contract.fluid.yaml
```

## Which variable may go where

`federation/upstreams.yaml` is a committed file. The policy for sending a
credential is therefore set by the environment that runs `fluid apply`, not
by the manifest.

| `secret_ref` | Sent to | Configure |
|---|---|---|
| `GITHUB_TOKEN`, `GH_TOKEN` | `github.com`, `api.github.com`, `raw.githubusercontent.com` | nothing; allowed by default |
| `NAME` listed as `NAME@host` | exactly `host` | `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` (recommended) |
| `NAME` listed bare | any host | `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` |
| anything else | nowhere | the fetch is refused |

`FLUID_FEDERATION_SECRET_REF_ALLOWLIST` is a comma-separated list. Names are
case-sensitive, like environment variables; hosts are matched exactly
(case-insensitive), with no port and no subdomain wildcard. Prefer the
`NAME@host` form: a bare `NAME` lets any manifest change send that variable
to any host.

Write the host without a port: `CATALOG_TOKEN@catalog.acme.com` covers every
port on that host. An entry with a port, such as
`CATALOG_TOKEN@catalog.acme.com:8443`, never matches, and the run logs
`federation_secret_ref_allowlist_entry_has_port` once per distinct entry. The
warning names `FLUID_FEDERATION_SECRET_REF_ALLOWLIST`, says that an entry has
a port and never matches, and suggests the form `NAME@<host>`, for example
`'NAME@catalog.acme.com'`. Apart from that host, it repeats no part of the
entry, not even the variable's name: an entry copied from a URL can hold a user name, a password
or a token, and the warning cannot tell which part is which. Look in the
variable for the entry that ends in `:` and a port number.

The suggested host is filled in only when Python's `urlparse` reads the text
after the entry's last `@` as a hostname with no `@` in it, and with no `:`
and no brackets around it unless it is an IPv6 address. It is then the host
the endpoint is compared by: lower-cased, without the port or the brackets.
Otherwise the warning prints the literal placeholder `<host>`, for example
for userinfo inside hand-written brackets (`NAME@[user:password@host]:8443`)
or for brackets around a name that is not an IPv6 address.

A user name and a password with only digits, `user:12345`, has the same
shape as `host:port`, so in two cases the suggested host is a user name copied
from a URL (never its password):

- an entry with no host after the userinfo, such as `NAME@user:12345`;
- a password holding a `,`, which separates entries: `NAME@user:12,34@host:8443`
  is read as `NAME@user:12` and `34@host:8443`, and the warning for the first
  suggests `NAME@user`.

Write an IPv6 host without brackets, for example `CATALOG_TOKEN@fd00::5`.

For GitHub Enterprise Server, list the token with its host, for example
`GITHUB_TOKEN@github.acme.internal`.

### Set the policy outside the checkout

`FLUID_FEDERATION_SECRET_REF_ALLOWLIST` and `FLUID_FEDERATION_HOST_ALLOWLIST`
must come from the environment that runs `fluid apply`: the shell, a CI
secret or variable, or a `FLUID_SECRETS_FILE` that lives outside the project.
`fluid apply` also loads the project's `.env`, `.env.<env>` and `.env.local`,
but those files are part of the same checkout as `federation/upstreams.yaml`,
so they may not set the policy that constrains it.

If any `.env` or `.env.*` file in the project directory gives one of these
variables a value, the variable is ignored, whoever set it. The same applies
to the file `FLUID_SECRETS_FILE` names when a project dotenv file sets
`FLUID_SECRETS_FILE`. A refused fetch names the project file whose value was
ignored. Federation also logs `federation_policy_env_ignored` naming the file
when it reads the variable, but only if the variable is also set in your own
environment (the shell, CI, or your `FLUID_SECRETS_FILE`). A value that is
only in a project file never reaches the run's environment, so federation
does not log it: `fluid apply` does not load these variables from project
files, and warns `Ignored 1 setting from project dotenv files` when a file
it loads sets one. The refused fetch is then where federation names the file.
Remove the variable from the project file and set it in the operator
environment instead. A template line with no value, such as
`FLUID_FEDERATION_SECRET_REF_ALLOWLIST=` in `.env.example`, is fine.

## How the credential travels

- **https only.** A credential is attached only to `https://` endpoints. An `http://` or `git://` endpoint with `auth.secret_ref`
  is refused, because the credential would cross the network in cleartext.
  Without `auth.secret_ref`, `http://` and `git://` endpoints keep working
  anonymously. `ssh://` git endpoints authenticate with your SSH keys and are
  never sent the token.
- **git.** The token reaches `git` only through that process's environment,
  as an `http.<endpoint>.extraHeader` entry appended to `GIT_CONFIG_PARAMETERS`
  (the variable `git -c` uses; git 2.31 or later), keyed to the endpoint URL
  itself. It is not part of the clone URL, the command line, or
  `.git/config` in the cache under
  `~/.cache/fluid/federation-git/<workspace-id>`. A rotated token is used on
  the next fetch. Caches cloned by earlier versions are repaired on the next
  fetch: `remote.origin.url` is reset to the manifest's `endpoint`. A cache
  directory that is not a clone (for example an interrupted clone) is removed
  and cloned fresh; git only ever runs on the cache directory itself, never
  on a repository that encloses it. If a refresh cannot fetch, the upstream
  is reported `unreachable` rather than checked against the stale cache.
  An endpoint that already carries its own `user@` part is used as written
  and is not sent the token.
  git sends every `http.extraHeader` that applies to a URL, so federation
  first resets the list for the endpoint (an empty `extraHeader`, which git
  documents as clearing it), then hands back the extra headers your git
  configuration sets for the endpoint, except `Authorization`: the server
  receives one `Authorization` header, federation's, even when your git
  configuration already sets one for that host, and an access gateway's
  header (`Proxy-Authorization`, `cf-access-token`, a client header) still
  goes out. git's own URL matching decides which of your headers apply to
  the endpoint, reading your configuration the way `git clone` does.
  If one of those headers holds a line break (CR or LF) or a NUL, curl sends
  the text after the break as a header of its own, which could be a second
  `Authorization`, so the fetch carries no token: git fetches the endpoint
  anonymously, and the run logs `federation_git_extra_header_line_break`
  naming the setting, never its value.
  With a credential, git does not follow redirects
  (`http.<endpoint>.followRedirects=false`, set the same way as the header).
  git has no same-origin-only setting, and once it follows a redirect it
  sends the rest of the fetch to the new location with the header attached,
  so a redirecting endpoint is reported `unreachable` instead. git applies
  the `http.<url>` entry that matches the URL most specifically, and the
  last one read among equals. Federation's entries are keyed to the endpoint
  URL and read after every config file, `GIT_CONFIG_COUNT` pair and
  inherited `GIT_CONFIG_PARAMETERS` entry, which are kept as they are, so an
  `http.followRedirects = true` of yours, bare or for any part of the
  endpoint URL, does not turn redirects back on. If your configuration
  rewrites the endpoint (`url.<base>.insteadOf`) to another scheme, host,
  port or path, including a path below the endpoint's, git fetches the
  rewritten URL without the token. A rewrite that only adds a user name or
  other userinfo (the `https://ci-token:...@host/` form set for Go modules or
  submodules) keeps the endpoint, so the token is still sent. The settings are
  then keyed to that `user@` URL as well, so an `http.followRedirects = true`
  of yours for the `user@` URL does not turn redirects back on either.
  Anonymous git fetches keep git's default redirect handling.
- **HTTP and catalog registries.** Redirects are followed for at most 3 hops.
  A hop to a different origin (scheme, host or port) drops `Authorization`,
  `Proxy-Authorization` and `Cookie` for the rest of the chain. A redirect
  from `https://` to `http://` is refused, with or without a credential.
- **Logs.** A resolved credential is registered with the global log
  redactor, and refusal messages name the variable, never its value.

## When a fetch is refused

A refusal happens before anything is sent. `fluid apply` reports it as an
`unreachable` finding whose reason states the cause and the fix, and the
pinned digest is **not** verified:

```text
federation workspace 'marketing-cdp': auth.secret_ref 'CATALOG_TOKEN' may not be
sent to host 'catalog.acme.com'. GITHUB_TOKEN and GH_TOKEN are sent only to
api.github.com, github.com, raw.githubusercontent.com; any other variable or host
must be listed in FLUID_FEDERATION_SECRET_REF_ALLOWLIST (recommended form:
CATALOG_TOKEN@catalog.acme.com). The credential was not sent.
```

```text
federation workspace 'legacy': endpoint scheme 'http' would send the
'CATALOG_TOKEN' credential in cleartext. Use an https:// endpoint (ssh:// for git),
or remove auth.secret_ref to fetch anonymously. The credential was not sent.
```

The policy applies whenever `auth.mode` attaches a credential and
`auth.secret_ref` is set, even if the variable is unset on the machine
running the check, so a manifest fails the same way locally and in CI.
Library callers get the typed `FederationCredentialPolicyError`
(a `ValueError`) from `fluid_build.forge.federation`.

## Upgrading

- A manifest whose `secret_ref` is not `GITHUB_TOKEN`/`GH_TOKEN` toward
  GitHub now needs a `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` entry.
- An `http://` or `git://` endpoint with `auth.secret_ref` must move to
  `https://` (or drop the credential).
- Git authentication needs git 2.31 or later. An older git cannot parse the
  settings federation hands it, so an authenticated clone or refresh fails
  and the upstream reports `unreachable`. This includes a cache cloned by an
  earlier version: once its token is removed from `remote.origin.url`, its
  refresh fails the same way.
- A git refresh that cannot fetch now reports `unreachable`. It used to
  compare the pin against the contract already in the cache.
- A policy variable set in a project `.env` / `.env.*` file is now ignored.
  Move it to the shell, CI environment or an out-of-project
  `FLUID_SECRETS_FILE`.
- A redirect chain that drops back to `http://` now fails. Point the
  endpoint at the final `https://` location.
- A git endpoint with a credential that redirects (for example a renamed
  GitHub repository) now reports `unreachable`. Point the endpoint at the
  repository's current URL.
- An authenticated git fetch sends no `http.extraHeader` from your git
  configuration that sets `Authorization` for the endpoint: federation's
  header replaces it. Your other extra headers for the endpoint are still
  sent. One that holds a line break or a NUL makes the fetch anonymous.
- Your `url.<base>.insteadOf` rules now apply to an authenticated git fetch,
  whose URL no longer carries the token. One that rewrites the endpoint to
  another scheme, host, port or path makes the fetch anonymous.
