# Contract SQL runs in a DuckDB sandbox

Every DuckDB connection the engine opens is confined to the files a contract
declares and its own directory. SQL in a contract cannot read the rest of the
host, fetch a URL, attach another database, or write anywhere else.

```yaml
# contract.fluid.yaml
builds:
  - id: summarise
    pattern: embedded-logic
    engine: sql
    properties:
      sql: SELECT * FROM read_csv('/etc/passwd')
```

```console
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
🔷 Build 'summarise' (embedded-SQL / local DuckDB)
   ❌ Failed: 1 action(s) failed
      Permission Error: Cannot access file "/etc/passwd" - file system operations are
      disabled by configuration DuckDB refused it: contract SQL may only read and write
      the locations the contract declares and its own directory (/work/orders,
      /work/orders/runtime, /tmp/fluid_h44kgt2z, /work/orders/out/summary.csv). Declare
      the file as an input, or move it under the contract's directory.
```

The same SQL reading the contract's own data builds as before:

```yaml
    properties:
      sql: SELECT id, amount * 2 AS doubled FROM read_csv('data/orders.csv')
```

## What a build's SQL can reach

| Where the SQL runs | It can read and write |
|---|---|
| Embedded-SQL build on the local DuckDB engine (`builds[].properties.sql`) | the contract's directory, the FLUID workspace it sits in (`fluid.workspace.yaml`), `./runtime`, the run's scratch directory, each declared `parameters.inputs[].path`, each resolved `consumes[]` upstream, the expose's landing path, and the `s3://` prefixes those name. A declared local path counts only [inside the allowed directories](#declared-locations-stay-inside-the-allowed-directories) |
| DuckDB acquisition build (`pattern: acquisition`, `engine: duckdb`) | the contract's directory, the declared `source.connection.uri` (or stream paths), and each stream's landing file, each inside the allowed directories. A `mysql` source is attached before the sandbox closes; so is a `sqlite` source, and its file must also be inside the allowed directories |
| `fluid validate` quality rules, `fluid verify`, `fluid diff` | the one file being checked |
| `fluid contract-tests` local actions | each declared input file and each output file |
| Discovery (`fluid forge data-model from-source`, `discover`) | the one file or URL being introspected; a JDBC source is attached first |
| MCP output port (DuckDB driver) | the bound file |

Everything else is refused, including:

- an absolute path outside the list: `read_csv('/etc/passwd')`, `read_text(...)`,
  `read_blob(...)`, `read_parquet(...)`, `read_json(...)`, `glob('/etc/*')`;
- a path that climbs out: `../`, `<dir>/./../`, or a symlink pointing outside;
- `~/...` unless the matching file under `$HOME` is itself in the list;
- a URL (`http://`, `https://`, `s3://` ...) the contract does not declare;
- `ATTACH` of another database file, `COPY ... TO` / `COPY ... FROM` elsewhere;
- `INSTALL` / `LOAD` of an extension, and any `SET` (the configuration is locked,
  so `SET enable_external_access = true` is refused too);
- a function from an extension the engine does not load for that build:
  `sqlite_scan`, `read_xlsx`, `ST_Read`, `delta_scan`, `iceberg_scan`. DuckDB
  used to load these on first use; with autoloading off they are not in the
  catalog, even for a file inside the contract's directory. Read the data as
  CSV, Parquet or JSON, or land it with an acquisition build first.

## Declared locations stay inside the allowed directories

Each declared input and output is granted to the build's SQL, and whoever
writes the contract writes the declarations. So a declaration grants a local
path only inside these directories:

- the contract's directory and the FLUID workspace it sits in;
- `./runtime` and the run's scratch directory (`./runtime` only when it is a
  real directory, or a symlink into the contract's directory or workspace; see
  below);
- the upstream roots in `FLUID_UPSTREAM_CONTRACTS`;
- the directories the operator lists in `FLUID_DUCKDB_ALLOWED_DIRS`.

Anything else is refused before any SQL runs. Declaring an innocuous glob in
`$HOME` does not make `~/.aws/credentials` readable:

```yaml
    properties:
      sql: SELECT content FROM read_text('~/.aws/credentials')
      parameters:
        inputs:
          - name: d
            path: /home/me/*.csv
```

```console
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
🔷 Build 'summarise' (embedded-SQL / local DuckDB)
   ❌ Failed: 1 action(s) failed
      The contract declares '/home/me/*.csv' (/home/me), outside the directories it
      may read and write (/work/orders, /work/orders/runtime, /tmp/fluid_yc3ryr57).
      The operator can allow a directory with FLUID_DUCKDB_ALLOWED_DIRS.
```

Both sides are compared after resolving symlinks, as DuckDB resolves them: a
symlink inside the contract's directory that points at `/` grants nothing. A
relative declared path is resolved where DuckDB opens it, the working
directory, and is confined the same way, so `path: ./*.py` cannot grant a
server's working directory.

`./runtime` is granted by convention, not by a declaration, and it sits in the
working directory, which is usually the contract's own. A repository that
ships `runtime` as a symlink out of itself (`runtime -> ../../..`, which is
`$HOME` for a clone at `~/src/repo/product`) does not get that directory
granted: the symlink is ignored with a `local_runtime_not_granted` warning, and
SQL that reads or writes under it is refused.

A SQLite source (`source.kind: sqlite`) is confined the same way, and refused
with the same `FLUID_DUCKDB_ALLOWED_DIRS` message outside the allowed
directories. This check is the only one on it: the sqlite scanner opens the
file through its own library, which DuckDB's allowlist does not bound.

What a declaration grants, once allowed:

| Declared | Granted |
|---|---|
| a file (`/shared/reference/rates.csv`) | that file |
| a glob (`data/*.csv`, `landing/**/*.parquet`) | the directory above its first wildcard (`data/`, `landing/`), which must itself be inside the allowed directories |
| a directory (`data/`) | everything under it |
| an `s3://` URL | its prefix (see the limits below) |

A glob grants its directory, not the files it matches when the run starts,
because DuckDB expands it again when the SQL runs: that expansion includes
dotfiles (macOS `._orders.csv` on an exFAT or SMB volume) and files that landed
after the run started, and DuckDB refuses the whole read if any one of them is
not granted. The directory is no wider than the declaration could already be:
everything inside the allowed directories is declarable. DuckDB checks each
expanded file after resolving symlinks, so a matched symlink that points
outside the directory is refused.

### Reading a file outside the contract's directory

The operator allows its directory; the contract then declares the file:

```console
$ export FLUID_DUCKDB_ALLOWED_DIRS=/shared/reference   # ':'-separated, absolute
$ fluid apply contract.fluid.yaml --mode amend-and-build --yes
```

```yaml
    properties:
      sql: SELECT * FROM rates
      parameters:
        inputs:
          - name: rates
            path: /shared/reference/rates.csv
```

The declared file becomes readable. Its neighbours in `/shared/reference` do
not, unless the contract declares them too (or declares a glob or the
directory, which grant `/shared/reference` itself, as the table above says). `FLUID_DUCKDB_ALLOWED_DIRS`
is read from the environment of the process that runs the engine; no contract
field can set it. A relative entry, or `/`, is refused.

A product inside a FLUID workspace can also read its sibling products' files by
path, and a `consumes[]` entry resolves to the upstream's landed file.

## How it works

`fluid_build/providers/_duckdb_sandbox.py` is the one place the engine calls
`duckdb.connect`. A test (`tests/providers/test_duckdb_sandbox.py`) fails if
any other module opens or queries DuckDB without it. For each connection it:

1. connects (a file-backed database is opened here and needs no grant);
2. turns off persistent secrets and community extensions, loads the extensions
   the call site needs, and runs its set-up (an `ATTACH` of a declared source,
   an object-store secret);
3. turns off extension autoinstall and autoload;
4. pins `home_directory` to `$HOME`, then sets `allowed_directories` and
   `allowed_paths` to the list above;
5. sets `enable_external_access = false`;
6. sets `lock_configuration = true`.

These are DuckDB's own settings, in the order the
[Securing DuckDB](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview.html)
guide gives.

## Requirements and limits

- **DuckDB 1.5.0 or newer.** `allowed_directories` arrived in 1.2, but up to
  1.4.3 `<dir>/./../` escaped it, and through 1.4.x a symlink inside an allowed
  directory reached its target and relative paths were refused. The `local`
  extra now requires `duckdb>=1.5.0`, and the engine refuses to open DuckDB on
  anything older.
- **`~` follows `$HOME`.** DuckDB expands `~` with `$HOME` when it checks a
  path and with its `home_directory` setting when it opens one
  ([duckdb/duckdb#26064](https://github.com/duckdb/duckdb/issues/26064)). The
  sandbox sets the two to the same directory and locks them.
- **A remote prefix bounds the bucket, not the path.** DuckDB 1.5 does not
  resolve `..` inside a URL, so a declared `s3://bucket/landing/` also lets
  the SQL reach other keys in that bucket with the same credentials.
- **A loaded database scanner is not bounded by the allowlist.** The `sqlite`,
  `postgres` and `mysql` extensions open files and sockets through their own
  client libraries, not through DuckDB's file system, so neither
  `allowed_directories` nor `enable_external_access` limits them. On a
  connection that loads `sqlite` (an acquisition build with a SQLite source),
  `sqlite_scan` and `ATTACH ... (TYPE sqlite)` can open any SQLite file the
  process can read; the declared source file itself is confined before it is
  attached (see above). On one that loads `postgres`, `postgres_scan` can connect
  to any host the process can reach, the Command Center's own database
  included. The engine loads them only for an acquisition build's declared
  source, discovery and the copilot's sample-rows tool, whose SQL the engine
  builds from validated identifiers; contract SQL never runs on such a
  connection (`test_contract_sql_runs_on_a_connection_with_no_database_scanner`).
- **One open connection per database file.** DuckDB shares one instance per
  database file within a process, and the sandbox locks that instance. A second
  connection to a file that is already open is refused (`DuckDB database ...
  is already open in this process`). Each call site closes its connection,
  failures included. Two MCP DuckDB drivers bound to the same `.duckdb` file
  in one process, or two concurrent `persist=True` local runs, hit this.
- **Persistent DuckDB secrets are off.** Secrets saved in
  `~/.duckdb/stored_secrets` are no longer loaded; object-store builds use the
  credential-chain secret the engine creates.
- **Defense in depth, not isolation.** DuckDB describes these settings as "not
  a substitute for proper sandboxing". A service that runs other people's
  contracts (the Command Center, a shared CI runner) should still run each one
  in its own container.
