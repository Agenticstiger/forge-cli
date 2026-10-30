# Changelog

All notable changes to FLUID Forge CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Databricks (Unity Catalog) applies through the OpenTofu engine.** A contract bound to
  `platform: databricks` validates, plans, generates (`fluid generate iac`) and applies like the
  other clouds. The `databricks/databricks` provider is pinned to `~> 1.140.0`, and `tofu init`
  checks it against a committed lock. `location.database` names an existing catalog, read at plan
  time and never created; `location.schema` and `location.table` are the schema and the managed
  Delta table the product owns. `accessPolicy.grants` and an expose's `policy.authz.readers` /
  `writers` become one grant per principal, plus `USE_SCHEMA` on the schema. The workspace and
  the SQL warehouse come from `DATABRICKS_HOST` and `DATABRICKS_WAREHOUSE_ID` only, never from the
  contract. A table that already exists is imported before the first apply instead of being
  replaced. A dropped column is refused like a destroy unless `--allow-data-loss` is given, and
  so is a rename: with the column count unchanged the provider renames by position, so the old
  values would stay under the new name. A column reorder is refused outright, as no apply can
  reach it. A revoked grant is not refused. A column type change, a missing catalog, missing
  credentials or a workspace that does not answer fails at plan, naming the column, the catalog,
  the variables to set or the host. `fluid validate` refuses what would apply without being enforced: a host in
  the contract (on a binding or a build), Iceberg, masks, row filters, column restrictions, a
  packaging block, a `binding.location` key it would drop (`path`, `region`, ...), duplicate
  partitions, and a column type Databricks has no mapping for (it would be created as `string`)
  or would reject. It warns when a DECIMAL with no scale would keep no fractional digits. The
  product owns its
  schema: a grant is authoritative for its principal, so on a schema that already existed and
  that other teams share, each principal the contract names keeps `USE_SCHEMA` alone there.
  Jobs (dbt on serverless compute) come next.

### Changed

- **A dbt build with no `builds[].execution.runtime.platform` takes its platform from the first
  `exposes[].binding`**, for the generated `profiles.yml` and the model-contract column types. It
  fell back to DuckDB whatever the binding said, so a contract that named its platform only on the
  binding got a profile for the wrong warehouse. A build that declares its runtime platform is
  unchanged. The dbt runner's own generated profile, used only when a project carries no
  `profiles.yml`, still defaults to DuckDB.
- **`fluid import`, `fluid product-new`, `fluid init --discover`, `fluid forge data-model
  from-source` and the `fluid forge --scaffold` templates declare the newest stable schema, 0.7.6,
  not 0.7.3**, which was two stable versions behind the templates #619 moved, so a contract they
  wrote started without what 0.7.4 and 0.7.5 added. They read the newest stable bundled schema
  now, so a promoted preview moves them too (as do the forge interview's live preview and the
  product-type scaffold library). The shape they write is unchanged, and 0.7.6 accepts it wherever
  0.7.3 did. The market blueprints (`fluid init --blueprint`, `fluid market --instantiate`) and
  the gcp and snowflake quickstarts still declare 0.7.4.
- **`fluid bundle` without `--out` (or with `--out -`) does what its help says on every
  contract.** With a `fragments/` directory beside the contract, `yaml` and `json` went to a file,
  `contract.bundled.fluid.yaml`, instead of stdout, so `fluid bundle contract.fluid.yaml | ...`
  received nothing; and `--format tgz` wrote its gzip tarball under that `.yaml` name, which the
  next stage read as YAML (`'utf-8' codec can't decode byte 0x8b`) and `--sign` / `--attest`
  refused (`signing_bundle_wrong_format`). `fluid ship` bundled that way on every run. Now `yaml`
  and `json` go to stdout and a `tgz` to `<contract id>.fluid.bundle.tgz`, as for any other
  contract.
- **FLUID schema 0.7.6 is stable, and 0.7.7 opens as the next preview** (#752). 0.7.6 is the
  default now: `FluidSchemaManager.latest_bundled_version()`, the version forge-cli writes into
  the contracts it builds and assumes where none is declared. What graduates is everything 0.7.6
  added while in preview, as the 0.13.0 to 0.25.0 entries list it: among it declarative packaging
  modes, `engine: flink`, `trigger.type: continuous`, identity-keyed overlay merges, column
  masking and row filters, KMS keys, federation pins, and the exporter fields (`metadata.team`,
  the object root `description`, `exposes[].contract.id`, a column's `quality`). 0.7.7 is
  identical to 0.7.6 at open; additive preview fields land there. A contract validates against
  the version it declares, as before, so nothing changes for one that declares 0.7.5.
- **An ODCS export records the contract's `fluidVersion` in `fluidExtras` whatever the version.**
  It was recorded only where it differed from the exporting CLI's default, and an import takes a
  missing version from its own default. With 0.7.6 the default, a 0.7.6 contract's export would
  have come back from 0.12.0 to 0.25.0, which default to 0.7.5, as 0.7.5, and its 0.7.6 fields
  would have failed. One exception keeps a third-party document's ODCS → FLUID → ODCS exact: a
  contract this release (or a later one) imported from a document that recorded no version
  exports without one while it still declares the version the import gave it and carries only
  what ODCS has fields for. `ODCS_FLUID_EXTRAS=false` still writes no `fluidExtras` at all.

### Security

- **`fluid auth logout databricks` no longer deletes `~/.databrickscfg`.** That file belongs to
  the Databricks CLI, SDK, dbt and terraform and holds every profile and its token; logout
  removed all of it, through `AuthManager.logout` and, since #769 added `databricks` to the
  provider registry the CLI checks its `provider` argument against, through the command too. It
  now removes only the profile you name (`fluid auth logout databricks <profile>`, or
  `AuthManager.logout("databricks", profile=...)`), keeps every other profile's lines, writes
  the file back owner-only (0600) and never deletes it. With no profile named, a file that holds
  profiles is left alone and the command exits 1, listing them; `DATABRICKS_CONFIG_PROFILE` is
  quoted in that hint, never acted on. `DATABRICKS_CONFIG_FILE` names the file, as it does for
  the Databricks tools, and `fluid auth login` and `fluid auth status databricks` now read that
  file instead of `~/.databrickscfg` when it is set. An OAuth profile (`auth_type = databricks-cli`) is left to `databricks auth logout
  --profile=<name> --delete`, which clears its cached tokens too; a hard-linked file, or one that
  belongs to another user (a logout under sudo), is left as it is. Every logout also removes the
  host and token `fluid auth login` saved in the OS keyring, and fails if the keyring refuses. A
  removed token stays valid until it is revoked in the workspace.
- **The default `fluid bundle --format tgz` path gets the write check an explicit `--out` gets.**
  A symlink planted at `<contract id>.fluid.bundle.tgz` could point the write at a forbidden path
  (`/etc`, as root in a CI container); it is refused now, as `--out` with that path always was.
- **`fluid validate` sent a terminal escape sequence in a message to the terminal.** An argument
  or a contract value it quotes back (a `$ref` in `contract_load_failed`) could retitle the
  window, rewrite lines or, where the terminal allows it, write the clipboard. Control characters
  in those messages are now printed as text (`\x1b`), as the CLI's own error line already did.

### Fixed

- **`fluid generate` writes a Databricks `profiles.yml` and Databricks column types for a dbt
  build on Databricks.** It wrote a DuckDB profile and DuckDB types, and the dbt runner prefers a
  `profiles.yml` in the project directory, so a run could build into a local DuckDB file instead
  of the workspace. The profile reads the same environment variables as the dbt runner's
  (`DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`), with the catalog and schema
  from the contract's Databricks binding. For every adapter, a contract's catalog, schema or
  dataset now reaches `profiles.yml` only as literal text, a FLUID placeholder or dbt's exact
  `{{ env_var('NAME') }}` (with an optional quoted default) naming no credential-shaped
  variable; other Jinja, such as `{{ target.name }}`, is left out with a warning and the
  environment default applies, since dbt prints these values back in errors and run logs.
- **`fluid validate --fragment` no longer guesses a schema version.** With no `--schema-version`
  and no root contract above the fragment, it checked the fragment against the newest stable
  schema (0.7.5), so a valid 0.7.6 fragment failed on a field 0.7.5 does not have; a root contract
  it could not read, or one that kept its `fluidVersion` behind a `$ref`, got the same guess. The
  version now comes from `--schema-version`, else from the nearest root contract, read as every
  command reads it: a root that is a `$ref` to its base, a `fluidVersion` kept in a fragment and a
  same-document pointer are all followed. With neither, the run stops with
  `schema_version_required` and says why: no root contract above the fragment could be taken, or
  the nearest one cannot be read or declares no `fluidVersion`.
- **`fluid validate --fragment KIND` without a file** validated the `contract.fluid.yaml` of the
  working directory as if it were the fragment. It now says the file is required
  (`fragment_file_required`). When the KIND was left out, so the file was read as the KIND, the
  error says that instead.
- **`fluid validate --fragment KIND bundle.tgz`** validated the bundle and ignored `--fragment`.
  It is refused now (`fragment_bundle_unsupported`): a bundle is validated whole.
- **The `--format json` result of a fragment run names the fragment kind**, in a new `fragment`
  field after `valid`. The JSON of a whole-contract run is unchanged.
- **A `--schema-version`, `--min-version` or `--max-version` that is not a version** ended as
  `cli_unhandled_exception`, because the handlers around the parsers caught a `ValueError` the
  parsers never raise. It is the event the flag names now (`invalid_schema_version`,
  `invalid_min_version`, `invalid_max_version`), with its hints; the `invalid_schema_version`
  hint names `fluid validate --list-versions`, the command that lists the versions.
- **`fluid validate --quiet` printed nothing when a run stopped** on an error such as
  `contract_load_failed`, although `--quiet` is "errors only". The error now prints; the hints
  and docs link stay hidden.
- **`fluid validate missing.fluid.yaml` said "Contract file not found: None"**; it names the
  file again.
- **One odd contract no longer ends `fluid docs`.** A contract holding a value of an unexpected
  type (an `id` that is a number, `tags: [1, 2]`, a YAML date or `!!set`, a non-finite float, an
  integer too long to print, `exposes: 5`, a column `sensitivity` that is a list, a string with a
  lone surrogate) stopped the whole catalog with `docs_failed` and a bare Python message, or wrote
  an `index.json` strict JSON parsers reject (`NaN`). That contract is now
  listed like the rest: an odd character is shown escaped, a set as a sorted list, a non-finite
  float or an over-long integer as text, and `index.json` is strict JSON with json's own key
  spelling (`true`, `null`), as before.
- **A failing `fluid docs` run no longer leaves a broken catalog.** `index.html` was opened for
  writing before it was rendered and `index.json` was streamed into an open file, so a run that
  failed left `index.html` empty or `index.json` cut off mid-document; an id of 242 characters or
  more made a page name the file system refused, after the index already linked it. Every file is
  now rendered and encoded first and written beside its target; only when all are written are they
  moved in, pages before the index, so a run that fails while writing keeps the catalog the last
  run wrote, and one that fails while moving files in leaves no dead link. A page's slug is at most
  100 characters (a longer id ends in a hash of the whole id); a symlink planted in `--out` is
  replaced, not written through. A run that fails removes the directories it made itself, and
  a later run removes the temp files a killed run left beside the catalog's files, once they are
  over an hour old.
- **A contract that does not load is a row with its error, not a dead link.** `index.html` linked
  it to `contract-contract.html`, a page the run never wrote, and its error was only in
  `index.json`.
- **`fluid docs --src` lists a product's root contract only, and walks the tree safely.** It
  globbed `contract.fluid.*`, so the overlay `contract.fluid.prod.yaml` beside a contract got a
  page and an index row with no id; it followed symlinked directories with no loop check (two
  self-links hung it) and out of `--src`; and a `--src` holding glob characters
  (`my [team] products`) silently gave an empty catalog. It now lists `contract.fluid.yaml`
  (`.yml`, `.json`) only, does not follow a symlinked directory, and skips a contract symlink that
  leads out of `--src`; `--files` takes a glob, which does follow links, for a tree that needs them.
  An empty `--out` now says so, instead of a bare `[Errno 2]`.
- **`fluid apply` no longer takes a platform for a provider** (#752). Without `--provider`, a
  default-mode apply named its provider after the last expose's `binding.platform`, else the
  first build's `execution.runtime.platform`, word for word. So a Flink job writing a Kafka topic
  ended as `provider_unknown` ("kafka"), and so did a local product with a Flink build on
  `kubernetes`. The platforms are read in the same order, and the provider is the one they
  named before; what changed is a platform that names no provider. One that a build running
  outside forge-cli (`engine: flink`, or `properties.language: flink_sql`) runs on or writes to
  is passed over: the local planner lands nothing for it, and the rest of the contract applies.
  Any other platform no provider serves (`databricks`, `postgres`, a Kafka topic nothing in the
  contract writes, a build's `kubernetes` runtime) stops the run with
  `apply_platform_unsupported`, naming each field. Before, it was `provider_unknown` when it was
  the one read, and otherwise handed to the local provider, which wrote a placeholder CSV for it
  and reported success; an allow or block list set for the plugins does not change that. A
  `plan.json` whose contract has a build that runs outside forge-cli is not applied by the local
  provider (`apply_plan_runs_outside_forge`): for a plan it derives its own actions from the
  contract, the first build's SQL into the first expose's file, which can be the file the job
  writes. Most such plans stopped before too, as `provider_unknown`; one whose Flink build named
  no runtime overwrote that file and reported success. In the default mode, `fluid apply` of the
  contract file applies them. `--provider` (or `FLUID_PROVIDER`) still overrides the provider,
  and `--provider auto` now leaves the choice to the contract, as it does for the other engines.
- **`fluid split` rebases a relative `$ref` inside a part it moves.** A ref such as
  `sql: {$ref: ./sql/transform.sql}` was copied unchanged into `fragments/builds/transform.yaml`,
  where it named a file one or two directories off, so split refused the contract, and a split in
  two passes (`--parts sql`, then `--parts builds`) could not work. The ref now names the same file
  from the fragment (`../../sql/transform.sql`, a `#/pointer` kept); a URL, an absolute path and a
  same-document pointer are left as written.
- **A refused `fluid split` leaves the tree as it was**, as its message says: it took back the
  files it wrote but left the directories it made (an empty `fragments/` changed what other
  commands did), and after a failed write the empty file. A read-only root ended in "Unexpected
  error" (exit 2); it is now refused like any failed write (exit 1), as is a JSON root with a
  character that has no UTF-8 form. An interrupt (Ctrl-C) halfway through the writes is taken
  back too. The dry run refuses a symlinked directory on the way, as the real run already did.
- **A part in a container a YAML alias or `<<` merge shares is split from its own place.** Two
  builds merging the same defaults (`<<: *defaults`) made `fluid split --parts sql` fail with
  "write() argument must be str, not dict" and leave an empty `sql/b.sql`; each build's sql is now
  a file of its own.
- **`fluid split --out DIR` never writes over another file.** An existing `DIR/contract.fluid.yaml`
  that is not the contract being split (or a byte-for-byte copy of it that is no hard link to
  another file) was replaced without a word; a second split into the same `--out` wrote `-2`
  copies and left the first run's fragments unreferenced. Both are refused now, the dry run
  included, with the remedy.
- **A long id gets a file name a file system takes.** An id of 300 characters gave a 305-byte
  name: the dry run listed it and the real run failed. A slug over 100 characters now ends in 8 hex
  digits of a hash of the whole id, so two long ids that start alike keep names of their own.
- **`fluid split` keeps more of the root as written:** a comment on the last line of a moved block
  keeps the line's CR in a CRLF file; a keep-chomped block scalar (`|+`, `>+`) keeps its trailing
  blank lines; a tag or an unused anchor on a moved section's key line no longer makes the splice
  write unparseable YAML; and a comment on a block scalar's header line stays beside its `$ref`.

### Upgrade notes

- Running `fluid import` again over a contract it wrote before changes the contract's
  `fluidVersion` line from 0.7.3 to 0.7.6.
- `fluid apply` stops with `apply_platform_unsupported`, exit code 2 as before, where a platform
  the contract declares has no provider; in `fluid apply`, `provider_unknown` now means a
  `--provider` value no provider has. A contract with an expose on such a platform beside the
  exposes that chose the provider, which applied with exit 0 (handed to that provider), now
  stops: pass `--provider` to apply it as before.
- When the provider is `local`, every build's `execution.runtime.platform` is checked, not only
  the first: a later build declared for a platform no provider serves (`composer`,
  `kubernetes`, ...), whose SQL the local provider ran on DuckDB, now stops the run. Declare it as
  the job it is, or drop the platform.
- In the default mode, a `plan.json` of a contract with a Flink build stops with
  `apply_plan_runs_outside_forge` (exit 2) on the local provider, even with `--provider local`;
  apply the contract file instead. (`--mode amend-and-build` and `replace-and-build` hand both to
  the build runner, which this change does not touch.)
- **`fluid bundle` no longer writes `contract.bundled.fluid.yaml` on its own.** A script that
  bundled a split contract and then read that file must pass `--out contract.bundled.fluid.yaml`.
  A `contract.bundled.fluid.yaml` an earlier `fluid ship` left beside a contract is a tarball, and
  one an earlier `fluid bundle` left is a copy of the contract that may be stale; delete either.
- What forge-cli builds now declares 0.7.6 (the importers, `fluid product-new`, `fluid init
  --discover`, the `fluid forge --scaffold` templates, the copilot). A 0.7.6 contract merges
  overlay `builds`, `exposes` and `consumes` by id where every base entry names a unique one, so
  an overlay entry that names no id fails to load where the base list has more than one entry, or
  where another overlay entry names an id. The file templates `fluid init` copies keep declaring
  the version written in them.
- `fluid odcs import` and `fluid odps import` of a document that records no version declare
  0.7.6, so more of it lands in place instead of in the extensions bucket: the team roster and a
  column's `quality`. `fluid odps import` also places each output port's contract id and an
  object's `description`; `fluid odcs import` places a contract id only for a document with one
  schema object whose id is not a FLUID identifier (one that is becomes the root `id`, as
  before). Re-importing such a document writes a different file.
- A 0.7.5 contract's ODCS export now carries `fluidVersion: 0.7.5` in `fluidExtras`.
- To try what lands next, declare `fluidVersion: "0.7.7"`.

## [0.25.0] — 2026-10-28

### Added

- **A `$ref` can name a SQL, Markdown or other text file, and becomes its text.**
  `properties: {sql: {$ref: ./sql/transform.sql}}` and `usage: {$ref: ./docs/usage.md}` now compose.
  A target ending in `.sql`, `.md`, `.markdown`, `.txt`, `.py`, `.scala` or `.r` (any case) is read
  as UTF-8 text, never parsed: its BOM is dropped, CRLF line ends become LF, and it may be at most
  5 MiB. A JSON pointer into a text file is refused. `fluid bundle` inlines the text, so a bundle
  stays one self-contained document, and `--format tgz` hashes the file like any other fragment.
  Before, such a ref failed with "YAML root must be an object/dict", or with a raw PyYAML error for
  SQL holding `: `.

- **A fragment may be any JSON or YAML value**, not only an object, so a port's quality rules can
  live in a file that is the list itself: `quality: {$ref: ./fragments/quality.yaml}`. The root
  contract and an overlay must still be objects. A file with no known suffix keeps the old rule
  (JSON of any shape, otherwise a YAML object), so a dotfile such as `.env` never composes as a
  string.

- **Keys beside a `$ref` are merged over what it names**, as an RFC 7386 JSON Merge Patch on a copy
  of the target (the extended `$ref` of json-schema-ref-parser): objects merge, lists and scalars
  replace, `null` removes a key. `{$ref: ../../shared/columns.yaml#/order_id, description: Order
  key}` reuses a shared column with one field changed. A target that is not an object cannot take
  keys beside its `$ref`, and the error says so. Refs inside the keys resolve relative to the file
  that holds them, and are confined like any other.

- **Same-document pointers resolve.** `{$ref: "#/builds/0/properties/parameters/shared"}` names a
  place in the **composed** contract (every fragment read and the environment's overlay merged),
  whichever file the pointer is written in. So splitting a contract never changes what a pointer
  names, and an overlay that changes a shared value reaches every pointer to it. RFC 6901 escapes
  (`~0`, `~1`) decode, a pointer may go through other refs, and keys beside a pointer merge like
  keys beside a file ref. `#foo` is not a JSON pointer and is refused; a pointer that reaches
  itself is a cycle. A pointer resolves after every merge, so it cannot take part in one, and
  these are refused, naming the file and the place: a pointer that would be **merged into an
  object already at its place** (a key beside a file `$ref` whose target holds an object there, or
  an overlay value over a base object); an overlay pointer over a base **list of objects** (which
  the overlay would merge entry by entry), the base list written out or itself a pointer; and an
  overlay object or list of objects over a **base** pointer. Write those values out, or name a file
  (`./x.yaml#/a`), which is read before the merge. Anything else replaces, as any value does.

- **`fluid split --parts`** chooses what moves to files of its own: `sovereignty`,
  `accessPolicy`, `builds`, `exposes` (the default, as before), `sql` (a build's `properties.sql` to
  `sql/<build id>.sql`, or `.py` / `.scala` / `.r` by its language), `quality` and `schema` (an
  expose's lists), `extensions` or `extensions.<name>`, and `description` (the texts to
  `docs/<part>.md`). A part inside another is referenced from the file it ends up in.

- **`ref_root` in `fluid.workspace.yaml`** widens the `$ref` root for every product under it, so
  fragments the products share (a team, glossary terms) no longer need `FLUID_REF_ROOT` in CI, in
  the editor and in every tool on the library:

  ```yaml
  # fluid.workspace.yaml
  ref_root: .        # refRoot and workspace: {ref_root} are read too
  ```

  The file is found by walking up from the contract, and is read only for a contract that names
  another file. A project file may not widen confinement past its own tree, the reason a project
  `.env` cannot set `FLUID_REF_ROOT`, so the root is used only when it is a relative directory at
  or below the workspace file's own, contains the contract, and passes the vetting the DuckDB
  sandbox gives a workspace: not `/`, a home or a temp root, and not above the repository the
  contract is in. Anything else is ignored with a WARNING, and the contract keeps its own
  directory. Precedence: `ref_root=` > `FLUID_REF_ROOT` > the workspace > the contract's
  directory. `FLUID_IGNORE_WORKSPACE_REF_ROOT=1` turns the setting off, and a project `.env` cannot
  set it.

- **An authoring schema editors can check fragments with.**
  `fluid_build/schemas/fluid-authoring-schema-0.7.6.json` is the 0.7.6 schema with `{$ref: ...}`,
  keys beside it included, accepted at every value position, wrapped the way the OpenAPI 3.1
  meta-schema admits a Reference Object. A value that is not a `$ref` gets exactly the canonical
  checks and messages. It is generated from the canonical schema
  (`python -m fluid_build.schema_authoring`), and a test fails when it drifts. Its name stays out
  of the `fluid-schema-*.json` glob that version discovery reads. The canonical schemas are
  unchanged: every command validates the composed contract against them.

- **`fluid validate --fragment KIND FILE`** checks one fragment against the schema definition it
  stands for: `expose`, `build`, `column`, `sovereignty` or any other `$defs` name, or `quality` /
  `schema` for an expose's lists. The version comes from `--schema-version` or from the nearest
  `contract.fluid.yaml` above the fragment, and the errors read as the whole contract's do.

- **Validation errors name the file and line they were written in:**
  `fragments/exposes/orders.yaml:14 (exposes[0].contract.schema[1].type): 42 is not valid ...`
  instead of a path into a document nobody wrote. A value set by an overlay names the overlay. An
  unexpected key names the key; a missing one names the object's definition, not an overlay or
  keys beside a `$ref` that only merged other keys into it. Errors in the root contract read as
  before. The library answers the same question with `LoadedContract.origin_of(path)`.

### Changed

- **One composition for every command and the library.** `fluid validate`, `plan`, `apply`,
  `bundle`, `generate` (`artifacts`, `standard`, `ci`, `airflow`), `docs`, the library's
  `load_contract` and the MCP `validate_contract` and `score_contract_quality` tools now read the
  same document: the contract's fragments, then the overlay with its own refs resolved relative to
  the overlay file, then same-document pointers. **No `$ref` survives** anywhere, open objects
  such as `parameters`, `labels` and `extensions` included: a ref that cannot be resolved fails the
  command and names the file and the place it was written at. The silent fallback that re-read the
  base contract when a `$ref` was left over is gone, and with it the dropped overlay.

- **`fluid split` keeps every comment of a YAML root.** The root is spliced instead of re-dumped:
  every byte outside a moved part is kept, and each part takes its own text, comments and block
  scalars to its fragment. A part that is already a `$ref` is left alone, so a second run changes
  nothing; a file that exists is never written over (two parts bound for one file name get `-2`,
  `-3`); a JSON root stays JSON; a CRLF root keeps its line ends. Before it ends, split composes the
  result and compares it with the original, and takes back every file it wrote if they differ; a
  contract that does not compose here (a ref that needs `FLUID_REF_ROOT`, unset) is not split at
  all, dry run included. A root with YAML aliases is split as data, as before, and the command says
  its comments were not kept.

- **Library API 1.2.** `LoadedContract.origin_of(path)`; `load_contract_from_text` and
  `load_contract_from_dict` take `overlay_dir=` for the refs inside an overlay given as data;
  `LoadedContract.overlay` and `.files` come from the composition, the overlay's fragments
  included; `unresolved_refs` is empty for every file load, and the `contract_overlay_not_applied`
  warning is no longer emitted, because an overlay is never dropped.

### Security

- **A contract could have a product's database credential sent to a host it named.** A public
  orchestrator host must now be listed in `FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST` (see
  Upgrade notes).
- **Overlay files are confined like `$ref` targets, and `--env` must name an environment, not a path.**
- **Every file a contract composition reads is read bounded and only as a regular file**, with caps
  on the refs one composition resolves and on how deep it nests.
- **A `$ref` can no longer hang a command or read system directories.**

### Fixed

- **`fluid init --discover` against a remote URL failed on DuckDB 2.0.0**, before a byte was read:
  `Permission Error: Cannot read a CA certificate bundle: file system access is restricted by
  configuration`. TLS needs the host's trust store, the sandbox granted no path to it, and nothing
  granted one when a remote prefix was declared — so the whole remote-discovery surface was broken
  on the engine the project resolves to by default, while the same call worked on 1.5.x. The cause
  is upstream and deliberate: duckdb/duckdb-httpfs#474 moved the bundle read inside DuckDB's own
  file system, where curl and OpenSSL used to open it themselves and bypassed `allowed_paths`
  entirely.

  The sandbox now takes the first CA bundle that exists out of the five DuckDB probes and grants a
  **private copy** of it — one file in `allowed_paths`, mode 0400 in a 0700 directory the connection
  owns, with `ca_cert_file` pinned to it (`SET GLOBAL`, so a `con.cursor()` inherits the pin) and
  the copy removed when the connection object is finalised — and only when the allowlist carries a
  remote prefix AND that pin lands. A local read gains nothing, `allowed_directories` is
  untouched (so what a remote prefix grants locally is unchanged), and the allowlist object is not
  widened. On a host with none of the five (a minimal container, Windows) nothing is granted and
  DuckDB's own refusal, which names the paths it looked for, is left to speak.

  **Why a copy and not the store itself:** a path in `allowed_paths` is granted for *writing*. The
  first version of this fix granted the host store directly, and contract SQL could then run
  `COPY (SELECT 'ATTACKER CA') TO '<store>'` and replace it — measured on 1.5.6 and 2.0.0, a
  333 KB trust store overwritten with 14 bytes, with an ungranted sibling in the same directory
  still refused. Only file ownership stood in the way: `/etc/ssl/cert.pem` is root-owned, so an
  unprivileged `fluid` cannot write it, but `fluid generate ci` emits a `python:3.12-slim` job that
  runs as root, and a contract built there could have installed its own CA into that container's
  trust store. Granting a copy removes the reach, which is the part that mattered: the host store is
  not in `allowed_paths` at all, so the CA grant writes nothing outside this process's own temporary
  directory (the connection still reaches whatever the contract declared — that is the allowlist's
  job, unchanged here). It does **not** make the copy itself immutable — the mode is a speed bump. `COPY TO` with
  its default options writes a temporary file and renames it over the target, so 0400 stops nothing
  once the containing directory is writable by the SQL, and root ignores the mode regardless. The
  grant is therefore dropped outright when the copy would land inside a granted directory (`$TMPDIR`
  pointed into the workspace — an ordinary CI, direnv or Nix arrangement), and dropped when the
  `ca_cert_file` pin cannot land, which is every connection that loaded no httpfs. What an attacker
  who replaces the copy gets is the trust anchors of one connection, over remote locations the
  contract named anyway, on a file that is gone when that connection is.

  Certificate verification is untouched and stays on: an expired and a self-signed certificate are
  refused on 1.5.6 and 2.0.0 alike, with and without `enable_server_cert_verification` — that
  setting governs the httplib client, while `s3://` and `https://` go through curl. 1.5.6 exposes
  `enable_curl_server_cert_verification` (default true) and 2.0.0 no longer does;
  duckdb/duckdb-httpfs#378 proposes unifying the two and is still open, so the behaviour above is
  what was measured rather than what upstream documents. Nothing here disables verification.

  Verified by running the CLI: `fluid init --discover s3://noaa-ghcn-pds/csv/by_year/2024.csv`
  emits 8 columns and `--discover https://…/customer.csv` emits 18, on 1.5.1, 1.5.6 and 2.0.0,
  and `fluid validate` accepts the result.

- **An overlay was dropped, or half applied, when a `$ref` was involved.** An overlay was merged
  with its own refs unresolved, and the two loaders then disagreed. With the prod binding in a
  fragment, `fluid validate --env prod` logged `overlay_applied` and passed while the library, and
  every plugin on it, re-read the base contract and exported the **dev** topic for prod with
  exit 0; `fluid bundle --env prod` wrote the dev topic plus a literal `$ref`. A `$ref` left
  anywhere in the base (a same-file pointer, or one with keys beside it) dropped every overlay on
  the library path, so a plugin could combine two different documents in one run. An overlay's
  refs now resolve relative to the overlay file, inside the same root, and nothing falls back to
  the base.

- **A `$ref` with keys beside it, or a same-file `#/` pointer, stayed in the contract as a literal
  key.** Its target was never read, `fluid validate` passed when the ref sat in an open object such
  as `parameters`, and the literal `$ref` reached the runtime config a plugin rendered. A shared
  column reused with another description failed with "'name' is a required property", which did
  not say the reference had been ignored.

- **A fragment whose root is a list could not be referenced** ("YAML root must be an
  object/dict"), while the same list as `.json` loaded.

- **`fluid docs` ignored fragments and the 0.7.x shapes.** A contract written by `fluid split`
  listed its port as `expose-0`; every 0.7.x port showed "No schema columns defined.", because
  columns were read from `exposes[].schema`; and a description object (`{purpose, usage}`) aborted
  the whole run with "'dict' object has no attribute 'replace'" after truncating the page to 0
  bytes. Contracts are now loaded composed; ports are named by `exposeId`; columns come from
  `contract.schema` (a legacy top-level `schema` still reads); Nullable follows `required` and PII
  follows `sensitivity`; a description object renders its purpose, usage and limitations. A
  contract that fails to load gets an index row naming the error, and the rest of the catalog is
  written. Two contracts with one id get a page each instead of one overwriting the other.
  `index.json` keeps its keys and value types.

- **A split contract got different artifacts from the same contract written flat.** `fluid
  generate artifacts` without `--env` read the raw root, so a split contract failed with "FLUID
  expose has no usable name" or exported a literal `$ref`, and `generate ci` judged
  reference-only builds and engines from the raw root too. A split contract now gets
  byte-identical artifacts to the flat one, with or without `--env`; a contract with no `$ref` is
  read exactly as before.

- **`fluid split` could lose data and break the contract it split.** It wrote YAML into a `.json`
  root, overwrote one fragment with another when two ids made the same file name, and on a second
  run wrapped its own `$ref`s into new fragments until the contract no longer loaded. It also
  dropped every comment of the root. See **Changed** for what it does now.

- **Two readers in validate and apply still read the root as written.** The composition rule
  (an ADP or CDP may not consume what its type forbids) read each upstream's `productType` from its
  raw root, so an upstream whose metadata lives in a fragment passed unchecked; and apply's ADP
  auto-replay found downstream products from their raw `consumes[]`, so one whose consumes live in
  a fragment got no replay marker when its upstream rewound. Both now read each contract composed
  where the values they need come from a fragment (and parsed as written otherwise, so a large
  tree costs what parsing it does), skipping one that cannot be read.

- **The bundle's YAML turned a next-line character into a space.** Text holding U+0085 was kept
  exactly by the loader and the JSON bundle, but `fluid bundle` and the tgz's
  `contract.resolved.yaml` wrote it bare, and YAML readers fold that into a space, so
  `fluid plan bundle.tgz` planned different SQL than `fluid plan contract`. Such a string is now
  written escaped.

- **Naming the line of n errors in one mapping took n² time**, for every contract: 40,000
  invalid values took 38 s. A mapping's keys are now indexed once per report.

- **A workspace `ref_root` the file system could not stat crashed the load** with a bare
  `OSError`, and an unreadable `fluid.workspace.yaml` was ignored without a word. Both are now
  ignored with a warning, and the escape error says the workspace root was ignored and why.

- **The MCP `validate_contract` tool read a contract raw.** A fragment-first contract reached it
  with `{$ref: ...}` where its builds and exposes are, so it reported errors `fluid validate` does
  not, and an empty file validated nothing. The `contract_path` form of `validate_contract`,
  `score_contract_quality` and `enrich_contract_suggestions` now composes the contract like every
  command, fragments confined to the contract's directory unless widened.

### Upgrade notes

- **A `$ref` that 0.24.0 left in the contract now composes or fails.** A `$ref` with keys beside
  it, a same-file `#/` pointer, or a ref inside an overlay used to stay a literal key (and, on the
  library path, drop the overlay). It now resolves, and one that cannot be resolved fails every
  command, naming the file and the place. A contract that embeds a document carrying its own
  `$ref` keys (a JSON Schema or OpenAPI fragment inside `parameters` or an extension) now fails,
  because every `$ref` key is read as a contract reference; keep such a payload as a string.

- **Contracts that use the new forms need 0.25.0.** Text refs, list-root fragments, keys beside a
  `$ref`, pointers, refs inside overlays and a workspace `ref_root` fail on 0.24.0, or load
  differently there (a `$ref` with keys beside it inside `parameters` passed 0.24.0 as a literal
  key). Pin `data-product-forge>=0.25.0` wherever such contracts are validated or planned. The
  default `fluid split` output stays readable by 0.24.0: verified on every shipped example.

- **Overlays are held to the ref root, and `--env` names an environment.** An `overlays/` directory
  or overlay file that links outside the contract's tree is refused; widen the root
  (`ref_root` in `fluid.workspace.yaml`, or `FLUID_REF_ROOT`) to share one. `--env ../x` or
  `--env /abs/x` is refused by every command.

- **`fluid split` writes a different root than 0.24.0 did**: it keeps the root's text and comments
  instead of re-dumping it; the fragments are the same. It writes no file through a symlink.

- **A root contract must be a regular file of at most 5 MiB**, as every file the composition reads
  must be. A contract read through a pipe (`fluid validate <(git show HEAD:contract.fluid.yaml)`)
  or a JSON root over 5 MiB, both of which 0.24.0 loaded, is now refused; write it to a file, or
  move its large parts into fragments.

- **A composed contract nests at most 128 levels deep**, the cap every YAML file already had, so
  the bundle of any contract that composes can be read back.

- **A contract whose orchestrator is on a public host now fails until the operator allows it.**
  Set `FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST` in the shell, in CI, or in a
  `FLUID_SECRETS_FILE` outside the project — **not** in a project `.env`, where it is ignored — to
  the host or its parent domain (`connect.example.com`, or `example.com` to cover subdomains). A
  host written with a port never matches, so leave the port off. Deployments on `localhost`,
  `127.0.0.0/8`, `::1` or a private network need no configuration, which is the common case.

### Known limitations

- **`fluid contract digest` and the federation `upstreamDigest` still hash the root file as
  written**, so an edit inside a fragment does not change them; `LoadedContract.digest` covers
  the composed contract. Changing it changes existing pins, so it waits for a decision.
- **Some read-only views still read the root file as written** and so do not see what fragments
  hold: `fluid status`, `fluid stats`, the forge welcome scan, `viz-graph`'s workspace view, the
  workspace product listing, the copilot's contract tools and `forge --refine`.
- **`fluid split` does not rebase a relative `$ref` inside a part it moves**; it refuses such a
  split, changing nothing, instead of writing a fragment whose ref no longer resolves.
- **Left as they were:** `file.yaml#key` without the leading `/` is accepted, as 0.24.0 accepted
  it; two `$ref` keys in one mapping keep the last, as YAML does; a file with a text-like suffix
  not in the list above (`.hql`, `.ddl`, `.j2`) is read as YAML or JSON, not as text.

- Two credential paths this policy does not reach, both pre-existing. An object-store URI can carry
  its own credentials in the URI itself, which is not a `secretRef` and so is not policed; and
  `{{ env.NAME }}` interpolation inside a contract bypasses `secretRef` entirely. Neither is changed
  by this release.

- **A CA bundle outside DuckDB's five candidate paths cannot be used.** There is no operator
  override (no `FLUID_DUCKDB_CA_BUNDLE`, and DuckDB consults neither `SSL_CERT_FILE` nor OpenSSL's
  defaults — measured), so a host whose trust store lives elsewhere, including one behind a
  TLS-inspecting proxy whose own CA is installed somewhere else, still gets DuckDB's refusal on a
  remote read. That is no worse than before this release, where every such read failed on 2.0.0
  regardless, and the refusal still names the paths it looked for — but it is not closed.
- **`azure://` / `az://` is not measured.** The fix grants the copy for every declared remote
  prefix, but the Azure filesystem extension does its own TLS and may not read the bundle through
  this path. `s3://`, `gs://`, `http://` and `https://` were verified live.

## [0.24.0] — 2026-10-27

### Fixed

- **Exported lineage claimed a version the contract never pinned.** An ODPS `inputPorts[].version`
  came from the consume's own `version` or the provider's `1.0.0` default, and a consume that states
  a range (`versionConstraint: ^2.1`) has neither — so every such export asserted `version: 1.0.0`,
  a pin the contract did not make and, for a product whose 1.x line is retired, a version that no
  longer exists. The lowest version the range accepts is now resolved (`^2.1` → `2.1.0`,
  `~1.4` → `1.4.0`, `>=3.0.0` → `3.0.0`). A range the resolver cannot read falls back to the old
  constant rather than guessing: ODPS v1.0.0 has `version` in `InputPort`'s `required`, so the field
  cannot simply be omitted, and a wrong exact version reads as a fact somebody established. The
  refusals include a compound range, a wildcard, `>1.0.0` (whose true lower bound semver gives no
  way to name), and anything whose components are not plain ASCII digits without a leading zero —
  `^01.2` is a schema-valid constraint, but `01.2.0` matches neither `$defs.semver` nor any
  resolver. `versionConstraint` still rides along in `customProperties`, so the range this product
  accepts is not lost.

- **An exported output port named none of its upstreams.** `outputPorts[].inputContracts` was
  populated only from an imported ODPS document's pass-through, so a contract written in FLUID
  exported output ports with no lineage at all and a catalog reading dependencies there saw a
  product with none. The entries are now derived from the document's own resolved input ports — one
  per `consumes[]`, carrying exactly the `id` and `version` Bitol's `$defs.InputContract` allows,
  read from those ports rather than recomputed so the two halves of one document cannot disagree. An
  imported document's own `inputContracts` still win, and `expects[]` — the carrier an imported
  document's input ports arrive on — is excluded, so import → export stays a fixed point and never
  adds a dependency the author did not write. A `builds[].properties.source` stream is excluded too:
  its `<kind>://<database>/<stream>` id is synthesized to populate the *required* input-port field
  for a source that has no contract at all, and `inputContracts` is optional, so naming an id no
  catalog can resolve is worse than naming nothing. Both Data Mesh Manager publish paths — the
  registrar behind `fluid publish --target datamesh-manager` and `catalog.register`, and
  `fluid datamesh-manager publish` — strip the derived lineage alongside the input ports it mirrors,
  for the reason those were already stripped: Entropy draws product-to-product lineage from Access
  resources, so naming the upstream twice would require mirroring it as a SourceSystem and render
  duplicate graph nodes. They strip by different rules, so each passes the ports it dropped to one
  shared helper.

- **A quality expression exported as SQL no DQ engine could execute.** ODCS defines the `sql` rule's
  `query` as an individual query returning a value that can be compared, and FLUID's
  `exposes[].contract.quality[].expression` is a row predicate — it was emitted verbatim, so an
  engine was handed `preferred_customer_flag IN ('Y', 'N')` with `mustBe: true` and nothing to
  execute. A predicate now becomes a counting query compared to zero, against the `${table}`
  placeholder from ODCS's own example. The negation is `(…) IS NOT TRUE`, not `NOT (…)`: under
  three-valued logic a NULL row makes the naive form NULL and counts no failure, although that row's
  predicate did not hold. An expression whose *first token* is `SELECT` or `WITH` passes through
  unchanged; the token boundary matters, because matching a bare prefix left every predicate on a
  column named `selected_at` or `with_tax` in the broken state this fixes. The predicate is wrapped
  on its own line and a trailing `;` is dropped, so neither a line comment nor a stray semicolon can
  swallow the closing paren.

- **A renamed input port leaked a product consume into the Data Mesh Manager payload.** Found while
  covering the lineage strip above. When two `consumes[]` share an `exposeId` the exporter renames
  the second, and `fluid datamesh-manager publish` matched the renamed port by name — which misses
  two of the three rename shapes: a `productId` tail containing `__` of its own
  (`silver.crm__core` → `crm__core__customers`, split at the wrong `__`), and the numeric-suffix
  fallback (`customers_2`, with no `__` to match on). Both published a port Entropy cannot resolve
  without mirroring the upstream product as a SourceSystem. The match now also tests the contract id
  the exporter actually stamps on the port, read from the exporter's own helper so the convention has
  one definition; a source-system consume keeps its port, as before. This leak predates the release —
  but deriving `outputPorts[].inputContracts` would have doubled it, so it is fixed here rather than
  carried.

### Added

- `consumes[].contractVersion` (0.7.6 preview) — the exact upstream version this product was built
  and tested against, within `versionConstraint`. It wins over the resolved lower bound as the
  version an exported input port, and its matching `inputContracts` entry, claims.

### Changed

**Exported artifacts change shape in this release.** If you have tooling that reads forge's ODCS or
ODPS output, read this before upgrading. Each change is a fix for output that was wrong and is
described in full under *Fixed*; the summary is here because the old values may be baked into
consumers.

- An ODPS export of a contract with `consumes[]` now carries `outputPorts[].inputContracts`, where
  before it carried none, and an input port's `version` may differ from `1.0.0` where the contract
  states a range. A consumer that compares exported documents byte-for-byte will see both.
- An ODCS `sql` quality rule derived from `exposes[].contract.quality[].expression` now carries a
  counting query (`SELECT COUNT(*) FROM ${table} WHERE (<expression>) IS NOT TRUE`, the predicate on
  its own line) with `mustBe: 0`, where it previously carried the bare expression with
  `mustBe: true`; its `description` changes with it. A DQ configuration or test that pins the old
  `query` string or the boolean `mustBe` must be updated — ours was. An expression already written
  as a `SELECT` or `WITH` query is unchanged.

## [0.23.0] — 2026-10-27

### Security

- **A poisoned ODPS document could print its own credential to stderr.** `fluid odps import`'s new
  validate-before-write gate reports the first schema error, and a jsonschema error quotes the value
  that failed — a value that comes from the document the command was handed. Importing a bundle
  whose root `id` was `snowflake://svc_etl:<password>@host/?token=<token>` printed the password in
  full while masking the token in the same string: the repo's redactor knows that shape and simply
  was not on this path, which went through the console's weaker `key=value` sanitiser. The gate's
  message now goes through `observability/secret_redactor.py::redact_secret_text`. A value with
  nothing credential-shaped in it is still quoted, so the diagnosis is not swallowed.
  The underlying weakness is older than this release — `fluid validate` prints the same bytes — and
  that path is deliberately left for its own change; this fixes the surface that was added here.
- **An acquisition source no longer receives this host's ambient database credentials.** A contract
  names the host its postgres or mysql source lives on, and libpq and mysql_scanner read
  credentials from the process environment for whatever host they are handed — so a contract naming
  a host of its own choosing could be authenticated with this machine's `PGPASSWORD`, `PGPASSFILE`,
  `PGSERVICE` or `MYSQL_PWD`: a credential the contract was never given, belonging to a different
  server. `fluid init --discover` has suppressed these since its JDBC introspection landed; the
  acquisition runners had not, so the same contract fields reached the same client libraries with
  the environment intact.
  The duckdb runner's `ATTACH` now runs inside the same suppression window, which is shared from
  `credentials/host_db_env.py` rather than copied (`build_runners` may not import `cli`). 14
  variables are covered, and `cli/discover/_jdbc_introspect` keeps its original private names as
  aliases, so nothing that referenced them changes.
  **This route is independent of a contract's own `connection.secretRef`** and no allowlist over
  `secretRef` would close it, because the credential never passes through `secretRef` at all. The
  declared DSN still carries its own credentials, so a legitimate source is unaffected and no
  contract change is needed.

- **A contract could send any environment variable to a host of its own choosing.**
  `connection.secretRef: env://NAME` let the contract name both halves — the variable to read and
  the host the resolved value is sent to as a password — and nothing checked either. A run whose
  environment holds secrets (CI, an operator's shell) could therefore have any variable
  exfiltrated. All six acquisition runners (airbyte, debezium, dlt, duckdb, kafka_connect,
  meltano) go through one helper, so all six were affected.
  `env://` now resolves only when **every** destination the connection block carries is loopback,
  or when the operator has listed the variable in `FLUID_ACQUISITION_SECRET_REF_ALLOWLIST` as
  `NAME` (any host) or `NAME@host`. Every destination is checked, not just the first field that
  parses, so a decoy `host: localhost` beside a remote `connection_string` does not clear the
  send. The check runs **before** the reference is resolved, so a refused send never reads the
  secret, and the refusal names the variable and host but never the value. `vault://`, `aws://`,
  `gcp://`, `azure://` and `file://` are not policed: those name a secret the operator provisioned
  and granted this process access to, which is already an operator decision. A `secretRef` whose
  target field already holds an inline credential is not policed either — the reference is never
  read and nothing is sent.
  The allowlist is an **operator** setting: a value set by one of the project's own dotenv files is
  ignored, because `fluid apply` hydrates the environment from `.env`, `.env.<env>` and
  `FLUID_SECRETS_FILE`, and those files are committed beside the contract.
- **`TESTCONTAINERS_HOST_OVERRIDE` is now an operator-only setting**, as
  `FLUID_RUNNER_HOST_OVERRIDE` already was. Both rewrite a contract-declared loopback host to a
  reachable one *after* the policy above has cleared the send as local-only, so a project dotenv
  file able to set either could have redirected a credential allowed only because it was staying
  on this machine.
- Resolved `secretRef` values are registered with the exact-value redactor (#884), so a literal a
  client library echoes in its own error is masked by value rather than by pattern.


- A contract repository could hand the whole host to its own SQL through DuckDB's spill
  directory. `secure_duckdb_connect` never set `temp_directory`, and DuckDB resolves its
  temp directory into `allowed_directories` like any other grant; the default is relative
  (`.tmp` in the working directory, or `<db>.tmp` beside a file database), so a repository
  shipping `.tmp -> /` got `allowed_directories = ['/']` on a connection that granted
  nothing, and contract SQL could read any file the process could. Reproduced on duckdb
  1.5.1, 1.5.6 and 2.0.0, for an in-memory and a file database, through the same helper the
  MCP output port's `query_sql` runs on. The engine now creates and owns that directory
  (mode 0700, under `$TMPDIR`, removed with the connection), reads the grants back under
  the lock and refuses the connection if DuckDB took a directory the call site did not
  name, and refuses a caller-supplied `temp_directory` rather than silently overriding it.
  Spilling is unaffected. No contract, flag or configuration change is needed.
- Opening one DuckDB database file from two threads at once is now serialised. DuckDB
  shares one instance per file per process and the sandbox's settings live on the instance,
  so the two opens could cross-configure: a connection could be returned running under the
  other thread's allowlist. The second opener now reliably gets the existing
  "already open in this process" refusal.

### Added

- **Seven optional fields in the 0.7.6 preview schema, for things a contract could not previously
  say.** Each is optional and additive, so a contract that sets none of them validates and exports
  byte for byte as before. The names are taken from Bitol ODCS v3.1.0 and the released ODPS v1.1.0
  rather than invented, so import and export are a pass-through instead of a translation.
  `metadata.team` `{name, members[]}` with `members[].{username, name, role, email}` —
  `metadata.owner` could express at most one person and carried no role, so a catalog that requires
  named roles (product owner, steward, custodian) skipped the product. The root `description` now
  accepts either a string, unchanged, which becomes `purpose`, or `{purpose, usage, limitations}`,
  the shape both Bitol standards define. `consumes[].contractId` lets an author name the contract id
  of an upstream that was not built with FLUID; the exporter already preferred an explicit one, but
  the closed schema rejected the key. `exposes[].contract.id` pins a contract id so it survives
  renaming the product or the expose, and serves catalogs that require their own id format.
  `binding.location.host` carries Kafka bootstrap servers — the ODCS exporter already read `host`
  while the schema accepted only `account`, so the only valid way to set them was a field named for
  something else. `binding.serialization` `{value, key}` says how messages are serialized. A column
  may now carry `quality`, which the exporter already replayed although the closed column definition
  rejected it. All of these require `fluidVersion: "0.7.6"`, which is still a preview; see #752 for
  the promotion criteria.
- **`fluid validate` refuses two exposes that pin the same contract id.** JSON Schema cannot express
  uniqueness across a nested key, so this is a code check at the one chokepoint `fluid validate
  --offline` reaches: `contract id 'x' is used by exposes 'a' and 'b' — give each expose its own
  contract.id`.
- **`--env` on `fluid odps-bitol export` and `fluid odcs export`.** Neither accepted one, while
  `fluid odps export` had always honoured it.

### Changed

**Exported artifacts change shape in this release.** If you have tooling that reads forge's ODCS or
ODPS output, read this section before upgrading. Each change is a fix for output that was wrong, and
each is described in full under *Fixed*; the summary is here because the old values may be baked
into consumers.

- A per-port ODCS contract is now named after its **port** — the expose title, falling back to its
  `exposeId` — and the product name moves to a `fluidProductName` custom property. Previously every
  contract a product exported carried the product's name, so a product with several exposes emitted
  several contracts sharing one name. This changes the exported `name` for a single-expose product
  too, whenever the port's title differs from the product name. The ODCS `id` is unchanged in every
  case, and a whole-contract export is untouched.
- ODPS `inputPorts[].contractId` is now `<productId>.<exposeId>` rather than the bare upstream
  product id, and `versionConstraint` is carried in a custom property instead of being dropped.
- A Kafka server's ODCS `format` is now the format of the messages, as ODCS defines it, taken from
  the `binding.serialization.value` this release adds — `avro`, `json`, `protobuf` or `xml`, with
  the two `-confluent` spellings collapsing onto the wire format they carry. A contract that has
  not adopted that field still exports the FLUID binding format `kafka_topic`, exactly as before:
  the exporter will not guess a wire format from a contract that never stated one, and guessing
  `json` would be wrong for every Avro topic. So this changes a Kafka export only once the contract
  says what its messages are. (A Kafka binding that declares no `format` at all has defaulted to
  `json` since before this release — that is unchanged, and not what the new field replaces.)
- A Kafka column's `physicalType` is now an Avro type name. Notably `bigint` exports as `long`
  where it previously exported as `integer`.
- Derived quality rules now carry an `id`, and rules declared on a contract are exported as ODCS
  quality instead of riding only in the `fluidExtras` custom property — for the four `dq.rules`
  types ODCS can express: `completeness`, `uniqueness`, `valid_values` and `freshness`. The other
  four (`accuracy`, `schema`, `anomaly_detection`, `drift_detection`) have no executable ODCS form,
  so each is logged by name with the reason and still carried in `fluidExtras`, as the *Fixed*
  entry says.
- `fluid odps import` now refuses to write an invalid contract instead of reporting success. The
  existing `--lenient` flag, which downgraded output-port resolution failures to warnings, is
  widened to mean "write it anyway" and so restores the old behaviour. The pass-through bucket it
  writes moved from
  `metadata.odps_passthrough` to the root `extensions.odps`, because `metadata` is
  `additionalProperties: false` and the old address could never validate.


### Fixed

- **A Kafka contract exported a 64-bit column as a 32-bit one, so the registered schema and the
  data disagreed.** `fluid_to_physical` falls back to the ODCS *logicalType* when a binding's
  provider has no physical-type table, and Kafka had none. FLUID `bigint` maps to the logicalType
  `integer`, and ODCS's own schema defaults an unqualified `integer` to `i32` — so the exported
  column said 32 bits. A tool deriving an Avro schema from that contract reads Avro `int` while a
  producer such as Flink `BIGINT` writes a 64-bit `long`: the two disagree silently, which is a
  data-corruption class of defect rather than a cosmetic one. `bigint` and `long` now export as
  `long`, `int` stays `int`, and a `decimal` carries its precision and scale in `customProperties`
  the way an ODCS-to-Avro consumer reads them. The table covers every FLUID type rather than the
  handful that surfaced the bug, and it is derived from the Avro 1.12 specification and from
  datacontract-cli's own `avro_exporter.py`. Providers that already had a table are unchanged.
- **A DQ engine refused forge's contracts outright, because derived quality rules had no id.** The
  exporter synthesises not-null rules from `required` and uniqueness rules from primary-key tags.
  ODCS quality rules carry an `id`, and an engine that labels results by rule id rejected the whole
  contract — so a single derived rule made the export unusable there. Derived rules now carry a
  deterministic `<column>-<metric>` id, sanitised to the charset ODCS's `StableId` declares and
  unique within its schema object. Two renders of the same contract produce the same ids, so an
  export is not a spurious diff. Rules forge did not write are still replayed verbatim, ids and all.
- **Quality rules an author wrote were never exported.** `exposes[].contract.dq.rules` and
  `exposes[].contract.quality` were not mapped to ODCS quality at all; the extras pass parked them
  in the `fluidExtras` custom property, so a consumer running ODCS quality rules saw only the rules
  synthesised from `required` and primary keys and none of the author's own. They now map onto the
  property or object their selector names, using ODCS's own metric and operator names —
  completeness to `nullValues`, uniqueness to `duplicateValues`, valid-values to `invalidValues`,
  thresholds to the matching operator, freshness and expressions to `custom` and `sql`. A rule type
  with no executable ODCS form is now **logged by name with the reason** and still carried in
  `fluidExtras`; silence was the previous behaviour and the actual bug.
- **The exporter supported a column field the schema rejected, and read one the schema does not
  define.** A column's `quality` list was replayed verbatim on export, but the 0.7.6 column
  definition is `additionalProperties: false` and had no `quality`, so `fluid validate` rejected a
  column written the way the exporter supported. The field is now declared. Separately the exporter
  read `validations` while the schema defines `validationRules`, so rules written as documented were
  never exported; it now reads `validationRules`, and keeps `validations` as a tolerated alias
  because the exporter is also handed dicts that never passed through the validator.
- **`fluid odps import` reported success and wrote a contract its own validator rejects.** Export
  then import produced a document that `fluid validate --offline` failed with ten errors, while the
  command printed a success tick and its help claimed it "Always emits one validated FLUID
  contract". The importer now normalises, reusing the ODCS leg's helpers rather than restating them,
  and validates before writing: it names the first problem, writes nothing and exits 1, while
  `--lenient` writes anyway behind a warning. Nothing is dropped — anything with no schema home
  rides in the pass-through bucket and is replayed verbatim on export, so the product document still
  round-trips byte for byte.
- **A contract using the new 0.7.6 fields could be exported but never imported back.** The importer
  pinned every result to the latest bundled *stable* schema, which rejects the preview fields, and
  the new validate-before-write gate then correctly refused it. An import now declares the
  `fluidVersion` the source document recorded, falling back to stable when it records none. That
  value selects a JSON Schema and comes from a document that may be foreign, so it is honoured only
  when it is a string in the bundled set; anything else falls back to stable and logs a warning
  naming the rejected value, and a preview version is never the fallback. Only the sibling ODCS
  records the version, so a bundle import can honour it and a bare product import cannot.
- **A team's members and their roles survived an ODCS export and were flattened by an ODPS one.**
  Both exporters derived `team` from `metadata.owner`, which yields at most one member — username
  set to the owner's email, no name, no role. The mapper already read `owner.name`, `owner.role` and
  `owner.contacts[]`, but the schema rejected those keys, so a valid contract never reached that
  code. With `metadata.team.members` set, both exporters now emit every member and role; without
  it, the owner-derived path is unchanged.
- **A team member's `email` was advertised by the schema and dropped by both exporters.** The
  0.7.6 `teamMember` declares it, so an author could fill it in and `fluid validate` accepted the
  contract, but it reached neither the ODPS product nor the ODCS team block — in ODPS it reached
  nothing at all, and in ODCS it survived only inside the `fluidExtras` round-trip blob, which
  `ODCS_FLUID_EXTRAS=false` switches off. It cannot be a member field: Bitol's `TeamMember` has no
  `email` property and is closed in all three vendored schemas (ODCS v3.1.0, ODPS v1.0.0 and
  v1.1.0), so emitting one fails strict Bitol validation. All three do accept a member
  `customProperties` list, so the address is now published there as `fluidEmail`, the same escape
  hatch and the same `fluid`-prefixed naming as `fluidProductName` and `fluidExtras`. Both
  importers read it back into the roster, so the field round-trips, and the exported documents
  still validate against both vendored schemas.
- **The ODPS exporter dropped a structured description entirely.** Given the object the ODCS
  exporter has accepted since #585, it wrote no `description` at all, because it only knew how to
  wrap a string. It now passes the object through.
- **Lineage between two FLUID products did not resolve.** `consumes[]` became ODPS `inputPorts[]`
  with `contractId` set to the upstream *product* id — but forge names that upstream port's own ODCS
  `<productId>.<exposeId>`, so the edge pointed at a contract no catalog has. It now defaults to the
  id forge itself mints, while an explicit `consumes[].contractId` still wins. `versionConstraint`
  was read and then discarded; it now rides in a custom property, since Bitol's `InputPort` has no
  such field.
- **A pinned or imported contract id was re-derived on export.** An ODPS import kept an output
  port's own `contractId` in a pass-through bucket, but the export derived the id again, so a port
  imported with a UUID re-exported as `<productId>.<exposeId>` — and so did its ODCS. An explicit
  `exposes[].contract.id` is now used verbatim as both. Every site that mints a per-port contract id
  resolves it through `util/contract.py::expose_contract_id`, and that resolution reads the whole
  contract — including the root `extensions.odps` an import leaves the id in — so the five entry
  points that render a per-expose ODCS all answer the same string, not only the one that happens to
  rehydrate the bucket first. The fragments-mode invariant that the ODPS `contractId` and the ODCS
  `id` are the same string is then held by the export refusing a contract that would break it, not
  by agreement between call sites: two exposes resolving to one id are rejected by `fluid validate`
  and by `BitolOdpsProvider.render`, which would otherwise have written one per-port ODCS for two
  output ports and left the losing port with no schema in the bundle at all.
- **Several contracts from one product shared a name.** A per-port export named every ODCS after the
  product, so tools that key files or registry subjects on the contract name collided. ODCS puts
  identity on `id`, which was already per-expose, so this was invisible to id-keyed consumers and
  silent for everyone else.
- **`fluid odps-bitol validate` checked five keys and never ran the schema.** It asserted that
  `apiVersion`, `kind`, `id`, `name` and `status` were present and that `kind` was `DataProduct`,
  then printed a pass. A document with `status: 123` and an unknown top-level key exited 0 while
  `fluid odps validate` rejected the same file and exited 1 — two commands, one standard, opposite
  answers. It now calls the same code path, which selects the schema from the document's own
  `apiVersion`.
- **Three validate commands crashed instead of diagnosing a malformed file.** On the
  `odps-bitol` and `--spec odps-4.1` paths only some parse failures were caught, so a malformed
  `.json`, non-UTF-8 bytes, or a pathologically deep document escaped as an unhandled exception and
  exited 2. Each now exits 1 naming the file and the problem, as does a missing path, a directory
  and a document that parses to something other than a mapping. (Which error a deep document raises
  is interpreter-specific: a `RecursionError` up to 3.13, and a plain parse error on 3.14, whose
  json decoder no longer recurses. Both are handled.)
- **`fluid generate standard --env` was accepted and ignored for `odcs`, `odps` and
  `odps-bitol`.** Those paths loaded the base contract with no warning, so an overlay that changes a
  Kafka topic or a Glue database produced the base value under a `--env prod` flag, while `fluid odps
  export --env` and `fluid generate artifacts --env` had always applied it.
- **`fluid generate standard -o out.yaml` failed on a bare file name**, with
  `[Errno 2] No such file or directory: ''`, because `os.path.dirname` of a bare name is empty and
  `os.makedirs("")` raises. Two of the four sites that derive a directory this way were guarded; all
  four now are, and a test parses the module and fails if a fifth unguarded site is ever added.
- **The vendored ODPS v1.1.0 schema was a dev-branch snapshot, so valid documents were rejected.**
  Bitol released v1.1.0 on 2026-09-08; the file here was fetched from the dev branch on 2026-07-26,
  before the release opened `$defs.SBOM` for `authoritativeDefinitions`, `customProperties` and
  `tags`. Because `SBOM` is `additionalProperties: false`, `fluid odps validate` rejected a document
  valid against the published standard with "Additional properties are not allowed ('tags' was
  unexpected)". The file is now vendored verbatim from the v1.1.0 tag. Forge's own exports were never
  affected, since forge writes no SBOM; the gap showed when validating another tool's document.
  `v1.0.0` stays the default emit target, now as a stated compatibility decision rather than a wait
  for the release: changing the emitted `apiVersion` changes what every existing consumer parses.
- **`fluid forge` seed loading misread an ODCS-only directory as a plain directory.** The ODPS
  importer's pass-through bucket moved to the root `extensions.odps`, and the seed loader still read
  the old `metadata.odps_passthrough` address, so the lookup always yielded nothing and every
  ODCS-only directory was classified as an ordinary one. A test now pins the marker's address at the
  writer, so the next relocation fails at the source.
- **An incremental cursor written on DuckDB 1.5 and read on 2.0 silently dropped or
  duplicated rows.** The cursor is stored as text and compared as an untyped SQL literal,
  and the two versions disagree about the column type it is coerced against:
  `read_json_auto` over a timestamp carrying `Z` gives TIMESTAMP on 1.5 and TIMESTAMP WITH
  TIME ZONE on 2.0, where a naive literal is read in the session time zone -- the host's.
  On a host with a non-zero UTC offset the `>` boundary therefore moved: an end-to-end run
  that wrote its cursor on 1.5 and continued on 2.0 landed 5 of 6 rows and advanced the
  cursor past the row it skipped, at exit code 0 with the stream recorded `succeeded`.
  Both DuckDB versions resolve to `duckdb>=1.5.0`, so one install can write state another
  reads.
  A timestamp cursor now compares INSTANTS (`epoch_us`), which no session time zone can
  shift, and the stored text is normalised to a naive UTC instant. **No state file needs
  rewriting**: a cursor a shipped 2.0 install already wrote carries an explicit offset and
  is normalised when read. Integer, date-as-integer and opaque string cursors keep the
  plain comparison. CSV sources were never affected -- `read_csv_auto` already sniffed
  TIMESTAMPTZ on 1.5.
  Pinning the session zone to UTC would also have worked and is deliberately not used:
  `TimeZone` is ICU-backed, the sandbox forbids autoloading extensions, and without ICU
  DuckDB does not recognise the option at all, so a pin would fail the connection in
  exactly the hardened environments the sandbox exists for. The accepted cost of the
  instant comparison is that wrapping the column in a function can stop the predicate
  being pushed into the Parquet or JSON reader.
- **A pipeline discovered on DuckDB 1.5 failed `SchemaDriftError` on 2.0.** The drift gate
  compared type *names*, and 2.0 infers HUGEINT where 1.5 infers DOUBLE for the same
  integer literal, and TIMESTAMP WITH TIME ZONE where 1.5 infers TIMESTAMP. Upgrading
  DuckDB — no contract edit, no new data — broke a working pipeline on a false positive.
  Those pairs are now treated as one physical type under two names. The widening/narrowing
  classification is unchanged: the family check runs after it, so `type_widened` and
  `type_narrowed` still fire for every ordered numeric pair. Note DOUBLE and HUGEINT are
  not value-identical — 1.5 landed a lossy 17-significant-digit float where 2.0 lands the
  exact 128-bit integer — so the non-failing path says the physical type moved rather than
  passing silently.
- **`fluid init --discover` emitted contracts its own `fluid validate` rejected.** The
  emitter wrote the source's type name straight through, so a discover on DuckDB 2.0
  produced `type: hugeint` and `type: '"null"'` and `fluid validate` then exited 1 on the
  file the tool had just written. Validating every name in DuckDB's own `duckdb_types()`
  against the bundled schema showed **35 rejected names**, not the two reported: `uinteger`
  and `integer[]` were rejected on 1.5 as well, so this was never only a 2.0 problem.
  Discovered types are now canonicalised at the emitter, mapping each rejected name to the
  nearest one the schema accepts and leaving every already-valid name untouched. An
  unsigned or 128-bit type maps to a width that holds its range (`uinteger` → `bigint`,
  `hugeint` → `numeric`) rather than to the similar-sounding name. Contracts already emitted
  with an invalid type stay invalid — the fix is emit-time — but those already failed
  validation, so nothing working changes.

### Known limitations

- **The loopback exemption covers only the engines where this process makes the connection** —
  dlt, duckdb, meltano, and the `embedded` modes of debezium and airbyte. For kafka_connect and
  the `bring-your-own` / `managed` modes of debezium and airbyte, forge-cli does not connect to
  the database at all: it hands the whole connector config, resolved credential included, to the
  orchestrator named by `properties.<engine>.deployment.server_url`. That URL is contract-chosen
  and **this policy does not check it**, so a loopback `connection.host` still clears a send whose
  credential then leaves the machine in an HTTP body. The behaviour is unchanged from before this
  release — nothing regressed — but it is NOT closed, and `deployment.server_url` has no host
  allowlist of the kind `FLUID_COMMAND_CENTER_HOST_ALLOWLIST`, `FLUID_FEDERATION_HOST_ALLOWLIST`
  and `FLUID_WEBHOOK_HOST_ALLOWLIST` already provide elsewhere. Treat an `env://` secretRef on
  those three paths as unprotected whatever the declared host.
- A connection whose only destination field is an object-store URI (`s3://`, `gs://`, `az://`,
  `file://`) is not policed: the authority there is a bucket, not a host, so a host-based policy
  has nothing to decide.
- `{{ env.NAME }}` interpolation in a connection field reads the environment without going through
  `secretRef` at all, so it is not covered by this policy.


- On DuckDB 1.5.x, `lock_configuration` does not hold for logging reached as a table
  function: `SET enable_logging = true` is refused, but `SELECT * FROM enable_logging(...)`
  is accepted and the sandbox's own path refusal is then thrown from DuckDB's logger write
  path as an uncaught C++ exception that aborts the process. It widens no grant, and the
  engine's own SQL never spells it; the exposure is a free-form SQL surface such as the MCP
  output port's `query_sql` under `policy.allow_free_form_sql`. DuckDB 2.0 refuses it
  cleanly.

### Upgrade notes

- **A contract using `env://` against a remote host now fails until the operator allows it.** Each
  one is restored by setting a single operator-environment variable,
  `FLUID_ACQUISITION_SECRET_REF_ALLOWLIST`, to the variable's own name — for example
  `PG_PASSWORD@db.example.com` for that host only, or `PG_PASSWORD` for any host. Set it in the
  shell, in CI, or in a `FLUID_SECRETS_FILE` outside the project — **not** in a project `.env`,
  where it is ignored.
  A host written with a port never matches, so leave the port off. Local development against
  `localhost`, `127.0.0.0/8` or `::1` needs no configuration. The better long-term answer for a
  production credential is a secret store: `vault://`, `aws://`, `gcp://`, `azure://` or `file://`.
- `fluid init --discover` against a remote host emits a contract carrying `env://`, which `fluid
  apply` will now refuse until that variable is allowlisted. The discover hint does not yet print
  the allowlist line; the refusal from `apply` does.

## [0.22.0] — 2026-10-22

### Upgrade notes

- forge-cli runs on DuckDB 2.x as well as 1.5.x (`duckdb>=1.5.0`, no ceiling; DuckDB 2.0
  needs Python 3.11, so a Python 3.10 install stays on 1.5.x). On 2.x, a DuckDB database
  file forge-cli creates (`fluid demo`'s `.fluid/db.duckdb`, the local provider's session
  and `persist` databases, a Meltano acquisition's DuckDB destination) is written in the
  format DuckDB 1.5 writes, which DuckDB 1.0 and later open, so a type that needs a newer
  format, such as a `VARIANT` column, is refused in it on 2.x as it is on 1.5; a file
  that already exists keeps its format.
  DuckDB 2.x's own default format cannot be read by 1.5: a file another 2.x tool created
  (dbt-duckdb and dlt included), or forge-cli 0.21.0 or older on DuckDB 2.0, opens only
  on 2.x. To convert one, run on 2.x: `ATTACH 'old.duckdb' AS src (READ_ONLY); ATTACH
  'new.duckdb' AS dst (STORAGE_VERSION 'v1.0.0'); COPY FROM DATABASE src TO dst;`.
- Contract SQL runs on the DuckDB that is installed, and DuckDB 2.0 changed some SQL: the
  `x -> x + 1` lambda is an error (write `lambda x: x + 1`), and integer `//` and `%` by
  zero raise an error instead of returning NULL (wrap them in `TRY(...)`). See
  https://duckdb.org/docs/2.0/operations_manual/migration.html.
- A DuckDB acquisition build now resolves a Postgres source's `connection.secretRef`. It
  ignored it, and libpq's `PGPASSWORD` or `~/.pgpass` supplied the password, so a
  `secretRef` that does not resolve where `fluid apply` runs now fails the build. To keep
  using `PGPASSWORD`, remove the `secretRef`; an inline `password` still wins over it.
- `fluid validate --env <env> --strict` (on one contract or a whole workspace), `fluid
  contract-validation --env <env> --strict` and `fluid test --env <env> --strict` (the
  native engine; `--engine soda` does not read `--strict`) now fail when an overlay patch
  lands on an entry that names another id (`overlay_patch_retargeted`), which an overlay
  merged by position can do: a 0.7.5 or older contract, or a 0.7.6 base whose entries lack
  unique ids. Without `--strict` these commands list it among their warnings (`fluid test`
  as an "Environment overlay" row, `fluid validate --quiet` as a `WARNING:` line), and the
  loader no longer also logs it there. Put the overlay's entries in the base's order, or
  set `fluidVersion: "0.7.6"` with a unique id on every base entry, so overlays patch by
  id. `overlay_entry_added` stays a log line and does not fail `--strict`. A bundle is
  never re-overlaid, so `fluid bundle --env --format tgz` records the warning in the
  bundle's MANIFEST (`source.overlayWarnings`) and says how many it recorded; `fluid
  validate <bundle>.tgz` lists it, and under `--strict`, which stage 2 of the generated
  pipelines runs, fails on it. A bundle built before this change records none and passes
  as before: rebuild it to have it checked. `fluid contract-validation` and `fluid test`
  do not read the record, so on a bundle they miss the warning: run them on the contract
  file with `--env`.
- `fluid validate --strict` now says `Validation failed: warnings treated as errors
  (--strict)` when warnings alone fail it, after a summary that reads valid; with
  `--quiet` it prints each such warning as `ERROR (--strict): <warning>`.
- `fluid validate <bundle>.tgz` now prints what it prints for a contract file: a bundle
  that passes lists its warnings, not only their count; `--quiet`, which printed nothing,
  prints each error as `ERROR: <finding>`, a recorded overlay warning as `WARNING:
  <finding>`, and under `--strict` each warning as `ERROR (--strict): <finding>`; and
  `--strict` failing on warnings alone says so, as above.
- A product whose every build becomes `continuous` (or runs outside forge-cli) gets no
  `schedule/` artifacts, so pipeline stage 11 skips `fluid schedule-sync`, and a DAG
  deployed for it earlier stays in the scheduler: remove that DAG by hand.
- `fluid scaffold-composer` writes no DAG for a contract whose every build runs outside
  forge-cli (`engine: flink`, `properties.language: flink_sql`) or continuously and that
  declares no `orchestration.tasks`, where it used to write one. A DAG it wrote for such a
  contract before stays in `--out-dir` and in Cloud Composer if you deployed it: delete
  both. The command names the file when it finds it under the name it writes now.
- An environment overlay cannot turn a scheduled build continuous: overlays cannot
  delete the base's `cron` or `schedule`, which a continuous trigger refuses. Declare the
  trigger in each environment's overlay instead of in the base.
- Generated Airflow DAGs run Snowflake SQL through `SQLExecuteQueryOperator`
  (apache-airflow-providers-common-sql, which the snowflake provider installs) with
  `conn_id`, because apache-airflow-providers-snowflake 6.0 removed `SnowflakeOperator`;
  a DAG generated before needs regenerating to load. A GCP `create_table` task uses
  `BigQueryCreateTableOperator` where the google provider has it (13+; 17.0 removed the
  old one), and `BigQueryCreateEmptyTableOperator` before that.
- Both new operators template more of their arguments than the ones they replace:
  `SQLExecuteQueryOperator` renders `conn_id` and `hook_params` (the warehouse and
  database), and `BigQueryCreateTableOperator` renders `table_resource` (the schema). A
  literal `{{`, `{%` or `{#` in a warehouse or database name, or in a schema field such
  as a description, is now rendered as Jinja when the task runs, and fails it. Escape it
  as `{{ '{{' }}`, `{{ '{%' }}` or `{{ '{#' }}`, or wrap the value in `{% raw %}` ...
  `{% endraw %}`.

### Added

- **`trigger.type: continuous` (0.7.6 preview).** A build whose job its platform keeps
  running, such as an `engine: flink` job or a `kafka-connect` or `debezium` connector,
  can say so: `execution.trigger: {type: continuous}`. No scheduler starts it: `fluid
  generate schedule`, `fluid schedule-sync` and the `fluid apply` DAGs leave the build out,
  where a connector used to get a nightly `0 2 * * *` task. A contract with no other build
  has nothing to schedule. The schema refuses `cron`, `schedule`, `datasets`, `event` and
  `timetable` next to it, as Databricks Jobs refuses a continuous trigger next to any
  other, and the error names the field and the trigger type that takes it. It is not
  Airflow's `@continuous`, which starts batch runs back to back. 0.7.5 is unchanged.
  Declare it on a build that runs outside forge-cli or on a connector: on a build forge-cli
  runs as a batch run, `fluid validate` warns and `--strict` fails. The dbt and python
  runners refuse the trigger and say why (the python runner takes every build no other
  runner does, such as one with no engine or a local `spark` script), and an inline-SQL
  build or a `duckdb`, `airbyte`, `meltano` or `dlt` acquisition runs only when `fluid
  apply` runs builds. A continuous `spark`, `glue` or `custom` build without inline SQL
  that names a `runtime.platform` other than `local`, or inline SQL on a platform with no
  embedded-SQL executor, gets no warning: `fluid apply --mode amend-and-build` leaves it
  to that platform, as it does a Flink job.

### Changed

- A schema error from a `not: {required: [...]}` rule now reads `<field> is not allowed
  here`, or the rule's own `description`, instead of quoting the whole object and the
  subschema it must not match.
- `fluid scaffold-composer` takes the DAG's cron from the first build a scheduler starts,
  so a Flink job's own cron no longer schedules it, and writes no DAG for a contract whose
  every build runs outside forge-cli or continuously, unless it declares
  `orchestration.tasks`: the test `fluid generate schedule` applies before it writes no
  files.

### Fixed

- **DuckDB 2.0.** `fluid verify` reported every local file as `error` ("Relations no
  longer have fetchone()"); `fluid apply`'s local SQL and materialize steps, and the
  actions of the legacy `fluid_build.contract_tests` module, lost their row counts (a
  materialize step logged `local_materialize_fallback`, a `contract_tests` action
  reported `-1` rows). DuckDB 2.0 relations have no `fetchone()`, and a query result
  keeps its connection busy until it is read to the end, which fails the next step on
  that connection. Every DuckDB result is now read to the end, the same way on 1.5.x and
  2.x.
- **DuckDB 2.0: Postgres and MySQL sources.** A DuckDB acquisition build reading
  Postgres failed on DuckDB 2.0 (`Table Function with name postgres_scan does not
  exist!`): the postgres extension dropped `postgres_scan`. A MySQL or MariaDB source
  failed on every DuckDB version, before connecting (`Unknown secret storage found:
  'local_file'`): its `ATTACH` looked for a default secret in the persistent storage the
  sandbox turns off. Both are now attached read-only before the sandbox locks, each with
  an empty temporary secret named in the `ATTACH` (the DSN carries the credentials), and
  read as `<alias>.<schema>.<table>` on 1.5.x and 2.x; a Postgres source's
  `connection.secretRef` is resolved into the password, as a MySQL source's already was.
  A Postgres enum column still reads, and lands, as text. A stream name is looked up as
  DuckDB looks up names, ignoring case: where two tables' names differ only in case, the
  build can read the other one.
  A source the runner cannot attach (unreachable, refused, a `secretRef` that does not
  resolve) fails the build with the reason, its password masked, and writes a run
  record; a MySQL or MariaDB source used to end `fluid apply` with `Unexpected error
  during execution` instead.
- A local SQL action, or a legacy `fluid_build.contract_tests` action, whose SQL ends in
  a statement that returns no rows (`CREATE TABLE ... AS`, an `INSERT` without
  `RETURNING`) now fails with a message saying to write just the `SELECT`. DuckDB 1.5
  failed there on `'NoneType' object has no attribute 'write_csv'`, and DuckDB 2.0
  reported success with a one-row `Count` file, an empty `Success` file (a `CREATE TABLE
  ... AS PIVOT`) or the rows of an earlier `SELECT` in the same SQL as the output.
- **DuckDB 2.0: contract SQL.** A local SQL action whose SQL ends in a comment after its
  last `;` failed to write its output (`syntax error at or near ";"`); SQL with a
  statement that does not parse ran the statements ahead of it before failing, where
  DuckDB 1.5 runs none; and the result of a `PIVOT` without an `IN` list, or of a query
  calling `nextval()`, could not be written to an `s3://` output or, in
  `fluid_build.contract_tests`, to Parquet (`Cannot create the view ...: it reads rows
  held in this process`). Each now behaves as on 1.5.
- **`fluid test` and `fluid contract-validation` print the contract's text as written.**
  `fluid test` read its report as Rich markup, so a JSON Schema pattern such as
  `^[a-z0-9][a-z0-9-]*[a-z0-9]$` printed as `^*$` and `exposes[orders]` lost its id; in
  both commands an id holding `[/]` crashed the command with a `MarkupError` traceback.
  The soda engine's check names and stderr, the `❌` lines printed by the CLI's shared
  error helper, and the `Unexpected error:` line print as written too, so an exception
  that quotes `[/]` no longer ends in a second traceback. That line, the `Provider
  error:` line and the `--debug` traceback are now redacted, as the `❌` lines already
  were. In all of these a `:name:` code stays text
  (`fe80::a:b:c` showed an emoji) and a control character prints as `\x1b` and the like,
  so an escape sequence in a contract or an error message can no longer clear the
  terminal or plant a link. JSON and JUnit output are unchanged.

- **Generated Airflow DAGs load on Airflow 3.** `fluid export --engine airflow`, `fluid
  generate schedule`, `fluid init` and the AWS, GCP and Snowflake code generators emitted
  `DAG(schedule_interval=...)` and `start_date=days_ago(1)`, which Airflow 3 removed, so
  their DAGs failed to import. They now pass `schedule=` with a fixed `start_date` and
  `catchup=False`. Every generator, `fluid scaffold-composer` and `fluid schedule-sync`
  included, imports `DAG`, `dag`/`task` and the core operators and sensors from their
  Airflow 3 homes (`airflow.sdk`, apache-airflow-providers-standard), falling back to the
  Airflow 2 paths, so the same DAG loads on Airflow 2.7+ and 3.x without the deprecation
  warnings Airflow 3 gives the old import paths.
- The AWS TaskFlow DAG wired its `@task` functions (`load >> publish`), which raised
  `TypeError` when Airflow parsed it; it now wires each task's result, and an operator
  task as itself.
- The GCP DAG imports the Dataflow operator only when a task uses it: its module needs
  apache-airflow-providers-apache-beam, and the unused import stopped every GCP DAG from
  loading where that provider was absent.
- `fluid init`'s basic DAG spliced `orchestration.retries` and `retry_delay` into the DAG
  source as text, so a contract value ran as Python when Airflow parsed the DAG. Both are
  now read as numbers (retries 0 to 100, the delay in whole minutes), falling back to 3
  and 5.
- **A python task no longer loses the Snowflake orchestration DAG.** `generate_airflow_dag`
  in `providers/snowflake/orchestration`, a library function no `fluid` command calls
  (`fluid export` uses `providers/snowflake/codegen`), emitted `python_callable=<callable>`
  for a function it never defined, so Airflow raised `NameError` parsing the file and
  dropped the whole DAG. It now defines a stub of that callable above the task: the DAG
  loads, and the task fails with `AirflowFailException`, naming itself and skipping the
  retries a stub cannot pass, until the function is written. A callable whose name the DAG
  already uses (`dag`, an operator class, an earlier task) or a `__dunder__` name is refused
  with a `ValueError` when the DAG is generated.
- **The `fluid init` DAG helper writes what `fluid generate schedule` writes, or nothing.**
  `generate_dag_for_project` ran `fluid generate-airflow --output-dir`, a flag that command
  does not have, so it always wrote a basic validate/plan/apply DAG, or no DAG when `fluid`
  was not on PATH. It now runs `fluid generate schedule --scheduler airflow --overwrite`
  in-process through the CLI's own parser, and its `dags/README.md` names the DAGs that run
  wrote and says to edit the contract, not the DAG. It writes no DAG, and says why, when the
  contract schedules nothing (no build with a cron trigger and no `orchestration.tasks`, as
  with customer-360's `manual` build) or when the command refuses the contract, an invalid
  cron for instance, instead of a basic DAG on a schedule the contract never declared.
  `create_basic_dag`, no longer a fallback, passes `fluid apply --yes` instead of the
  rejected `--auto-approve` and quotes the project path. No CLI command reaches the helper
  with DAGs enabled today: `fluid demo` turns them off.

## [0.21.0] — 2026-10-19

### Upgrade notes

- The YAML guard has new caps for every file it reads, FLUID contracts included: a file
  is refused when it nests more than 128 levels deep, or when its aliases add more than
  100,000 nodes or 5,242,880 characters of text, or grow it more than 100-fold past 1,000
  nodes. The 5 MiB size cap now counts UTF-8 bytes for text as well as for bytes. ODCS and
  ODPS files now go through the guard at all, so `fluid odps import`/`validate`, `fluid
  odcs validate`/`import`, `fluid odps-bitol validate`, the `contractId` resolver and
  `fluid forge --seed-from` also refuse a YAML file over 5 MiB or with more than 50
  aliases, the caps FLUID contracts already had. Write the repeated blocks out instead of
  reusing them through anchors. A bundle directory without a readable ODPS doc that holds
  a file the guard refuses, or one that does not parse, fails instead of importing as
  ODCS-only.
- A file the resolver probes for a port's `contractId` must now declare that `contractId`
  as its `id`. A bundle that relied on a file named after the port's `contractId` but
  declaring another id fails a strict import, or skips the port under `--lenient` (an
  input port is always skipped); make the port's `contractId` and the ODCS `id` agree.
- In a contract whose `fluidVersion` is 0.7.6 (the schema 0.7.6 preview) or later, an
  overlay patches `builds`, `exposes` and `consumes` by identity, not by position. An
  overlay that names each entry's id merges as before when it lists them in the base's
  order; one that names an id at another position now patches the entry with that id, and
  an entry whose id the base lacks is appended and logged as `overlay_entry_added`. When
  the base's entries carry unique ids, an overlay entry with no id, or two naming the same
  id, now fails the load (`contract_load_failed`): give each the id of the entry it
  patches. Id-less patches still apply to a base with at most one entry, as long as no
  overlay entry for it names an id. A 0.7.5 or older contract still merges by position.
- A build with `properties.language: flink_sql` is no longer run as SQL of forge-cli's own.
  `fluid apply --mode amend-and-build` ran its Flink SQL on DuckDB, or refused an unknown
  embedded-SQL platform, and the local, AWS, GCP and Snowflake planners planned it as a
  DuckDB, Athena, BigQuery or Snowflake statement. It is now treated like `engine: flink`
  (below): reported as run outside forge-cli, planned without SQL, and left out of
  generated schedules. Declare `engine: flink` with `fluidVersion: "0.7.6"` to name the
  engine too.

### Added

- **`engine: flink` (0.7.6 preview).** A build can declare Apache Flink as its engine,
  usually a long-running Flink SQL job with `properties.language: flink_sql`; the contract
  must say `fluidVersion: "0.7.6"`. forge-cli validates and plans it but ships no runner:
  the job runs on the Flink Kubernetes Operator, AWS Managed Service for Apache Flink, or
  whatever platform runs it. `fluid apply --mode amend-and-build` reports such a build as
  run outside forge-cli and counts it on its own summary line, so a contract whose only
  builds run elsewhere is not "every build was skipped"; the run report records it as
  `skipped` with `reason: runs_outside_forge`. No provider planner turns its SQL into a
  DuckDB, Athena, BigQuery or Snowflake action, and the local planner does not land an
  expose it writes; when a contract mixes such a build with a local one and does not say
  through `builds[].outputs` which writes which expose, the local planner lands neither
  candidate and warns rather than guess. `fluid generate schedule` gives it no task or DAG
  (a contract with no other build, and no hand-declared orchestration tasks, has nothing
  to schedule and exits 0), and `fluid generate transformation` says there is no code to
  generate for it. Deployment settings (parallelism, upgrade mode, Flink configuration) go
  under `properties.parameters`. 0.7.5 is unchanged.
- **Overlays merge `builds`, `exposes` and `consumes` by identity (schema 0.7.6 preview).**
  Lists of maps merged by position, so inserting or reordering a build in the base silently
  retargeted every overlay: with `builds: [ingest, transform]` and `overlays/prod.yaml`
  raising `builds[1]`, adding a `backfill` build first made prod patch `ingest`, copying
  `id: transform` onto it, and `fluid plan --env prod` failed with `planner_failed:
  'schedule_transform'`. From `fluidVersion` 0.7.6, `builds[].id`, `exposes[].exposeId`
  and `consumes[]`'s `productId` + `exposeId` match an overlay entry to its base entry, as
  Kubernetes server-side apply matches list entries by their list-map keys. An entry that
  matches nothing is appended and logged as `overlay_entry_added`, which names the base's
  ids, so a typo shows. An overlay entry the merge cannot match (no id, or an id named
  twice) fails the load with the index, where falling back to position would retarget it;
  a base whose own ids are missing or repeated keeps the positional merge and logs
  `overlay_merged_by_position`. Lists inside an entry, such as an expose's schema, still
  merge by position. Every command that applies an overlay, and the contract API, merges
  this way.
- **Older contracts warn when a positional patch lands on another entry.** Before 0.7.6,
  an overlay entry that names an id (or part of a `consumes` key) other than the base
  entry at its position logs `overlay_patch_retargeted`, naming both, and the merge is
  unchanged. Each merge warning names its contract and is logged once per process, so a
  command that loads the contract twice says it once.

### Fixed

- **A local expose lands the last build that produced a table.** The local planner took
  the output of the contract's last build, and when that build produced none here (a dbt
  model, a Flink job) it fell back to the first input it had loaded and landed it raw in
  the expose, columns the expose does not declare included. It now takes the last build
  whose output it has.
- **`fluid forge --seed-from` catches a dropped expose.** When the model left out an
  expose the seed declares, the ground-truth guard raised `IndexError`, logged
  `structural_seed_guard_failed` and kept the generated contract, so the expose was lost
  without an error. A missing expose is now a seed mismatch, which the repair loop feeds
  back to the model on its next attempt.
- **A malformed or mismatched `contractId` no longer breaks an ODPS import.** A
  `contractId` with a NUL byte, a value that is not a string, a segment longer than a file
  name may be, or an unparsable URL such as `http://[::1` made `fluid odps import` fail
  with `ValueError`, `TypeError` or `OSError`, even with `--lenient` and on input ports,
  which always import leniently. It is now an unresolved contract: `--lenient` and input
  ports skip it with a warning, and a strict import stops with an unresolved-contract
  error (`not a valid contractId` for a NUL byte or a value that is not a string, `false`
  and `0` included, which a strict import used to skip; an empty or missing one is
  ignored, as before). A file the resolver probes
  for a `contractId`, such as `orders.odcs.yaml` for `commerce.orders`, is now used only
  when it declares that `id`; a file that declares another contract is skipped with a
  warning naming both ids instead of being linked to the port.
- **`fluid odps import <dir>` no longer drops a product doc it cannot read.** A product
  file with a YAML syntax error, or with an upper-case suffix such as `PRODUCT.ODPS.YAML`
  (which the ODCS reader parsed as JSON), was skipped without a word, and the directory
  imported as ODCS-only. Every ODCS/ODPS reader now reads an upper-case `.YAML`/`.YML`
  suffix as YAML, and a directory file that does not parse is skipped with a warning
  naming it; a directory left without a readable ODPS doc fails.
- **`fluid generate artifacts` reports a contract it cannot read.** With `--emit
  schedule` alone, a contract that did not parse skipped the only emitter and exited 0
  with no files; a contract that was not UTF-8 or not a mapping ended in "Unexpected
  error". Each is now a `generate_artifacts_failed` error naming the file.
- **`fluid odcs validate --report` writes the report when the YAML does not parse.** The
  command printed the failure and exited 1 without the report, so a CI gate reading it
  found no file; the report now records a failed `parse` pass.

### Security

- **The YAML guard caps what aliases expand to, in nodes and in text, and how deep a file
  nests.** It allowed 50 aliases, which is not a bound: 22 levels of `[*a, *a]` are 44
  aliases that expand to 25,165,822 nodes, and a 685-byte contract shaped like that held
  `fluid validate` for more than 90 seconds. A document is now also refused when its
  aliases add more than 100,000 nodes or 5,242,880 characters of text (a
  1,000,000-character string repeated through two nested lists of four aliases adds
  20,000,000), or grow it more than 100-fold once it is past 1,000 nodes, and the error
  names the sizes. A file nested more than 128 levels deep is refused before PyYAML scans
  it to the end (a 64 KB file nested 32,000 deep took over 40 seconds) or fails with
  `RecursionError`, and a stream is read only up to the 5 MiB cap. The ODCS and ODPS
  readers had no cap at all: `fluid odps import`, the `contractId` resolver (fetched
  contracts included), `fluid forge --seed-from`, `fluid odps-bitol validate` and `fluid
  odcs validate`/`import` read YAML with a plain `yaml.safe_load`. They now use the same
  guard and report a refused file as a failed validation or import, and the OpenAPI
  validator, the artifact fan-out and the federation manifest reader report it as the
  unreadable file it is instead of failing with `UnsafeYamlError`.
- **A committed `.env` can no longer point `REKA_API_BASE` at the FLUID LLM key.**
  litellm 1.106.0 added Reka to its registry of OpenAI-compatible providers, read
  from `REKA_API_BASE`, and the vendored list of litellm endpoints did not have it.
  With `FLUID_LLM_API_KEY` (or the keyring key) in use, a project's `REKA_API_BASE`
  is now ignored, as every other litellm provider endpoint is.

## [0.20.0] — 2026-10-15

### Security advisories

This release fixes six security advisories. Each entry below has its own upgrade notes; read them before upgrading.

#### A — project configuration trust (fix/sec-project-config-trust) — [GHSA-vw85-xrwh-3p3x](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-vw85-xrwh-3p3x)

**Security**
- **A config file in the working directory is no longer trusted like your own config**.
  A catalog endpoint set by `.fluidrc.yaml`, `.fluidrc`,
  `fluid.config.yaml` or `.fluid/config.yaml` in the checkout (Command Center, Data Mesh
  Manager, DataHub, OpenMetadata or a plug-in catalog) is sent only a key that the same
  file carries where that catalog reads one. It does not get `FLUID_API_KEY`,
  `FLUID_BEARER_TOKEN`, `DMM_API_KEY` or any other key from the environment,
  `/etc/fluid` or `~/.fluid`. When something of yours would have to be withheld from such
  a block, `fluid publish` fails, `fluid apply` prints `run not reported (...)`, and a
  `properties.catalog.register` target is reported as not registered; each message names
  what was withheld, never its value, and how to use the endpoint. Previously the
  checkout's endpoint was paired with your key.
- `${VAR}` placeholders in a project config's catalog settings are kept as written; when
  the variable is set, a warning names the setting and the file. Before, they were filled
  from the environment, so a committed endpoint could carry any variable to its host. A
  contract's `{{ env.NAME }}` placeholders are also sent as written to an endpoint from a
  project config.
- From your own config files and environment, an endpoint from a project config gets only
  a fixed list of non-secret settings (`organization_id`, `organization`,
  `data_product_specification`, `provider_hint`, `odps_lineage_mode`,
  `auto_approve_access`, `auth.type`, `enabled`, `force_contract_sync`, `timeout`,
  `max_retries`, `retry_delay` and the circuit-breaker settings). Any other value, at any
  depth, is withheld, and the call refuses and names it. Where the project file carries
  its own key, the environment's credential and an operator credential setting that is
  `null`, blank, or only a `${NAME}` whose variable is unset or empty are left out without
  a refusal. A key the project file sets to anything but a non-empty string (`""`,
  `null`, `false`, `0`, `[]`, `{}`, an unquoted number or date) is no key, so it can no
  longer let the runner's key fill the slot.
- An endpoint from a project config must be `https://` unless its host is loopback or on
  `FLUID_COMMAND_CENTER_HOST_ALLOWLIST`, and a host that resolves to a private or
  cloud-metadata address must be on that list, for `fluid publish --target` and
  `properties.catalog.register` alike. `fluid publish` had no host check before.
- The `fluid apply` Command Center report applies the same host check to every endpoint,
  follows a redirect only within the endpoint's origin (or from http to https on the same
  host and default ports), and names the destination as `scheme://host[:port]`, including
  in `run <id> reported to ...`. `X-API-Key` used to follow a redirect to any host.
- `fluid market`: an `mcp` block in a `market.yaml` read from the working directory (when
  there is no `~/.fluid/market.yaml`) can no longer use `token_env`, `env_from`, `command`
  or the `stdio` transport, and its `url` must pass the host check. Such a block could send
  any environment variable to its own URL or start a program. An Apache Atlas block from
  that file no longer falls back to `ATLAS_USERNAME` / `ATLAS_PASSWORD`. The Command Center
  URL for `fluid market` and `--blueprints` comes from `FLUID_COMMAND_CENTER_URL`,
  `~/.fluid/config.yaml` or the built-in `http://localhost:8000` default, never from
  `./fluid.yaml`; it passes the host check, and the client's probes, its marketplace
  requests and the market Command Center catalog connector do not follow a redirect to
  another origin, other than the same-host upgrade to https.
- A YAML anchor, alias or merge key in a config file shares a value, not a setting, so a
  project config cannot write into one of your catalog blocks through an alias. A config
  file or `market.yaml` whose aliases or merge keys would expand far beyond its size is
  refused, using go-yaml's "excessive aliasing" limit.

**Upgrade notes**
- If a committed project config names a catalog endpoint and relies on a key from the
  environment or `~/.fluid`, set the endpoint yourself instead: `FLUID_CC_ENDPOINT`,
  `DMM_API_URL`, `DATAHUB_GMS_URL` or `OPENMETADATA_SERVER_URL`, the endpoint in
  `~/.fluid/config.yaml`, or `fluid publish --target <name>:<endpoint>`. Move `${VAR}`
  placeholders out of project configs the same way.
- A project config that carries both its endpoint and its own key now sends that key even
  when the runner exports one.
- The `fluid apply` Command Center report and `fluid market`'s Command Center detection now
  need `https://` for every host that is not loopback or on
  `FLUID_COMMAND_CENTER_HOST_ALLOWLIST`, your own endpoint included. `fluid publish` to an
  endpoint you set still has no host check.
- If `~/.fluid/config.yaml` or `/etc/fluid/config.yaml` sets anything outside the list
  above in a catalog block whose endpoint comes from a checkout's project config, and the
  checkout's file does not set that setting itself, that block is refused (`fluid publish`
  fails, `fluid apply` does not report the run, a `properties.catalog.register` target is
  not registered) and the message names it. That includes a literal key, even next to a
  checkout that carries its own. When the checkout's block carries its own key, a
  credential setting that is `null`, blank, or only a `${NAME}` whose variable is unset or
  empty does not count, so the Data Mesh Manager provider README's block does not cause a
  refusal.
- Quote credentials. In a project config, an unquoted number, date or boolean is no key
  for an endpoint that file sets; a Command Center `auth.api_key` or `auth.token` written
  that way is not sent to your own endpoint either, and `FLUID_API_KEY` /
  `FLUID_BEARER_TOKEN`, if set, is sent instead. A non-zero number, a date or `true` gets a
  warning to quote it, except in a setting that an exported variable also fills for an
  endpoint the file sets (for example `FLUID_API_KEY` for the Command Center's
  `auth.api_key`, `DMM_API_KEY` for the Data Mesh Manager's key, `DATAHUB_GMS_TOKEN` or
  `OPENMETADATA_JWT_TOKEN` for their `api_token`): that endpoint is refused without the
  explanation. In your own config files an unquoted number or date is sent as YAML reads
  it, with a warning to quote it.
- An unquoted `true`, `yes` or `on` as a Data Mesh Manager, DataHub or OpenMetadata token is
  no longer sent as `Bearer True`; the next token setting applies, such as the
  `auth.api_key` that `DMM_API_KEY` fills.
- `fluid market`: move an `mcp` block that uses `token_env`, `env_from`, `command` or
  `stdio` from `./market.yaml` to `~/.fluid/market.yaml`. `command_center.url` in
  `./fluid.yaml` is no longer read; use `FLUID_COMMAND_CENTER_URL` or
  `~/.fluid/config.yaml`. The `--instantiate` POST, which carries your parameters, to a
  blueprint registry that moved host now stops at the old origin, so the command fails;
  searches, `--blueprint-id` lookups and the lookup before that POST in a registry you name
  with `FLUID_API_URL` (or `FLUID_PUBLIC_REGISTRY` with `FLUID_MARKETPLACE_FALLBACK=public`)
  still follow it, unless the request carries a credential header or a cookie (for example
  an `Authorization` header from `~/.netrc`), in which case it stops at the registry's
  origin like the POST.
- Library callers: `FluidConfig.get_catalog_config(name)` raises `UntrustedCatalogEndpoint`
  instead of returning a block it would have to strip, takes a keyword-only `endpoint=`
  override and returns a `CatalogConfig` (a `dict` with `project_endpoint_file`).
  `get_auth_headers` takes a keyword-only `env_fallback` (default `True`).
  `DataMeshManagerRegistrar(api_url=...)` no longer fills its token from `DMM_API_KEY`
  when it is given an endpoint.

#### B — untrusted-checkout files: workspace roots and project `.env` (fix/sec-workspace-root-ceiling, fix/sec-dotenv-protected-vars) — [GHSA-58px-v64p-6ppm](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-58px-v64p-6ppm)

**Security**
- **A `fluid.workspace.yaml` above the project (for example one `fluid init`
  wrote into `~`) no longer widens the DuckDB sandbox when it sits in a broad
  directory or above the contract's repository**. A
  workspace file at `$HOME`, above `$HOME`, at another account's home or a
  home-holding directory (`/home`, `/Users`, `/var/home`), at a system temp
  directory (`/tmp`, `/var/tmp`, `$TMPDIR`, `/private/var/folders`,
  `/dev/shm`) or a directory that holds one, or at `/`, is found but not granted to contract SQL, and
  neither is one above the contract's version-control repository (`.git`,
  `.hg`), so a repository cloned below a shared workspace file does not inherit
  it. The build runs with the contract's own directory and logs
  `local_workspace_not_granted`, naming the directory and the fix. A contract
  saved directly in one of those broad directories is refused before any SQL
  runs. Paths are compared after resolving symlinks, and without case on macOS
  and Windows. Previously a workspace at any of these but `/` opened its whole
  tree to any contract's embedded SQL (one at `/` failed the build).
- **A project `.env`, `.env.<environment>` and `.env.local` can no longer set
  operator-only settings.** These are variables that widen a sandbox or
  confinement root (`FLUID_DUCKDB_ALLOWED_DIRS`, `FLUID_REF_ROOT`,
  `FLUID_UPSTREAM_CONTRACTS`, …); move where FLUID reads trusted configuration
  or writes state (`FLUID_SECRETS_FILE`, `FLUID_STATE_BACKEND`, `HOME`, the XDG
  and Windows `APPDATA` / `LOCALAPPDATA` / `ProgramData` directories, the
  temporary directory); change an authentication or trust mode; change where
  data and credentials are sent (the Command Center, catalog, lineage,
  telemetry and LLM endpoints, proxies and CA bundles, `PGHOSTADDR`, the libpq
  service variables, `PGSSLMODE` and the libpq CA lists, the litellm proxy and
  GitHub Copilot settings); or choose which program runs (`PATH`,
  `PYTHONPATH`, `LD_*`, `ComSpec`, `SystemRoot`, `windir`, `GIT_SSH_COMMAND`,
  `GIT_CONFIG_*`, `GIT_DIR`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, …). `FLUID_DBZ_*`
  is protected as a prefix (the Debezium runner sets those itself).
  `fluid apply`, `verify` and `publish` skip them with one warning that names
  them, never their values; credentials in `.env` still load. The list lives in
  `fluid_build/credentials/protected_env.py`, an unlisted `FLUID_*` whose shape
  says it is an endpoint, location, allow-list or program is protected anyway,
  and a test fails on any `FLUID_*` name that appears as a literal in the
  package and is left unclassified.
- **A project `.env` can no longer point a credential of yours for a listed
  service at a host the project chose.** An endpoint such as
  `OPENAI_BASE_URL`, `PGHOST`, `SNOWFLAKE_ACCOUNT`, `DATABRICKS_HOST` or a dlt
  destination's host is ignored when a credential of the same service is in
  your environment, your
  `FLUID_SECRETS_FILE`, or — for a service that also reads a keyring, a stored
  CLI login, an SDK config file or cloud default credentials — whenever the
  project files do not supply the credential every reader sends in its place.
  Previously the SDK, driver or data loader sent your credential to the host
  the project chose. A decoy credential of the right service does not pair the
  endpoint, a project name that differs from yours only in case no longer
  loads, and while a Snowflake account from the project's files is in effect
  every Snowflake reader (the resolver, OpenTofu, the hosted MCP server, dlt)
  takes credentials only from the environment and the project files; a
  Snowflake authenticator other than `snowflake` the project sets is refused,
  so it cannot sign in with your browser session or identity provider.
- **A dlt destination whose credentials a project `.env` touches gets the
  project's credentials only.** Every field of the destination's dlt
  `credentials` configuration that the project's files set pins the
  destination — at any depth, under every name dlt reads it under, the `query`
  map and its single keys included — for every destination type (default-deny),
  except the in-account fields `database`, `connect_timeout`, `client_encoding`
  and `session_timezone` (postgres, redshift) and `database`, `warehouse` and
  `role` (snowflake). `PGHOSTADDR` is yours alone, which closes every libpq
  reader at once. A destination you exported (its own dlt host, or a connection
  string naming a host) stays yours against the project's FLUID endpoint
  variables (`POSTGRES_HOST`, `SNOWFLAKE_ACCOUNT`, `REDSHIFT_HOST`); a dlt
  credentials field the project sets still pins it. The FLUID-to-dlt bridge's
  copies from one build no longer count as yours for the next build in the
  same `fluid apply`.
- **dbt and litellm no longer re-read the project `.env` around the filter.**
  `fluid` sets `PYTHON_DOTENV_DISABLED` at start-up and once it has read a
  project's files, and forwards it to the dbt container; a scheduled apply
  keeps a `PYTHON_DOTENV_DISABLED` the Airflow worker's operator set. A python
  build step is your own code, so it still loads its own dotenv file.
- Project `.env` values are read literally: `${VAR}` is no longer expanded from
  your environment.

**Upgrade notes**
- If your `fluid.workspace.yaml` lives in your home or a temp directory and
  your products read each other's files by path, move it into the directory
  that holds the products when they share one repository or sit in no
  repository. For products cloned side by side, list their common directory
  in `FLUID_DUCKDB_ALLOWED_DIRS` and declare the file; do the same to let
  contracts read a directory outside any project. Move a contract saved
  straight in `$HOME` or `/tmp` into a directory of its own, or allow-list its
  directory. A `fluid.workspace.yaml` with no version-control
  root between it and a contract still grants its whole tree, so put only
  directories you trust below one.
- If your project `.env` set a protected variable (for example
  `FLUID_COMMAND_CENTER_URL`, `DMM_API_URL`, `HTTPS_PROXY`,
  `FLUID_STATE_BACKEND`, `FLUID_DUCKDB_ALLOWED_DIRS`, `PGHOSTADDR` or a git
  transport variable), `fluid` now prints `Ignored ... from project dotenv
  files` and runs without it. Export it in your shell or CI job, or in a
  `FLUID_SECRETS_FILE` you export; a `FLUID_SECRETS_FILE` path set in `.env` is
  itself ignored.
- If CI or your shell exports a credential while a committed `.env` names the
  matching endpoint (`OPENAI_BASE_URL`, `PGHOST`, `SNOWFLAKE_ACCOUNT`, a dlt
  destination's host, …), that endpoint is now ignored. Set it in the CI
  environment, the shell or `FLUID_SECRETS_FILE`. A project that brings its own
  endpoint and its own credential still works when your environment and
  `FLUID_SECRETS_FILE` hold no credential of that service.
- A dlt destination whose host, port, query field, password or any other
  credentials field a committed `.env` sets now uses the project's own
  credentials or fails, naming what is missing. The in-account fields above are
  still read. Export the endpoint to use your own dlt configuration.
- With dbt-core 1.12 or later, dbt no longer finds a `.env` above the
  contract, and litellm no longer loads one at import. Export the variable (also list its name in
  `FLUID_DBT_FORWARD_ENV` for the dbt container), keep the `.env` in the
  directory you run `fluid` from, or set `PYTHON_DOTENV_DISABLED=0` for a
  trusted checkout. The minimum `python-dotenv` is now 1.2.0 and the minimum
  `litellm` 1.83.14.
- Library callers: `DotEnvCredentialStore(...)` takes a keyword-only
  `operator_env=`, `DotEnvCredentialStore.load()` applies the filter and sets
  `PYTHON_DOTENV_DISABLED` once it reads a project file, and `hydrate_dotenv`
  exports a `FLUID_SECRETS_FILE` itself rather than through `load_dotenv`.

#### C — MCP host identity gate (fix/sec-mcp-metadata-service-gate) — [GHSA-m7j5-jcrq-cmxm](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-m7j5-jcrq-cmxm)

**Security**
- **Using this host's cloud metadata-service or ADC identity is the operator's decision in
  `fluid mcp serve`.** The source-catalog tools (`list_source_tables`,
  `inspect_source_table`, `list_source_lineage`, `list_source_glossary`,
  `forge_from_source`) no longer let an MCP client use the host's cloud metadata-service /
  ADC identity. For a catalog source, the client's `allow_metadata_service`
  argument is honoured only when the server runs with the new `--allow-metadata-service`
  flag or `FLUID_ALLOW_METADATA_SERVICE=1`, and is otherwise refused with a tool error that
  names the flag; a JDBC `forge_from_source` ignores the argument and is classified by its
  URI (see below). The credential resolver behind these tools and
  `fluid forge data-model from-source` also no longer turns a bare region or project
  variable into the host's instance role, workload identity or gcloud ADC without that
  opt-in.
- With `--allow-inline-credentials` alone, an inline catalog block is accepted only when it
  carries non-blank credential material of its own as values (see docs/MCP.md for the
  forms per catalog). A path to a file on the server, a named AWS profile, an auth method
  that uses the host's identity or a login on the host, a DataHub server that is not an
  http(s) URL, `DATAHUB_CLIENT_CERT_PATH`, or a netrc file on the host (DataHub, Unity) now
  also needs `--allow-metadata-service`. A blank or whitespace-only key, secret, token or
  credential-file path is not credential material anywhere: inline blocks, saved sources and
  the CLI. An accepted Unity `pat` block uses its token even when the server's
  `DATABRICKS_AUTH_TYPE` or `~/.databrickscfg` profile names another auth type, and an
  accepted Snowflake block no longer probes the server's cloud identity.
- `forge_from_source` with a postgres, mysql or sqlite `uri` now needs an operator opt-in:
  `--allow-inline-credentials` for a password in the URI, and `--allow-metadata-service`
  for a URI with no password, no user name, a sqlite path, a mysql `localhost` or a postgres
  host that is this server's unix socket. The scheme must match the source. A postgres
  connection with a client-supplied password no longer presents the server's default
  client certificate, and for a postgres or mysql connection with a client-supplied
  password the server's `PGPASSWORD`, `PGPASSFILE`, `PGSERVICE`, `PGSERVICEFILE`,
  `PGSYSCONFDIR`, `PGSSLCERT`, `PGSSLKEY`, `PGOAUTHTOKEN`, `PGGSSDELEGATION` and
  `MYSQL_PWD` / `MYSQL_HOST` / `MYSQL_TCP_PORT` / `MYSQL_UNIX_PORT` /
  `MYSQL_GROUP_SUFFIX` variables are hidden while it attaches.
- DuckDB `ATTACH` strings are quoted in each driver's own syntax (libpq for postgres, double
  quotes for DuckDB's mysql DSN) inside one SQL literal. That covers MCP
  `forge_from_source`, `fluid init --discover`, `fluid forge data-model from-source`,
  `fetch_sample_rows`, and the build runner's mysql and mariadb sources. Control characters
  in a host, user, password or database are refused there and in the build runner's
  `postgres_scan` connection string.
- Connect errors from `fluid forge data-model from-source`, `forge_from_source` and the
  build runner's mysql or mariadb `ATTACH` no longer show the password. Neither does the
  error for a JDBC URI that cannot be used in `fluid forge data-model from-source`,
  `forge_from_source` or `fetch_sample_rows`, which now masks the password where it sits in
  the URI.

**Upgrade notes**
- MCP clients that send `allow_metadata_service: true` for a catalog source now get an error
  unless the server runs with `--allow-metadata-service` (or
  `FLUID_ALLOW_METADATA_SERVICE=1`); a JDBC `forge_from_source` ignores the argument and is
  classified by its URI. Add the flag to the server's `args` in your MCP client config if
  you rely on instance-role, workload-identity or ADC auth.
- A catalog read that relied on `AWS_REGION` / `AWS_DEFAULT_REGION` alone (Glue),
  `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` alone (BigQuery), or a project variable plus
  `DATAPLEX_LOCATION` / `GOOGLE_CLOUD_LOCATION` with no `GOOGLE_APPLICATION_CREDENTIALS`
  (Dataplex) now fails with "No credentials resolved" unless you pass
  `--allow-metadata-service` or set `FLUID_ALLOW_METADATA_SERVICE=1`. This includes
  `fluid forge data-model from-source`. Over MCP with the flag (not the variable), the call
  must also send `allow_metadata_service: true`.
  `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`,
  `GOOGLE_APPLICATION_CREDENTIALS` and saved `credential_id` sources resolve as before.
- `fluid mcp serve --allow-inline-credentials`: BigQuery and Dataplex inline blocks, Glue
  blocks with a `profile_name` or an auth method other than `iam_key`, Snowflake
  `private_key` and `sso`, Unity `azure_ad` and `google_id`, DataHub `none` and `oauth`, and
  DataHub or Unity blocks on a host with a netrc file now need `--allow-metadata-service` or
  a saved `credentials.credential_id`.
- A saved source or CLI credential with a blank key, secret, token or path that its auth
  method needs now fails that auth method's check; a Glue key and secret that are both
  blank no longer let boto3 fall back to the host's identity.
- MCP `forge_from_source` with a JDBC `uri`: a password in the URI needs
  `--allow-inline-credentials`; a URI with no password, no user name, a sqlite path, a mysql
  `localhost` or a postgres socket host needs `--allow-metadata-service` (plus the inline
  flag when it carries a password). Use `127.0.0.1` for mysql over TCP. `jdbc:`,
  `mariadb://` and `mysql+pymysql://` URIs are refused. A postgres database that requires a
  client certificate refuses an MCP connection whose URI carries a password; run
  `fluid forge data-model from-source` from the CLI instead.
- A `?` in a mysql host, user, password or database, a `/` in a mysql host, a control
  character in a host, user, password or database, and a port that is not ASCII digits are
  now refused before connecting, including in `fluid apply` builds. DuckDB's mysql
  extension cannot carry a `?`, so a mysql password containing one has to change.
- Library callers: `JdbcDiscoverer.discover(uri)` called directly now pins a URI that
  carries a password; pass `operator_supplied=True` for a URI your operator supplied, as
  `introspect_jdbc` also accepts.

#### D — federation credentials (fix/sec-federation-credentials) — [GHSA-m288-j7hc-rhrj](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-m288-j7hc-rhrj)

**Security**
- **A federation manifest no longer decides where a credential goes.**
  `auth.secret_ref` in `federation/upstreams.yaml` could name any environment
  variable, and `fluid apply` (including `--dry-run`) sent its value to whatever
  host the manifest named. By default, `GITHUB_TOKEN` and `GH_TOKEN` now go only
  to `github.com`, `api.github.com` and `raw.githubusercontent.com`. Any other
  variable or host must be listed by whoever runs `fluid apply` in the new
  `FLUID_FEDERATION_SECRET_REF_ALLOWLIST`, as `NAME@host` (recommended) or a bare
  `NAME` (any host). Outside that policy nothing is sent: the upstream is
  reported `unreachable` with a reason that names the variable, the host and the
  entry to add. See docs/FEDERATION.md.
- **Federation credentials travel only over https.** An `http://` or `git://`
  endpoint whose auth would attach a credential is refused before anything is
  sent. Without `auth.secret_ref`, such endpoints still work anonymously.
- **HTTP and catalog registries no longer pass credentials to another origin
  on a redirect.** A hop to another origin drops `Authorization`, `Proxy-Authorization` and
  `Cookie` for the rest of the chain, and a redirect from `https://` to
  `http://` is refused. Before, every hop received the same headers.
- **Git registries no longer put the token in the clone URL.** It used to
  appear in the `git clone` command line and stay in
  `~/.cache/fluid/federation-git/<workspace-id>/.git/config`. git now gets it
  only through its process environment, as endpoint-keyed entries appended to
  `GIT_CONFIG_PARAMETERS`: an empty `extraHeader` that clears git's header list,
  your own non-`Authorization` extra headers for the endpoint, federation's
  `Authorization` header, and `followRedirects=false`. The endpoint receives one
  `Authorization` header, and a redirect is reported `unreachable` instead of
  being followed with it. Each refresh resets a cached clone's
  `remote.origin.url` to the endpoint, which removes a token an earlier version
  stored there.
- **The federation policy comes only from the operator.**
  `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` and `FLUID_FEDERATION_HOST_ALLOWLIST`
  are ignored when a project `.env` / `.env.*` file (or a `FLUID_SECRETS_FILE`
  while a project file sets `FLUID_SECRETS_FILE`) gives them a value. A refused
  fetch names the file whose value was ignored, and
  `federation_policy_env_ignored` is logged when your own value was set too.
  A committed `.env` could otherwise widen the policy meant to constrain the
  same checkout.
- A git refresh runs only on the cache directory itself, never on a repository
  that encloses it, and a refresh that cannot fetch reports the upstream
  `unreachable` instead of checking the pin against the old cache.
- The `federation_secret_ref_allowlist_entry_has_port` warning, for an
  allow-list entry written with a port (which never matches), suggests
  `NAME@<host>` with the entry's hostname filled in, and repeats nothing else
  from the entry, which may have been copied from a URL that holds a password.

**Upgrade notes**
- A `secret_ref` whose auth mode attaches it now needs an operator-set entry
  such as `FLUID_FEDERATION_SECRET_REF_ALLOWLIST=CATALOG_TOKEN@catalog.acme.com`,
  unless it is `GITHUB_TOKEN` or `GH_TOKEN` going to a GitHub host. For GitHub
  Enterprise Server, list `GITHUB_TOKEN@<your host>`. The check runs even when
  the variable is unset, so a manifest that names a variable for a host it may
  not go to reports `unreachable` instead of fetching anonymously.
- Move an `http://` or `git://` endpoint that carries `auth.secret_ref` to
  `https://` (`ssh://` for git), or remove the credential.
- Set `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` and
  `FLUID_FEDERATION_HOST_ALLOWLIST` in the shell, the CI environment or a
  `FLUID_SECRETS_FILE` outside the project, unless a project `.env` / `.env.*`
  file also sets `FLUID_SECRETS_FILE`, which ignores the policy in the secrets
  file too. While a project `.env` / `.env.*` file sets either one, it is
  ignored, including your own value. A template line with no value is fine.
- Write allow-list hosts without a port (`NAME@host` covers every port) and
  IPv6 hosts without brackets.
- Authenticated git fetches need git 2.31 or later; with an older git they
  report `unreachable`.
- An authenticated git endpoint that redirects, such as a renamed GitHub
  repository, now reports `unreachable`; point it at the current URL. An HTTP or
  catalog registry whose redirect chain goes from `https://` to `http://` at any
  hop now fails; point it at the final `https://` location.
- A git refresh that cannot fetch now reports `unreachable`.
- On an authenticated git fetch, an `Authorization` extra header from your git
  configuration for the endpoint is replaced by federation's; your other extra
  headers are still sent, unless one contains a line break or a NUL, which makes
  the fetch anonymous. A `url.<base>.insteadOf` rule that rewrites the endpoint
  to another scheme, host, port or path also makes it anonymous.
- Refusals are reported as `unreachable` findings; as in 0.19.0, the federation
  gate warns and does not stop `fluid apply`.

#### E — MCP output-port trusted proxies (fix/sec-mcp-trusted-proxies) — [GHSA-x4fj-f8p7-p9f3](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-x4fj-f8p7-p9f3)
**Security**
- `fluid mcp output-port serve --transport http` reads the `X-Client-CN` / `X-Client-Fingerprint` headers only under two conditions: the request passed `shared-token` or `jwt` auth, and its TCP peer is listed in the new `FLUID_MCP_TRUSTED_PROXIES`. That variable takes comma-separated IPs or CIDRs and is empty by default, so no peer is trusted. Before, any client that held the token or a valid JWT and could reach the gateway without going through the proxy could name any certificate and pass a `${caller.client_cn}` / `${caller.client_fingerprint}` row filter. Headers from any other peer are ignored, and so are empty values and values with control characters. A filter that needs them then fails closed. The check uses the TCP peer, never `X-Forwarded-For`: uvicorn's proxy-header rewriting is off for the gateway.
- `${caller.client_cn}` and `${caller.client_fingerprint}` no longer bind from a value the client supplies about itself (`clientInfo` extras or its `fluid` capability block). This holds in every auth mode.
- `serve` refuses to start, with exit 2, in two setups where no forwarded certificate can be read:
  - `--transport http` with gateway auth not enabled, while `FLUID_MCP_TRUSTED_PROXIES` is set or a row filter reads either attribute;
  - `--transport stdio` when a row filter reads either attribute.
- `data_access` audit records carry `authKind` (`shared-token`, `jwt` or `none`). When a trusted proxy forwarded a certificate, they also carry `clientCertCn` and `clientCertFingerprint`.
- These settings now stop `serve --transport http` with exit 2 (`invalid_auth_mode` / `invalid_trusted_proxies`), and the error names the accepted values:
  - an unknown `FLUID_MCP_AUTH_MODE` (for example `mtls`), which used to produce a traceback;
  - a `FLUID_MCP_TRUSTED_PROXIES` entry that is not an IP or CIDR, is `*` or is empty;
  - a `FLUID_MCP_TRUSTED_PROXIES` host address with a prefix, such as `10.0.4.17/8`, which is refused instead of widened to its network.
- When the gateway ignores the headers, it logs `output_port_mtls_headers_ignored` at WARNING, once per reason and peer, with the peer address and, for an untrusted peer, the `FLUID_MCP_TRUSTED_PROXIES` value to set.

**Upgrade notes**
- **Set `FLUID_MCP_TRUSTED_PROXIES` if you run behind an mTLS proxy and rely on the forwarded certificate.** Use the proxy's address: `127.0.0.1` for the shipped nginx and Caddy templates, or the proxy's address when it runs in another container or host. Until you do, calls that a `${caller.client_cn}` / `${caller.client_fingerprint}` filter applies to fail with `RowFilterIdentityMissing`, and the WARNING names the address to add. List only the proxy's own address, or the narrowest CIDR covering your proxy replicas: any peer in a listed range can assert any client certificate. See docs/MCP_OUTPUT_PORT.md.
- **The certificate headers need gateway auth.** Use `shared-token` with `FLUID_MCP_AUTH_TOKEN`, or `jwt` with `FLUID_MCP_JWT_ISSUER`, `FLUID_MCP_JWT_AUDIENCE` and `FLUID_MCP_JWT_JWKS_URL`. Without auth, `serve --transport http` now exits 2 if `FLUID_MCP_TRUSTED_PROXIES` is set or a row filter reads a certificate attribute. The message names the variable that keeps auth off.
- **stdio no longer serves an expose whose row filter reads `${caller.client_cn}` or `${caller.client_fingerprint}`.** `serve` exits 2. Before, the client's own claim bound the filter. Serve it over HTTP behind the proxy instead.
- uvicorn's access log for the gateway now shows the TCP peer (the proxy) instead of an `X-Forwarded-For` address, and `FORWARDED_ALLOW_IPS` no longer affects the gateway. The "NO built-in auth" warning now prints only when HTTP auth is actually off.

#### F — init --discover credential redaction (fix/sec-discover-uri-redaction) — [GHSA-25mh-f2f4-9vxp](https://github.com/Agenticstiger/forge-cli/security/advisories/GHSA-25mh-f2f4-9vxp)

**Security**
- **`fluid init --discover <uri>` no longer prints the credentials in its URI, or writes
  them into the contracts, in the cases listed below.** It printed the URI as given,
  password included, in the progress line and the connectivity and unsupported-scheme
  errors, both rendered and in the typed error's JSON form. It also logged the URI as
  given in the `discover.emitted` line (from 0.10.1 the CLI's log filter masks a plain
  `user:password@` password there, but not a user-name token, and up to 0.17.0 a query
  signature reached that line unmasked). And it wrote the password into every emitted
  `contract.*.fluid.yaml`. For a URL source it wrote the whole URL, including its
  `user:password@` part and its query string (a remote URL's only up to 0.17.0).
  - For a URI with a `scheme://`, the progress line, the log line and the errors now
    show the URI masked. The `user:password` part is masked up to the last `@`.
    Database URIs keep the user name; any other scheme masks it too, because it can be
    a token. Every query value is masked and its name kept, except that everything after
    a parameter named like a credential (`password=`, `token=`, `api_key=`) is masked as
    well. The fragment is left out.
  - Contracts carry no `user:password@` part, query string or fragment (a URL
    whose user name holds a raw `/`, `?` or `#` is an exception; see the upgrade notes).
    A Postgres contract gets `secretRef: env://PGPASSWORD` and a MySQL/MariaDB contract
    gets `secretRef: env://MYSQL_PWD` when the URI held a password. A URL contract keeps
    its scheme, host, port and path. For a URL contract, a closing line names the query
    parameters left out, never their values. It shows masked a name that the display
    rules mask (for example a piece of a user name or token). After a parameter named
    like a credential, such as `token=`, it can still name later parameters that the
    printed URI no longer shows.
- A discoverer that a plugin registers under a key that is not an RFC 3986 scheme
  (`my_db`, `jdbc:postgresql`) no longer has the URI's user name or password written to
  its contracts, except where a URL is used as given (a user name with a raw `/` before
  its `:`; see the note on a raw `/`, `?` or `#`). The text after the key's `://` is
  read as it would be after a scheme, and the contract keeps `<key>://host:port/path`. Without a project name, the contract's
  id and name are built from that path. They were built from a path that held the
  `user:password` part, and carried the password in lower case.
- A URI whose `user:password` part a URL parser could read as the host or port is
  refused before any connection, with the masked URI in the error. That is when the
  text from `://` to the last `@` holds a `:`, `urlsplit` reads that part differently
  (the `@` comes after the first `/`, `?` or `#`, or the part holds a tab or a line
  break), and the text between `://` and that first `/`, `?` or `#` holds a `:` (a port
  or an IPv6 address included), an `@`, `[` or `]`, or `urlsplit` cannot parse the URI.
  A database URI of that shape is refused whatever its host. A discoverer registered
  under a key that is not an RFC 3986 scheme gets the same refusal for the text after
  its `://`: by the database rules when it is built on the JDBC base, by the URL rules
  otherwise. A URL that is not a database URI, and whose text between `://` and the
  first `/`, `?` or `#` is a host name alone, is used as given when a `:` and an `@`
  come after that host (`s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv`).
- A failed discovery's reason is masked before it is shown, in three ways:
  - Copies of the URI that the driver quotes, whole or cut short, are masked. That holds
    whether the copy is as given or re-escaped as a SQL literal, a libpq DSN value,
    `repr()`, JSON or percent-encoding.
  - The password is masked where it follows a password key: `password=`, `PGPASSWORD=`,
    `MYSQL_PWD=`, or any key ending in `password`, `passwd` or `pwd`. So a password the
    URI also spells as its user or database name (`postgres:postgres@.../postgres`) is
    hidden there, while the user and database names stay readable.
  - Hidden values are registered with the process-wide redaction registry, so later
    output in the same process masks them too. A value is not registered if it is
    shorter than 6 characters, is a fragment of a redaction key name, or is also spelled
    by the shown URI.
- The `ConnectivityProbeError` that `--discover` raises no longer carries the driver's own
  error as its `__cause__` or `__context__`. That error can quote the URI or the DSN
  unmasked, and any formatted traceback printed it: a crash reporter, `logger.exception`,
  or a program that embeds the CLI.
  - Its cause is now a stand-in that names the driver's exception type, with the masked
    text and no traceback.
  - The masked reason keeps, one line each, the errors a plugin chains with
    `raise ... from` and an exception group's sub-errors.
  - A driver error whose `str()` fails no longer escapes with the URI attached. It reads
    `<exception str() failed>`.
- A credential URI typed without its `//` is now masked like a URI. Examples are
  `postgres:alice:PW@host/db` and `https:/host/f.csv?sig=...`. This applies when the
  text before the location's first `:` is postgres, postgresql, mysql, mariadb, sqlite,
  file, http, https, s3, gs, gcs, az, azure, abfs or abfss, or a scheme a registered
  discoverer reads.
  Text before the first `://` counts as a scheme only where RFC 3986 allows one, so a
  `://` later in the query or fragment no longer leaves the credential in clear. Any
  other location with no `://` is still read as a local path and shown as given
  (`ftp:alice:PW@host`, `user:PW@host/db`). `fluid init --help` says so.
- The filesystem discoverer names a remote stream, and reads its format, from the URL's
  path, never from its query string or fragment. Up to 0.17.0 a query string or fragment
  could reach a stream name, the contract's name and its file name, and up to 0.19.0 so
  could the `user:password@` part of a URL whose path is `/`. A URL whose path is empty
  or `/` is named after its host.

**Upgrade notes**
- **Database contracts.** Contracts from `fluid init --discover` with a Postgres or
  MySQL/MariaDB URI that holds a password no longer contain `password:`. They reference
  `secretRef: env://PGPASSWORD` or `secretRef: env://MYSQL_PWD` instead. Export that
  variable before `fluid apply`. To keep the password off the command line as well,
  export it before `--discover` and leave it out of the URI. Those contracts get no
  `secretRef`, and the database client reads the variable itself.
- **URL contracts.** A URL contract keeps no query string at all, including harmless
  parameters such as `versionId=7` or `format=csv`. It also keeps no fragment and no
  `user:password@` part. The closing line lists the dropped parameter names. Add back by
  hand any parameter the source needs that is not a credential.
- **Remote stream names.** A remote stream is named, and its format read, from the URL's
  path, never from its query string or fragment. A URL whose path is empty or `/` is
  named after its host. That is a new contract name and file name when the host holds a
  `.`, or the URL has a port or a `user:password@` part: `https://files.example.test/`
  was `files.example` and is now `files.example.test`; `http://localhost:9000/` was
  `localhost:9000` and is now `localhost`.
- **Masked user names.** The display of a URI that is not a database URI masks the user
  name along with the password: `https://***REDACTED***@host/...`. If the text before <!-- pragma: allowlist secret -->
  `://` is not an RFC 3986 scheme, the display masks from the first `:` to the last `@`.
  For example, `jdbc:postgresql://alice:...@host/shop` is shown as `jdbc:***REDACTED***@host/shop`.
  A URL with an `@` in its path is masked from `://` to that `@`:
  `s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv` is shown as
  `s3://***REDACTED***@example.com/x.csv`. Its contract keeps the path as given.
- **New refusal.** `--discover` now refuses, with "cannot tell the password from the
  host", the URIs described above. They include URIs with no password that earlier
  releases accepted: a URL with a port or an IPv6 host and an `@` later in its path,
  query or fragment (`http://localhost:9000/bucket/a@b.csv`), and a database URI with a
  `:` before an `@` that follows its first `/`, `?` or `#`
  (`postgres://db:5432/shop?application_name=ops@laptop`). For an `http://` or
  `https://` URL, percent-encode that `@` as `%40`. For a password that holds a raw `/`,
  `?` or `#`, take the password out of the URI (`PGPASSWORD`, `MYSQL_PWD`), or, for
  other URLs, percent-encode `/`, `?`, `#` and `@` in the `user:password` part.
- **Locations typed without `//`.** Type the `//` after the scheme. A location with no
  `://` is masked only when the text before its first `:` is one of the schemes listed
  above.
- **A raw `/`, `?` or `#` in a URL's user name.** A URL whose user name holds one of
  these characters, before its `:` or as a token with no password, is used as given:
  the text before that character is contacted as the host and written to the contracts.
  After a `/`, the contract also keeps the rest, password or token included, in its
  connection `uri` (for a database URI given such a token, in its database name). After
  a `?` or `#`, the rest is read as the query string or fragment, which is not written.
  A database URI whose user name holds such a character before its `:` is refused. The
  output still masks the user name and password. Leave such credentials out of the URI
  and let the storage client's own credential chain supply them.
- **Plugin discoverers.** A discoverer registered under a key that is not an RFC 3986
  scheme (`my_db`, `jdbc:postgresql`) now gets the refusal above. Its contracts get a
  connection `uri` of `<key>://host:port/path`, with no user name or password and no
  `secretRef`, except where a URL is used as given (a user name with a raw `/` before
  its `:`; see the note on a raw `/`, `?` or `#`). Without a project name, their id
  and name are built from that path instead of from one that held the `user:password`
  part.
- **Library callers.** The `ConnectivityProbeError` that `run_discover` raises now has a
  `RuntimeError` stand-in as `__cause__`, whose message starts with the driver's exception
  type name. Its `__context__` is `None`. Code that inspected the driver's exception on
  the chain must read the type name from that message instead.


### Upgrade notes

- The minimum `mcp` is now 1.22 (was 1.20). mcp 1.20 and 1.21 import
  `eval_type_backport` from pydantic's private `_typing_extra` module, which pydantic 2.14
  removed, so `mcp.server.fastmcp`, and every forge-cli MCP surface with it, failed to
  import on a fresh install that resolved those releases with current pydantic. 1.22 is
  the first 1.x that imports with pydantic 2.14. The CI drift leg that pins the floor moves
  from 1.20 to 1.22.
- An AWS contract that declared `location.catalog` other than `glue` (for example
  `rest`, `iceberg_rest`, `polaris`, `unity`, `nessie` or `lakekeeper`) on an Iceberg
  expose holds a Glue database and table in its OpenTofu state that earlier releases
  created for it. `fluid apply` now stops before planning with
  `iceberg_catalog_move_blocked` and prints the `tofu state rm` commands that release
  them, instead of planning to destroy them: destroying a Glue database deletes every
  table in it. The resources stay in AWS; run the commands, then apply again.
- The same guard covers Snowflake. An Iceberg expose on `platform: snowflake` with
  `catalog: lakekeeper`, `bigquery`, the `iceberg-rest` spelling, `hive`, `jdbc`,
  `hadoop` or `dynamodb` got a Snowflake EXTERNAL VOLUME from 0.19.0 and earlier and no
  longer does (`rest`, `iceberg_rest`, `polaris`, `unity`, `nessie` and `glue` never
  had one). `fluid apply` stops with `iceberg_catalog_move_blocked` and the `tofu state
  rm` command for the volume instead of planning to drop it.
- An AWS contract with `orchestration.engine: airflow` or `mwaa` and an
  `orchestration.schedule` now fails `fluid apply` and `fluid generate iac` with
  `unsupported_binding` (kind `mwaa-environment-not-emitted`). Its plan promised an MWAA
  environment that the OpenTofu engine never created, since `aws_mwaa_environment` needs a
  VPC network the contract does not carry; apply reported success and no environment
  existed. To schedule without Airflow, set `orchestration.engine: none` and keep the
  schedule (an EventBridge Scheduler schedule is created). To keep Airflow, create the
  MWAA environment yourself (or name your existing one in
  `orchestration.mwaa.environmentName`), push the DAG with `fluid generate schedule` and
  `fluid schedule-sync --scheduler mwaa --destination s3://<dag-bucket>/dags/`, and apply a
  copy of the contract without `orchestration.schedule`. The refusal follows the contract,
  so it holds when the AWS planner fails on another field and when a multi-cloud contract
  whose first expose is on another cloud is applied with `--provider aws`. A planned op the
  engine does not provision (IAM bindings from `metadata.policies`, Athena views, Redshift
  schemas and tables, `execution.trigger` schedules, `orchestration.tasks`, another
  cloud's op under `--provider aws`) is named in a warning.
- A GCP Iceberg expose that a streaming sink writes to must now name its catalog in
  `binding.location.catalog`. Without one, the sink wrote through whatever catalog
  reached the worker (REST by default, Glue for `sink.catalog: glue` or a hand-written
  `catalog-impl`) while dbt-bigquery and the GCP IaC created a BigLake table, so one
  table lived in two catalogs; `fluid validate` and the runners now refuse it unless the
  sink writes BigLake. Set `catalog: bigquery`, or the REST kind your catalog is. A
  hand-written sink config must then select that same catalog.
- An Iceberg expose with `catalog: dynamodb` or `catalog: jdbc` that a streaming sink
  writes to now needs a warehouse: both catalogs refuse to start without one. Set
  `location.warehouse`, or an explicit `location.bucket` on `platform: aws` or `gcp`, from
  which forge-cli derives `s3://` or `gs://` `<bucket>/<path>` (`path` defaults to
  `<database>/<table>/`), or set the sink's `warehouse` property in an override. With none
  of these, `fluid validate` and the runner preflight refuse the build.
- An Iceberg expose with `catalog: bigquery` that a streaming sink writes to now needs
  `location.project`, which becomes the catalog's `gcp.bigquery.project-id`, or that
  property in an override. A blank value counts as missing.
- The derived BigQuery sink config no longer sets `client.region`: `location.region` goes
  to `gcp.bigquery.location`. Its warehouse is now the `gs://` storage URI dbt-bigquery and
  the GCP IaC use (a `gs://` `location.warehouse`, else `gs://<bucket>` plus
  `location.path` when set, with the bucket's `{{ env.* }}` templates rendered), not
  `location.warehouse` as written; a warehouse with another scheme, or none and no bucket,
  gives none, and `fluid validate` warns.
- A `platform: confluent` Iceberg expose with no `location.catalog` now reads as Glue, the
  catalog Tableflow publishes it to, and dbt-snowflake `catalogs.yml` writes
  `catalog_linked_database_type: glue` for it. `fluid policy compile` grants a Confluent
  Tableflow expose its S3 bucket and the Glue table Tableflow publishes: the topic's table
  (`topic`, else `location.table`, else the expose id, as the Tableflow IaC names it) in
  `location.database`, or, with none, in the Kafka cluster id database Tableflow defaults
  to. With neither database nor `kafka_cluster_id` it warns and grants no Glue table.
- A Kafka Connect build whose Iceberg sink config forge-cli derives now writes exactly one
  Iceberg expose, picked by its `outputs`. `fluid validate` and the run preflight refuse it
  when the outputs name no Iceberg sink expose or name two or more, and when it declares
  no outputs and the contract has two or more. Outputs that named no Iceberg sink expose
  used to draw only the warning "the join is implicit". Split the build into one build per
  expose, or hand-write `properties.kafka-connect.sink_connector_config`.
- An embedded Debezium Server build whose Iceberg sink config forge-cli derives now writes
  the Iceberg sink exposes its `outputs` name, or all of them with no outputs. It is
  refused when those exposes sit in more than one database, resolve to different catalogs,
  or when the outputs name none of them.
- `fluid policy compile` now exits 1 with `policy_compiler_crashed` and writes no bindings
  file when the policy compiler crashes. It used to write an empty bindings list and exit
  0, so the next stage read the crash as "no grants to enforce".
- `fluid apply` now validates the contract on every engine and in every mode. On the
  OpenTofu engine (aws, gcp, snowflake, confluent and IaC plugin clouds), and with
  `--mode amend-and-build` or `replace-and-build` on any provider, a contract that is not
  schema-valid, or is older than 0.7, reached `tofu` or the build runner without being
  checked and could apply with exit 0. It now stops before planning with
  `apply_contract_invalid` (or `contract_version_unsupported`), exit 1, as a native
  `fluid apply --mode amend` always did. A cloud or build apply that succeeded on a
  contract that was never valid now fails: run `fluid validate <contract> --env <env>`,
  fix what it reports, and apply again. A `plan.json` is checked on the contract it
  embeds, and one that embeds none is now refused with `apply_plan_missing_contract`
  (exit 1) on every engine; it used to fall back to the native engine and run its
  actions unchecked. Re-run `fluid plan` to get a plan that embeds its contract. The
  contract is read once, and the engine, hooks, `tofu` and build runner all take that
  checked copy. There is no flag to skip the check.
- A Kafka Connect or embedded Debezium build whose sink config forge-cli derives is now
  refused when its expose names no table namespace (`location.database`, or on GCP
  `location.dataset`), and a Kafka Connect one when its expose names no `location.table`.
  The derived config used to name the table `None.<table>`, or leave Debezium Server on its
  default namespace. An override that sets `iceberg.tables` or `table-namespace` itself is
  not affected.
- `fluid validate` and the streaming runners now warn when an Iceberg sink override
  (`iceberg_catalog_overrides`, `sink_connector_config`, `debezium.server.sink.config`) sets
  the catalog's `credential` or `token` to a literal value, which forge-cli then pushes to
  the Kafka Connect REST API or passes to Debezium Server. A value that starts
  with `env:` or carries a `${...}` expression does not warn. Under `fluid validate
  --strict` the warning fails validation; use `sink.catalogAuth`, or an `env:<VARIABLE>`
  value.
- The embedded Debezium Server's `application.properties` no longer holds the credentials
  forge-cli resolves or derives. A property whose name carries `password`, `secret`,
  `token`, `credential`, `authorization`, `authParams`, `jaas.config`, a connection
  string, or an API, private, account, keystore or SSE key is written as
  `${FLUID_DBZ_<PROPERTY>}` (for example
  `debezium.source.database.password=${FLUID_DBZ_SOURCE_DATABASE_PASSWORD}`), and forge-cli
  passes the value to the server in that environment variable. A `server_binary` wrapper
  that replaces the environment must pass the `FLUID_DBZ_*` variables through. A sink
  `env:<VARIABLE>` value is still written as it is. A hand-written sink key outside that
  list is written as before, in a file that is now created at mode 0600.
- A Meltano / Singer tap and target now read their `--config` file from a private
  temporary directory that is removed when the tap or target exits (a `fluid` process
  killed by a signal can leave it there), instead of
  `.fluid/meltano/<product>/<build>/tap_config.json` and `target_config.json`. A copy an
  earlier release left there is deleted on the next run. A tap that writes a refreshed
  token back to its config file loses that write; the next run resolves the credential
  again.
- `fluid init` now git-ignores `**/.fluid/debezium/` and `**/.fluid/meltano/`, in every
  product directory of the workspace. A project whose `.gitignore` already has the
  fluid-cli block does not get the new lines; add them yourself, and check that no
  `application.properties`, `tap_config.json` or `target_config.json` under those
  directories was committed with a credential in it. Rotate any you find.

### Added

- **`sink.catalogAuth` (schema 0.7.6 preview).** A Kafka Connect (`sink.format: iceberg`) or
  embedded Debezium build whose Iceberg sink config forge-cli derives can authenticate to a
  REST catalog (`rest`, `lakekeeper`, `polaris`, `unity`, `snowflake-managed`) by naming an
  environment variable: `type: oauth2` with `credentialEnv` (a variable holding
  `client_id:client_secret`), `oauth2ServerUri` and `scope` (all required, plus optional
  `audience` and `resource`), or `type: bearer` with `tokenEnv`. The derived config carries
  `rest.auth.type=oauth2` and `credential=env:<NAME>` or `token=env:<NAME>`, under
  `iceberg.catalog.` for Kafka Connect and `debezium.sink.iceberg.` for Debezium Server;
  Iceberg's REST client resolves `env:` from the environment of the process that runs the
  sink, so no secret value appears in a config forge-cli writes or pushes. A variable name
  that is not exactly `[A-Za-z_][A-Za-z0-9_]*` (a trailing newline included) fails the
  schema, and so does one shaped like a personal access token (a run of 20 or more letters
  and digits, with a digit, between underscores, such as `dapi` + 32 hex); neither error
  quotes the value. The token-shape rule is a heuristic: a short or digit-free token still
  reads as a name. `oauth2ServerUri` must start with a lowercase `http://` or `https://`
  (Iceberg treats any other spelling as a path on the catalog host) and may not carry a
  `user:password@` part. `oauth2ServerUri`, `scope`, `audience` and `resource` may not
  hold a `${...}` expression, which Debezium Server would expand and Kafka Connect would
  pass on as text, a control character, or a value starting with `env:`, which Iceberg
  would resolve from the environment. `fluid validate` refuses `catalogAuth` on a build
  with no derived Iceberg sink; it and the run preflight refuse it on a catalog not
  reached over REST, on a sink config forge-cli does not derive, and next to an override
  that sets `rest.auth.type`, `credential`, `token`, `oauth2-server-uri`, `scope`,
  `audience`, `resource`, `rest.sigv4-enabled` or a `header.Authorization`. Schema errors
  under `catalogAuth` do not quote the value, on a contract and on a bundle. The embedded
  Debezium run refuses to start when the
  variable is unset or empty, because Iceberg drops a property whose variable is unset;
  the Kafka Connect variable lives on the Connect worker, which forge-cli cannot check.

- **`catalog: lakekeeper` is checked as Lakekeeper.** A Lakekeeper, Polaris or Unity
  catalog addresses its warehouse by NAME (`analytics`, or `<project-id>/<name>` on
  Lakekeeper), so `fluid validate` refuses an object-store URI there, and warns when a
  Lakekeeper `uri` does not end in `/catalog`, where Lakekeeper serves its Iceberg REST
  API. The refusal reads the warehouse that reaches the sink: `location.warehouse`, or
  the warehouse an override or a hand-written sink config sets. For `platform: gcp`, the
  GCP Iceberg check no longer suggests a `gs://` location for these catalogs. Schema
  0.7.6 (preview) accepts `lakekeeper` and `polaris` in `sink.catalog`; 0.7.5 is
  unchanged.

### Added

- **`catalog: lakekeeper` is checked as Lakekeeper.** A Lakekeeper, Polaris or Unity
  catalog addresses its warehouse by NAME (`analytics`, or `<project-id>/<name>` on
  Lakekeeper), so `fluid validate` refuses an object-store URI there, and warns when a
  Lakekeeper `uri` does not end in `/catalog`, where Lakekeeper serves its Iceberg REST
  API. The refusal reads the warehouse that reaches the sink: `location.warehouse`, or
  the warehouse an override or a hand-written sink config sets. For `platform: gcp`, the
  GCP Iceberg check no longer suggests a `gs://` location for these catalogs. Schema
  0.7.6 (preview) accepts `lakekeeper` and `polaris` in `sink.catalog`; 0.7.5 is
  unchanged.

### Changed

- CI: the emulated-heavy lane runs a real Kafka Connect worker (cp-kafka-connect 7.9.10
  with the Apache Iceberg sink 1.9.2) against a real Lakekeeper (v0.13.6, credentials
  vended over STS from an S3-compatible store). It streams records through the config
  forge derives for `catalog: lakekeeper`, shows that a config carrying both
  `iceberg.catalog.type` and `catalog-impl` fails on the worker, and shows forge's Glue
  config gets past that check. `assert_lane_coverage.py` fails the job if every test
  skipped. Run it locally with `FLUID_TEST_LAKEKEEPER=1` (Docker required).

- CI: `release.yml` can publish through a TestPyPI outage. A manual run with
  `skip_testpypi: true` skips the TestPyPI upload and its install check and
  publishes the existing tag straight to PyPI; `verify-pypi` still installs and
  smoke-tests it from PyPI. Only a manual run can skip, never a tag push. Every job
  now builds the tag's commit, also when a manual run starts from `main`, and the
  quality gate checks `ci.yml` on that commit.
- CI: on a manual run, `release.yml`'s quality gate refuses a start ref that does not
  contain the tag's commit, or that GitHub cannot compare with it, and names a tag that
  does not exist. The SLSA provenance and PyPI's Sigstore attestations name the commit
  the run started from, not the one it built, so a tag `main` does not contain would
  have been attested as `main`. When the start ref is not the tag, the gate warns that
  the provenance names it. A tag after #702 is dispatched with `--ref vX.Y.Z`, which
  makes the provenance name the tag.
- CI: the GHCR image and the GitHub Release now wait for PyPI and follow only an upload
  it accepted. They ran beside `publish-pypi` and ignored its result, so a refused upload
  (for example "file already exists" on a re-dispatched tag) still cut a release and
  moved the GHCR `latest` tag. The release takes the quality gate's validated tag name.
- A derived Kafka Connect or embedded Debezium Server Iceberg sink resolves the exposes it
  writes from the build's `outputs`, through one resolver that `fluid validate`, the run
  preflight, both runners and the Kafka Connect late-arrival target read. The Debezium
  Server sink writes every captured table under one `table-namespace` through one catalog,
  so the exposes must share one `binding.location.database` and one catalog. A Glue
  warehouse is a per-table prefix: it is not compared, and the sink takes the first
  expose's. A DynamoDB or JDBC catalog creates every missing table under the one warehouse
  the sink is given, so warehouses derived from `location.bucket` that differ (each is that
  expose's own table prefix) are refused; set one `location.warehouse` on those exposes, or
  split the build. A hand-written config, and an override that sets
  `iceberg.tables` (Kafka Connect) or `table-namespace` (Debezium Server), is still checked
  against the first Iceberg sink expose.
- The Kafka Connect deriver now forwards the catalog's own properties (for BigQuery,
  `gcp.bigquery.project-id` and `gcp.bigquery.location`) under the `iceberg.catalog.`
  prefix, as the Debezium Server deriver already did.

### Fixed

- **Command output keeps its square brackets.** Rich read text in brackets as console
  markup and dropped it, so `fluid validate` printed `exposes declares
  governance.lakeFormation` instead of `exposes[orders] declares ...`, bundle findings
  lost their `[error]`/`[warning]` severity, and a suggestion such as `pip install
  'data-product-forge[gcp]'` lost its extra. Validation messages, error context and
  suggestions now print literally, and a printed `tofu state rm <address>` command stays
  on one line, so a copied line runs with its address.
- **The streaming runners check the sink they push.** The Kafka Connect and embedded
  Debezium Server runners ran the Iceberg sink checks only for a config they derived; a
  hand-written `sink_connector_config` or `server.sink.config` that `fluid validate`
  refuses was still deployed. A hand-written config in a build that declares
  `sink.format: iceberg` is now checked the same way, before anything is created. In a
  contract with several builds, each runner also reads the build it is running instead
  of the first one, which the checks already did. A `sink_connector_config`,
  `server.sink.config` or `iceberg_catalog_overrides` whose `type` or `catalog-impl`
  selects a different catalog than the expose's is now an error on every platform: the
  sink would write one catalog while dbt and the IaC read another. Declare the catalog
  the sink writes to in `binding.location.catalog`; a REST endpoint that fronts Glue
  (Glue's Iceberg REST endpoint) is `catalog: rest`.
- **`catalog: bigquery` on a Kafka Connect sink warns.** It sets
  `iceberg.catalog.type=bigquery`, which the published Apache Iceberg sink (1.9.2) cannot
  load; Iceberg adds the type in 1.10. The Nessie and BigQuery runtime warnings follow the
  catalog type that actually reaches the worker, so a hand-written config that sets
  `type: rest` no longer draws them.
- **A typo in `location.catalog` is reported once, as a typo.** The native AWS planner now
  refuses an unknown catalog value as the IaC does (it used to plan the S3 buckets and no
  Glue table), without first logging it as a `plan_failed` JSON line, and a typo no longer
  also draws a Lake Formation refusal advising to "govern access in the <typo> catalog".
- **The Iceberg catalog-move guard no longer blocks a legitimate apply.** It flagged a
  Glue database that a since-removed parquet expose had created as a catalog move, and
  nothing could get past it. A database is now flagged only when the moved expose's own
  Glue table is in state, or when the expose names no table: its database is then all
  an earlier release created for it, and the guard used to let its destroy through. On
  Snowflake the guard finds the EXTERNAL VOLUME by its contract-derived name, so it also
  stops an upgrade that changed `location.warehouse` to a catalog name or dropped
  `iam_role_arn` in the same edit. When the guard cannot read state or its probe fails,
  it says so at WARNING level instead of DEBUG.

- **A Kafka Connect Iceberg sink on AWS Glue starts.** The derived connector config set
  both `iceberg.catalog.type` and `iceberg.catalog.catalog-impl`, and Apache Iceberg's
  `CatalogUtil` refuses that ("both type and catalog-impl are set"), so the sink failed
  at startup. It now sets `catalog-impl` or `type`, never both, as the Debezium Server
  sink already did. `fluid validate` also refuses an `iceberg_catalog_overrides` or
  hand-written sink config that would add the other key back.
- **Every emitter reads `location.catalog` the same way.** The streaming sinks, dbt
  `catalogs.yml`, the Snowflake, AWS and Confluent IaC, the native AWS planner, `fluid
  policy compile`, `fluid diff` and `fluid test` each classified the free-string value by
  hand, and disagreed: `catalog: lakekeeper` streamed over REST while dbt wrote a
  Snowflake-managed table and the AWS IaC created a Glue table of the same name. One
  table in `providers/_iceberg_catalog.py` now classifies it; spellings fold case and
  `-`/`_` (`iceberg-rest` is `rest`, `snowflake` is `snowflake-managed`).
  - AWS: an Iceberg expose in another catalog gets its S3 bucket but no Glue database,
    table, import, Glue IAM grant or Lake Formation resource, and `fluid diff` and
    `fluid test` no longer look for it in Glue. `governance.lakeFormation`, column
    restrictions and row filters on such an expose are refused at `fluid validate` and
    at apply, by catalog name, instead of being emitted against a Glue table that does
    not exist. An absent catalog or `catalog: glue` emits exactly what it did.
  - Snowflake: `lakekeeper` and the `iceberg-rest` spelling are external catalogs
    (`catalog_type: iceberg_rest`, no EXTERNAL VOLUME), and `fluid validate` no longer
    demands an `s3://` or `gs://` warehouse for them. `hive`, `jdbc`, `hadoop` and
    `dynamodb`, which Snowflake has no catalog integration for, are a validate error and
    are left out of `catalogs.yml` instead of becoming a Snowflake-managed table.
  - dbt-bigquery: an expose naming a catalog other than `bigquery` is left out of
    `catalogs.yml` with a warning instead of becoming a BigLake table.
  - Confluent Tableflow: a `location.catalog` other than `glue` is a validate error; it
    was ignored and the table was published to Glue.
  - `catalog: dynamodb` reaches Iceberg as `catalog-impl` (`DynamoDbCatalog`); Iceberg
    has no `dynamodb` type.
- **`fluid validate` refuses an Iceberg catalog value it does not know**, and lists the
  accepted ones. The emitters used to fall back differently, so a typo split one table
  across catalogs. `fluid apply` on AWS refuses it too (`unknown-iceberg-catalog`).
- **The streaming-sink checks cover every catalog.** `uri` and `warehouse` were required
  only for the literal `rest`; every REST catalog (and Nessie) now needs them. A
  `sink.catalog` that disagrees with the expose's catalog is refused, since dbt and the
  IaC read only the expose. A warehouse override equal to the REST binding's warehouse
  no longer warns that "the static Glue table may differ". The Kafka Connect runner and
  the embedded Debezium Server runner run the same checks before they create anything.
- **A crash in an Iceberg or Confluent gate fails `fluid validate`.** It was printed only
  with `--verbose`, and the contract passed unchecked.
- **`fluid validate` catches two `catalog: snowflake` Iceberg exposes that derive one
  EXTERNAL VOLUME on different storage**, instead of `fluid apply` failing mid-emit.
- **`fluid policy compile` grants an Iceberg table where its catalog lives.** Every
  Iceberg expose off GCP compiled to AWS S3 and Glue grants, whatever its platform. A
  Snowflake-managed table now compiles to Snowflake grants, and a table in another
  catalog gets no Glue grant and a warning to enforce access in that catalog.

- **A Lake Formation tag the contract does not define is associated, and the module
  validates.** A key in a binding's `governance.lakeFormation.tags` with no
  `governance.lakeFormation.tagDefinitions` entry got a `depends_on` on an
  `aws_lakeformation_lf_tag` the module never declared, so `tofu validate` and
  `fluid apply` failed with "Reference to undeclared resource". Such a key is now a
  tag the platform owns: associated as written, with no `depends_on`, no warning and no
  refusal. It must already exist in the account, and the identity running `fluid apply`
  needs `ASSOCIATE` on it. A key the contract defines is named by reference,
  `key = aws_lakeformation_lf_tag.<name>.key`, the provider's documented pattern,
  instead of a `depends_on` edge, so the association still waits for the tag. The
  reference evaluates to the same key, so an association applied from 0.19.0 plans no
  change (applied and planned against moto).
  Refused at `fluid validate` and at emit, where each used to fail at apply or be
  dropped: a value the contract's definition of the tag does not allow, compared
  case-insensitively (`lakeformation-tag-value`); a definition with no values, or two
  that would be one resource name, such as `a-b` and `a_b`
  (`lakeformation-tag-definition`); and tags on a binding that names no Glue table
  (`lakeformation-tag-association`). The `tags` and `tagDefinitions` descriptions in
  fluid-schema 0.7.3 to 0.7.6 say so.
- **A BigQuery replace or rollback keeps the table's governance.** Nothing forge-cli
  runs recreates a governed BigQuery table any more. `CREATE OR REPLACE TABLE … AS
  SELECT`, which BigQuery documents drops every row access policy, and whose SELECT
  schema has no policy tag, left every filtered or restricted principal reading every
  row and column until the next apply. dbt's `table` materialisation still does (a
  known gap, documented in `docs/governance-parity.md`).
  - `fluid rollback` restores with one query job, `SELECT * FROM <backup>` into the
    live table with `WRITE_TRUNCATE_DATA` and `CREATE_NEVER` (`WRITE_TRUNCATE` on an
    emulator). The destination is a table reference built from the validated
    identifiers, and the recorded `ddl[]` is still never run. A backup the restoring
    identity cannot read in full, because a row access policy shows it only some rows,
    is refused (`rollback_bigquery_backup_filtered`) instead of restoring that subset.
  - `GcpProvider.restore_ddl` records no restore SQL, as AWS does.
  - The GCP planner's replace emits a SQL build as the SELECT with a structured
    destination and `WRITE_TRUNCATE_DATA` / `CREATE_NEVER`, keeps the table's
    `bq.ensure_table`, and backs the table up with a zero-copy
    `CREATE SNAPSHOT TABLE … CLONE` that expires after 30 days, not a plain copy with
    no policy and no tag. SQL that names its own sink passes through as before.
  - When `tofu` replaces a BigQuery table (partitioning or a key added to a live
    table), its row access policies and table IAM members are recreated with it in
    the same apply (`lifecycle.replace_triggered_by` on the table's `id`). BigQuery
    deletes them with the table, and `tofu` planned them as unchanged. An in-place
    update of the table leaves them alone, so a label change still needs no
    `--allow-data-loss`.
- **A row filter on an AWS binding that is not a Glue-catalog table is refused, not
  dropped.** On AWS a row filter is a Lake Formation data cells filter on a Glue table,
  and the emitter writes no Lake Formation resource for any other format. So
  `rowFilters` on a `redshift_serverless`, `redshift_table`, `redshift_external_schema`
  or Kinesis binding, or on a Glue format with no `location.database`, passed
  `fluid validate`, and `fluid generate iac` and `fluid apply` wrote the Redshift
  namespace and workgroup with no filter and reported success. It is now refused
  (`row-filter-unenforceable`), and the message names the formats that can carry a
  filter. A mask or a deny on such a binding was already refused
  (`column-restriction-unenforceable`), and still is.
- **`fluid validate` reports every row-filter and mask refusal the AWS emitter makes.**
  For an aws binding it ran only the column-restriction check, so a row filter with no
  Lake Formation read grant or with an unmapped principal, a mask with no
  `maskedViews`, a text mask on a column that is not a string, and a masked view over
  a name its SQL cannot quote all passed stage 2 and failed first at
  `fluid generate iac` or `fluid apply`. Both now run the same derivation
  (`lf_governance`) and refuse with the same message.
- **A second CDC build no longer streams into the first build's table.** A Kafka Connect
  or embedded Debezium build whose sink config forge-cli derives always wrote the first
  Iceberg expose, so a build for `refunds` pushed `iceberg.tables=sales.orders` while
  `fluid validate`, the preflight and the run all passed. It now writes the expose its
  `outputs` name (see Upgrade notes).
- **A GCP streaming sink writes into the expose's BigQuery dataset.** The sink named the
  table's namespace from `location.database` only, but a GCP binding names its dataset in
  `location.dataset`, so a BigQuery or other GCP Iceberg sink pushed
  `iceberg.tables=None.<table>` and `fluid validate` passed. The namespace is now
  `location.database`, else `location.dataset` (the BigQuery metastore catalog maps a
  namespace to a dataset), and a sink with neither is refused (see Upgrade notes). The
  Kafka Connect late-events table and the Debezium shared-database check read it the
  same way.
- **DynamoDB, JDBC and BigQuery sink configs carry what the catalog needs to start.**
  `DynamoDbCatalog` and `JdbcCatalog` refuse to initialise without a warehouse, and
  `BigQueryMetastoreCatalog` without `gcp.bigquery.project-id`; forge-cli derived neither
  and `fluid validate` asked for neither, so the connector failed at start (see Upgrade
  notes). A warehouse or project an override supplies counts, and a whitespace-only
  `location.warehouse` or `location.project` counts as unset.
- **A bucket written as a `{{ env.* }}` template is read as written.** When its variable is
  unset or empty where `fluid validate` runs, validate warns and names the variable instead
  of asking for a bucket the contract already sets. The runner preflight, where the sink
  config is derived, refuses the build instead of pushing a warehouse in a bucket such as
  `acme--lake` that the contract does not name; this includes a run from a plan, whose
  embedded contract is read as written. A Kafka Connect BigQuery sink reads the warehouse
  only to auto-create tables, so it is refused only with auto-create on; otherwise it
  warns and pushes no warehouse. An override warehouse is kept, and a hand-written sink
  config that does not derive is not checked against the bucket's variables.
- **The BigQuery no-warehouse warning states what fails.** With auto-create on, the Kafka
  Connect sink calls `createNamespace` for each table it creates, and
  `BigQueryMetastoreCatalog` refuses that without a warehouse, so auto-create fails even in
  a dataset that exists. For Debezium Server it says the server does not boot without
  `debezium.sink.iceberg.warehouse`, which has no default.
- **`fluid policy compile` grants a Confluent Tableflow table with no catalog.** A
  `platform: confluent` Iceberg expose with no catalog read as `rest`, so `fluid policy
  compile` dropped its grants with a false "cataloged in 'rest'" warning (see Upgrade
  notes).
- **`fluid policy compile` shows its warnings.** Each compiler warning, for example a grant
  on an Iceberg table in a catalog it cannot grant on, which compiles to no table grant, is
  now printed at WARNING level, and the exit code stays 0; the warnings are still written to
  `bindings.json`. `fluid policy apply` echoes the warnings stored in `bindings.json`. A
  contract with no grants prints no warning.
- **A policy compiler crash fails the command.** `fluid policy compile` and the policy step
  of `fluid generate artifacts` now fail with exit 1 and `policy_compiler_crashed` (see
  Upgrade notes). The error names the schema type errors at the values the compiler reads
  (`accessPolicy`, its grants, each grant's permissions, each expose's binding and
  location) by path and expected type, never by value (a value of the wrong type can be a
  connection URL with a password in it), or the compiler's own exception when the schema
  finds none there.
- **The Snowflake EXTERNAL VOLUME catalog-move guard names both causes.** It blocks the
  same applies, but its message no longer claims forge-cli stopped
  creating the volume or that the moved exposes held it. It names both possible causes (an
  upgrade from a release that gave a non-Snowflake-managed catalog a volume, or a
  Snowflake-managed expose removed or re-cataloged in this change) and keeps the `tofu
  state rm` remediation.

### Security
- **The Debezium and Singer config files forge-cli generates no longer keep the source or
  destination password on disk.** The embedded Debezium Server's `application.properties`
  held the source database password a `secretRef` resolved, under the contract directory,
  briefly at mode 0644 and kept after the run. The Singer `tap_config.json` and
  `target_config.json` held the source and warehouse passwords at 0644 and were never
  deleted. None of them was git-ignored, so `git add -A` staged the password. The Debezium
  credentials now go to the server's environment and the file is created at 0600; the
  Singer configs live 0600 in a private temporary directory for the life of the tap or
  target. (CodeQL alerts 211, 53, 54.)
- **A failing `fluid mission` predicate no longer quotes a contract's inline credential.** A
  predicate on a path such as `exposes[*].binding.location.password` printed
  `got '<value>'` to the terminal, `--json`, the stored scorecard and the planner's LLM
  prompt, because the redactor read `password: expected == …` as the assignment. A leaf at
  or under a key the redactor treats as a credential (`password`, `secrets`, `auth`,
  `connection_string`, `apiKey`, `PGPASSWORD`, …) now shows as `***REDACTED***`. A key that <!-- pragma: allowlist secret -->
  refers to a credential (`secretRef`, `tokenEnv`, `catalogAuth.type`) or only contains such
  a word (`maxTokensPerDay`, `authz`, `author`) still shows its value. (CodeQL alert 241.)
- **The shared redactor masks `aws.secret.access.key=`, `secret.access.key=`,
  `fs.s3a.secret.key=` and `secret_key=`.** It knew `secret_access_key` with `_` or `-`, and
  no `secret.key` form at all, so a Kafka Connect task trace stored in a run record kept an
  S3 secret key. The Snowflake provider's redactor masks the same spellings. (CodeQL alert
  231.)
- **The local provider's log context no longer carries a raw error.** A failed plan, load,
  SQL or copy action put the exception's text into the log record's context, which a JSON
  log formatter renders. That text now goes through the shared redactor, and every context
  value first gets the two layers the Snowflake provider runs: each credential forge-cli
  holds masked whole, then any `scheme://user:password@` userinfo. (CodeQL alert 292.) <!-- pragma: allowlist secret -->
- `mask_known_secrets` is now `redact_known_values`; the old name stays as an alias. CodeQL
  treats a call to a function named like `*secret*` as a source of secrets, so every caller
  of the redactor was reported as logging or storing a credential it had already masked.

### Documentation

- `docs/governance-parity.md` said these refusals happen at `fluid plan`. `fluid plan`
  never runs the emitter: they happen at `fluid validate`, `fluid generate iac`,
  `fluid apply` and the state check of `fluid diff`.

## [0.19.0] — 2026-10-06

A contract can now say that a principal reads a column masked rather than not at all,
and which rows a principal reads, in fields that name no cloud (fluid-schema 0.7.6).
GCP and AWS each enforce them in their own terms, and a binding that cannot is refused.
The AWS provider moves to terraform-provider-aws 6.

### Added

- **Column masking: `columnRestrictions[] {access: mask, mask: last_four | first_four |
  nullify}`** (#700). On GCP the masked columns get a policy tag of their own with a
  BigQuery data policy (`LAST_FOUR_CHARACTERS`, `FIRST_FOUR_CHARACTERS`, `ALWAYS_NULL`),
  and `roles/bigquerydatapolicy.maskedReader` for the masked principals; the tag's
  fine-grained readers read the clear value. On AWS, where Lake Formation cannot mask,
  the principal's grant excludes the masked columns from the table. A protected Glue
  Data Catalog view, `<table>_masked`, returns them masked by the same rule, and selects
  nothing the principal's own grant does not. It needs
  `governance.lakeFormation.maskedViews {definer, validationConnection}`, and is refused
  without it (`column-mask-unenforceable`).
- **Row filters: `exposes[].policy.authz.rowFilters[] {principal, where, name?}`** (#700).
  The predicate is parsed before it is written into a policy: a statement separator, a
  comment, a subquery or an undeclared column is refused. On GCP each filter is a row
  access policy naming every identity of its principal, and `fluid_all_rows` (`TRUE`)
  names every other principal the expose grants anything to. On AWS each filter is a
  Lake Formation data cells filter, and the principal's `SELECT` is granted on it. A
  filter on a BigQuery view, on a GCP binding that is not a BigQuery table, or on an
  AWS binding with no Lake Formation read grant is refused.
- **Governance labels** (#700). The contract's and the expose's labels, and its
  classification, jurisdiction, regulatory frameworks and residency, land as GCP labels
  on the dataset, table and key, and as AWS tags on the bucket and key and parameters
  on the Glue table.

### Changed

- **terraform-provider-aws `~> 6.0`** (#700), for the view definition 5.x does not
  have. With only the pin moved, eleven applied AWS products planned no change on 6.67.
  Provider 6.23 and later read a bucket's tags through S3 Control
  (`s3:ListTagsForResource`), which the applying identity needs.
- **A full-refresh BigQuery load is `WRITE_TRUNCATE_DATA`** (#700). `WRITE_TRUNCATE`
  removes every row access policy of the table it writes, so each build would have
  undone the row filters the apply had just made. `WRITE_TRUNCATE_DATA` replaces the rows
  and keeps the table's policies, policy tags and partitioning (measured on BigQuery).
  An emulator keeps `WRITE_TRUNCATE`, since goccy appends for a disposition it does not
  know.

### Fixed

- **On an emulator, each Glue database, table and masked view names its catalog**
  (#700). Provider 6 refuses to read back a Glue id with an empty catalog, and an
  emulator skips requesting the account id, so every Glue resource applied against
  LocalStack or moto failed right after its create.
- **A DuckDB landing outside the contract's directory lands on every run, not only the
  first** (#699). Since 0.18.0 (#689) a landing outside the contract's directory, in a
  directory the operator lists in `FLUID_DUCKDB_ALLOWED_DIRS` or elsewhere in the
  workspace, is granted to the run as its file and its `__late_events` file. DuckDB's
  `COPY ... TO` over a file that exists writes `tmp_<name>` beside it and renames that
  into place, so the first run landed and every later one failed with "Permission Error:
  Cannot access file .../tmp_<name>". The late-arrival split rewrites the file the run
  has just landed, so for such a landing it failed on every run, logged a warning, and
  the build still succeeded with the late rows left in the file. Each landed file and its
  `__late_events` file are now granted with their `tmp_` siblings, confined as the files
  are. Found on the demo lab on 0.18.2.

## [0.18.2] — 2026-10-05

`fluid diff --exit-on-drift` passes for a silver or gold whose gcp overlay names its
project through the environment. 0.18.1 fixed the live check, and the bronze then
passed, but `fluid diff` still planned with the project as written. The GCP planner
refused it while wrapping the product's SQL build.

### Fixed

- **`fluid diff` plans the contract it resolved, not the one as written** (#696). 0.18.1
  resolved `{{ env.* }}` for the live check only. `fluid diff` still built the provider
  from the binding's project as written, and planned with it. For a gcp binding whose
  project is `{{ env.FLUID_GCP_PROJECT }}`, the GCP planner then refused the placeholder
  ("Invalid GCP project ID") while wrapping an embedded-SQL build into
  `CREATE OR REPLACE TABLE <project>...`. So `--exit-on-drift`, stage 5 of the
  generated pipelines, failed with `diff_failed` for every silver and gold. A bronze,
  with no SQL build, passed. The contract is now resolved once, as soon as it is
  loaded, the way `fluid plan` and `fluid apply` read it. Found on the demo lab's GCP
  chain on 0.18.1.

### Documentation

- **`docs/governance-parity.md` states the GCP half as measured** (#694): applied,
  verified and queried as each principal against real BigQuery, Cloud KMS and Data Catalog
  on 4 October 2026. What stays unproven is narrowed to the Lake Formation half as 0.17.0
  derives it.

## [0.18.1] — 2026-10-05

`fluid diff --exit-on-drift` reads a BigQuery target whose project is named
through the environment. On 0.18.0 the drift gate, stage 5 of the generated
pipelines, failed every build of such a product, while `fluid apply` and
`fluid verify` read the real table. Found on the first run of a gcp overlay
against real BigQuery.

### Fixed

- **`fluid diff`'s live check reads the target a `{{ env.* }}` binding resolves to** (#692).
  It read the placeholder itself. A BigQuery binding whose project is
  `{{ env.FLUID_GCP_PROJECT }}` was refused as an invalid id ("the binding's BigQuery
  project, dataset or table is not a valid id"), so `--exit-on-drift`, stage 5 of the
  generated pipelines, failed on every build of a product whose `fluid apply` and
  `fluid verify` read the real table. The live check now resolves the contract, and
  the last applied one, the way the state check, `fluid apply` and `fluid verify` do.
  Found on the first real GCP run of a gcp overlay that names its project through the
  environment.

## [0.18.0] — 2026-10-03

A contract can no longer make the engine read the machine it runs on.
- `$ref` composes only files inside the contract's own directory tree.
- Contract SQL runs inside DuckDB's own sandbox.
- A new public API loads a contract exactly as `fluid plan` sees it.

These changes matter most for services, CI jobs and shared hosts that run
contracts other people wrote.

### Upgrade notes

- **`$ref` is confined to the root contract's directory tree** (#687).
  - URL refs (`file://` included) and absolute paths are refused.
  - `..` and symlink escapes are refused after resolution.
  - Monorepos that shared fragments across products with `$ref:
    ../other-product/…` must widen the root. Set `FLUID_REF_ROOT` to a directory
    holding the products and their shared fragments (for example the repository
    root), or pass `ref_root=` to `load_contract` / `load_with_overlay` /
    `compile_contract`.
  - A `FLUID_REF_ROOT` that does not contain the contract is ignored for that
    contract, with a `ref_root_env_ignored` warning.
  - See `docs/contract-refs.md`.
- **Bundled OpenAPI fragments with an external `$ref` fail `fluid validate`**
  with `OAS-REF-EXTERNAL` (#687).
  - `file://` and `http(s)://` refs used to be followed by
    openapi-spec-validator. They are now reported, not followed.
  - A relative ref was already reported as unresolvable (`OAS001`), and now
    reads `OAS-REF-EXTERNAL`.
  - A `$ref` key inside an `example`, `examples.*.value` or `x-*` payload is
    reported too.
  - So a bundle that validated before can now fail if it holds a resolvable
    `file://` or `http(s)://` ref, or a `$ref` key in such a payload. Inline the
    schemas or rename the key.
- **Contract SQL can read only what the run may read** (#689). That is:
  - the contract's directory and its FLUID workspace
  - `./runtime`
  - the run's scratch directory
  - the locations the contract declares, and only those inside the same roots
  - any directory the operator lists in `FLUID_DUCKDB_ALLOWED_DIRS` (absolute
    paths, `:`-separated)

  Consequences:
  - A declared input, output or acquisition source outside those roots is
    refused before any SQL runs. This includes absolute landing paths such as
    `/data/landing/*.csv`. Acquisition builds have narrower roots than
    embedded-SQL builds.
  - A declared glob grants the directory above its first wildcard, because
    DuckDB re-expands it at read time. That directory must itself be inside the
    allowed roots.
  - See `docs/duckdb-sandbox.md`.
- **Contract SQL can no longer read `http(s)://`, `gs://` or Azure URLs**,
  declared or not; only declared `s3://` locations are reachable (#689). Land
  remote data with an acquisition build (`source.kind: http`) first.
- **`SET` / `PRAGMA` statements that change a DuckDB setting fail in contract
  SQL** with "the configuration has been locked" (#689). This covers
  `memory_limit`, `threads`, `TimeZone` and similar. Remove them.
- **Functions DuckDB used to autoload no longer work in contract SQL** (#689):
  `sqlite_scan`, `read_xlsx`, `ST_Read`, `delta_scan`, `iceberg_scan`. Read CSV,
  Parquet or JSON instead, or land the data with an acquisition build.
- **`duckdb>=1.5.0`** is required by the `local` extra (#689). Older versions
  let `<dir>/./../` and symlinks escape the allowlist.
- A file DuckDB database already open in the same process can no longer be
  opened a second time. It raises a clear `DuckDBSandboxError` instead of
  sharing the instance.

### Security

- `$ref` resolution refuses remote, absolute and escaping targets, before it
  tests whether the target exists (#687).
- `fluid validate` no longer hands a bundled OpenAPI fragment with an external
  `$ref` to openapi-spec-validator, which followed `file://` and `http(s)://`
  refs. It reports `OAS-REF-EXTERNAL` instead (#687).
- Every DuckDB connection in the engine goes through one helper,
  `secure_duckdb_connect` (#689). It applies DuckDB's built-in sandbox:
  - `allowed_directories` / `allowed_paths`
  - `enable_external_access = false`
  - autoload and autoinstall off, no persistent secrets, no community extensions
  - `lock_configuration = true`

  Each action's connection is closed even when the action fails. A guard test
  fails if a new `duckdb.connect` bypasses the helper.
- The legacy local-provider module `fluid_build.contract_tests`, which no
  `fluid` command uses, confines each action's DuckDB connection to the files
  that action declares, under the working directory or a
  `FLUID_DUCKDB_ALLOWED_DIRS` directory (#689). `fluid contract-tests` does not
  run DuckDB and is unchanged.

### Added

- `fluid_build.api.load_contract`, `load_contract_from_text` and
  `load_contract_from_dict` (#688). They return a `LoadedContract` whose
  `contract` is the dict `fluid plan` plans, after parsing, `$ref` composition,
  the env overlay and the engine's alias and legacy-`build:` rewrites, plus its
  plan-digest canonicalisation. Failures raise a typed `ContractLoadError`. The
  `fluid_build.api` version is 1.1. See `docs/CONTRACT_LOADING_API.md`.

## [0.17.0] — 2026-09-28

One contract now deploys to AWS and to GCP through its `--env` overlays and is
governed the same on both. Found by an offline verification of the demo's three
products on 0.16.5: the two clouds shared one OpenTofu state, silver and gold
could not build on GCP, and every GCP policy but the grants was dropped without
a word. Measured with the real providers against moto and the goccy BigQuery
emulator; the live GCP apply is the proof still to come.

### Upgrade notes

- **The first `fluid schedule-sync` after upgrading must retire the old DAG
  of each env.** An env's Airflow DAGs now live in `<product-id>__<env>/` with
  dag id `<product>__<env>__<build>`; 0.16.7 and earlier wrote them to
  `<product-id>/` as `<product>__<build>`. Left in place, the old DAG runs
  beside the new one, and both apply the same product against the same state.
  With `--delete-scope product` (the default) to a local path or a `git+ssh`
  repository, the sync retires them itself: it deletes from `<product-id>/`
  only the DAGs rendered for the same product and env under the old id, and
  keeps any other file. For every other transport it prints the step to take:
  delete those DAG files at the destination once. `--delete-scope destination`
  removes the old directory with the rest of what the sync does not ship.
  The report's `superseded_scopes` records which case applied.
- **Remote state moves to a per-provider key on the first apply.**
  `fluid/<id>/<provider>/terraform.tfstate` replaces `fluid/<id>/terraform.tfstate`.
  The apply copies the old state with OpenTofu's own migration
  (`tofu init -force-copy`) and leaves the source untouched. A shared key that
  already holds another cloud's resources is refused
  (`state_shared_with_another_provider`) instead of planned for destruction.
- **A GCP dataset's grants stop being authoritative.** They are
  `google_bigquery_dataset_iam_member` resources now. The first apply on a
  dataset whose state holds the old `access` list revokes, once, the entries no
  member covers, and prints them (see `docs/governance-parity.md`).

### Added

- **Governance parity on GCP** (#674). Each policy is one contract field,
  enforced natively on both clouds and checked by `fluid verify`:
  retention (`lifecycle.expire`) as daily partitions that expire, never a
  table TTL; `binding.encryption.kms: product` as a key ring and a 90-day
  rotating key per product and dataset, granted to the BigQuery service agent;
  `policy.authz.columnRestrictions` as a Data Catalog taxonomy with fine-grained
  access control and policy tags, and on AWS as each Lake Formation grant's
  excluded columns. `docs/governance-parity.md` has the table.
- **`binding.principals`** (#674). An overlay maps the contract's logical
  principals to the identities they are on that cloud (one, a list, or `[]`).
  With the block present an unmapped principal is refused; on GCP a placeholder
  principal (a reserved domain, or not an IAM member at all) is refused either
  way, since BigQuery refuses an access entry for an identity that does not exist.
- **Embedded-SQL builds read and land BigQuery** (#673). A `consumes[]` entry
  whose upstream is a gcp `bigquery_table` resolves to that table, and a build
  whose expose is one loads its result through the acquisition runner's own
  load (`WRITE_TRUNCATE`, `CREATE_NEVER`), failing on a short load. Naive
  timestamps load as UTC `TIMESTAMP`, never `DATETIME`.
- **`fluid verify` checks BigQuery data** (#673): `row_count` against the
  build's run records and `masking` of the declared columns, as it already did
  on Glue and Athena.
- **`fluid apply` reports each run to the Command Center** (#676), best effort
  and without the credential or any tofu output ever leaving as data.

### Fixed

- **One contract on aws and gcp no longer shares one state** (#676), so a gcp
  apply can never plan the destruction of the aws product.
- **Sovereignty fails closed on GCP** (#676). A binding with no region is
  refused instead of landing in `US`, `fluid generate iac` checks the region,
  `location.location` is checked like `location.region`, and the generated
  stage 6 runs `fluid plan --check-sovereignty`.
- **Stage 8 no longer crashes on a gcp build** (#676): `fluid policy-apply`
  raised a `TypeError` from its own log call.
- **The aws and gcp DAGs of one product no longer collide** (#676): the env is
  in the dag id and the schedule scope.
- **`--env gcp` with no gcp overlay is refused** (#676) when the workspace's
  `expected-environments` lists gcp for the product, instead of quietly
  running the base. A contract's own `environments` block only warns.
- **A BigQuery load runs where its table is** (#676, #673), never at a guessed
  `US`, and on the emulator it takes no real credential and checks the loaded
  rows by count when the job reports none.

## [0.16.7] — 2026-09-28

A Lake Formation grant that hides columns now applies on AWS, not only plans.
Found when the demo's AWS chain on 0.16.6 reached `tofu apply`: Lake Formation
refused the analyst's grant with `InvalidInputException: Permissions
modification is invalid`. moto does not check Lake Formation's permission
rules, so no emulator test could have caught it; the fix follows AWS's
permissions reference, and the live apply is the proof still to come.

### Fixed

- **A column-limited Lake Formation grant carries only `SELECT`** (#679).
  Lake Formation takes only `SELECT` on a column-filtered table ("This object
  is only used when granting a SELECT permission"), and refuses `DESCRIBE` to a
  principal holding a partial `SELECT`. forge-cli sent `[DESCRIBE, SELECT]`
  with `excludedColumns` or `columns`, so the apply failed. A column-limited
  grant now emits `SELECT` alone: the principal still sees the table's
  metadata for the permitted columns, because Lake Formation implies
  `DESCRIBE` with the `SELECT`. A column limit next to `ALTER`, `DROP`,
  `INSERT`, `DELETE` or `ALL`, or with no `SELECT`, is refused at emit, since
  Lake Formation refuses those beside a partial `SELECT`. Grant addresses are
  unchanged.

## [0.16.6] — 2026-09-28

A Lake Formation grant that hides columns from a principal now plans. Found
when the demo's first AWS chain run with column grants failed at the drift
gate on 0.16.5; checked with `tofu plan` against moto on the three demo
products (bronze and gold failed on 0.16.5, all three plan clean now).

### Fixed

- **A Lake Formation grant with `excludedColumns` sets `wildcard = true`**
  (#675). forge-cli emitted `table_with_columns` with `excluded_column_names`
  and no `wildcard`, which the AWS provider rejects at plan ("one of
  `column_names`, `wildcard` must be specified"), so `fluid diff` and
  `fluid apply --env aws` failed for any contract hiding a column from a
  grantee. `tofu validate` did not catch it, because the table names are
  references that are unknown at validate time. A column allow-list emits
  `column_names` and never a wildcard, and a grant with neither keeps the
  table block. A grant naming both is refused, as are excluded columns the
  schema does not have (a typo used to open every column), and column limits
  on a binding with no table are refused rather than dropped.

## [0.16.5] — 2026-09-28

The policies an AWS contract declares now reach AWS, a chained product reads
what its `consumes[]` names, and the drift gate reads the apply's own OpenTofu
state. How it was checked: real `tofu plan` / `apply` / `destroy` runs against
moto (S3, KMS, STS, Glue), DuckDB builds landing into a moto S3 server, and a
three-product chain built from a seeded Postgres. Not run against a real AWS
account in these changes' tests: moto enforces neither key policies nor Lake
Formation, so Athena reading an SSE-KMS location through Lake Formation
credentials rests on the AWS documentation.

### Added

- **Retention and encryption at rest reach S3, and `fluid verify` checks them**
  (#666). `exposes[].lifecycle.expire: true` turns the existing
  `lifecycle.retention` period into an S3 lifecycle rule scoped to the expose's
  prefix; without `expire` the period stays a declaration, as before.
  `binding.encryption.kms` (`product`, `none`, `alias/<name>` or a key ARN)
  gives SSE-KMS with a bucket key. `product` creates a rotated key and alias
  per product and bucket; an existing key must be enabled and symmetric, and
  customer managed when Lake Formation registers the location, or the plan
  fails before anything is written. Both are written only on a bucket the
  product owns. A bucket that already carries lifecycle rules or SSE is
  adopted, not recreated. `fluid verify` reads the live lifecycle rules and
  the key state and heads the objects, and fails when the rule, the key or an
  object's encryption differs from the contract. Both fields are in
  fluid-schema 0.7.6 only, so a contract needs `fluidVersion: "0.7.6"` to use
  them.
- **The DuckDB runner lands masked columns treated** (#667).
  `exposes[].policy.privacy.masking[]` with `hash`, `mask`, `tokenize` or
  `encrypt` is applied inside the `COPY`, after the quality gates, so the local
  file, the S3 object, the BigQuery staging file and the DLQ all hold treated
  values. `hash` is a salted SHA-256 (salt from `params.saltEnv`, default
  `FLUID_PII_HASH_SECRET`, at least 16 bytes) and equals the same expression in
  DuckDB and Athena, so joins still work. `k_anonymity` is refused rather than
  skipped. `fluid verify` fails a masked column that lands in cleartext.
- **An embedded-SQL build on DuckDB reads what its `consumes[]` names and lands
  on its AWS binding** (#669). Each entry the SQL reads becomes a view named by
  its `exposeId`, over the upstream contract's own binding under the same
  `--env` overlay (found under the nearest `fluid.workspace.yaml`, or a
  `FLUID_UPSTREAM_CONTRACTS` root). DuckDB's parser decides what the SQL reads;
  entries it does not read stay lineage only. A result whose binding is an S3
  bucket and path is written to the object the acquisition runner would write
  for that binding. `AWS_ENDPOINT_URL_S3` / `AWS_ENDPOINT_URL` now reach
  DuckDB's S3 secret, so a chain runs against MinIO or moto with AWS bindings.
- **`fluid diff` reads the apply's OpenTofu state** (#671). It emits the module
  the apply would emit and runs `tofu plan -detailed-exitcode` against the
  apply's own state and backend, so a tag or setting changed by hand, or a Glue
  table deleted by hand, fails `--exit-on-drift` where the SDK comparison saw a
  match or "absent, to be created". It writes no state and leaves the apply's
  workdir as it found it. With no reachable state it reports `not_checked` and
  the SDK comparison is the whole answer, as before. New flags:
  `--state-backend`, `--workspace-dir`, `--no-state-drift`, `--ensure-opentofu`.
- **`fluid generate ci --fluid-env-default ENV`** (#664) sets the env a build on
  the defaults runs in: a job chained by an upstream trigger, a first build and
  a first build after a restart all ran in `dev` whatever the pipeline was for.

### Changed

- On the DuckDB inline-SQL path, a `consumes[]` entry the SQL reads must
  resolve from the workspace or be bound by an explicit input of the same
  name; it used to be ignored with a warning (#669). Contracts that read their
  upstream by path in the SQL, or bind inputs under other names, keep building.
  An entry naming the building contract's own id is refused.
- `fluid plan --env X` records `contract_metadata.env: X` in `plan.json`, under
  `planDigest`. `fluid apply plan.json` without `--env` uses it, and a
  contradicting `--env` fails with `plan_env_mismatch` before any build (#669).
- With `AWS_ENDPOINT_URL[_S3]` set, a DuckDB `CREATE SECRET` that fails now
  fails the run (`ObjectStoreEndpointError`) instead of sending reads and
  writes to AWS (#669).
- The local provider's error text is redacted in the build output, in
  `runtime/out/local_apply_log.jsonl` and in its retry log lines (#669).

### Fixed

- **The live drift check reads a `{{ env.NAME }}` path where the build wrote
  it** (#665). #661 fixed `fluid verify`; `fluid diff` still anchored the raw
  template, called the file absent, and so passed a drifted file.
- **`fluid contract-tests` runs its comparison** (#670). It imported a function
  that does not exist and its fallback returned "compatible" for every
  contract. The fallback `plan`, `apply` and graph commands a broken install
  registered, which wrote a made-up plan and exited 0, now fail loud. A test
  holds every internal import to a module and name that exist.
- **The agent loop and team memory find the workspace root** (#668). `fluid
  forge --agent-loop` crashed on a missing import before starting, and
  `.fluid/team-memory.yaml` was never read; team memory in the prompt is now
  bounded. The Command Center catalog connector and `fluid version`'s GCP
  check had the same kind of import and are repointed.

## [0.16.4] — 2026-09-27

A local data path that names its directory through an environment variable now
means the same file for every stage. Checked by running the generated
pipeline's stage commands for two chained products against a seeded Postgres,
with the fix and without it.

### Fixed

- **`fluid verify` and `fluid diff` find a local file whose path uses
  `{{ env.NAME }}`** (#661). The build runner resolved the placeholder before it
  wrote, but the readers anchored the raw template, so stage 9 reported
  `Output file not found: <contract dir>/{{ env.NAME }}/...` for a file stage 7
  had just landed, and the drift gate called an existing table absent. This is
  how two products in separate CI workspaces share a landed file (a shared data
  directory named by a variable), so a chained downstream product's pipeline
  failed at stage 9 and never published. The readers now resolve exactly as the
  writer does, with one shared placeholder pattern.

## [0.16.3] — 2026-09-26

The generated 11-stage pipeline now runs as generated and its gates check what
they claim. How each part was checked: the generated Jenkinsfile end to end in a
throwaway Jenkins (stages 0-11, a parameterless first build, a restart) against a
seeded Postgres and a stub Command Center; the Lake Formation bucket policy with a
real `tofu plan` against moto; the Athena verifier and the live drift reads
against stubbed AWS and Google clients; the publish changes against a stub
server. None of it has run against a real AWS or Google account yet.

### Fixed

- **The generated Jenkins pipeline runs as generated** (#657). `CONTRACT`
  defaults to the contract you generated from; stage 0 installs into a
  workspace venv (the official Jenkins images are PEP 668 managed) and pins
  `data-product-forge` to the generating version with the extras the contract's
  base and overlays need (`--fluid-package-spec` overrides it); cleanup uses the
  core `deleteDir()`, so no ws-cleanup plugin is needed; every parameter's
  default is also its shell fallback, so a first build or a build after a
  Jenkins restart is a correct build. Stages 1-9 run the documented bundle
  chain: stage 1 bundles with `--env`, later stages read the bundle, stage 6
  plans in the mode stage 7 applies, and `--build-id` goes only with a
  `*-and-build` mode. Pip index parameters can no longer smuggle options.
- **The structural gates check what they claim** (#654). A tampered `plan.json`
  applied in a build mode used to exit 0: plan digest and mode are now checked
  before any build, on both engines. `--build-id` with a mode that runs no build
  is refused. The bundle records its source contract and env, and a stage asked
  for a different env refuses it. Relative local paths resolve at the source
  contract's directory in the build, the local provider and verify, so stage 9
  finds what stage 7 wrote. `verify --strict` fails a local output whose columns
  differ from the declared schema, and its console now matches its report.
- **The Command Center publish creates its assets** (#651). It sends
  `X-Organization-Id` from `FLUID_CC_ORG_ID` (or the caller's single
  organization), so a first publish is no longer a 400; `command-center` is
  accepted as the target name the docs use. A local-target verify no longer
  asks AWS Secrets Manager for credentials 22 times.
- **A Command Center product keeps its classification, env and versions**
  (#656). Visibility comes from the contract, so a confidential product is no
  longer published public; `fluid publish --env`; each publish records the
  contract through `/api/v1/contracts/sync`; `--dry-run` shows the body and the
  lineage edges it would create; a blank `FLUID_CC_ORG_ID` is an error.
- **A Lake Formation grantee no longer gets a raw S3 bypass** (#658). The AWS
  emitter gave every grantee an authoritative bucket-policy statement, so a
  grantee in the same account could read the data straight from S3 around Lake
  Formation. By default only grantees in another account get one;
  `lakeFormation.bucketPolicy: none | cross-account | all-grantees` chooses. The
  schema now says `admins` is authoritative.

### Added

- **`fluid verify` checks an S3+Glue binding** (#650): the Glue table's columns
  against the contract, and an Athena `COUNT(*)` against the rows the same build
  landed.
- **The drift gate compares the live target** (#652): local files and DuckDB,
  Glue and BigQuery tables. A target not created yet is not drift;
  `--last-applied` tells a contract change from drift.
- **Scheduled builds get an Airflow 3 DAG that runs `fluid apply`** (#653),
  written into the artifacts where stage 11 syncs it; schedule-sync deletes only
  within the product's own folder.
- **`FLUID_STATE_BACKEND`** is the default for `apply --state-backend`; a
  bucket-only value keys each contract's state apart (#654).
- **A Command Center organization can be named by its slug** (#660):
  `catalogs.fluid-command-center.organization: <slug>` in a FLUID config, for a
  setting that must be the same on every deployment (ids are generated per
  Command Center). An id still wins; a slug no organization carries writes
  nothing.
- `fluid generate ci` options: `--apply-mode-default`, `--schedule-sync-default`,
  `--scheduler-default`, `--scheduler-destination-default`,
  `--diff-last-applied` (needs the copyartifact plugin), `--fluid-package-spec`;
  `--default-publish-target` now also sets the parameter's default (#657).

### Changed

Upgrading can change results, deliberately:

- Relative local outputs land under the contract's directory, not the working
  directory.
- `verify --strict` fails on a schema-structure mismatch it used to downgrade.
- `apply --build-id` with a non-build mode, and a plan/apply mode mismatch, now
  fail.
- A drift gate that could never fire now can.
- Command Center products are private unless the contract says public.
- Lake Formation bucket policies no longer name same-account grantees;
  `bucketPolicy: all-grantees` restores the old output.
- Regenerate committed pipelines (`fluid generate ci`) to pick up #657.

## [0.16.2] — 2026-09-23

Applying one contract to AWS and to Google Cloud now does what the contract
says on both. How each part was checked: the Athena fix against a real AWS
account; the region pin and the region-move guard against an emulator; the
BigQuery load against a faked client, and not yet against real BigQuery.

### Fixed

- **A BigQuery binding now lands its rows in the table, with no keys.** The
  DuckDB build treated a `bigquery_table` binding's `location.path` as a file
  destination and wrote Parquet to `gs://`, which DuckDB can only authenticate
  to with HMAC keys, never Application Default Credentials; and even when that
  write succeeded, nothing loaded the rows into the table `tofu apply` had just
  created. The build now stages the file under `.fluid/staging/` (not `out/`,
  which is the local target's output) and one `load_table_from_file` job moves
  it into the declared table, passing the table's own schema and never creating
  one. Authentication is the client's `google.auth.default()`, so gcloud ADC, a
  VM's service account and Workload Identity Federation work with nothing to
  configure. A load that fails, finds no table, or loads a different row count
  from the file fails the build; so do the shapes this path cannot load (more
  than one stream, a sink other than Parquet, a mode other than `full_refresh`
  or `incremental_append`, a missing `gcp` extra), and they are refused before
  any work runs. The pattern follows dlt's BigQuery destination.
- **The BigQuery IaC emits types BigQuery accepts.** Any column type missing
  from the map was upper-cased verbatim, so a contract read from Postgres
  emitted `"type": "VARCHAR"`. A miss now resolves through the ODCS BigQuery
  physical-type table (VARCHAR, CHAR and UUID to STRING, SMALLINT to INT64,
  TIMESTAMPTZ to TIMESTAMP, BLOB to BYTES), which agrees with every existing
  entry, so no emit that was valid changes. The multi-word spellings the
  contract schema accepts (`double precision`, `timestamp with[out] time zone`)
  map as well. Of the 82 spellings the schema's type pattern admits, only a bare
  `array` still has no BigQuery form, because BigQuery arrays are REPEATED
  columns of an element type the spelling does not name.
- **`binding.location.project` is honoured.** It was ignored, so the dataset
  and table went to whatever project the environment named. It now goes on
  both resources and on a shared pool's dataset lookup, and brownfield import
  ids name the same project.
- **A build writes to its own expose.** The DuckDB runner took the contract's
  first expose whatever build was running, so in a contract with two builds
  the second wrote to the first one's destination; with a BigQuery binding
  that truncated the other build's table and reported success. It now takes
  the expose the build's `outputs` names, and keeps the first expose only for
  a build that names none.
- **`fluid verify` compares BigQuery types by what they are.** BigQuery answers
  with legacy names (INTEGER, FLOAT, BOOLEAN) for what was created as INT64,
  FLOAT64 or BOOL, so a correct BOOL column was reported as drift, and verify's
  own map had no `varchar`. Both sides now go through the IaC's type map and
  one normalisation.
- **Athena can read the Glue tables forge-cli creates.** A Parquet table was
  created with no Hive input format, output format or SerDe, so every Athena
  query failed with `HIVE_UNSUPPORTED_FORMAT: Unable to create input format`
  while the data sat correctly under the table's location. Parquet tables now
  declare the `MapredParquetInputFormat` / `MapredParquetOutputFormat` /
  `ParquetHiveSerDe` classes the `aws_glue_catalog_table` documentation uses.
  Measured on real AWS: the same table, with those added, answered
  `SELECT COUNT(*)` with the source's 10,172 rows. Iceberg (read through its
  metadata) and the formats not verified live are unchanged.
- **AWS resources are created in the region the contract names.** The provider
  took its region from the environment only, so a contract bound to
  `eu-west-1`, applied from a shell whose default was `us-east-1`, created its
  bucket and Glue database in `us-east-1` while the sovereignty check passed
  for `eu-west-1`. When every AWS binding names the same region it now goes on
  the provider block and wins over the environment. A contract whose bindings
  span regions keeps the environment's, with a warning, because one provider
  block has one region. Only real region codes are pinned: a jurisdiction
  (`EU`), a Google region, or an unresolved `{{ env.AWS_REGION }}` placeholder,
  as some shipped examples carry, still leaves the region to the environment.
- **`fluid apply` refuses to move resources between AWS regions silently.** A
  contract applied before this release from a shell in another region has its
  resources there. With the region now pinned, the refresh would drop them as
  drift and the plan would create them again: no destroy is planned, so the
  data-loss gate could not fire, and the originals would be left unmanaged.
  Before planning, the apply now reads the regions its existing state records
  and fails with `opentofu_region_moved` when they differ from the pinned one,
  naming both. Reproduced against an emulator with the previous release, then
  refused by this one through the real apply path.

## [0.16.1] — 2026-09-23

### Security

- **The Docker image moves to Python 3.14, and 0.16.1 is the first release to
  publish one since 0.15.3.** The v0.16.0 release job failed its Grype gate
  (`--fail-on high --only-fixed`) on CVE-2026-82049, a High in the Python 3.13
  interpreter whose fix is listed only from 3.14. The gate runs before the push,
  so no vulnerable image reached GHCR — which also means no 0.16.0 image exists
  there. Reproduced locally with the same Grype release CI pins (0.112.0) before
  changing anything: the 3.13.15 image exits 2 with exactly that one High; the
  3.14.7 image exits 0 with none. 3.14 was already in the classifiers and the
  CI test matrix. The PyPI packages for 0.16.0 were unaffected; this release's
  wheel and sdist are identical in content to 0.16.0's.

### Changed

- CI: five GitHub Actions bumps — docker/setup-buildx-action 4.4.1 (#636),
  codecov/codecov-action 7.1.1 (#637), aws-actions/configure-aws-credentials
  6.3.0 (#638), docker/build-push-action 7.4.0 (#639),
  github/codeql-action/upload-sarif 4.38.1 (#640). Two of them change
  `release.yml`, which only runs on a tag, so this release is their first run.

## [0.16.0] — 2026-09-23

### Security

- **A contract field could write files outside the output directory.**
  `fluid generate transformation` built each output path as
  `output_dir / rel_path` and called `mkdir(parents=True)` + `write_text` with
  no confinement check. Engines derive those keys from contract fields — the
  sql engine names each file `{NN}_{stages[].name}.sql`, the dbt engine builds
  `models/{layer}/{model}.sql` — and `stages[].name` is an unconstrained
  string in the schema. So a contract declaring
  `name: ../../../../ESCAPED` passed `fluid validate` ("✅ Valid FLUID
  contract") and the writer then landed the file four directories above
  `--output`, outside the project entirely. Contracts travel: `fluid
  federation` pulls them from other people's registries, so whoever runs
  `generate` is not necessarily whoever wrote the contract. The sibling writer
  in `cli/_template_mode.py` has carried this guard, and a comment describing
  this exact attack, since the copilot hardening; this one never had it. Every
  generated path is now resolved and checked against the output root *before
  any file is written*, so a late-sorting offender cannot leave a half-written
  project behind, and an escape raises `generated_path_outside_output_dir`
  rather than being silently skipped — a dropped stage would yield a project
  that looks generated but is missing a step.

- **A contract value could inject Python into a generated Airflow DAG (#620).**
  `AirflowDAGGenerator` interpolated contract values straight into
  `bash_command="{command}"` inside an f-string. A quote in any of them closed
  the Python literal and put the rest of the value at module scope, where Airflow
  executes it at DAG-parse time — no task run required. Seven vectors were
  reachable from `contract.fluid.yaml`, `builds[].script` being the sharpest
  (passed through verbatim by design). The DAG *header* was a second surface with
  the same hole: `dag_id`, `description`, `schedule_interval` and the module
  docstring. Values now route through `shlex.quote` at the shell layer,
  `py_str_literal` at the Python-literal layer, and `sanitize_identifier`
  wherever a value becomes a variable name — the pattern this repo already
  documents and applies in `cli/scaffold_composer`.

### Added

- **Cross-mesh upstreams can be named and pinned in the contract (#626).**
  A `consumes[]` entry could already reference a data product in another mesh,
  but the contract had nowhere to say *which* mesh or *what the upstream looked
  like when we composed against it*. `fluid apply` shipped a federation gate
  that wanted both, so the only contracts that could reach it were ones that
  failed `additionalProperties` first. Two optional fields on `consumeRef` in
  the 0.7.6 preview schema — `upstreamWorkspace` (an id from
  `federation/upstreams.yaml`) and `upstreamDigest` (`sha256:<64 hex>`) — with
  `dependentRequired` tying them together, so declaring a federated upstream
  without pinning a digest is refused at validate time rather than discovered at
  apply time, where "no pin" would read as "no drift". That only bites because
  the schema is drafted at 2020-12; under Draft 7 `dependentRequired` is
  silently ignored, so the pin would *look* enforced while accepting anything —
  there is a test pinning the draft for exactly that reason.

- **An engine that ignores `consumes[]` now says so (#625).** `consumes[]` is the
  mesh edge, and only the dbt engine acts on it (emitting `models/sources.yml`).
  The other five — `sql`, `spark`, `glue`, `dataform`, `dataflow` — generate from
  the build's own SQL and never read it, so a declared upstream was simply absent
  from the output and generation still exited 0. Not hypothetical: the shipped
  `customer-360` template declares three upstreams, uses `engine: sql`, and none
  of them appears anywhere in the five files it generates. Engines now declare
  `wires_consumes` and generation warns once per build, naming every upstream
  that will not be wired. Deliberately a warning — contracts in this shape are
  already out there, and it is a gap in what we generate, not an invalid
  contract. It does **not** invent a wiring: how a SQL or Spark job should
  address a federated upstream is a real per-engine design decision, and guessing
  it inside a warning would be worse than the silence.

### Changed

- **BEHAVIOUR CHANGE: generated SQL scripts now create views.** The `sql`
  engine emitted bare `SELECT` statements, so a script an operator ran
  against a warehouse had no side effect. Each multi-stage script now carries
  `CREATE OR REPLACE VIEW <output> AS …` and creates a relation in whatever
  catalog the session points at. This is what makes the generated project
  runnable at all — nothing previously created a relation under any name, so
  a stage's `FROM <upstream's output>` could never resolve — but it is not a
  drop-in: `OR REPLACE` drops grants on Snowflake and Databricks and makes
  any Snowflake stream over the view stale, and replacing an existing *table*
  of the same name fails loudly rather than converting it. Every emitted file
  says which it is, in its header: `-- Materialises: <name>  (CREATE OR
  REPLACE VIEW)` or `-- Not materialised: <reason>`. The single-stage
  `embedded-logic` pattern is unchanged — it has no `outputs` to name a view
  after and nothing downstream inside the contract to chain to.

- **Every shipped contract now declares the current stable schema, `0.7.5`.**
  All 13 templates under `fluid_build/templates/` and all 18 contracts under
  `examples/` declared `0.7.2` — three stable versions behind — so a reader who
  scaffolded one started on a contract that predated everything added in
  `0.7.3`-`0.7.5`. 31 files. Each was validated at `0.7.5` individually before
  the change and again after; zero failures. `0.7.5` and not `0.7.6`: 0.7.6 is
  bundled but preview, and a scaffold is the first contract a user owns.

  **Not included, deliberately.** Twelve code paths that CONSTRUCT a contract
  (the five `fluid import` importers, `product new`, the discover emitter, the
  streaming preview, the JDBC data-model scaffolder, the forge template builder)
  pin `0.7.3` as a literal. They should read the newest stable version instead —
  the same fix applied to the two serialisation fallbacks in the previous
  release — but doing so turns ~22 tests red, because those tests validate
  emitter output against a HARDCODED schema version and the schemas put `const`
  on `fluidVersion`. That is a test-expectation refactor across ten files, not a
  template bump, and it is scoped separately rather than bundled here.

- **Every shipped template is now schema-validated in CI.** The existing tests
  checked that scaffolding *produces* a `contract.fluid.yaml` and that the file
  exists; none ever validated its content, which is how the above shipped through
  every release. `tests/templates/test_shipped_templates_are_valid.py` asserts
  both layers the CLI runs, in its order — and that matters, because
  `validate_contract_file` returns `is_valid=True` on the broken template.
  JSON-schema validation passes; the refusal comes from
  `normalize_metadata_in_place`, reported separately as "metadata consistency".
  A fence built on the obvious API alone would have passed on the one file we
  knew was broken. It also refuses a template pinned to a preview schema.

- **No shipped default may point at a host that can never resolve.**
  `tests/test_no_reserved_tld_defaults.py` walks the AST of `fluid_build/` for
  string defaults on RFC 2606 reserved names. Three shipped as runtime endpoints
  — `openmetadata.test`, `datahub.test`, `airbyte.test` — each producing a DNS
  error where a configuration error belonged, and each found by hand after
  release. `.localhost` is deliberately permitted: five defaults use it
  legitimately (the Command Centre and marketplace dev endpoints, the LocalStack
  mocks), and a gate that fired on those would have been switched off. `tests/`
  is unscanned, because test code *should* use `.test` hosts.

- **`dbt-core` is capped below 2.0 everywhere forge installs dbt (#624).** dbt v2
  is a separate, non-Apache-2.0 distribution published as `dbt` / `dbt-oss`, and
  its emitter differences are distinct work from what these paths do. The runtime
  container install, the engine specs and the CI legs now state the ceiling.
  `DBT_ADAPTER_PACKAGE` still overrides it for anyone deliberately testing v2.

- **The federated-digest gate warns instead of aborting the apply (#626).**
  Unlike plan-binding — which compares two artifacts forge produced itself, so a
  mismatch means tampering — this gate's verdict depends on a third party's
  registry being reachable and honest. Hard-failing would let another team's git
  outage block production applies, and the first thing anyone reaches for is
  `--no-verify-federation`, permanently; a gate whose predictable outcome is its
  own escape hatch protects nothing. Findings log at WARNING under
  `apply_consumes_drift` with a payload shaped like `PlanBindingError`'s, so one
  log-parser regex matches both gates. Each violation carries a
  `violation_kind` — `drift` / `unreachable` / `unpinned` / `unknown-workspace` /
  `not-wired` — separating *the upstream really changed* from *we could not
  check it*. `FLUID_FEDERATION_TIMEOUT_SECONDS` (default 30s, listed in
  `fluid doctor --env`) bounds each git operation.

### Fixed

- **A binding that names a bucket now lands its data in that bucket.** The
  DuckDB acquisition runner read `exposes[].binding.location.path` and ignored
  `location.bucket`, while the AWS planner composes `s3://{bucket}/{path}` from
  the same binding. So `{platform: aws, location: {bucket: acme-lake, path:
  bronze/orders/}}` produced a Glue table pointing at
  `s3://acme-lake/bronze/orders/` while the rows were written to
  `./bronze/orders` on the local disk, and the build reported `Executed: 1,
  Failed: 0` either way: a correct catalogue over an empty prefix. Nor could a
  contract work around it, since putting the full URI in `path` fixed the data
  and broke the catalogue (`s3://acme-lake/s3://acme-lake/bronze/orders/`). Six
  faults had to go for `--env aws` to work at all: the path composition; the
  destination's extensions and credentials, which were both derived from the
  SOURCE, so the COPY went out unauthenticated and returned 403; an object
  written *at* the prefix rather than inside it, invisible to anything listing
  that prefix for files; three other connections (the incremental cursor
  read-back, the late-arrival split, the post-land scan) that could not read a
  bucket, the first of which runs before a stream is marked succeeded and so
  turned a successful write into a FAILED run; `Path("s3://b/k").exists()`
  always being False; and `fluid verify` dispatching on `format` rather than
  platform, which sent an `aws`/`parquet` expose to the local-file branch.
  Verified against a real AWS account: 10,172 rows readable through the Glue
  table's own prefix. The combination was uncovered because the MinIO
  end-to-end test points at a full `s3://…/orders.parquet` URI, the branch that
  always worked. (#644)

- **Generated SQL scripts now bind the inputs the contract declares.** A
  build declares which file backs which name under
  `builds[].properties.parameters.inputs`, and the author's SQL then says
  `FROM <name>`. `fluid apply` has always honoured that — the local provider
  registers each input as a DuckDB view — but the *emitted* script did not,
  so a project that applied cleanly still failed standalone with
  `Catalog Error: Table with name <name> does not exist!`. Six worked
  examples were in that state (02–06 and `local/high_value_churn`); measured
  before and after, 0/6 of them produced a runnable script, now 6/6. The
  statement is the provider's own, shared through
  `providers/_duckdb_read.build_register_view_sql`, so `apply` and the
  generated script cannot drift apart again.

- **Two generated files could resolve to one path, silently losing a
  stage.** The `#634` writer guard rejected paths that *escape* the output
  directory but not two that land on the *same* file inside it: a stage
  named `z/../01_a` normalises onto `01_a.sql`, and the later write replaced
  the earlier stage's SQL with no diagnostic. Rejected now as
  `generated_path_collision`, in the same place as the escape check, so it
  covers every engine.

- **`fluid apply` no longer implies `consumes[]` is wired when it is not.**
  The local provider's fallback read `c["path"]` / `c["location"]["path"]` /
  `c["id"]` from a consumeRef; the schema is `additionalProperties: false`
  and permits none of them, so it could never fire for a contract that
  validates. A consumeRef carries a logical address only. Applying a
  contract whose `consumes[]` are unbound now says so, and names the field
  that does bind a reader.

- **A shipped template did not validate, and nothing noticed.**
  `fluid init --template multiple-outputs` scaffolded a contract that `fluid
  validate` rejected immediately: `metadata.layer: 'Multi-Layer'` is not in the
  permitted set. It is a medallion demo exposing bronze, silver and gold outputs,
  each already carrying its own per-expose layer label; the product-level value
  was trying to say "spans layers", which the field cannot express. It now
  declares `Gold`, the layer of the output a consumer actually reads.

- **`fluid import airbyte` could not be pointed at an Airbyte instance.**
  `AirbyteImporter.server_url` defaulted to `https://airbyte.test`, and the
  importer registry constructs the class bare, so that placeholder was the
  endpoint every real run dialled. RFC 2606 reserves `.test` precisely so it
  never resolves, and no flag, environment variable or config key existed
  anywhere to supply the right URL. The command failed with:

  ```
  why  cannot resolve hostname 'airbyte.test'
  fix  Check the source path/identifier and that the foreign tool's config is well-formed.
  ```

  a DNS diagnostic, and a `fix` line sending the reader to inspect a workspace
  id that was never the problem.

  The endpoint now comes from `--server-url`, falling back to
  `FLUID_IMPORT_AIRBYTE_URL` (parallel to the catalog convention
  `FLUID_CATALOG_<NAME>_URL`). With neither set, the importer refuses by name
  and does so *before* any client is constructed, so a missing setting costs no
  socket and returns immediately instead of after a resolver timeout.

- **Generated CI pipelines ran nine `fluid` commands that do not exist (#620).**
  `fluid generate ci` writes pipelines that *other people's* CI executes, where a
  bad flag fails in their logs and reads as a FLUID bug. Nine invocations were
  wrong across all seven CI systems — `--check` flags that were never added,
  `fluid audit` / `benchmark` / `lineage` which are not commands at all,
  `viz-plan --output` where the flag is `--out` and `plan` is a required
  positional. Several were unguarded: GitHub's `plan` job carries
  `needs: generate`, so an exit 2 there skipped everything downstream. Replaced
  with the real equivalents where one exists. The regression test parses every
  emitted invocation against the real argparse tree; 16 of its 30 cases fail
  without the change.

- **Two `actionId`s differing only in punctuation dropped a task (#623).**
  `sanitize_identifier` is deliberately not injective — `a-b` and `a.b` both
  become `a_b` — so two distinct actions collapsed onto one Airflow task id and
  one silently overwrote the other. Task identifiers are now uniquified with a
  `_2` / `_3` suffix on collision.

- **A forged contract with semantics emitted a dbt project that would not parse (#621).**
  `generate_semantic_models` wrote `model: ref('<exposeId>')`, but the intent,
  multi-stage and embedded-logic emitters name their files from *stage* names, so
  the semantic model referenced a node nobody emitted and dbt rejected the whole
  project. It now takes the set of model names generation actually wrote and
  drops — loudly — any semantic model whose target is missing.

- **The emitted dbt YAML now parses on dbt-core 1.8 through dbt-oss 2.0 (#621).**
  Version floors were *measured* against a rig of nine dbt-core releases plus
  dbt-oss 2.0.4 rather than taken from the docs — which mattered: the documented
  floor for nested generic-test `arguments:` is 1.10.5, but 1.10.5 and 1.10.7
  both fail and the real floor is 1.10.8, where the behaviour flag flips.
  `access` and `freshness` under `config:` parse everywhere but are *silently
  ignored* below 1.10.5, which only reading `target/manifest.json` revealed.

- **`--mesh-hub` skipped the `dbt deps` step it depends on (#621).** The gate ran
  `dbt deps` only when `packages.yml` existed, but under `--mesh-hub` the pins go
  into `dependencies.yml` (dbt forbids both), so the one configuration that most
  needed it never ran it.

- **The shipped quickstart READMEs documented 36 commands that do not work (#622).**
  Every template README is the first thing a new user follows. The guard walks
  the fenced commands in each and parses them against the real argparse tree; a
  permissive fence-tag rule matters — restricting to ` ```bash ` desynchronised
  the pairing and hid 21 further defects.

- **One unreachable upstream could hide drift in another (#626).** The federated
  fetch sat outside any per-row handler, so the first network error propagated
  out of the whole walk, discarding violations already collected for other
  upstreams. One team's registry going down made a genuinely drifted pin
  elsewhere invisible.

- **A registry being down was reported as missing FLUID code (#626).** All three
  fetch backends return `None` on timeout / 404 / TLS error / auth reject / SSRF
  block / failed clone, and `fetch_federated_digest` turned that into a bare
  `NotImplementedError` — the same exception used for "no fetcher for this
  workspace kind". A partner mesh being *down* surfaced as "fetcher for
  kind='http_registry' not yet wired", sending operators to read our source
  instead of their registry, and `unreachable_count` read 0 exactly when an
  upstream was unreachable.

- **The federated git fetch could hang an apply indefinitely (#626).**
  GitPython's `kill_after_timeout` is *accepted* by `Repo.clone_from` — it sits
  in `execute_kwargs`, so it is not rejected the way an unknown kwarg is — and
  ignored, because `_clone` passes `as_process=True` and the watchdog runs only
  on the other branch. Measured against a TCP listener that accepts and never
  speaks: `clone_from(..., kill_after_timeout=3)` was still running at 90s while
  `subprocess.run([...], timeout=3)` raised at 3.0s. This was the live path, not
  a latent one — GitPython arrives transitively via `dlt` and was tried *first*.
  The gitpython path is removed; every federated fetch goes through the bounded
  shell-out. Nothing is lost: GitPython shells out to the same `git` binary, so
  it was never a fallback for git being missing.

## [0.15.3] — 2026-09-15

`0.15.2` existed to stop the front door pointing at links that go nowhere. It
missed the worse case: links that go somewhere, owned by someone else.
`docs.fluid.io` resolves, returns 200, and belongs to an unrelated fintech, and
`fluid validate` printed it into its own error output:

```
$ fluid validate contract.fluid.yaml
  ❌ Unknown provider 'redshift'
  📚 Docs: https://docs.fluid.io/providers
```

### Fixed

- **Shipped templates pointed at another company's documentation site.**
  `docs.fluid.io` resolves and returns 200, and it belongs to an unrelated fintech
  ("Contracts Overview | Fluid Technical Docs", "Financial system of the future").
  Twelve template READMEs and seven `documentation_url=` values in
  `contract_validation.py` sent users there. A dead host announces itself; a live
  foreign site sharing our product word does not. 28 links repointed in total,
  also covering `community.fluiddata.io` and `docs.fluiddata.io` (NXDOMAIN),
  `fluid-community.slack.com` (404, no such workspace) and
  `github.com/yourusername/fluid-mono`. Topics with no page go to the docs root
  rather than to a URL invented from their own name; all 25 resulting first-party
  URLs verified 200.
- **`fluid mcp output-port` linked a repository that does not exist.**
  `github.com/Agenticstiger/forge-docs` — a hyphen where the repository has an
  underscore — inside the gateway's own warning about untrusted networks.
- **The scaffolder wrote a 404 into the user's own README.**
  `github.com/your-org/{project_name}/issues` substituted the project but not the
  org. Now inline code with angle brackets, so it reads as a placeholder.

### Changed

- **The link gate missed all of the above, and now does not.** `documentation_url`
  was absent from the attribute alternation; the retired-host list held four names
  and none of this family; and the canonical-host rule ran over `.py` only, so 21
  template README links were never examined. Two new assertions that do not depend
  on guessing an attribute name: a first-party GitHub link must name a repository
  that exists, and no URL may carry a placeholder token that renders as a real
  address. The canonical-host rule is also now scoped to hosts containing
  fluid/forge/agenticstiger — a BigQuery error rightly links to cloud.google.com,
  and catalog connectors carry sample entries pointing at a customer's own Alation.

## [0.15.2] — 2026-09-15

A second front-door patch, for the same reason as the first: the release notes for
`0.15.1` announced that dead documentation domains had been cleaned up, and
`0.15.1` shipped a third one. It was not a stray string — it was `_DOC_BASE`, the
documented single source of truth, so **every** typed error and every catalogued
event printed it. One mistyped filename was enough to see it:

```
$ fluid validate no-such.fluid.yaml
   📖 https://forge.fluid.dev/ref/troubleshooting#err_contract_file_not_found
```

### Fixed

- **Every typed error linked to a domain that does not exist.** `_DOC_BASE` was
  `https://forge.fluid.dev/ref`, and `forge.fluid.dev` has no DNS record — so all
  15 typed `FluidUserError` classes and all 54 catalogued events printed a `Docs:`
  line pointing at nothing. This is the third dead host found in the same family;
  the sweep that removed `fluid-build.dev` and `dustlabs.co.za` in 0.15.1 grepped
  for *those* names, so a host that had never been grepped for survived it (#612)
- **The docs path was composed out of the topic name**, which is the half a host
  swap would not have fixed. `f"{DOC_BASE}/{topic}"` produced a confident URL for
  every topic word whether or not a page existed, and eleven of the sixteen topics
  in use had none — including `troubleshooting`, the fall-through destination for
  34 of the 54 catalogued events. Topics now resolve through a map of routes the
  documentation site actually serves; an unmapped topic goes to the troubleshooting
  page instead of to a URL invented from its own name. All 24 resulting URLs
  return 200, verified individually (#612)
- **A scaffolded Airflow DAG shipped a dead link into the user's repository.**
  `fluid init` wrote `https://fluid.dev/docs/orchestration` into the generated
  DAG's `README.md`, so the link persisted in their project rather than scrolling
  past in a terminal. Found by the new gate on its first run, not by a grep (#612)
- **`fluid apply --help` printed a 404** (`.../blob/main/docs/apply.md` in the
  schema repo), and `fluid demo`'s failure panel sent bug reports to the schema
  repo's issue tracker rather than this project's (#612)
- **The docs link wrapped mid-URL, which made it unclickable.** Found by running
  the built wheel, not by reading the diff: Rich word-wraps at the terminal width,
  so all three sites that print a `📖` link put a real newline inside the URL at 80
  columns. The dead `forge.fluid.dev/ref/…` links this release replaces were short
  enough to fit; the real routes are not, so fixing the destination without this
  would have traded a link that went nowhere for a link you cannot click. All
  three now pass `soft_wrap=True` (#612)

### Changed

- **`test_docs_urls_are_well_formed_under_doc_base` is gone**, because it could
  not fail: "well formed" meant "starts with the base the builder had just
  prefixed to it". It was green throughout the releases in which every URL it
  checked pointed at a nonexistent host. `tests/cli/test_doc_links.py` replaces it
  and asserts the two things that actually break — the host stops existing, and
  the path is invented rather than taken from a page somebody wrote. Each of its
  assertions was verified by seeding the defect and watching it go red (#612)

## [0.15.1] — 2026-09-15

A front-door patch. Every fix here was already on `main` but landed *after* the
`v0.15.0` tag, so none of it reached anyone who ran `pip install` — which is the
whole reason this release exists.

### Fixed

- **`fluid --version` advertised a shipped feature as forthcoming.**
  `next_milestone()` looped for a milestone with a future date, found none — the
  bundled `roadmap.md` holds exactly two, both past — then fell through to
  `return milestones[-1]` and the caller labelled it `next:`. The line read
  `next: v1.5 · MCP Server · by Jun 11, 2026`, three months overdue, while the
  MCP output port is a headline feature of the release printing it. `roadmap.md`
  says so itself: "MCP Server (originally v1.5) shipped early". It now returns
  `None` when every milestone is past and the teaser stops printing; every caller
  already guarded on the empty string. No roadmap dates were invented. (#607)
- **The banner claimed `forge-cli v1.0` while the package shipped 0.15.0**,
  contradicting `fluid --version` two lines above it. It now reads the real
  distribution version. (#607)
- **The product did not link to its own documentation.** `fluid` and
  `fluid --help` sent readers to `github.com/open-data-protocol/fluid` — the
  *schema* repo — rather than the documentation site, and `fluid market --help`
  deep-linked to a path that returns 404. The schema link is kept, relabelled
  `Spec`, and repointed at the rendered spec site. (#607)
- **The footer credited a domain with no DNS record.** `dig` returns nothing for
  `dustlabs.co.za`, apex or www, while the package metadata, NOTICE and LICENSE
  all name Agentics Transformation Limited. 0.14.1 corrected the published entity
  across the distributed files and this string was missed. (#607)
- **Every scaffolded contract carried a dead domain on line 2.** `fluid init`
  wrote `# Docs: https://fluid-build.dev/docs/contracts` into the second line of
  the first file a new user ever sees, and `fluid-build.dev` has no DNS record
  either. Eight occurrences across six modules, each mapped to the page that
  actually covers the topic rather than blanket-replaced; all six resulting URLs
  return 200. (#609)

### Changed

- **The README is the PyPI project page** (`pyproject.toml` sets
  `readme = "README.md"`) and cannot be edited after a version publishes. It was
  698 lines, 26 headings and 10 badges, ~180 of them restating a CLI reference
  that lives on the docs site. Now 372 lines, 11 headings, 6 badges, with every
  claim carrying the command that proves it. The old "the only line that moves"
  claim about swapping cloud is replaced by the measurable one: strip the
  `binding:` block from the three platform contracts and the remainder hashes
  identically. Sections that a reader could not reproduce — a Docker pull that
  returns `unauthorized`, a provider `fluid providers` does not list — are gone
  rather than softened. (#608)



## [0.15.0] — 2026-09-14

A sovereignty enforcement release, and the one whose upgrade notes matter. Five
things can break an existing user. A contract that passed `fluid validate` on
0.14.1 can fail here, because data-residency controls that reported clean while
checking nothing now actually block: a pinned `jurisdiction` is enforced against
the binding region, the engine's defaults now match the schema's stricter ones,
and the region table no longer places London in the EU. A jurisdiction-pinned
contract makes `fluid mcp output-port serve` refuse to start on the default stdio
transport, and on HTTP with no auth mode set. The MCP SDK ceiling widens from
`<2.0` to `<3.0`, so a fresh `pip install` resolves the 2.x generation, where a
client can no longer self-attest identity through `clientInfo` extras. Federation
digests for a `git_registry` upstream are computed differently, so a
`consumes[].upstreamDigest` pinned on 0.14.1 reads as drift and aborts
`fluid apply`. And DataHub `customProperties` keys lose their dots
(`fluid.layer` → `fluid_layer`), so anything keyed on the old spelling must be
updated.

### Security

- **`FLUID_MCP_JWT_CLAIM_MAPPING` merges over the default claim mappings instead of
  replacing them.** `AuthValidator.from_env` assigned the parsed value wholesale, so
  mapping one extra claim silently dropped `sub`, `model`, `use_case` and
  `tenant_id` — switching off the `agentPolicy` gate and emptying every
  `${caller.*}` row filter, with no error and a server still reporting healthy.
  0.14.1 named this as the reason a custom mapping could be self-attested past
  (#492). Mapping a default away explicitly still works. (#574)
- **Integration CI jobs no longer inherit a token that mints cloud credentials.**
  `integration.yml` granted `id-token: write` and `issues: write` at the workflow
  level, which every job without its own `permissions:` block inherits: all eight
  held both, three need either. That token federates into GCP and assumes an AWS IAM
  role, and it mattered most for `multi-lang-mcp-conformance`, which executes
  third-party code at build time with no lockfile. The floor is now
  `contents: read`, with each job declaring what its own steps call. (#586)

### Added

- **Caller-jurisdiction enforcement at query time in the MCP output port — on by
  default, fail-closed, verified-claims-only, derived from the contract's own
  `sovereignty` block.** A contract declaring `jurisdiction: EU` used to refuse to
  provision into `us-east-1` and then answer a tool call from a caller sitting
  there; the gate is now live on every `fluid mcp output-port serve`, with no flag
  either way, keyed on a pinned `jurisdiction` with `crossBorderTransfer` unset or
  false. Every escape hatch is in the contract — `crossBorderTransfer: true`,
  `jurisdiction: Global` or `Multi-Region`, or no `jurisdiction` at all — so every
  contract that never pinned one is entirely unaffected, policy digest included. A
  claim binds only when verified, read from what the HTTP middleware stamps after
  validating a JWT or mTLS identity and never from self-attested `clientInfo`;
  matching is exact and case-sensitive. **Newly blocking:** a jurisdiction-pinned
  contract refuses to serve at startup with exit 2 on the two deployments that could
  never satisfy the rule — `--transport stdio`, the default, which carries no
  headers, and `--transport http` with `FLUID_MCP_AUTH_MODE` unset — each message
  naming the jurisdiction and the command that fixes it. `jurisdiction` also joins
  the default JWT claim mappings, and is the one default whose absence closes the
  gate: an operator whose IdP names the claim differently is locked out until they
  map it. (#574, #576, #581)
- **`agentPolicy` decisions come from one function, with a closed reason vocabulary
  and a policy digest.** `fluid_build/policy/decision.py` owns `decide()`, a
  `ReasonCode` str-enum over all eleven outcomes, and `CHECK_ORDER` — precedence as
  data rather than as an accident of statement order. Wire values stay the existing
  kebab-case strings, so a caller comparing against `"tool-not-allowed"` keeps
  working, but `ReasonCode("invented")` now raises. `policy_digest()` is a
  `"<scheme>:<sha256>"` over the RFC 8785 canonical form of the effective rule
  lists, sorted and de-duplicated so authoring order cannot change it, with `None`
  and `()` digesting differently. 35 portable vectors ship inside the wheel at
  `policy/data/vectors/agent-policy-vectors.json`, `rfc8785` is a new runtime
  dependency, and the module is enrolled in `mypy --strict`, since a silent `Any` in
  a gate that decides data access is a silent allow. (#568, #574)
- **The MCP gateway's audit record names which rules produced a decision, not just
  where they came from.** `policySource: contract` cannot distinguish the contract
  before an `allowedModels` edit from the contract after it, so once the contract
  moved on a denial was no longer reconstructable. Every `data_access` event, allow
  and deny alike, now carries `policyDigest`, a `jcs-sha256:<hex>` that hashes
  identically however the YAML was written. **Additive:** one new key, nothing
  renamed, retyped or removed, so only a reader validating against a closed key set
  notices; it completes the digest #568 introduced, which was written into no record.
  (#591)
- **An ODCS contract can be read back out of the OpenMetadata catalog.** The
  registrar could publish an ODCS document and not retrieve one, so a contract held
  in the catalog could not drive `plan` / `apply`; `fetch_odcs_contract(fqn)` closes
  the read half. It prefers the table's `extension.odcs_contract`, preserved
  verbatim, over OpenMetadata's native ODCS export, whose converter drops `servers`
  and six other top-level blocks (open-metadata/OpenMetadata#30493), and keeps that
  route as the fallback for a contract fluid did not publish. No command calls it
  yet: this is the library half, and nothing in the CLI behaves differently. (#588)
- **A validate-time gate for GCP bindings the IaC emitter cannot resolve.**
  `fluid validate` now reports a GCP expose that would emit no resource, resolved
  through the emitter's own dispatch so it can neither block a contract that would
  have emitted nor pass one that emits nothing. Only a format that *names* a
  container while omitting the location key it needs is an error — in practice
  `format: gcs_file` with no `location.bucket`; everything else is a warning, since
  `fluid validate` runs for contracts that never reach `fluid generate iac`. Formats
  with no `hashicorp/google` resource by design stay silent, and Iceberg exposes are
  left to the Iceberg gate. (#580)
- **Snowflake is now exercised over the real wire protocol, keyless.**
  `tests/providers/test_snowflake_server_emulated_happy_path.py` runs the
  **unpatched** snowflake-connector-python against fakesnow's server
  (`fakesnow[server]`, Apache-2.0) — real login, real query submission, real
  result-set decoding, and `_initialize_session`'s `USE` statements reaching an
  endpoint for the first time. The in-process `fakesnow.patch()` test could cover
  none of that, because it replaces the connector rather than talking to it. Runs on
  every PR including forks, at no cost. (#595)
- **A runnable sovereignty and platform-swap example corpus.**
  `examples/sovereignty-platform-swap/` carries one contract compiled against AWS,
  Google Cloud and Snowflake with the `binding` block as the only difference between
  the three files, plus a README walking `validate` → `policy-check` → three
  `generate iac` runs → the ODPS/ODCS export. Re-run from this checkout, AWS emits
  three resources, GCP two and Snowflake three, exactly as the README claims. The
  contracts declare `fluidVersion 0.7.6`, a preview version: validatable because
  they name it explicitly, never the default for an untagged contract. (#556)

### Changed

- **The MCP SDK ceiling moves from `<2.0` to `<3.0`, so a fresh install resolves the
  2.x generation.** `mcp` is a core dependency, not an extra, so
  `pip install data-product-forge` picks up 2.x from here on; an existing environment
  does not move until it is upgraded, and pinning `mcp>=1.20,<2.0` still works. The
  dual-support seam shipped in 0.14.1 (#492); what changed is the audit that lets the
  ceiling follow it. `tests/test_mcp_sdk_rename_guard.py` parses the 21 modules that
  import the SDK or live in forge's own MCP packages and asserts that none of the
  twelve renamed field names it tracks is read by its camelCase spelling, which on
  2.x returns the default instead of raising — so an error reads as success and a
  schema reads as empty. That is a static scan, not a behavioural proof; the 0.14.1
  note that a dozen read sites fail silently under 2.x described the danger rather
  than the state, since those sites closed when dual support landed. The drift
  canary's `range-max` leg now installs `mcp>=1.20,<3.0`, resolves to 2.x and is no
  longer warn-wrapped, selects with `pytest -k mcp tests/` rather than a hand-kept
  list that had drifted, and asserts per leg which SDK major it installed. **One
  behaviour difference survives the widening:** on 1.x a client can self-attest
  identity (`model`, `useCase`, tenant attributes) as extra fields on `clientInfo`,
  because v1's `Implementation` is `extra="allow"`, while 2.x drops unknown fields at
  wire-parse. Forge's own clients go through
  `_mcp_compat.self_attesting_client_kwargs` and are unaffected, but a third-party
  client hard-coded to that shape stops being attested with no error; move it to the
  client's declared capabilities under `experimental.fluid`, which both generations
  parse. Self-attestation does not bind when authentication is enforced in any case
  (#492). (#604)
- **The sovereignty engine's defaults now mirror the schema's, which makes them
  stricter.** Every bundled schema from 0.7.1 to 0.7.6 declares
  `enforcementMode: strict`, `dataResidency: true` and `crossBorderTransfer: false`,
  while the engine defaulted to advisory / false / true — the permissive inverse of
  all three, so a strict GDPR contract with exposes in `eu-west-1` and `us-east-1`
  printed `PASS`. The three values are now constants read by both the engine and the
  display, pinned against the bundled schema. This is the entry most likely to be
  felt on upgrade, because `fluid validate` shares this engine with `fluid plan`: a
  contract that declares `sovereignty` while omitting those keys is judged under
  strict residency and can newly fail a stage it passed on 0.14.1 — an
  `allowedRegions` mismatch that warned now errors, and `dataResidency: true` with
  exposes in two jurisdictions now trips the cross-border check where 0.14.1 said
  nothing. Declaring the keys explicitly restores the old evaluation. (#510)
- **A resource-free OpenTofu module is now an error, and `--allow-empty` opts out.**
  Nothing downstream can catch one — `tofu validate` calls it valid — so
  `fluid generate iac` fails with `generate_iac_empty_module` instead of warning and
  exiting 0, naming the `binding.location` fields to check plus the `fluid validate`
  gates that name the missing field. Pass `--allow-empty` when a module that
  provisions nothing is intended. (#546)
- **Internal, no runtime change — the weekly model-catalog refresh fetches from
  OpenRouter and needs no API key.** `scripts/update_model_catalog.py` is maintainer
  tooling, pruned from the sdist and imported by nothing in `fluid_build`, so no
  credential is asked of a user and the CLI makes no such call; the old host is
  undeployed, so the job could never have worked with or without the key it asked
  for. OpenRouter's public `/api/v1/models` republishes the `intelligence_index`
  alongside pricing, the two signals the selector ranks on, and degradation stays
  quiet: nothing rankable means a stderr warning, the committed catalog untouched and
  exit 0. Three deliberate drops at the fetch boundary — priced variants (`:batch`,
  `:free`), models with no intelligence index, and `gpt-oss`, unreachable by the
  OpenAI adapter — plus a family-prefixed `TRACKED_MODEL_PREFIXES`, which had listed
  ids OpenAI no longer ships and so skipped that provider in silence. Capabilities
  for a promoted model now come from `litellm.model_cost` read in-process, falling
  back to the per-provider `DEFAULT_CAPABILITIES` table wherever litellm omits a
  flag, because an omitted flag means no data and not "cannot" — without it a freshly
  promoted flagship would report `structured_output=False`, a wrong `False` that
  fails closed. `fluid_build/cli/llm_models.json` is byte-identical to 0.14.1: this
  lets a refresh be proposed, it is not one. (#593, #594)
- **CI:** routine pinned-action bumps — seventeen of them, including two majors
  (`actions/stale` 10.4.0→11.0.0, `actions/labeler` 6.2.0→7.0.0) and
  `github/codeql-action/upload-sarif` 4.37.9→4.38.0. Workflow files only; no runtime
  dependency moved. A `semver-major` ignore on `mcp` was added while the SDK audit
  was open and removed when it closed, so the shipped `dependabot.yml` carries no
  `mcp` entry, the same as 0.14.1. Also back-fills the `[0.14.1]` changelog section
  that tag shipped without, and points the README's latest-version line at it. (#502,
  #503, #504, #505, #507, #508, #521, #522, #523, #524, #525, #549, #550, #552, #565,
  #566, #567, #600, #603, #604)
- **Internal, no runtime change:** a terminology gate,
  `tools/product_guardrail/check.py`, now runs in the CI lint job and as
  `make guardrail`, checking the repo's prose against a vendored canon it verifies
  offline; its first run found the two entity misses below, and follow-ups closed
  three hook defects (an owner approval discarded after a 600-second TTL, an unlocked
  read-modify-write in the grace recorder, and a hardcoded `.git/hooks` that installs
  nothing in a worktree). The BigQuery emulator now seeds its full project set from
  `--data-from-yaml`, so four tests stop 404'ing against a container that was up and
  serving; `fluid verify` gained its first live-cloud GCP coverage in both
  directions; and `HONESTLY_TESTED.md` lost a claim its own deferred section
  contradicted, pinning the Snowflake CLI matrix at 2 of 6 in both places. The
  multi-language MCP conformance nightly also passes for the first time since it was
  added, every defect having been in the harness rather than the gateway; the Go and
  Rust clients attest no caller identity at all, so their deny scenarios passed
  vacuously and are now gated off behind `RUN_MULTILANG_GO_RUST` rather than deleted,
  pending #583. (#512, #547, #561, #562, #563, #584)
- **Internal, no runtime change:** the guards that now keep the emulated lane honest,
  plus four unrelated pieces of CI and test debt.
  `scripts/ci/assert_lane_coverage.py` fails that lane unless every provisioned area
  contributed a passing test, and a security review of it found two bugs of the class
  it exists to catch — substring matching, which a trivially-passing
  `..._happy_path_stub.py` would have satisfied with no emulator running, and a
  global rather than suffix `.py` strip — both now anchored and pinned by tests. The
  label gate standing in for the removed required-reviewer rule is enforced by
  `scripts/ci/check_env_label_gate.py`, which parses the workflow instead of grepping
  it; a missing `LOCALSTACK_AUTH_TOKEN` fails the nightly with the `gh secret set`
  command that fixes it while pull requests and manual runs still skip clean; and the
  LocalStack image is pinned to `2026.08.2` after tracking `stable` through the
  dormancy. Elsewhere: the two GCP emulator SDK probes hardcoded their dataset and
  bucket names, so a repeat create against a warm container failed like a network
  problem rather than an API-fidelity gap, fixed with the UUID suffix the rest of the
  tier already used; `iac-tests.yml`'s two Stage 3 jobs are now gated on an
  `IAC_STAGE3_ARMED` repository variable instead of failing at the OIDC step for
  credentials nobody has set, every scheduled run having been red for at least eight
  days; and two suites flaky under `-n auto` were fixed by patterns already in the
  repo, the UX wall-clock budgets moving to `tests/perf/` with no threshold changed
  and four Hypothesis file-IO properties gaining their sibling's `deadline=None`. The
  leftovers: two always-xpassing `xfail` markers removed so the tests assert again,
  two SC2034 wait-loop warnings turned into elapsed-time reporting, and the
  deliberately unquoted `${CHANGED}` in the secret-scan step given a written
  `shellcheck disable=SC2086`, since quoting it passes one impossible filename and
  scans nothing. (#590, #595, #597, #601, #605)

### Fixed

- **`sovereignty.enforcementMode` now means what the schema says it means, and it had
  failed in both directions at once.** Check 3, the only check that reads
  `jurisdiction`, hardcoded `severity="warning"` and so could not block in any mode,
  while check 4's cross-border mismatch hardcoded `severity="error"` and failed the
  build under advisory — `cli/validate.py` routes on the rendered ❌ / ⚠️ / ℹ️ prefix,
  which the returned boolean could not override. Severity is now one function of the
  mode (strict → error, advisory → warning, audit → info) across checks 2, 3 and 4,
  and `is_valid` is `not has_errors` in every mode, rather than re-applying the mode
  and letting `is_valid=True` return alongside an error. Two carve-outs stand:
  `deniedRegions` is an error in every mode, and an unmappable region stays a warning
  under strict, because "unknown" is an inability to evaluate rather than a
  violation. **Newly blocking:** a contract pinning `sovereignty.jurisdiction` whose
  binding region resolves elsewhere now fails `fluid validate` with exit 1 under
  strict, the default. `jurisdiction: Global` was already skipped. (#569)
- **`jurisdiction: Multi-Region` is treated as the catch-all it is.** It is a valid
  value in every bundled schema's `$defs.sovereignty` enum, but check 3 compared it
  to the region's resolved jurisdiction by equality and special-cased only `Global`,
  so nothing could ever satisfy it: no region resolves to `Multi-Region`, because it
  is not a place. Harmless while check 3 was hardcoded to `warning`; once the mode
  decides severity and strict is the default, it would have refused every region such
  a contract could name. Check 3 now reads the same `UNCONSTRAINED_JURISDICTIONS`
  constant the query-time caller gate uses, so both paths agree on what a catch-all
  means. Enforcement is otherwise unchanged: a contract pinning `EU` with an expose
  in `us-east-1` still fails under strict. (#606)
- **The region→jurisdiction table is derived from the vendors' own data instead of
  hand-kept, and it had said London was in the EU.** 31 hand-typed regions mapped
  `eu-west-2` and `europe-west2` — both London — to `EU`, and `Global` was worse,
  since check 3 skips any region whose jurisdiction is `Global`, making
  `ap-southeast-1` (Singapore), `ap-northeast-2` (Seoul) and `asia-southeast1`
  pass-anything wildcards. AWS regions now resolve through botocore's shipped
  `endpoints.json`, all eight partitions including GovCloud and the EU Sovereign
  Cloud, and GCP and Azure through CSVs vendored from `dgl/cloud-regions` (ODbL-1.0,
  recorded in `NOTICE`), with `VENDORED_CORRECTIONS` filling three rows upstream
  ships empty: 31 hand-written entries became 121 resolved and 0 hand-written,
  nothing resolves to `Global` or `Unknown`, and resolution is lazy so `fluid --help`
  imports no botocore. The AWS provider kept a second table that had drifted on 16
  regions, so `fluid validate` and the provider could reach opposite verdicts on one
  contract; it now delegates. Identity, not adequacy: the UK and Switzerland hold
  GDPR adequacy decisions, but a contract asking for `jurisdiction: EU` has not asked
  for the UK. Also folded in: `cli/init_scan.py` emitted a sovereignty block that
  failed its own schema, so `fluid import` handed back a contract `fluid validate`
  rejected. **Newly blocking**, with #569: an EU-pinned contract bound to London,
  Singapore or Seoul passed 0.14.1 silently and now fails, while a `UK`-pinned
  contract bound to `eu-west-2` stops being a false positive. (#514)
- **AWS enforces residency from the key that actually holds it, and stops writing a
  module after refusing a contract.** The AWS util read the allow-list *out of* the
  boolean `sovereignty.dataResidency` rather than the sibling `allowedRegions`, so
  `true` — the schema default, and what the repo's own `eu-customer-data-gdpr`
  example writes — raised `TypeError` while `false` skipped the check in silence: the
  strict setting broke and the permissive one worked. `allowedRegions` is now
  enforced whatever the boolean says, `deniedRegions` is honoured for the first time,
  and the two non-schema shapes in the wild are read as a warned fallback, never
  merged in. The same confusion reached the cloud tags, where one tag read
  `fluid:allowed_regions = "allowedRegions"` — tags an AWS Config rule or a tag-based
  SCP keys on. Separately, `fluid generate iac` swallowed the provider's sovereignty
  veto in a best-effort `except Exception` and emitted `main.tf.json` anyway; the
  veto is now recognised through the `__cause__` chain and re-raised, and
  `_validate_sovereignty` no longer catches only `SovereigntyViolationError`, which
  had let every `ResidencyViolationError` — a sibling, not a subclass — skip the
  `sovereignty_violation` audit event. **Newly blocking:** an AWS contract binding
  outside its own `allowedRegions`, or an EU-jurisdiction contract bound to
  `us-east-1`, exits 1 and writes no module, where 0.14.1 wrote it with exit 0.
  (#513)
- **A `--provider` that contradicts the contract's binding is rejected before
  anything is written.** `fluid generate iac --provider gcp` on an AWS- or local-bound
  contract emitted a resource-free `main.tf.json` and exited 0, and `--validate` made
  it worse, since `tofu validate` reports success for a configuration with no
  resources: the operator generated, validated, saw green and had provisioned
  nothing. `--provider` disambiguates a contract that spans clouds or declares none;
  retargeting is done by editing `binding`. The requested provider must now be among
  the clouds the contract declares, and the same gate covers `fluid apply
  --provider`, which had reported `tofu plan: +0 ~0 -0` with exit 0. (#546)
- **`fluid plan --check-sovereignty` no longer prints `PASS` for a check it never
  ran.** `AwsProvider` has no public `validate_sovereignty` hook, so the helper
  returned an empty violation list, which rendered as `PASS` on a contract
  `fluid validate` rejects with two residency errors; a hook that *raised* was
  indistinguishable from one that found nothing, because `invoke_hook` swallows the
  exception. The helper now returns `None` for "no usable verdict", and a new
  reporter tries the provider hook, then the built-in policy engine, before reporting
  `NOT CHECKED`, always naming which answered. Since no shipped provider implements
  the hook, a contract declaring a `sovereignty` block that printed a bare `PASS` on
  0.14.1 now gets a real verdict, and a failing one **exits 1** where 0.14.1 exited 0
  (the plan file is still written); a contract with no `sovereignty` block prints
  `NOT CHECKED` and still exits 0. (#510)
- **The cross-border transfer check is order-independent, and a gap in our own region
  table no longer blocks a valid deployment.** `None` meant both "no baseline
  jurisdiction yet" and "this region has no known jurisdiction", so the verdict
  depended on declaration order: `eu-west-1` plus `eu-south-1` (both EU, the latter
  unmapped) was blocked, the same pair reversed passed, and two unmapped regions in
  different jurisdictions passed as clean. An unknown jurisdiction is now its own
  warning-severity finding that never seeds the baseline, and the check moved out of
  the per-expose loop, where it emitted one duplicate error per expose. (#510)
- **Git-backed federation digests were the raw-text fallback on every call, and
  fixing it changes the digest value.** `_fetch_digest_via_git` imported
  `compute_contract_digest` inside a `try/except`, and that function existed nowhere
  in `fluid_build` — absent at both v0.14.1 and the 0.15.0 branch point — so every
  call took the except branch and hashed the raw contract bytes. A raw-text hash
  makes key order, indentation, quoting style, comments and CRLF look like upstream
  drift, the one thing `upstreamDigest` pinning exists to tell apart from a real
  change. The helper now exists and is imported at module scope, hashing the *parsed*
  mapping as NFC-normalised, sorted compact JSON and sharing one canonical form with
  `compute_plan_digest`; an unparseable upstream contract returns `None`, which the
  caller escalates to a violation, so federation fails closed instead of degrading.
  **Upgrade note, and a silent one:** for a `git_registry` upstream a
  `consumes[].upstreamDigest` pinned against the old value now reads as drift and
  `fluid apply` aborts with `apply_consumes_drift`, exit 1, before any DDL — re-pin
  those rows, or use `--no-verify-federation`. Two caveats: the per-workspace cache
  at `.fluid/federation/<workspace>.digest-cache.json` has no expiry check, so a
  machine already holding an entry keeps returning the 0.14.1 value until it is
  cleared, and only `git_registry` is affected, since `catalog` and `http_registry`
  read a digest off the remote. (#587)
- **A contract published to a default DataHub install was readable nowhere, and the
  `customProperties` keys change spelling.** One commit shipped two contradictory
  specifications, name for name: the unit suite asserted `fluid_contract`,
  `odps_spec` and the ODCS blob were *forbidden* there, the integration suite that
  they were *required* — and the unit suite ran on every pull request and pinned the
  implementation while the integration suite needed a live GMS and had never once
  run. Neither was right: the "link, don't inline" design rests on a spec URL that is
  `None` unless `spec_source_base_url` is configured, a field set by no factory and
  documented nowhere, while `DataContract.rawContract` is absent from the OSS GraphQL
  schema — so on a default install the contract was neither inlined, nor linked, nor
  readable. `_specs_are_linkable` now decides in one place: large YAML is linked when
  a base URL exists and inlined when it does not. **Key rename to know about:**
  `fluid.layer` → `fluid_layer`, `fluid.productType` → `fluid_product_type`,
  `fluid.version` → `fluid_version`, plus a new `fluid_domain`, matching the
  underscore spelling the other emitters use; DataHub was the only one using dots, so
  a saved search, dashboard or ingestion rule keyed on the dotted names must be
  updated, while structured properties keep their dotted `qualifiedName`, a different
  namespace. Expect larger entity payloads too: with no `spec_source_base_url` set,
  the dataset aspect carries `odcs_contract` and the DataProduct aspect
  `fluid_contract` and `odps_spec` inline. (#596)
- **The `agentPolicy` composite gate reports the reason its own docstring promised.**
  `check_tool_call` documented tool denylist > tool allowlist > model denylist >
  use-case denylist > model allowlist > use-case allowlist, then evaluated the whole
  model stage before the use-case stage, so a request whose model was merely absent
  from an allowlist and whose use case was explicitly denied reported
  `not-in-allowedModels` where the prose above promised `in-deniedUseCases`.
  Precedence is now data in `CHECK_ORDER`, pinned by the conformance vectors. No
  verdict changes across 1,728 policy and request combinations, but 48 reason codes
  differ, all `not-in-allowedModels` becoming `in-deniedUseCases` — operators routing
  alerts or dashboards on those two strings should re-check their rules. (#568)
- **Anticipated `forge_run` failures reached the IDE as "Error executing tool
  forge_run" on MCP SDK 2.x.** All six raise sites used a bare `RuntimeError`, which
  1.x passed through intact but 2.x treats as a crash, substituting that generic
  string; the call still returned `isError: true`, so the only thing lost was the
  part that helped — an operator on an IDE without the `sampling` capability got the
  generic sentence instead of the message naming that capability and its two ways
  out, `mode='blank'` or `fluid forge --agent --blank`. All six now raise the SDK's
  `ToolError` through the new `_mcp_compat.get_tool_error()`, since the class moved
  package between generations, with a function-local import so `mcp` stays off the
  `fluid --help` cold path. `forge_run` is the only tool on that server that raises,
  so this is the whole surface. (#602)
- **Every schema is now validated with the dialect it declares.** `Draft7Validator`
  was pinned at three call sites, and Draft 7 ignores keywords it does not recognise
  rather than rejecting them, so `fluid validate-artifacts` reported ODCS documents
  clean that the published standard rejects: the vendored ODCS 3.1.0 schema declares
  2019-09 and guards nine objects with `unevaluatedProperties: false`, so a typo'd
  key in a `servers[]` entry validated with zero errors, and `schema[].properties[]`
  lost its `required: ["name"]` check. Both now fail, naming the offending key. The
  change only tightens — 20 documents generated across ten example contracts validate
  identically under both dialects, so only hand-authored or third-party artifacts can
  newly go red. Two things went with it: the try/except around the old validator was
  dead code, and the jsonschema probe no longer names the deprecated `RefResolver`,
  an ImportError on which sets `JSONSCHEMA_AVAILABLE = False` and makes validation
  skip rather than error. `extension_schemas.py` keeps Draft 7 deliberately. (#582)
- **A GCP expose resolves to its target from the whole binding, not from
  `binding.format` alone.** The emitter dispatched on five `format` spellings, two of
  which appear in no shipped schema, while the one schema-valid Cloud Storage
  spelling (`gcs_file`) matched none of them — so
  `{platform: gcp, format: gcs_file, location: {bucket: acme-raw}}` validated clean
  and emitted nothing, a silent no-op on a correctly auto-detected provider. One
  resolver now decides: an explicit GCP `format` wins, otherwise the shape of
  `binding.location` does (`dataset` to BigQuery, `bucket` to Cloud Storage, `topic`
  to Pub/Sub), and `emit`, `emit_data`, `discover_imports` and the new validate-time
  gate all route through it. `discover_imports` also lacked the emitter's exposeId
  table-name fallback, so a brownfield apply would try to create a table that already
  exists; and the AWS plugin filtered `exposes[]` against the literal `"aws"`, so
  `platform: glue`, `s3`, `athena` or `redshift` auto-detected as AWS and were then
  skipped by every AWS filter, emitting an empty module. (#580)
- **`fluid verify` checks what `fluid apply` provisioned for a GCP binding, not what
  the format string says.** Stage 9 dispatched on the literal
  `binding.format == "bigquery_table"`, which matched the emitter until the emitter
  stopped keying on `format` alone; after that
  `{platform: gcp, format: csv, location: {project, dataset}}` — provisioned as a
  BigQuery table — fell through to the local-file branch and returned `status: error`
  with "no location.path declared", diagnosing a missing file for a table that
  exists, and `error_count` fails the run with or without `--strict`. Verify now asks
  the emitter's own resolver and addresses a BigQuery table by the name the emitter
  used, where an expose with no `location.table` previously built `project.dataset.`
  and looked for a table named `""`. A GCS bucket, Pub/Sub topic or Iceberg warehouse
  reports `unsupported`, which is "not checked" rather than "check failed"; non-GCP
  bindings, local files, Snowflake and the legacy dialect are unchanged. (#580)
- **The ODCS `description` block is emitted as ODCS declares it.** ODCS models
  `description` as an object of string fields (`purpose`, `limitations`, `usage`)
  while FLUID models it as a single string, and only the importer type-checked: the
  exporter wrapped unconditionally, so a `description` mapping came out as an object
  where the schema declares a string. A mapping now passes through as siblings, a
  string is still wrapped as `{purpose: <string>}`, and both the `odcs/` and per-port
  `odps-bitol/*.odcs.yaml` outputs are fixed at once. Round-trips never saw it, since
  only a caller rendering a document it had not imported reached the broken branch;
  when it did, installs carrying the optional `vowl` validator
  (`fluid-build[odcs-strict]`) aborted `fluid generate artifacts` and wrote no
  artifacts at all. The old shape was already schema-invalid, so only a consumer
  written against the bug breaks. (#585)
- **`fluid apply` reaches Snowflake again when `SNOWFLAKE_ACCOUNT` is set.** From
  `snowflakedb/snowflake` 2.x — the only major the generated module pins, `~> 2.0` —
  the bare `account` field is gated behind the
  `PROVIDER_CONFIGURATION_ACCOUNT_FALLBACK` experiment, and the provider errors the
  moment it sees the legacy `SNOWFLAKE_ACCOUNT` variable whether or not the v2
  organisation/account pair is present; the credential overlay split the legacy
  variable into that pair but *added* it alongside, so every `tofu plan` failed.
  Verified against a live account on provider 2.19.0 and OpenTofu 1.12.0, only the v2
  pair with the legacy variable blanked plans successfully, which is what the overlay
  now produces. **No contract or configuration change is needed:** an operator
  setting `SNOWFLAKE_ACCOUNT` in the standard `<org>-<account>` form goes from
  failing to working. A bare account locator with no organisation (`xy12345`)
  deliberately keeps its legacy value so the provider's actionable error survives;
  that operator sets the two v2 variables, or opts into the experiment. (#511)
- **`pip install` on Python 3.10 produces a package that can import again.** litellm
  1.98.0 imports `NotRequired` straight from `typing`, which landed there only in
  3.11 (PEP 655), so merely importing litellm raises `ImportError` on 3.10 — while
  litellm's own metadata still declares `requires_python ">=3.10,<3.15"`. litellm is
  a core dependency and 3.10 is advertised in both `requires-python` and the
  classifiers, so this broke installs, not just the red 3.10 leg of the matrix. The
  pin is now split on an environment marker: `litellm>=1.83.7,<2` on 3.11 and newer,
  `litellm>=1.83.7,<1.98` below it, bisected against real wheels. The `>=1.83.7`
  floor is unchanged on both branches, so the CVSS 9.3 SQL-injection fix it exists
  for stays in force and the compromised 1.82.7/1.82.8 artifacts stay excluded.
  (#555)
- **The emulated integration lane reported success while proving almost nothing.**
  `integration-emulated-heavy.yml` selected 54 tests by marker and provisioned an
  emulator for 3 of them: it started LocalStack but never the GCP emulator compose
  stack, so 15 GCP tests skipped themselves inside a job named for them, and pytest
  exited 0. Measured before/after on the same commit: **3 passed / 53 skipped → 17
  passed / 38 skipped**, keyless, no token. The lane now starts the GCP stack it
  advertises, and `scripts/ci/assert_lane_coverage.py` fails it unless every
  provisioned area contributed a passing test. (#595)
- **That lane had also not run at all since 2026-07-13.** Its
  `ci:integration-emulated` label is stripped on every push by
  `integration-label-guard.yml`, so the vouch had to be re-applied and re-approved
  per push; the single run that reached the queue waited twelve days for a required
  reviewer and was cancelled. It now runs nightly at 05:00 UTC unattended, keeping
  the label for the pull-request path where a maintainer vouch is the point, and
  `actionlint.yml` checks that the label condition and its guard both still exist,
  since that is now the control gating un-reviewed PR code. (#595)
- **The fourteen AWS `tofu apply` round-trips had never executed in CI, anywhere.**
  `tests/iac/test_iac_aws_localstack_e2e.py` is the repo's strongest AWS evidence,
  each round-trip independently verified through boto3, and `HONESTLY_TESTED.md` has
  recorded it as passing since the file was written. The cause was structural rather
  than a missing secret: the only workflow referencing the suite declares no
  `environment:` on any job, so it could never read `LOCALSTACK_AUTH_TOKEN` and the
  step always skipped. The suite now carries `pytest.mark.emulated_heavy` and is
  selected into the heavy lane, taking `-m emulated_heavy` from 54 tests to 68 with a
  matching `--require` in the lane-coverage guard. Running them for the first time
  found two things: the lane's `docker run` omitted the `/var/run/docker.sock`
  bind-mount LocalStack's Lambda runtime needs (3 failed / 11 passed before the
  mount, 1 failed / 13 passed after), and the Lake Formation `GrantPermissions`
  round-trip does not pass and never has, because LocalStack answers
  `UnrecognizedClientException` even for a registered DataLakeAdmin — which is why a
  file of fourteen tests has always been recorded as thirteen. That one is now
  `xfail(strict=False)` with the upstream issue named in
  `docs/upstream-issues/localstack-lakeformation-grant-auth.md`, kept unskipped so an
  upstream fix reports xpass rather than going quiet. (#598)
- **Release image: two CPython `tarfile` CVEs suppressed with justification.** Both
  are fixed only in a CPython pre-release, so no stable base image clears them — and
  0.14.1 published to PyPI while its GHCR job failed the grype gate on them.
  `.grype.yaml` now records each with its reachability argument: the single
  `extractall` call is guarded by `_safe_tar_members`, which raises on symlink and
  hardlink members, and no streaming (`"r|"`) mode is used anywhere. Each entry
  carries justification, EPSS and an explicit removal condition, so the release gate
  no longer fails on an unfixable finding. (#501)
- **The wrong legal entity no longer ships in the container image or on the docs
  site.** 0.14.1 renamed the maintainer to Agentics Transformation Limited across
  `LICENSE`, `NOTICE`, `pyproject.toml` and the docs, but two published surfaces were
  missed because neither is a file a docs linter would open: `Dockerfile`'s
  `org.opencontainers.image.vendor` label, so every image pushed to `ghcr.io` since
  carried "Agentics Transformation Pty Ltd", and the footer of the GitHub Pages site.
  Both now read Agentics Transformation Limited; PyPI metadata was already correct.
  (#561)
- **The Apache-2.0 notice is now complete in every source file that carries one.**
  346 tracked files held a header truncated mid-boilerplate — 323 after the
  `LICENSE-2.0` URL, 23 after "you may not use this file except in compliance…" —
  dropping the warranty and liability disclaimer Apache-2.0's appendix asks to be
  attached. No licence terms changed, and across all 358 files touched the diff
  contains zero non-comment, non-blank lines. The cause was two scripts disagreeing
  about what counts as a header: the checker grepped only for the copyright token in
  the first five lines, so all 346 passed it while the writer re-completed them on
  every run, which is how the CI job's own remediation advice turned a one-file fix
  into a 361-file diff. The checker now validates every substantive line and names
  the first missing clause per file; the writer emits PEP 8 spacing so a second run
  is a no-op. (#493)

## [0.14.1] — 2026-08-03

A correctness and security patch: the MCP output port works against both
generations of the MCP SDK, two authorisation bypasses in that port are closed,
and the project's legal entity is named correctly across the distributed files.

### Security

- **Two HIGH authorisation bypasses closed in the MCP output port.** Verified
  caller attributes (JWT/mTLS) and self-attested ones were flattened into a
  single dictionary, so the "cryptographic identity wins" rule held only for
  the keys a claim mapping happened to emit. A caller could therefore (a)
  self-attest any `rowFilter` the mapping did not cover — including another
  tenant's value, turning a fail-closed denial into an attacker-chosen
  row-level-security predicate — and (b) self-attest `model` and `useCase` past
  the `agentPolicy` gate whenever the mapping omitted them, which any custom
  `FLUID_MCP_JWT_CLAIM_MAPPING` does, since it *replaces* the defaults rather
  than extending them. The flattening predated the SDK port and was reachable
  on SDK 1.x through `clientInfo` extras. Fixed by reading the `fluid_auth_kind`
  stamped by the transport middleware but never consulted: when authentication
  is enforced, only verified claims bind, and dropped self-attestations are
  logged at WARNING naming each one. The no-authentication path is unchanged.
  Five regression tests pin all four exploit shapes plus a no-auth control.
  (#492)

### Changed

- **The MCP output port supports SDK 1.x and 2.x.** `mcp` 2.0.0 renamed
  `FastMCP` to `MCPServer`, inverted the lowlevel `Server` registration API from
  decorators to `on_*` constructor handlers, removed
  `create_connected_server_and_client_session`, and renamed model fields from
  camelCase to snake_case at the attribute level. All generation branching is
  confined to one new tier-0 leaf, `fluid_build/_mcp_compat.py`, which probes
  with `find_spec` rather than importing `mcp` at module scope so the
  `fluid --help` startup budget is preserved. Twelve `getattr`-with-default
  camelCase reads were failing **silently** under 2.x — errors reading as
  success, tool schemas as empty — and now route through a dual-name accessor.
  Verified green against both `mcp` 1.29.0 and 2.0.0, including a live
  end-to-end run of `fluid mcp output-port serve` over HTTP/SSE under 2.0.0.
  The dependency pin stays `mcp>=1.20,<2.0` and the drift canary stays
  warn-only until the 2.x leg has a track record. (#492)

### Fixed

- Corrected the legal entity name across `NOTICE`, `LICENSE`, `pyproject.toml`
  and repository docs: the project is maintained by **Agentics Transformation
  Limited** (Ireland); earlier files misnamed the entity as "Pty Ltd". The
  trademark notice now correctly asserts an unregistered mark ("FLUID Forge™ is
  a trademark of Agentics Transformation Limited") instead of claiming a
  registration.

## [0.14.0] — 2026-07-28

A live-verification hardening release: the dbt Iceberg loop reaches BigQuery
behind a new validate-time no-op gate, 104 defects fixed and proved against a
real Snowflake account, and DOT-injection, secret-redaction and
leak-prevention security work.

### Security

- **`fluid viz-graph` escapes contract-authored text before it reaches
  generated DOT source.** Mesh node/edge labels were interpolated verbatim, so
  a product label like `X" label="SPOOFED` injected a second `label` attribute
  and let a contract display a name of its choosing — including another
  product's — in the rendered mesh graph; node IDs were interpolated unquoted
  through a denylist that missed `"` and `\`, letting a `consumes[].ref`
  inject phantom nodes into the lineage graph; and an unescaped trailing
  backslash failed the entire render. Both shared helpers are fixed (allowlist
  IDs, backslash-then-quote label escaping), covering the legacy inline
  emitter and `viz_renderers/dot.py`. (#481)
- **Secret redaction rewritten to redact by exact known value.** The previous
  regex-based approach guessed a secret's extent from its shape and leaked 17
  known inputs; redaction is now delimiter-agnostic by construction, masking
  the literal values it knows. (#478)
- **Commits carrying values from the committer's own environment are
  refused.** A new pre-commit/CI hook flags any diff line byte-identical to a
  non-trivial value in the committing machine's environment (`SNOWFLAKE_*`,
  `AWS_*`, `*_TOKEN`, `*_PASSWORD`, …) or a supplied `--env-file` — the
  low-entropy identifiers (account locators, usernames) that secret-shape
  scanners cannot see — and never prints the matched value. `detect-secrets`
  now also actually runs in CI, on changed files. (#479)
- **Vulnerability reports route through GitHub Private Vulnerability
  Reporting.** `SECURITY.md` now points at PVR (email stays as the fallback)
  and the supported-versions table reflects the released 0.13.x line. (#473)

### Added

- **The dbt Iceberg loop extends to BigQuery.** An `iceberg` expose on a GCP
  binding — which previously fell through the emit dispatch and produced
  nothing, silently — now emits `catalogs.yml` with
  `catalog_type: biglake_metastore` and provisions the GCS bucket dbt refuses
  to create. The IaC bucket name is derived from the same warehouse URI dbt
  writes into `external_volume`, so the bucket dbt loads into is always the
  bucket the IaC creates and governs, and whole-bucket `force_destroy` is
  dropped when the product owns only a prefix of a shared warehouse root.
  (#474)
- **Validate-time anti-no-op gate for Iceberg prerequisite bindings.** The
  Snowflake and GCP IaC emitters skip an Iceberg expose missing a required
  input rather than emitting a broken resource — previously the user found out
  at `dbt run`. `fluid validate` now errors on exactly those cases, naming the
  missing field; each check mirrors one emitter skip-branch and the pairing is
  test-pinned in both directions. Note for CI: under `--strict`, Snowflake
  catalogs that authenticate with secrets (polaris / unity / rest / nessie)
  now fail, since the emitted module is credential-free and strict promotes
  that existing warning. (#475)

### Fixed

- **Snowflake builds actually run on Snowflake.** Embedded-SQL builds on a
  `platform: snowflake` contract executed against local DuckDB (exit 0 either
  way); `--model-contracts` flattened every parameterized/alias type to
  `VARCHAR(16777216)`, so contract enforcement passed vacuously; generated
  `profiles.yml` ignored the contract's schema and silently targeted `PUBLIC`;
  a freshly generated project failed its own contract on first `dbt run`; and
  a `freshness` dq rule made the generated project unparseable. Part of a
  104-defect wave found by exercising a month of shipped features against a
  real Snowflake account, every fix re-verified by a second agent re-running
  the original failing scenario. (#476, #478)
- **`fluid import dbt` → `apply` → `verify` round-trips cleanly.** Importing
  then applying no longer creates a duplicate lowercase shadow namespace
  (`+2 ~2 -0` → `+0 ~2 -0`), `verify` can read the real objects through
  `snowflake_view`, and a p90 metric no longer silently round-trips into a
  median. (#476)
- **`fluid publish` no longer silently skips contract-declared catalog
  targets** (an `ImportError` swallowed by a bare `except`), and apply hooks
  now run on the OpenTofu path, making `--env` plumbing reachable for
  Snowflake / AWS / GCP applies. (#476)
- **Governed access enforcement holds under aliasing.** The governed `query`
  path now enforces `policy.authz.columnRestrictions` (a denied column was
  readable as a measure), and PII redaction can no longer be bypassed by
  aliasing a restricted column — policy was previously checked on the output
  column name, which the semantic layer aliases away. (#478)
- **Data-quality test reporting is truthful.** A failing `severity: critical`
  rule no longer reports PASS with exit 0; `accuracy` rules evaluate upper
  bounds instead of only `MIN()`; `summary.checks_passed` no longer counts
  warnings and failed criticals as passed; `--no-data` no longer asserts
  checks it never performed; and `--engine soda` can now actually produce
  checks (the schemas never defined the `exposes[].quality.tests[]` key it
  reads). (#478)
- **ODCS export/import fidelity.** Export no longer flattens every
  parameterized type to `logicalType: string`, round-trips no longer lose 88
  of 114 leaf fields, and `odcs import` no longer rewrites a Snowflake binding
  to `bigquery` while reporting success — now validated against the official
  ODCS schema. (#478)
- **`--adopt-shared-container` respects per-exposure `binding.packaging`
  overrides.** Flipping one binding no longer absorbs a shared platform pool
  into the tenant's state (which erased the pool's COMMENTs); transition
  scoping now keys on which container is nested where, not on resource type.
  (#478)

## [0.13.1] — 2026-07-26

A security + interoperability release on top of 0.13.0: two path-confinement
security fixes, the dbt importer's product-boundary split, the Snowflake
Iceberg dbt loop, Bitol ODPS v1.1.0 support, and OpenLineage emission that
real consumers can actually ingest.

### Security

- **`binding.location.dbFile` is now confined to `--readable-paths`.** The
  DuckDB output-port driver gated `binding.location.path` and `.attach`
  against the operator's allowlist but passed `dbFile` raw to
  `duckdb.connect`, so a served contract could open and read any host
  database — a read-side sandbox escape. `dbFile` now flows through the same
  resolve-and-contain gate as its siblings (`:memory:` passes through;
  no-allowlist behaviour is unchanged). (#463)
- **Document-controlled ids are contained in exporter filenames.** A contract
  with a traversal-shaped `exposeId` run through
  `fluid generate artifacts --out dist` could write an attacker-influenced
  file outside `--out` — and `MANIFEST.json` would bless the escaped path.
  New `providers/_path_safety.py` (the filename sibling of `_sql_safety.py`)
  passes schema-valid FLUID ids through verbatim, cleans-plus-digests
  anything else, and re-checks the resolved path against the output root at
  all three write sites; manifest emitters derive names through the same
  helper. Verified against 204k+ separator/control-char cases and a full
  Unicode sweep with zero escapes and zero canonical-layout changes. (#472)

### Added

- **`fluid import dbt --split-by {project|folder|group}`** splits a dbt
  manifest into multiple data products along folder or dbt-group boundaries
  (`project` remains the byte-stable single-contract default). Cross-split
  `ref()`s become cross-product `consumes[]`, `--out` becomes the output
  directory for multi-contract imports, and dbt ≥1.10 manifests with
  `arguments:`-nested test params now import correctly alongside legacy flat
  kwargs. In passing, model-level expose descriptions are now scrubbed like
  their column/semantic siblings (closing a hostile-Jinja smuggling path into
  generated `schema.yml`), and no-columns exposes emit a schema-valid
  contract. (#465)
- **Snowflake Iceberg loop for dbt, both halves.** `fluid generate` emits
  dbt's `catalogs.yml` (v1 schema, Snowflake adapter) for Iceberg exposes,
  and `fluid apply` provisions the prerequisites dbt refuses to create — the
  EXTERNAL VOLUME for Snowflake-managed catalogs and the AWS Glue CATALOG
  INTEGRATION. A single deterministic naming helper is shared by both
  emitters, so the volume `fluid apply` creates carries exactly the name
  `catalogs.yml` references (explicit override honoured via
  `binding.icebergConfig.properties`). Live-verified with a real
  `tofu apply`. (#469, #470)
- **Bitol ODPS v1.1.0 top-level `type`** (approved RFC 0029):
  `sourceAligned` / `aggregate` / `consumerAligned` map 1:1 and
  bidirectionally to FLUID's SDP / ADP / CDP classification. The default emit
  target stays v1.0.0 until Bitol cuts the release; opt in via
  `--api-version` / `ODPS_API_VERSION`. Validation keys on the document's own
  `apiVersion`, and custom org types round-trip verbatim. (#471)
- **Declared `consumers:` block in the 0.7.6 preview schema.** Contracts can
  now declare their downstream consumers — dashboards, notebooks, ML systems,
  applications — with a shape borrowed from dbt exposures (`name` / `label` /
  `type` / `owner` / `url` / `maturity`) plus FLUID-native `exposeIds` tying
  a consumer to specific output ports. Additive and optional; preview schema
  only, no GA change. (#466)

### Changed

- **CI:** routine pinned-action bumps. (#454, #455, #456, #457, #458)

### Fixed

- **OpenLineage events are now spec-conformant — and actually emitted.** The
  emitter historically produced payloads no OpenLineage consumer would accept
  (missing required `producer`/`schemaURL`, flattened `run`/`job` structure,
  non-UUID `runId`) and was never wired up, so zero events were ever sent.
  Emission now routes through `openlineage-python` at the acquisition-runner
  chokepoints (all six engines, no per-runner wiring), honours the standard
  `OPENLINEAGE_URL` so an existing Marquez/DataHub deployment just works, and
  redacts run facets and stream names before anything leaves the machine.
  ODCS contract publishing also moves onto OpenMetadata's first-class Data
  Contracts entity. (#467)
- **`fluid-schema-0.7.5.json` no longer breaks YAML-based consumers.** Six
  emoji in the GA schema's description were encoded as UTF-16 surrogate-pair
  escapes — valid JSON, but rejected by libyaml, so tooling that reads JSON
  Schema through a YAML parser (e.g. `datamodel-code-generator`) could not
  process the schema at all. The escapes are now literal characters (a
  lossless, one-line change), and a new guard test asserts every bundled
  schema stays surrogate-free and YAML-round-trippable. (#451)
- **Safety-gate override audit events now log at WARNING, as documented.**
  `opentofu_destructive_gate_override` (`--allow-data-loss`) and
  `packaging_adoption_override` (`--adopt-shared-container`) were emitted at
  INFO — invisible to audit pipelines filtering at WARNING and above. Event
  names and payloads are unchanged. (#450)

## [0.13.0] — 2026-07-18

A packaging + autonomy + semantics-correctness release: declarative
`isolated`/`shared` infrastructure packaging, the new autonomous `fluid
mission` surface, the Apache Ossie resync, and a wave of fixes that make the
`semantics` block return right numbers through every consumer.

### Security

- **dbt manifest import hardened against Jinja exfiltration in recovered
  free-text fields.** A hostile `manifest.json` carrying e.g.
  `{{ env_var('AWS_SECRET_ACCESS_KEY') }}` in a tag or description could render
  the secret into the operator's own artifact after import → generate →
  `dbt parse`. Display/governance text is now stripped of Jinja spans (fixpoint
  loop, so no delimiter survives by reforming at a gap junction), with each
  redaction reported; SQL-bearing fields (`expr`, metric `filter`) are preserved
  verbatim, with any non-MetricFlow templating surfaced as a
  review-before-generate risk. (#449)

### Added

- **Declarative packaging modes: contracts can declare infrastructure containers
  `isolated` (product-owned, today's behavior) or `shared` (referenced from a
  platform pool).** The 0.7.6-preview schema gains a `packaging` block
  (contract-wide default, per-binding override, per-kind `containers` map for
  hybrid tiers); the AWS/GCP/Snowflake IaC emitters emit a data source instead
  of a managed resource for referenced pools, so a product can never own or
  destroy shared platform infrastructure; shared-bucket grants are always
  prefix-scoped and fail closed when `location.path` is missing; a pre-plan
  ownership-transition guard blocks `isolated`→`shared` flips that would plan a
  pool DESTROY and gates `shared`→`isolated` adoption behind the new
  `--adopt-shared-container` flag; and `plan.json` gains a digest-covered
  `packaging` summary that itemises dropped container-creation actions.
  Contracts without the block emit byte-identical output, enforced by a golden
  pin over every example contract. Two IAM-widening bugs and a CEL-condition
  injection introduced during implementation were caught and fixed by the
  series' own security pass before merge. (#432, #433, #435, #441)
- **Mission-based deep agents: `fluid mission check | trust | list | run`.**
  Declarative YAML mission specs (goal, success criteria, budgets, gates, tool
  allowlist) with direnv-style content-hash trust pinning for workspace specs;
  `fluid mission check` is a zero-LLM scorecard usable as a standalone CI gate;
  `fluid mission run` drives a VERIFY-anchored loop in which only code-owned
  checks — never the LLM — can declare success, with hard USD / wall-clock /
  iteration budgets, a fail-closed destructive-diff gate, and
  resume-by-re-verification (`--resume`). Ships `gdpr-clean` and
  `quality-coverage` built-in missions. A HIGH path traversal in run-manifest
  handling was found and fixed by the series' own security pass before merge.
  (#432, #434, #436)
- **Apache Ossie resync + conformant interchange sidecar.** The OSI
  implementation resyncs to the current Apache Ossie core-spec (`BIGQUERY` +
  `MAQL` dialects, free-form vendors, plain-string `ai_context`), and the
  `.semantics.osi.*` sidecar is now a valid Ossie document (root wrapper,
  internal-only fields relocated into `custom_extensions`) validated against the
  vendored upstream JSON Schema. New `--osi-sidecar-format json` emits the shape
  dbt Core v1.12+ reads natively, so forge output becomes queryable through the
  dbt semantic layer — and consumable by the upstream Ossie converters — with
  zero conversion. (#438)
- **The dbt importer recovers the semantic layer.** `fluid import dbt` now maps
  manifest `semantic_models` and project-level metrics (simple / ratio /
  derived, including where-filters) onto the owning expose's `semantics` block —
  entities, dimensions with normalized time grains, measures, and
  `defaultAggTimeDimension` — with a degrade-loudly posture: every unmappable
  feature lands in `report.unsupported` instead of vanishing. (#442)
- **Every template product ships a queryable semantics block.** All five
  templates (analytics / etl_pipeline / ml_pipeline / starter / streaming)
  derive a conservative, deterministic semantics block from their columns, so
  template-mode products get the governed MCP `query` tool and MetricFlow export
  out of the box instead of after hand-authoring; templates can still override
  with an explicit spec. (#443)

### Changed

- **Cross-account / cross-project access is now live-proven at the emulator
  tier.** New gated tests drive bilateral cross-account Lake Formation +
  prefix-scoped S3 bucket policies through `tofu apply` against a two-account
  LocalStack Pro instance (the emitted bucket policy proven to be the deciding
  access control under `ENFORCE_IAM=1`), and cross-project BigQuery
  `dataset.access[]` entries through the BQ emulator; what the emulators cannot
  prove is recorded explicitly in `HONESTLY_TESTED.md`. (#446, #448)
- **Tier-1 semantic-layer RFC published.** Design RFC at repo root covering the
  five audit-ranked gaps — cumulative metrics, structured filters, time spine +
  null filling, relationships, and SCD validity params — all additive and
  0.7.6-preview-gated. (#445)

### Fixed

- **Metric filters are honored on the governed query path, and `percentile`
  gains real parameters.** A metric filter like `status = 'completed'` was
  silently dropped by the MCP query compiler — unfiltered numbers, no error —
  while the dbt export honored it; the filter is now allowlist-validated and
  ANDed into the WHERE alongside caller filters and policy rowFilters, with
  hostile or unbalanced-paren filters failing closed. `agg: percentile` gains
  the 0.7.6-preview `measures[].aggParams` (`percentile`,
  `useDiscretePercentile`), rendered as `PERCENTILE_CONT/DISC(p) WITHIN GROUP`
  and failing closed on engines without grouped ordered-set percentiles. (#439)
- **Forge-emitted measures no longer double-aggregate.** The emitter copied
  whole aggregate calls (`SUM(amount)`) into `measures[].expr` next to an
  inferred `agg`, so the query compiler rendered invalid `SUM(SUM(amount))` and
  the dbt bridge exported the same double wrap. A shared semantics builder now
  splits single-aggregate expressions into `agg` + inner expr (including
  `COUNT(DISTINCT …)` → `count_distinct`), the time-grain vocabulary is
  single-sourced with all aliases normalized (interview input included), and
  `defaultAggTimeDimension` is populated by both producers. (#440)
- **Metric owner + tags/labels round-trip through dbt.** `metrics[].owner` and
  semanticModel `tags`/`labels` were dead schema surface — no producer, no
  consumer. They now emit into MetricFlow `config.meta` (namespaced
  `fluid_tags` / `fluid_labels`) and the manifest importer recovers them, so the
  governance surface survives contract → dbt → contract. (#444)
- **IaC access grants read from the schema-valid `accessPolicy` surface.** The
  GCP emitter read `metadata.policies` — a shape every shipped schema rejects —
  so a cross-project-access contract could emit but never validate. Grants now
  come from `accessPolicy` (the deprecated `metadata.policies` surface is still
  appended, so a mid-migration contract drops nothing), and typed principals fix
  a latent bug where every `group:` address was emitted as `user_by_email`.
  (#447)

## [0.12.0] — 2026-07-18

The dbt-core integration release: generated dbt projects gain source freshness,
enforced model contracts, auto-pinned packages, MetricFlow semantic models, and
dbt Fusion (v2) compatibility; a faithful `manifest.json` importer brings
brownfield dbt projects into fluid contracts; contract schema 0.7.5 goes GA;
and `fluid verify` learns to reconcile declared vs observed lineage.

### Security

- **Docker image: fixable OS-package CVEs are now patched at build time.** The
  `Dockerfile` runs `apt-get upgrade` during the build so the published image
  ships without known-fixable OS CVEs, and the Grype ignore list was re-curated
  down from 4 suppressions to 1 (a documented, unreachable `html.parser` DoS
  with a removal trigger) — un-blocking the release pipeline's HIGH/CRITICAL
  CVE gate. (#412)

### Added

- **`fluid import dbt` — faithful brownfield dbt importer.** A new
  manifest-based importer (`fluid import dbt <project-dir | manifest.json>`,
  manifest schema v9+) replaces the old 5-model regex scanner: every enabled
  model/seed/snapshot becomes an expose with real column types (`catalog.json`
  overlay when present), `ref()` lineage becomes `consumes[]` + a per-step
  transformation DAG, generic dbt tests map back to `dq.rules[]` via the shared
  reverse mapping table, source freshness becomes `qosExpectations`, and
  everything skipped is accounted for in the import report. Pure stdlib —
  no dbt-core dependency. (#424)
- **Generated dbt projects emit `sources.yml` freshness from contract SLOs.**
  `exposes[].qos.freshnessSLO` and consumer `qosExpectations.freshnessMax`
  now become `warn_after` / `error_after` blocks (with `loaded_at_field`
  derived from the acquisition cursor where resolvable), so `dbt source
  freshness` operationalizes the contract's freshness promise instead of
  silently dropping it. (#419)
- **Opt-in dbt model contracts.** `fluid generate transformation
  --model-contracts` emits `contract: {enforced: true}` with adapter-correct
  per-column `data_type` (BigQuery / Snowflake / Redshift / DuckDB matrices)
  and `not_null` / `primary_key` constraints on every expose model, so
  `dbt build` fails in producer CI whenever the model's output drifts from
  `exposes[].contract.schema`. Without the flag the generated project is
  byte-identical to before. (#422)
- **`packages.yml` is now emitted alongside generated tests.** Projects whose
  tests reference `dbt_utils.*` / `dbt_expectations.*` get a managed
  `packages.yml` with only the needed range-pins (folded into
  `dependencies.yml` under `--mesh-hub`; user-managed files are never
  overwritten), so generated projects pass their own `dbt parse` gate out of
  the box. Also fixes the emitted `dbt_utils.recency` test, which carried a
  non-dbt `_fluid_window` kwarg that failed `dbt compile` — recency windows
  now derive honestly from the dq rule's ISO-8601 window. (#425)
- **MetricFlow bridge: `semantic_models.yml` from the contract semantics
  block.** `fluid generate transformation` now emits `semantic_models:` +
  `metrics:` YAML (plus the required day-grain time-spine model) from
  `exposes[].semantics`, with parse-strictness defaulting for primary
  entities, agg time dimensions, and metric references — verified against a
  real `dbt parse`. Contracts without semantics produce byte-identical
  output. (#426)
- **dbt Fusion (dbt Core v2) compatibility.** The runner now detects the
  engine flavor from `dbt --version` (`fusion` vs `core`), so Fusion users run
  natively instead of being silently punted to the Docker pip-install
  fallback; `fluid doctor` reports the detected engine and the welcome scan
  annotates the dbt row; `$DBT_EXECUTABLE` is honoured by the parse gate. The
  generated YAML is engine-aware too: `--dbt-tests-key auto|tests|data_tests`
  (env `FLUID_DBT_TESTS_KEY`) auto-detects the user's dbt and emits
  `data_tests:` for Fusion / core ≥ 1.8 while keeping the legacy `tests:`
  spelling for older cores — Fusion's strict parser no longer rejects
  generated projects. (#423, #429)
- **dbt build results close the loop into run records and verify.**
  `fluid apply --mode amend-and-build` now parses `target/run_results.json`
  after `dbt build` into per-test run records — `fluid runs status` shows each
  dbt test with its status and failure counts, and `fluid verify` gains
  transformation checks (`dbt_tests_passed`, no error-severity failures) that
  gate the exit code under `--strict`. (#420)
- **`fluid verify --reconcile-lineage`.** A local-only cross-check (no
  network) that the contract's declared lineage (`consumes[]` / `exposes[]`)
  agrees with what was actually observed (run records + cursor state) and
  what would be published (the catalog registrar payload, rebuilt locally).
  Drift classes: `declared_but_never_read` (soft), `read_but_undeclared` and
  `publish_payload_mismatch` (critical, gate under `--strict`). (#430)

### Changed

- **Contract schema 0.7.5 is now stable (GA); 0.7.6 opens as the next
  preview.** Untagged contracts now validate against 0.7.5 (was 0.7.4),
  graduating the Redshift-Serverless/Kinesis `bindingLocation` fields, the
  vector/embeddings `vectorConfig` output port, and the streaming
  Kafka→Iceberg surface. Note: untagged contracts' plan/bundle digests churn
  once on upgrade; explicitly-tagged contracts are unaffected. (#431)
- **One shared contract→dbt-test mapping across all three generation paths.**
  The engine, exporter, and copilot generators now delegate to a single
  module, so they can no longer drift: `relationships` tests now derive from
  the engine and exporter paths (previously copilot-only), numeric ranges
  standardize on `dbt_expectations.expect_column_values_to_be_between`
  (retiring `dbt_utils.accepted_range`), and freshness/recency now surfaces
  in the engine path. A symmetric reverse table powers the manifest
  importer. (#421)
- **CI: the overloaded Python 3.12 test leg was split**, moving the coverage
  run and gates into a parallel `coverage-and-audit` job so no matrix leg
  exceeds ~8 minutes. (#428)

### Fixed

- **Copilot enrichment now writes schema-valid contracts.** `fluid forge`'s
  `--apply-enrichment` pass wrote five slots the schema rejects, so every
  enriched contract failed `fluid validate`; enrichment data now lands in
  schema-valid locations (`qos.freshnessSLO` + a `dq.rules[]` freshness rule,
  the `extensions.enrichment.*` namespace, `binding.properties.physical`),
  and re-applying migrates contracts enriched by older versions. The quality
  engine also now parses the ISO-8601 `dqRule.window` format the schema
  requires (`PT6H`, not just `6h`), and a declared-but-unparseable window
  fails the check loudly instead of silently disabling the gate. (#417)
- **Retired the intermittent test-leg hang and the last `datetime.utcnow()`
  calls.** All 18 remaining `utcnow()` sites are now timezone-aware
  (removing the 3.12+ deprecation warnings that fed an unbounded mcp 1.x
  warning-relogging loop — filed upstream with a minimal repro), the test
  suite isolates root-logger state, and hosted-MCP operations gained a
  wall-clock bound via `FLUID_HOSTED_MCP_TIMEOUT_SECONDS` (default 120s).
  (#418)

## [0.11.0] — 2026-07-13

A forge-UX + AI-tooling feature release: arrow-key menus, `--offline` and
`--watch` modes, multi-provider AI config with an LLM-provider plugin system,
a layered prompt-customization stack, nine new domain agents, opt-in agent
tools, a pgvector RAG output port, and contract↔dbt reconciliation — plus SQL-
safety and redaction hardening. No breaking changes (the bare `odps` emit key
becomes a deprecated alias for `opds`).

### Security

- **Four remaining SQL string-literal sites route through the central
  `quote_string_literal` helper** (local DuckDB provider, IAM policy compiler,
  Meltano runner, MCP DuckDB driver). The local provider used `repr()`, which
  emitted double quotes — so any CSV/Parquet path containing a single quote
  (e.g. `/tmp/o'brien/data.csv`) broke the `read_csv_auto` / `read_parquet`
  load outright. (#387)
- **Six redaction-symmetry gaps closed** between the global secret redactor and
  the Snowflake-local twin: bare `passphrase=` values, quoted-JSON
  `"credentials"` keys, `auth` / `conn_str` / `connection_url` dict keys,
  generic (non-`eyJ`) three-segment JWTs, and bare `private_key=` values are
  now masked by both layers. (#389)
- **Exception text no longer leaks.** The `read_logical_model` copilot tool
  returns typed errors instead of interpolating raw exception strings into the
  LLM context; the always-on logging redaction filter now scrubs exception
  *objects* (the pervasive `LOG.warning("… %s", exc)` shape); and litellm
  exception text in user-facing error messages is routed through the secret
  redactor. (#392, #394)

### Added

- **Arrow-key navigation for every interactive menu.** ↑/↓ (and vim `k`/`j`)
  plus Enter on a real TTY across all seven forge / ai-setup menus, degrading
  cleanly to the numbered prompt in CI, pipes, and non-TTY shells; opt out with
  `FLUID_FORGE_NO_ARROW_KEYS=1`. (#352)
- **`fluid forge --offline`** (env twin `FLUID_FORGE_OFFLINE=1`) — a
  first-class no-network guided authoring path: no LLM, no mode picker, no
  remote schema fetch, and `--non-interactive` support for air-gapped or
  scripted runs. (#355)
- **`fluid forge --watch`** — watches the discovery path and regenerates the
  contract on source change, debouncing save-storms and never retriggering on
  its own output; Ctrl-C exits cleanly. (#362)
- **Mid-run LLM failure recovery.** An API-key 401 now re-prompts for a fresh
  key and retries generation in place (interview answers preserved) instead of
  ending the run; a 429 prints a `Rate limited. Waiting Ns before retrying...`
  notice instead of a silent frozen spinner. (#353, #361)
- **Multi-provider AI config.** `fluid ai setup` saves each provider into a
  map — a second setup no longer clobbers the first — `fluid ai status` lists
  every saved provider, and `fluid forge --llm-provider <name>` resolves any
  saved provider's key even without its env var set. (#363)
- **Import API keys from other CLIs.** `fluid ai setup` detects existing keys
  in well-known credential files (Codex `auth.json`, OpenAI/Anthropic
  dotfiles, `gh` `hosts.yml` for GitHub Models) and offers to import them —
  explicit opt-in prompt, HOME-confined reads, key values never displayed or
  logged. (#366)
- **Custom LLM provider plugins.** Third-party packages register providers via
  the `fluid_build.llm_providers` entry-point group
  (`pip install` → `fluid forge --llm-provider <name>`, no core edit);
  built-ins always win over a name clash, and the operator plugin allow/block
  policy governs discovery. (#365)
- **Layered prompt customization.** `fluid forge --prompt-profile <name>`
  swaps the whole prompt-guidance set for a named profile (bundled
  `eu-gdpr-strict` and `ai-lab-permissive` exemplars); per-tenant home-directory
  shadows and per-domain fragments override individual guidance blocks; and
  stackable `--prompt-overlay a,b,c` patches compose on top with validator
  rules and optional ed25519 signing. The active profile is stamped into
  `metadata.provenance.prompt_profile`. (#359, #364, #383)
- **Nine new built-in domain agents.** Eight verticals — manufacturing,
  logistics, energy, government, insurance, pharma, education, media — each
  grounded in its industry's authoritative standards (ISA-95, GS1 EPCIS,
  IEC CIM, NIEM, ACORD, CDISC, Ed-Fi, EIDR, …), plus `ai_ready`, which
  deterministically enforces AI-readiness metadata: per-port `agentPolicy`
  ("reporting yes, training no" for sensitive data), PII/sensitivity flags,
  and `ai-embeddable` column labels for RAG consumers. (#371, #405)
- **Domain keyword learning.** Forge tracks which product domains you build
  across runs (locally, in `~/.fluid/ai_config.json`) and, once a domain
  repeats, proactively nudges its template using frecency ranking. (#367)
- **pgvector vector/embeddings output port.** `fluid generate vector
  <contract>` compiles pgvector-bound exposes into an embeddings table, ANN
  index DDL (`hnsw` default / `ivfflat`), and a RAG manifest — driven by
  `vectorConfig` and the `ai-embeddable` labels the `ai_ready` agent stamps. (#410)
- **Opt-in agent tools.** SSRF-safe `web_search` + `web_fetch` behind
  `FLUID_AGENT_WEB_TOOLS=1` (private/metadata address ranges and DNS-rebind
  blocked, typed errors, keys never logged); plus the AI-tools trio — a
  read-only, redacted `fetch_sample_rows` live-DB sampler
  (`FLUID_FORGE_DB_TOOLS=1`), tool-search deferred schema loading
  (`FLUID_FORGE_TOOL_SEARCH=1`), and a hosted-MCP registry for the GitHub and
  Snowflake MCP servers (`FLUID_GITHUB_MCP=1` / `FLUID_SNOWFLAKE_MCP=1`). All
  off by default. (#358, #407)
- **Semantic-drift guard (opt-in).** `FLUID_FORGE_DRIFT_GUARD=1` catches the
  LLM renaming, dropping, or silently retyping columns relative to the
  discovered source schema or the `--refine` prior contract, and feeds the
  drift into the existing self-healing repair loop. (#409)
- **`fluid verify --reconcile-dbt`.** Statically cross-checks
  `exposes[].contract.schema` against the dbt project's `schema.yml` columns
  and reports drift (missing columns, type mismatches, unmatched models);
  drift exits 1 for CI, `--warn-only` downgrades it. (#403)
- **One guided Quickstart starter picker + provider blueprints.** `fluid
  init`'s first-run menu collapses from five rows to Quickstart / AI / Empty,
  with Quickstart leading to a single starter picker; new bundled GCP
  (BigQuery) and Snowflake starter blueprints are reachable via
  `--quickstart --provider gcp|snowflake` or `--blueprint <id>`. Existing
  flags are unchanged. (#370, #402)
- **`fluid apply --env` reaches apply hooks.** Hooks accepting an env
  parameter receive the resolved environment via backward-compatible signature
  dispatch (3-arg hooks unchanged), removing the `DEPLOY_ENV` fail-open. (#368)
- **Opt-in UX telemetry: provider choice + run completion.** Two bounded,
  enum-like fields behind the existing default-OFF consent gate with unchanged
  `DO_NOT_TRACK` / `FLUID_TELEMETRY` precedence. (#357)
- **Emulator-friendly AWS IaC.** When a custom endpoint is configured
  (`AWS_ENDPOINT_URL*`), the emitted `provider "aws"` block enables path-style
  S3 addressing and the validation skips LocalStack needs; on real AWS the
  output is byte-for-byte unchanged. (#411)
- **Examples.** 5-minute quickstart READMEs for examples 01–06 with an index,
  plus three realistic AWS-first example contracts (S3/Glue/Athena Parquet
  lake, Iceberg lakehouse, medallion lake) — all pinned by e2e validate
  tests. (#356, #384)

### Changed

- **DataHub registrar self-heals transient GMS failures.** Exponential backoff
  with jitter on 429/5xx (mirroring DataHub's own emitter defaults), honours
  `Retry-After`, configurable via `FLUID_CATALOG_DATAHUB_MAX_RETRIES`; a
  terminal outage still degrades to a clean failure instead of blocking the
  publish. (#404)
- **Faster, lighter `fluid --help`.** `fluid_build.commands` plugin
  subcommands now load lazily (an installed plugin's heavy imports no longer
  land on the cold path — and plugin subcommands that previously crashed on
  dispatch now work), and the `cli.mcp` re-exports + OTLP exporter are
  deferred, cutting roughly 200 modules off the cold start. (#369, #390)
- **LLM runtime relocated to `fluid_build.llm`,** severing the `copilot ⇄ cli`
  import cycle; every legacy `fluid_build.cli.forge_copilot_llm_*` import path
  still resolves to the same module via aliases. (#391)
- **CI hardening.** An incremental `mypy --strict` gate now covers four
  security-hotspot modules (#360), alongside routine pinned-action bumps.

### Fixed

- **`fluid apply --mode replace-and-build` now hits the data-loss gate.** The
  build-mode early-return dispatched to the builders before the stage-7 safety
  gate ran, so a destructive replace-and-build in a protected env proceeded
  without `--allow-data-loss`; the gate now runs before engine dispatch on
  both the native and OpenTofu paths. (#386)
- **FLUID-emitted ODCS round-trips losslessly.** Export is now a fixed point
  (`export(import(export(x))) == export(x)`): a phantom top-level `name` and
  dropped explicit `required: false` no longer appear on re-export. (#373)
- **OPDS/ODPS naming untangled; `opds` restored to the default emit set.** The
  LF/ODPI Open Data Product Specification is `opds` (provider renamed to
  `providers/opds/`), Bitol's Open Data Product Standard stays `odps-bitol`,
  and the bare `odps` key is a deprecated alias that warns; OPDS artifacts now
  validate against the vendored v4.1 schema in `fluid validate artifacts`. (#381)
- **Redshift Serverless + Kinesis contracts validate.** `bindingLocation` in
  schema 0.7.5 now declares `stream`, `namespace`, `workgroup`,
  `iam_role_arn`, `external_schema`, and `glue_database` — fields the AWS IaC
  emitter already read — so those contracts no longer fail `fluid validate`
  while passing `fluid generate iac`. (#396)
- **AWS IaC declares `aws_caller_identity` whenever the account-derived
  warehouse bucket is referenced,** so bucket-less Glue table contracts no
  longer fail `tofu plan` with an undeclared-resource error. (#408)
- **Copilot project memory reads and writes the same file.** Load, save, and
  `--show-memory` / `--reset-memory` all resolve one canonical memory root, so
  scaffolding into a subdirectory no longer duplicates or orphans accumulated
  workspace memory. (#354)

## [0.10.2] — 2026-06-27

A polish + internal-quality release on top of 0.10.1. No breaking changes.

### Added

- **`fluid init --blueprint` accepts `--domain` / `--owner-team` /
  `--owner-email`** and now writes a `.fluid/forge-receipt.json`, reaching parity
  with the `--blank` and template scaffolds so `fluid status` and drift detection
  see the same shape. The metadata flags flow through into the rendered
  contract's `domain` + `metadata.owner`. (#324)

### Changed

- **Error catalog: diagnostic-only entries now carry an actionable next step.**
  Five high-traffic failures (`generate_ci_failed`, `product_new_failed`,
  `signing_bundle_not_file`, `schedule_sync_dags_dir_not_directory`,
  `loader_missing_functions`) now pair the "what's wrong" line with a concrete
  "what to do"; `missing_contract` shares the scaffold hint with
  `contract_required`. (#326)
- **Internal — `fluid forge`'s `run_ai_copilot_mode` decomposed.** The interview,
  domain-enrichment, and project-creation cores are extracted into named helpers
  behind a typed `ForgeRunContext` carrier (behaviour-preserving, guarded by the
  304-test characterization net; 691 → 478 LOC). (#329)

### Fixed

- **Thread-safe, resettable lazy `CopilotAgent` cache.** The lazily-built
  `CopilotAgent` class is memoised with `functools.lru_cache` (lock-guarded,
  stable identity, `cache_clear()` reset hook) instead of a hand-rolled module
  global — removing a double-checked-locking race. (#323)

## [0.10.1] — 2026-06-27

A bug-fix + security release on top of 0.10.0.

### Security

- **Mask credentials embedded in URL userinfo** (`scheme://user:password@host`) <!-- pragma: allowlist secret -->
  across both redaction layers — the global `secret_redactor` and the
  Snowflake-local twin. Such a password (an Iceberg REST catalog
  `binding.location.uri`, a JDBC / `connection_url`, or a redis/AMQP broker URL)
  could previously survive into a log line or a persisted run record. The shared
  pattern is length-bounded (ReDoS-safe) and masks only the password, preserving
  scheme / user / host. (#316)

### Fixed

- **Iceberg REST/GCP/Azure catalog profiles are now expressible in a
  schema-valid contract.** `$defs/bindingLocation` in `fluid-schema-0.7.5.json`
  now declares `catalog`, `uri`, `warehouse`, and `partitionBy` — the fields the
  resolver and the streaming-sink validator already read — so a catalog-profile
  contract no longer fails `fluid validate` with "Additional properties are not
  allowed". (#314)
- **`fluid market --blueprints --format json` now emits clean, machine-parseable
  JSON** instead of the rich human table plus status banners, so it can be piped
  into a script. (#317)
- **`fluid market --blueprints` no longer prints a spurious "'fluid marketplace'
  is deprecated" banner** — that notice now fires only for a direct (hidden)
  `fluid marketplace` invocation. (#319)

## [0.10.0] — 2026-06-27

A plugin-system + provider-taxonomy release: the unified plugin manager with all
four SDK roles wired in, a real plugin↔CLI version gate, the ODPS/ODCS
provider-vs-exporter fix, and the operator surfaces (`fluid plugins`,
`fluid exporters`) — plus first-run/UX and error-reporting improvements.

### Added

- **Unified plugin manager + all four SDK roles wired into the CLI.** One
  host-side discovery substrate (`fluid_build.plugin_manager`) walks the
  role-tagged entry-point groups with per-plugin fail-isolation and an operator
  allow/block policy (`FLUID_PLUGINS_ALLOWLIST` / `FLUID_PLUGINS_BLOCKLIST`). The
  `Validator` role is wired into `fluid validate`, the `CatalogAdapter` role into
  `fluid publish`, and IaC clouds are now entry-point-pluggable
  (`fluid_build.iac_providers`); the orphaned parallel `cli/plugins.py` framework
  was retired. (#292, #293, #294)
- **`fluid plugins`** — operator inspection of installed plugins by role with
  their allow/block status (name-only, never loads plugin code). **`--detailed`**
  loads *allowed* plugins to surface their declared `PluginMetadata`
  (version / author / license / url); blocked plugins are never loaded. (#296, #307)
- **`fluid exporters`** — a discoverable home for the spec exporters
  (`odps` / `odcs` / `odps-bitol`) with their spec name, URL, and
  `fluid generate standard --format` invocation, backed by a registry separate
  from the provider registry. (#308)
- **Stable error slugs + a central catalog** of suggestions / docs URLs, so CLI
  errors carry a stable identifier and actionable next steps. (#302)
- **`fluid forge` can delegate agent tool-calls to the dbt MCP server** (opt-in),
  letting the copilot drive dbt through MCP. (#305)

### Changed

- **ODPS and ODCS are no longer cloud providers — they are spec EXPORTERS.**
  ODPS (Open Data Product Standard) and ODCS (Open Data Contract Standard) are
  data-product / data-contract *spec / export formats*, not infrastructure
  providers (their `apply()` does not deploy). Both are removed from the provider
  registry and entry-points, so they no longer appear in `fluid providers`,
  `fluid plugins`, or as a `--provider` choice. The fix is **principled** — an
  invariant test rejects any registered provider whose `apply()` doesn't deploy,
  so a future sibling can't slip through. Export is unchanged via
  `fluid odps export` / `fluid odcs` / `fluid generate standard --format` and the
  new `fluid exporters`. **Breaking (CLI surface):** `--provider odps` / `opds` /
  `odcs` are no longer accepted. (#298, #300, #304)
- **Friendlier first-run.** `fluid init` defaults to a Quickstart when no LLM key
  is configured, and counts a keyless coding agent (Claude Code / Cursor / Kiro)
  as AI-available when picking the menu default. (#303, #309)

### Fixed

- **The plugin↔CLI version gate is now real** (was a dead handshake): the CLI
  version comes from `importlib.metadata`, reads the new `fluid_sdk`, and gates a
  plugin's `requires_cli` PEP 440 specifier via `packaging.SpecifierSet`. (#291)
- **`fluid skills` is registered** — it shipped invisible due to swapped
  `_try_register` arguments. (#287)
- **`fluid product-add` emits canonical, schema-valid contract shapes** (routes
  sources / exposures / dq to their canonical homes). (#288)
- Corrected stale title/description in `fluid-schema-0.7.5.json`. (#290)

### Security

- **Every code-executing entry-point group is governed by the unified
  allow/block policy** — providers, commands, apply-hooks, extension
  schemas/validators, modeling techniques, source adapters — closing the gap
  where a blocked plugin could still load and run. Provider discovery no longer
  leaks raw exception text / tracebacks into `DISCOVERY_ERRORS` (type-only). (#295, #297)

### Performance

- `requests` and `build_runners` are deferred off the cold `--help` path. (#301)

## [0.9.0] — 2026-06-25

The Kafka → Apache Iceberg streaming-sink release: `fluid` now derives Iceberg
sink configuration for Kafka Connect, Debezium Server, and Confluent Tableflow
behind an opt-in 0.7.5 schema — alongside Windows UTF-8 fixes, a deterministic
`planDigest`, and streaming-credential redaction hardening.

### Security

- **Run records are redacted before they reach disk.** Kafka Connect
  task-failure traces captured into run-record facets can echo the connector
  config — `database.password`, S3 keys, `sasl.jaas.config` — and were written
  to `.fluid/<product>/…/runs/<run_id>.json` in plaintext, bypassing the
  logging redactor entirely. Every runner's run record now passes through the
  recursive redactor at the single `write_run_record` chokepoint before the
  bytes hit disk. (#272)
- **`sasl.jaas.config` values are fully masked in both redaction layers.** The
  JAAS string's value embeds the SASL login secret but its key contains no
  sensitive substring, so the key matcher missed it and a non-`password=`
  secret (e.g. OAuthBearer `clientSecret=`) could leak into logs and failure
  traces. Both the global `SecretRedactingFilter` and the provider-local
  redactor now mask the whole value, on the dict-key and serialized-text
  paths. (#270)
- **Dotted streaming-credential keys redact symmetrically.**
  `s3.secret-access-key`, `session-token`, `gcs.oauth2.token`, and
  `jdbc.password` are masked across all redaction layers (text and dict
  paths), delegating the key decision to the single `is_sensitive_key_name`
  source of truth. (#286)

### Added

- **Opt-in `fluid-schema-0.7.5` with a `streamingSink` block.** The
  `kafka-connect` properties gain a closed `streamingSink` object (typos in
  the new surface are caught) with typed `iceberg_sink_enabled` /
  `sink_topics` / `iceberg_catalog_overrides`, and `iceberg_table` is accepted
  as an alias of the `iceberg` expose format. 0.7.5 is a preview version —
  bundled and validatable on explicit opt-in, never the silent default, so
  existing contracts' plan and bundle digests don't churn. (#266)
- **Derived Iceberg sink config for Kafka Connect.** With
  `sink.format: iceberg` and no hand-written `sink_connector_config`, the
  runner derives the full `iceberg.catalog.*` / `iceberg.tables.*` connector
  config: Glue and REST catalogs, a per-product control topic (avoiding the
  shared `control-iceberg` collision), JSON or Avro converters depending on
  Schema Registry presence, and an `iceberg_catalog_overrides` escape hatch
  where operator keys win. Contracts with a hand-written config are
  byte-for-byte unaffected. (#264)
- **REST + GCP/Azure Iceberg catalog profiles.** The catalog resolver picks
  the FileIO implementation from the warehouse scheme (`s3://` → S3FileIO,
  `gs://` → GCSFileIO, `abfss://` → ADLSFileIO) and resolves REST/GCP/Azure
  catalog kinds with a safe `rest` fallback. (#280)
- **Plan-time validation for the Iceberg streaming sink.** `fluid validate`
  now catches the connector's silent-fail-at-first-record traps: an Iceberg
  sink build with no matching expose, `upsertMode` (deferred in v1), dynamic
  routing without `routeField`, an incomplete REST catalog binding, and an
  `iceberg_catalog_overrides` warehouse that diverges from the binding
  warehouse. (#267)
- **Kafka Connect task-level failure detection.** The status poll previously
  checked only `connector.state`, so a connector reporting `RUNNING` over a
  `FAILED` task counted a broken sink as a successful run. Health now requires
  the connector RUNNING and no task FAILED — the Iceberg sink connector
  included — with each failed task's trace captured into the run record; the
  Iceberg late-events side table is named `<database.table>__late_events`
  beside the target. (#268)
- **Embedded Debezium-Server Iceberg sink.** The debezium runner derives the
  `io.debezium.server.iceberg` config surface (warehouse, `table-namespace`,
  catalog impl, FileIO, region, upsert/identifier fields, partitioning) — a
  different surface from the Kafka Connect keys. Off when a hand-written
  `config` block is present; opt in via `iceberg_sink_enabled: true`. (#284)
- **Confluent Tableflow IaC plugin — managed Kafka → Iceberg.** A `confluent`
  platform binding compiles into the `confluentinc/confluent` OpenTofu
  provider's managed control plane: a Tableflow topic (`byob_aws`), the AWS
  Glue catalog integration, and the storage provider integration, with a
  validate-time anti-no-op gate. It reuses the AWS warehouse writer so the
  managed and self-managed paths resolve to the same table identity, and the
  emitted module stays credential-free. (#285)

### Changed

- **`fluid --help` no longer loads the MCP server SDK.** The ~87 `mcp.*`
  modules that every non-MCP command eagerly imported are now built lazily on
  first `fluid mcp serve`, cutting ~245 modules (~15%) off the cold path, with
  the startup budget enforced as a hard CI gate; `cli/mcp.py` was subsequently
  split into a package with the lazy loading and all policy/permission gates
  preserved. (#265, #282)
- **`fluid_build/config.py` renamed to `config_defaults.py`.** Distinguishes
  the compile-time static defaults from the runtime hierarchical
  `config_manager.py`; update imports if you consume the package as a
  library. (#225)

### Fixed

- **`planDigest` is deterministic across identical plan runs.** The volatile
  `generated_at` timestamp leaked into the SHA-256 and topological action
  ordering depended on per-process hash randomization, so two identical
  `fluid plan` runs disagreed — breaking apply-time plan-binding verification
  and CI digest diffing. Timestamps are masked out of the digest (they remain
  in `plan.json` as audit metadata) and action levels are built in stable
  parse order. (#260)
- **The CLI no longer crashes on Windows cp1252.** stdout/stderr are
  reconfigured to UTF-8 at startup — previously the first emoji in the help
  banner raised `UnicodeEncodeError` whenever output was piped or captured —
  and all ~160 text file I/O sites now pass `encoding="utf-8"` explicitly, so
  contracts with accented names or emoji read and write correctly under a
  cp1252 locale. A static test gate rejects any new unencoded text-I/O site.
  (#263, #269)
- **Iceberg warehouse derivation unified into a single writer.** The S3
  warehouse location for an Iceberg/Glue exposure was derived at three drifted
  sites (native planner, OpenTofu Glue emitter, Lake Formation emitter) that
  could resolve the same binding to different warehouses; all three now call
  one canonical `get_iceberg_warehouse`, with contract-derived text kept out
  of OpenTofu interpolation. Explicit-bucket contracts emit byte-identical
  output. (#262)
- **`fluid bundle --out -` no longer leaks its logging redirect.** Bundling to
  stdout set `propagate = False` process-globally and never restored it,
  silently breaking log propagation for every later caller in the same
  interpreter (pytest `caplog`, the `forge_run` MCP tool, library embeddings);
  logging state is now snapshot on entry and restored on exit while stdout
  stays clean during the call. (#261)
- **Debezium `.properties` values escape backslashes.** `java.util.Properties`
  treats a bare backslash as an escape introducer, silently corrupting
  values; the emitter now escapes per the Java grammar. (#286)

## [0.8.11] - 2026-06-16

### Added

- **Pluggable metadata source adapters for `fluid forge data-model from-source`.**
  A new `fluid_build.source_adapters` entry-point group lets a third-party
  package register a custom `CatalogAdapter` (e.g. an internal/enterprise
  metadata catalog) that merges into the `--source` choices and dispatch — and
  the `forge_from_source` MCP tool — without forking the CLI. Built-in catalog +
  JDBC sources now resolve through one shared registry (the two previously
  duplicated dispatch tables are gone). (#247)
- **Pluggable modeling techniques, plus `flat` and `custom`.** A new
  `fluid_build.modeling_techniques` entry-point group makes
  `--modeling-technique` extensible. Two new built-ins: **`flat`** (source-aligned
  1:1 — one expose per source table, no reshaping) for bronze / source-aligned
  products, and **`custom`** (bring-your-own logical model used verbatim via
  `--logical-model <path>`, no reshaping). The closed
  `{data_vault_2, dimensional}` enum is now a registry the modeler and contract
  emitter read instead of branching on the technique name. (#248)

### Fixed

- **Contract schema now accepts dbt features the build runner already supports.**
  `build.engine` accepts the whole `dbt-<adapter>` family (`dbt-glue`,
  `dbt-snowflake`, …) generically, and the hybrid-reference build pattern accepts
  `properties.target`, `properties.select`, and `properties.models` — all of
  which the dbt runner already honoured but `fluid validate` rejected. Applied
  across every bundled schema (0.7.1–0.7.4). (#249)
- **Nightly live-LLM / TS-conformance gate** no longer false-fails when no LLM
  API key is wired; duplicate `CircuitBreakerOpenError` definitions consolidated
  into `fluid_build/errors.py`. (#223)
- **CI Docker base bumped to `python:3.13-slim`** with grype-ignores for the
  unfixable CPython CVEs. (#226)

### Security

- **`Dockerfile.verify` now runs as a non-root user** (`fluid`, UID/GID 1000,
  overridable via `--build-arg UID/GID` for Linux hosts), closing trivy
  DS-0002 and matching the production image's non-root posture. (#235)
- **`.gitleaks.toml` with triaged allowlists** so full-history `gitleaks
  detect` scans run clean. All 464 historical candidates were confirmed fake
  (`trufflehog --only-verified`: 0): the detect-secrets baseline's hashed
  digests, redaction-test fixtures, and the redactor pattern sources.
  Requires gitleaks ≥ 8.25.0. (#236)

### Changed

- Dependency / CI-action bumps via Dependabot: black 26.3.1→26.5.1 (#232),
  `actions/setup-node` (#231), `actions/setup-go` (#243),
  `actions/github-script` (#244), `github/codeql-action` (#246),
  `docker/metadata-action` (#230), `docker/login-action` (#227),
  `docker/setup-buildx-action` (#245),
  `aws-actions/configure-aws-credentials` (#229),
  `peter-evans/create-pull-request` (#228),
  `softprops/action-gh-release` (#242).

## [0.8.10] - 2026-06-08

### Added

- **`fluid describe --self` self-description for the Command Center.** Library-
  callable `fluid_build.describe.self_describe()` now returns `fluid_version`,
  `schema_version`, `providers`, `build_engines`, `templates`, `capabilities`,
  and the full **command tree** (every subcommand + its flags), consumed by the
  backend via `GET /api/v1/forge/capabilities` so the UI can render the CLI
  surface without lagging it. (#220)
- **Opt-in usage telemetry, default OFF.** Nothing is emitted unless explicitly
  opted in; honours `FLUID_TELEMETRY=0` and the `DO_NOT_TRACK` standard, and the
  resolved state is surfaced in `fluid doctor`. (#217)
- **Reuse an existing LLM API key during AI setup.** `fluid` AI setup now offers
  to persist a recognized built-in-provider key already present in the
  environment (read-only, explicit confirmation, never logged). (#217)
- **Contract diff before writing on refine/regenerate.** The refine/preview flow
  now shows what changed (reusing the changelog differ) before the contract is
  written. (#217)

### Changed

- Renamed `providers/snowflake/provider_enhanced.py` → `provider.py` (canonical
  `SnowflakeProvider`); class name and entry points are unchanged. (#217)
- Added a startup-budget perf gate (`fluid --help` module-count + cold wall-time
  in clean subprocesses) to catch CLI-startup regressions. (#217)

### Fixed

- **BigQuery rollback restore.** `fluid rollback` now restores BigQuery products
  (previously Snowflake only), replaying the snapshot's
  `CREATE OR REPLACE TABLE ... AS SELECT` via the BigQuery client with a `gcp`
  dispatch alias. (#217)
- **`fluid generate iac --provider auto` detection broadened.** The target cloud
  is now detected from top-level `binding.{provider,platform}`, `builds[].provider`,
  and the runtime platform — not just `exposes[].binding.platform` — fixing the
  common single-binding contract that previously errored as "could not detect a
  supported cloud" (surfaced as a 422 in the Command Center); `local`/DuckDB now
  raises an actionable `generate_iac_local_target` error. (#211)
- **odps export fails loud.** `fluid generate standard --format odps-v4.1` /
  `fluid odps export` now error instead of silently writing the literal `[]` to
  disk while exiting 0. (#211)
- **CI resilient to transient OpenTofu registry outages.** IaC tests convert
  `tofu init` 504 / network failures to skips while real `tofu validate` schema
  errors still fail. (#218)

### Security

- **Fixed injection / RCE / RLS findings across the platform.** (#215)
  - Code-injection (RCE) in generated Airflow DAGs — untrusted contract values are
    now routed through repr-escaped literals / sanitized identifiers.
  - Shell-RCE + SQL injection in the Redshift external-schema emit — closed.
  - Multi-tenant row-level-security **bypass** in the MCP `query` / `query_sql`
    tools — they now apply `policy.rowFilters[]` and fail closed on missing caller
    identity.
  - Path-traversal RCE in the dlt custom-source loader — now fails closed via
    workspace confinement.
  - Adds cross-provider AST regression tests.
- **Rollback no longer executes attacker-authorable DDL.** The Snowflake and
  BigQuery rollback restorers reconstruct the restore statement from validated
  identifiers instead of replaying `snapshot.ddl[]` verbatim, so a tampered
  `.fluid/rollback-state.json` cannot smuggle arbitrary SQL (DROP/DELETE/GRANT). (#217)
- **Hardened CI Security Scan.** Project dependencies are now audited before the
  SAST scanners are installed, unbreaking the scan job. (#212)

### Removed

- **Unused root `requirements.txt` / `requirements.lock.txt`.** Neither was
  referenced by CI, the Dockerfiles, packaging, or any install step —
  `pyproject.toml` is the single source of truth. Removing them clears spurious
  Dependabot churn and false security alerts. (#213)

### Dependencies

- Bumped runtime dependencies: typer 0.25.1→0.26.6 (#197), certifi (#201),
  openai 2.37.0→2.40.0 (#202), yarl (#203), more-itertools (#204), aiohttp (#207).
- Bumped CI actions: setup-python (#195), attest-build-provenance (#196),
  build-push-action (#199), google-github-actions/auth (#200), checkout (#198).
- CI also gained clean-skip of MCP e2e jobs when secrets are missing (#209) and
  SHA-pinned dev workflows with a refreshed `PINNED_ACTIONS` list (#206).

## [0.8.9] - 2026-06-01

### Added

- **Keyless contract authoring via local AI coding agents.** `fluid forge` can
  now author contracts without an LLM API key of its own by reusing an AI coding
  agent the user already has, across two topologies on the existing `LlmProvider`
  seam (selection is by provider name — no new backend switch):
  - **In-IDE (MCP sampling).** `--llm-provider mcp-sampling` is now accepted by
    the `forge` / `forge data-model` argparse `choices` allowlist and exempted
    from the api-key gate (added to `_KEYLESS_PROVIDERS`), so `MCPSamplingProvider`
    routes the LLM call back to the host IDE via `sampling/createMessage` with no
    key (previously rejected as `copilot_missing_llm_api_key`).
  - **Standalone terminal (new).** A single parametrized `CodingAgentProvider`
    (new module `fluid_build/cli/forge_copilot_coding_agent.py`) shells out to each
    agent's headless CLI — `claude -p`, `codex exec`, `cursor-agent -p`,
    `kiro-cli chat --no-interactive` — over one list-argv subprocess seam
    (`_run_agent`, never `shell=True`), with schema-constrained output and
    ANSI-stripped stdout before envelope extraction. Only Claude Code is truly
    zero-setup keyless (subscription OAuth); codex / cursor / kiro reuse their own
    stored login or key.
  - Adds welcome-scan detection of installed agent CLIs, an auto-offer in
    `ai_setup.run_ai_setup_inline`, a `--forge-agent-mode {envelope,agentic}`
    flag, `fluid doctor` reporting, and the `FLUID_FORGE_AGENT`,
    `FLUID_FORGE_AGENT_MODE`, `FLUID_FORGE_AGENT_TIMEOUT_SECONDS`, and
    `FLUID_FORGE_AGENT_CWD` env vars. (#189)
- **Native copilot support for ANY `contract.extensions.<key>` block.** A new
  `fluid_build.extension_schemas` entry-point group lets a plugin advertise the
  JSON-Schema for its extension sub-key
  (`get_extension_schema(fluid_version=None) -> dict`). The `fluid forge` copilot
  now (a) grounds the modeler on every installed extension schema so it can
  natively propose a valid block, and (b) schema-gates + validates the proposed
  block before emit. Adding a new extension needs **zero** forge-cli changes — the
  entry-point group is the entire contract. See `fluid_build/extension_schemas.py`. (#191)

### Fixed

- **Pre-emit extension validation.** The copilot's conformance pass now runs
  registered `fluid_build.extension_validators` (the same plugins `fluid validate`
  uses) before writing the contract, so a malformed `extensions.<key>` block
  surfaces as an error-severity finding and enters the repair loop instead of
  being emitted silently (the core schema treats `extensions` as
  `additionalProperties: true`). (#191)

### Changed

- Updated the `sdk` optional extra from the retired `fluid-provider-sdk` to
  `data-product-forge-sdk>=0.9,<1` (`fluid_sdk`). (#191)

## [0.8.8] - 2026-05-31

### Added

- **On-demand OpenTofu provisioner — `fluid apply --ensure-opentofu`.** A cloud
  apply shells out to `tofu` via the OpenTofu engine; the official standalone
  installer needs root (`/usr/local/bin`) and gpg/cosign, which a non-root CI
  runner (e.g. a locked-down Jenkins agent) can't provide. When `--ensure-opentofu`
  is set and `tofu` is missing, FLUID downloads a pinned, **SHA-256-verified**
  OpenTofu release and installs it using only the Python standard library — **no
  root, gpg, cosign, curl, or unzip** — extracting only the `tofu` entry (no
  zip-slip) and prepending it to the process `PATH`. Idempotent (a usable `tofu`
  at/above the engine floor wins untouched); override the pin with
  `FLUID_OPENTOFU_VERSION`. `fluid generate ci` bakes the flag into the apply
  stage of all seven CI runners. (#187)
- **`fluid market` real catalog discovery over MCP** — DataHub, OpenMetadata,
  and Data Mesh Manager, replacing the previous demo data. (#172)
- **`fluid market` metadata enrichment** — two-phase fetch of full product
  detail plus data-asset column schema; `--detailed` is now a true superset of
  the listing rather than a replacement. (#175, #183)
- **`fluid market` onboarding + trust/usage surfacing** — actionable next steps
  and trust/usage signals in the listing. (#173)
- **`fluid market --blueprints` works offline** via bundled blueprints. (#181)

### Changed

- `fluid market` no longer serves fabricated demo data for roadmap-only catalog
  connectors (datahub / glue / data-catalog / rest); they are skipped with a
  clear roadmap note. (#171, #174, #182)
- `fluid market --format json` emits clean, machine-parseable output. (#184)
- Faster CLI startup — validation providers are lazy-loaded. (#167)
- Internal: the two simple-mode reporting blocks were extracted out of
  `cli/apply.py::run()`. (#185)

### Fixed

- **generate-ci dev-source Jenkins bootstrap left `fluid` uncallable.** It ran
  `pip uninstall -y data-product-forge` — deleting the `fluid` console script
  (the package's `console_scripts` entry point) — then invoked `fluid` relying
  only on `PYTHONPATH`, so stage 0 died with `fluid: not found`. It now keeps the
  installed console script (a `PYTHONPATH` prepend already shadows its modules
  with the bind-mounted checkout) and adds an `import fluid_build` sanity check. (#187)
- **generate-ci stage-8 policy-apply emitted an empty `--mode`.** The Jenkins
  stage read `${POLICY_APPLY_MODE}` as a raw shell env var; on the first build
  after a Jenkinsfile change the param isn't injected yet, so it expanded to
  `--mode ` and `fluid policy-apply` rejected the empty choice. Now defaults to
  `enforce` (the param's own default). (#187)
- `fluid forge --from-source` sanitizes the contract id derived from a sqlite
  file path. (#166)
- build-runners now warn when an inline-SQL build declares `engine: dbt`
  (previously silently ignored). (#168)
- `fluid policy-apply` surfaces a no-op message when a provider has no policy
  applier instead of appearing to succeed silently. (#169)
- `fluid market` per-catalog MCP search-limit param corrected (OpenMetadata
  `size`, DataHub `num_results`). (#180)
- `fluid market` / marketplace raises a proper `CLIError` instead of crashing
  with a `TypeError`. (#170)

### Removed

- Dead module `fluid_build/validation.py` (test-only, never wired in). (#178)
- Dead SQL-allowlist helpers — `parse_and_allowlist_sql` and the type/language
  validators. (#179)

### Security

- Re-symmetrized the Snowflake provider-local secret redactor with the global
  logging filter so new secret shapes are masked in both layers. (#176)

## [0.8.7] - 2026-05-30

### Fixed

- **0.7.4 schema was not actually backward-compatible with 0.7.3 (shipped in
  0.8.6).** The MCP "schema minimize" refactor that introduced
  `fluid-schema-0.7.4.json` dropped fields that 0.7.4 still advertised as fully
  backward-compatible with 0.7.3, so any 0.7.3 contract using them failed 0.7.4
  validation. Restored: top-level and per-binding `governance` (AWS Lake
  Formation admins, LF-tag definitions, principal grants, row/column filters);
  the `snowflake_view`, `redshift_table`, `redshift_serverless`, and
  `redshift_external_schema` binding formats; the `athena`, `glue`, and
  `redshift` runtime platforms; `datamesh_manager` catalog registration; and the
  `opentofu` deployment target. 0.7.4 is now a strict superset of 0.7.3 again —
  every genuine 0.7.4 addition (`expose.mcp`, the `postgres` platform, and the
  `postgres_table` / `athena_table` / `glue_table` formats) is retained. (#162)
- **Stale `0.7.3` version fallbacks no longer freeze the default schema.** Three
  degenerate "schema-discovery-came-up-empty" fallbacks (`cli/validate.py`,
  `cli/version_cmd.py`, `forge/core/validation.py`) plus four hardcoded defaults
  in `cli/plan.py` still pointed at `0.7.3`. They now derive from
  `FluidSchemaManager.latest_bundled_version()` (or include `0.7.4`), so the
  bundled-schema default tracks the newest version instead of drifting each
  release. (#163)

### Changed

- **README polish + version sync.** Single Install section, refreshed table of
  contents, CLI-completeness pass, current version numbers, and removal of the
  legacy `opds` spelling from the backward-compatibility note. (#160, #161)

## [0.8.6] - 2026-05-29

### Fixed

- **Daemon-thread leak that OOM-hung CI on Python 3.13/3.14.**
  `forge.core.monitoring.MonitoringSystem` started four background daemon
  workers (metric / log / alert processors + aggregator) per instance and
  only stopped them on an explicit `shutdown()`. Code that constructed
  instances and dropped them (notably the test suite) leaked threads
  without bound; across a long run the per-thread virtual stacks exhausted
  address space and the OS OOM-killed the process. Workers now stop
  promptly via a `threading.Event`, live instances are tracked in a
  `WeakSet`, `shutdown()`/context-manager support is added, and a per-test
  fixture drains them.
- **`ConfigManager` could corrupt process-wide defaults.** `_load_defaults`
  shallow-copied the module-level `DEFAULT_CONFIG`, sharing its nested
  dicts — so a later `set("logging.level", ...)` or config-file merge
  mutated the shared defaults in place, affecting every other
  `ConfigManager` in the process. Now deep-copies the defaults.
- **MCP gateway rate limiter no longer leaks a background thread.**
  Replaced PyrateLimiter's `Limiter` (which spins a per-instance "leaker"
  thread) with an in-process monotonic-clock deque sliding window —
  functionally identical for the single-replica gateway, with no thread
  and no dependency. Drops the `pyrate-limiter` dependency.

### Added (closes the final 5 honest gaps from the prior audit)

- **JWT bearer + mTLS gateway-native identity**
  (`fluid_build/output_ports/mcp/auth.py`). New `AuthValidator`
  strategy with modes: `shared-token` (existing v0.7.4 default),
  `jwt` (RS256/ES256/EdDSA against an issuer's JWKS endpoint,
  validates iss/aud/exp/sig), `none` (operator opts out, gateway
  warns loud at startup). Maps configured JWT claims into
  `caller_attributes` so `policy.rowFilters` `${caller.<attr>}`
  placeholders resolve cryptographically rather than via
  self-attestation. Mirrors `X-Client-CN` + `X-Client-Fingerprint`
  proxy-forwarded mTLS metadata for combined identity attribution.
  Real RSA signing roundtrip + wrong-key rejection in the test suite.

- **BigQuery row-access policy compiler** (real impl, replaces the
  earlier stub). Emits `CREATE OR REPLACE ROW ACCESS POLICY ON
  <table> GRANT TO (<service_accounts>) FILTER USING (<predicate>)`
  with caller.user → SESSION_USER() mapping and
  `agentPolicy.allowedModels` → `serviceAccount:fluid-mcp-<MODEL>@<project>`
  GRANT clauses.

- **AWS Lake Formation compiler** (real impl, replaces the earlier
  stub). Emits a runnable boto3 Python script with
  `lakeformation.create_data_cells_filter` + `grant_permissions`
  calls bound to per-LLM IAM roles
  (`arn:aws:iam::<ACCOUNT>:role/fluid-mcp-<MODEL>`). Operators paste
  into CDK/Terraform or run directly.

- **Audit webhook forwarder** for multi-instance HA. Set
  `FLUID_MCP_AUDIT_WEBHOOK_URL` (and optionally
  `FLUID_MCP_AUDIT_WEBHOOK_HEADER_AUTH`) and every audit event is
  POST-ed on a daemon thread to a SIEM aggregator (Splunk HEC,
  Datadog, Elastic, Loki). Best-effort: webhook failures NEVER
  block the local-disk write — local copy is the source of truth.

- **Multi-language MCP-client conformance** harnesses + CI matrix.
  `scripts/conformance/conformance_test.{ts,go,rs}` exercise the
  same 3-step contract (initialize → tools/list → call with allow +
  deny) via the official TypeScript SDK
  (`@modelcontextprotocol/sdk`), the community Go SDK
  (`mark3labs/mcp-go`), and the community Rust SDK (`rmcp`). New
  `multi-lang-mcp-conformance` job in `integration.yml` runs all
  three on every nightly + workflow-dispatch trigger.

- **Provider-selection regression tests**
  (`tests/output_ports/test_provider_resolution.py`). Pins the
  `_resolve_provider()` contract: when both `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` are set, the live-LLM tests prefer Anthropic
  Haiku 4.5 (the Anthropic preference is now a regression-tested
  invariant, not just a docstring).

### Added (final gap-closing pass — production-grade defence in depth)

- **PostgreSQL driver** — psycopg v3, read-only sessions,
  per-statement timeout via `SET LOCAL`, `:p_<idx>` →
  `%(p_<idx>)s` parameter rewrite. Live e2e via the dockerized
  setup in `examples/mcp-output-port-docker/`.
- **AWS Athena driver** — boto3 default credential chain,
  `StartQueryExecution` → poll → page-through with
  `ExecutionParameters`. Mocked unit tests; live AWS validation
  deferred to a follow-up.
- **Out-of-tree driver registration tested** — `register_driver()`
  contract pinned by 3 new tests so a future refactor can't
  silently break customers' private wheels.
- **HTTP / SSE transport with optional bearer-token auth.** Set
  `FLUID_MCP_AUTH_TOKEN` and the gateway returns 401 on every
  unauthenticated request BEFORE the SSE handshake. Pair with the
  Caddy / nginx mTLS templates in
  `examples/mcp-output-port-docker/proxy/` for production
  defence-in-depth.
- **Per-tenant row-level security** via `policy.rowFilters[]`. Each
  filter compiles to a parameterised `WHERE` clause bound to MCP
  `clientInfo` extra fields (`${caller.tenant_id}` etc.). Missing
  caller attributes raise `RowFilterIdentityMissing` (fail-closed
  deny — the gateway never serves rows under undefined identity).
- **Cloud-IAM compiler** (`fluid_build.output_ports.iam_compiler`).
  Emits Snowflake row-access policies + Postgres `CREATE POLICY`
  statements that honour the same `agentPolicy` + `rowFilters`
  contract — defence-in-depth so bypass-the-gateway scenarios
  (analyst querying the warehouse directly) are also gated.
  BigQuery + AWS Lake Formation targets emit clear `-- TODO`
  stubs with explicit warnings.
- **EngineDriver.close() hoisted to base class** — every driver
  now has a uniform close path. Walks `_connection` / `_client`
  attributes for cleanup; idempotent.
- **Backpressure tracking split into `_in_flight` (drain count) +
  `_actively_dispatching` (concurrency cap)** — the unit test
  caught a real bug where the previous instrumentation conflated
  the two. Operators sizing connection pools now have a clean
  metric.
- **CI integration jobs** for Postgres docker e2e + Snowflake
  live e2e. `mcp-output-port-postgres-e2e` and
  `mcp-output-port-snowflake-e2e` join `mcp-output-port-live-llm`
  on the integration workflow's nightly + workflow-dispatch
  triggers.
- **Hash-pinned lockfile recipe** — generated via
  `uv pip compile pyproject.toml --generate-hashes -o
  requirements.lock.hashed.txt` (uv is the canonical pip-tools
  replacement; ~100× faster than the previous custom script). The
  earlier `scripts/generate_hashed_lockfile.py` placeholder was
  removed in this release per /borrow-before-build (uv covers the
  full surface and is dev-time only — no runtime cost).

### Changed (security)

- **Reverse-proxy templates** for production HTTP deployments —
  Caddy + nginx with mTLS + bearer token + SSE buffering.

### Added (enterprise-grade hardening — closes the v0.7.4 gap list)

- **PostgreSQL driver** (`fluid_build/output_ports/mcp/drivers/postgres.py`).
  Mirrors the Snowflake / BigQuery shape; binds on
  `platform=postgres`, `format∈{postgres_table, table}`. Read-only
  sessions enforced at connect time; per-statement timeout via
  `SET LOCAL statement_timeout`. Borrowed-not-built: `psycopg` v3
  (parameter-binding native, SQL-injection-safe identifier
  quoting). Exercised end-to-end against a dockerized Postgres in
  the new `examples/mcp-output-port-docker/`.

- **AWS Athena driver** (`fluid_build/output_ports/mcp/drivers/athena.py`).
  Binds on `platform=aws`, `format∈{athena_table, glue_table}`.
  Uses the boto3 default credential chain (env / `~/.aws/credentials`
  / IAM role / OIDC) — no long-lived keys baked in. Polls
  `GetQueryExecution` with configurable timeout, pages through
  `GetQueryResults`, parameterised queries via `ExecutionParameters`.

- **Row-level PII / PHI redaction** at the driver boundary. Columns
  marked `sensitivity: pii`, `sensitivity: phi`, or
  `sensitivity: sensitive` in `expose.contract.schema` keep their
  KEY visible (so the agent knows the field exists and can write
  `COUNT(DISTINCT)` aggregates) but VALUES are replaced with
  `[REDACTED-PII]` before the row leaves the gateway. Distinct from
  `columnRestrictions`, which drops the column wholesale.

- **Audit-log rotation**
  (`audit_trail.rotate_audit_directory`). Bounded by
  `FLUID_AUDIT_MAX_AGE_DAYS` (default 30) AND
  `FLUID_AUDIT_MAX_TOTAL_MB` (default 256). Runs automatically on
  gateway startup; oldest files dropped first when over budget.
  Replaces unbounded growth of `~/.fluid/store/audit/`.

- **Backpressure** via `asyncio.Semaphore`. Bounds concurrent tool
  calls to `FLUID_MCP_MAX_CONCURRENCY` (default 8) so a runaway
  agent can't saturate the engine connection pool.

- **Circuit breaker** on the driver layer. Trips after
  `FLUID_MCP_CIRCUIT_THRESHOLD` (default 5) failures within
  `FLUID_MCP_CIRCUIT_WINDOW_SECONDS` (default 60); open for
  `FLUID_MCP_CIRCUIT_COOLDOWN_SECONDS` (default 30). Returns a
  `CircuitOpen` envelope fast instead of pinning event-loop slots
  on a downstream Snowflake / BigQuery / Postgres outage.

- **Token-budget enforcement** for `agentPolicy.maxTokensPerDay`
  and `agentPolicy.maxTokensPerRequest`. Per-day counter rolls on a
  sliding 24-hour window; per-request cap evaluated against the
  serialised response payload. Cap denials emit a typed
  `TokenBudgetExceeded` envelope and an audit event with
  `policySource: "token-budget"`.

- **`canStore` advisory + retention startup warning.** When the
  contract sets `agentPolicy.canStore=false` or
  `retentionPolicy.requireDeletion=true`, the gateway surfaces a
  loud stderr notice at startup explaining the gateway honours the
  hint advisorily but cannot prevent the receiving model from
  storing data once it crosses the wire (cloud-IAM ephemeral
  credentials are the only true guarantee).

- **HTTP / SSE transport** option:
  `fluid mcp output-port serve --transport http --host H --port N`.
  Borrows `mcp.server.sse.SseServerTransport` + Starlette + uvicorn
  (transitive deps of `mcp[cli]`). No built-in HTTP auth —
  documented loud at startup; pair with mTLS / OAuth proxy until
  the auth phase ships.

- **Local Docker e2e harness**
  (`examples/mcp-output-port-docker/`). One-command
  `docker compose up -d` brings up Postgres seeded with a small
  telco customer table. `run_e2e.py` drives the gateway with a
  real LLM (litellm + OpenAI / Anthropic) across 4 scenarios:
  Postgres allow + PII redaction, Postgres deny by model, Postgres
  deny by use-case, DuckDB allow on the same gate (proves
  engine-agnostic enforcement). Total LLM cost per run ~$0.0001.

- **Throughput + latency benchmark**
  (`scripts/mcp_output_port_bench.py`). Drives the gateway via the
  SDK in-memory transport (zero LLM cost). Recorded baseline on
  laptop: **1080 calls/s, p50=6.7 ms, p95=7.5 ms, p99=34 ms** at
  500 calls / 8 concurrency against the local DuckDB driver.

- **Reproducible-build lockfile** (`requirements.lock.txt`). Pins
  the resolved transitive closure of every dep so a fresh install
  on CI / a contributor laptop / a container build resolves to
  the exact versions the maintainers tested.

### Added (v0.7.4 base — from prior turns)

- **FLUID schema v0.7.4 — Runtime agentPolicy Enforcement at the MCP gateway.**
  Closes the legitimacy gap where `agentPolicy` was declarative metadata
  only. Adds the `expose.mcp` block: its presence declares an expose
  agent-consumable over MCP (sampling caps + classification) and opts it
  into the gateway. Backward-compatible with v0.7.3: existing contracts
  validate unchanged.

- **`fluid mcp output-port serve` runtime enforcement of `agentPolicy`.**
  When an expose carries an `expose.mcp` block,
  the gateway loads `policy.agentPolicy.allowedModels` / `deniedModels`
  and `allowedUseCases` / `deniedUseCases` and enforces them on every
  `tools/call`. Caller `model_id` and `useCase` come from the MCP
  `clientInfo` handshake. Built on Anthropic's official
  [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  (`>=1.20,<2.0`); the prior custom JSON-RPC dispatcher and stdio
  transport are deleted. Driver registry (DuckDB / Snowflake / BigQuery)
  and audit-trail integration are reused unchanged.

- **CLI overrides for ops/incident response.** New `--allow-models`,
  `--deny-models`, `--allow-use-cases`, `--deny-use-cases` flags on
  `fluid mcp output-port serve`. CLI values replace the contract values
  entirely (not merged); the audit event records `policySource: "cli"`
  vs `"contract"` so an audit reader can distinguish ops overrides from
  declared policy.

- **Sliding-window rate limit on the gateway.** Defaults to 60 calls per
  60 seconds per session; tune with `FLUID_MCP_RATE_LIMIT` and
  `FLUID_MCP_RATE_WINDOW_SECONDS`. Set `FLUID_MCP_RATE_LIMIT=0` to
  disable. Rate-limit denials emit a `RateLimitExceeded` envelope AND an
  audit event with `policySource: "rate-limit"`.

- **OTel + run_id correlation on every tool call.** Each `tools/call` is
  wrapped in a `fluid.mcp.call_tool` span carrying `fluid.run_id`,
  `fluid.tool`, `fluid.expose_id`, `fluid.model_id`, `fluid.use_case`,
  `fluid.policy_source`, `fluid.decision`, and `fluid.reason`. Spans are
  no-op when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

- **Graceful shutdown.** SIGTERM / SIGINT install handlers that drain
  in-flight tool calls (up to 5 s) before tearing down driver
  connections. Cloud SDK connections (snowflake-connector-python,
  google-cloud-bigquery, duckdb) are closed explicitly.

- **`FLUID_AUDIT_ROOT`** env var redirects every gateway audit event to
  a custom root (e.g. a SIEM-forwarded volume). Unset → default to
  `~/.fluid/store/audit/`.

### Changed

- **`write_audit_event` no longer collides on burst writes.** The audit
  writer was previously second-precision and overwrote concurrent
  decisions on disk — disqualifying for an enterprise audit trail. Now
  uses `<timestamp>_<microseconds>-<pid>-<tag>-<seq>_<event>.json` with
  atomic rename so no two writes can clash even at thousands of events
  per second. Stress-tested with 400 concurrent writes across 8 threads
  (zero collisions).

- **Audit-event payload carries `runId` + `policySource`** in addition
  to the previous fields. Consumers that read `~/.fluid/store/audit/`
  get a consistent correlation token across all forge-cli stages and
  can distinguish CLI-override decisions from contract-driven ones.

- **Argument-summary in audit events is now redacted** through
  `fluid_build.observability.secret_redactor` (closes a side-channel
  where caller-supplied filter literals matching JWT / Stripe / GitHub /
  bearer / `k=v` shapes landed raw in audit JSON).

- **Tool-error envelopes no longer leak engine binding info to the
  wire**: the calling LLM gets a generic "see audit trail" message
  while the full annotated error (database / schema / table hints)
  lands on the operator log + audit event only.

- **Self-attestation startup banner**: when an `agentPolicy` model or
  use-case gate is configured, the CLI now writes a clear warning to
  stderr that caller `model_id` is self-attested via MCP `clientInfo`
  and that the gateway must not be exposed over an untrusted network
  until the OAuth/mTLS phase ships.

- **Multi-expose `find_expose` error** lists agent-eligible exposes
  separately so operators don't have to grep the contract to figure out
  which `--expose-id` choices the gateway can serve.

### Notes

- The MCP gateway integrates with `litellm` (already a core dep) for
  the live-LLM regression tests; no new test deps were added.
- `mcp` Python SDK pin is `>=1.20,<2.0` to bracket the `lifespan` API
  and `mcp.shared.memory` test fixtures we depend on. Tested against
  mcp 1.27.x.

## [0.8.5] - 2026-05-28

### Added (agentic world-class uplift — #155)
- **LiteLLM Router with cross-cloud fallback** — opt-in fallback chain
  (`anthropic→bedrock→vertex`) via `FLUID_LLM_FALLBACK_CHAIN`, auto-injected
  Anthropic `cache_control` points, and 3-token cost tracking (1.25× creation /
  0.10× read multipliers) persisted to `cost.json`.
- **JudgeAgent + CI judge-gate** — 6-axis CoT rubric (correctness, completeness,
  security, governance, performance, documentation) with a toggleable Self-Refine
  self-critique pass, a 10-contract eval set, and a snapshot regression gate.
- **Wave 2 enrichment** — deterministic `dbt_test_generator`, `freshness_emitter`,
  and `physical_layout` (Snowflake/BQ/Athena/Redshift) tools, surfaced via
  `fluid forge --apply-enrichment` with diff preview that never overwrites a
  user-set field.
- **Pause/resume + `fluid agents` namespace** — LangGraph-shape
  `BaseCheckpointSaver` (JSON-only file backend, no pickle); Ctrl-C writes a
  `.paused` marker that the next `fluid forge` auto-detects, plus
  `fluid agents list/show/prune` with safe-by-default archiving.
- **15-class PII classifier** wired into both catalog and JDBC intake paths
  (pattern borrowed from Presidio / piicatcher / GCP DLP / AWS Glue).
- **Catalog-driven model registry + 3-tier memory** — static model tables
  retired in favour of `cli/llm_models.json` (weekly-refreshed); personal/team/project
  memory with a documented precedence ladder (`fluid forge --show-memory`);
  `fluid doctor --env` enumerates the `FLUID_*` kill switches.

### Added (CC alignment — describe surface — #156)
- **`FluidSchemaManager.latest_schema_path()`** — returns the absolute path to the
  newest bundled schema JSON without hardcoding a filename.
- **`fluid_build.describe.self_describe()`** — flat, JSON-serializable snapshot of
  the installed environment (version, schema, providers, build engines, templates,
  capability flags), importable in-process by the CC backend. Capability flags are
  derived from importable backing modules (à la `pulumi about`), never hardcoded.
- **`fluid describe --self [--json]`** — human-readable summary by default, JSON
  with `--json`.

### Fixed
- JDBC PK/FK/CHECK extraction routed through `postgres_query()` / `mysql_query()`
  pass-throughs (DuckDB's `information_schema` union drops FK rows).
- Snowflake DDL honours `osi.datasets[].fields[].data_type` with parameterised
  `NUMBER(p,s)`; DV2 emits one expose per hub/link/sat.
- Catalog adapter paths corrected (DMM `/api/dataproducts`; DataHub
  `graph.get_urns_by_filter`) with description preservation at the
  `_translate_catalog_table` chokepoint.

## [0.8.4] - 2026-05-26

### Added (DMM CLI surface + lineage UX)
- **`fluid dmm wipe`** — multi-pass FK-aware mass delete of every DataProduct
  in the tenant. Supersedes the ad-hoc helper scripts everyone was writing.
  `--yes` to skip confirmation; `--max-passes <n>` to bound retry attempts.
- **`fluid dmm list-contracts` / `get-contract` / `delete-contract`** —
  parity with the DataProduct surface for the long-overlooked DataContracts
  side of the DMM API.
- **`fluid dmm delete` enriched error** — on DMM's `422 Cannot delete
  because data product is in use` lock, the CLI now enumerates the
  consumer products holding the FK and prints them in the error so the
  operator knows what to delete first.
- **`fluid dmm publish` access-agreement visibility** — output now shows
  the count + approval status of Access agreements created from
  `consumes[]`. When any are left `pending` the CLI prints a yellow hint
  pointing at `--auto-approve-access` / `DMM_AUTO_APPROVE_ACCESS=true`.
  This closes the most common "I published but the DMM lineage graph is
  empty" footgun: DMM only renders lineage from APPROVED agreements.
- **`fluid dmm publish --auto-approve-access` / `--no-auto-approve-access`** —
  explicit flags overriding the env var, with help text that explains
  the lineage-rendering implications.

### Fixed (DMM + emitter bugs)
- **`OdpsStandardProvider.render()` no longer drops duplicate-exposeId
  consumes.** Previously, two `consumes[]` entries sharing an `exposeId`
  (e.g. both upstream products expose `data_analytics_platform`) collapsed
  to ONE InputPort via name-keyed dedup; the second was silently dropped.
  Now dedup is keyed by `(name, contract_id)` and collisions are
  disambiguated by tail-of-productId prefix (`b2__data_analytics_platform`),
  so every consume produces its own InputPort. Pinned by
  `tests/test_odps_cli_spec.py::TestInputPortDuplicateExposeIdHandling`.
- **`_publish_access_agreements` pre-flight upstream check.** Previously,
  if a `consumes[].productId` pointed at a product that didn't exist in
  DMM, the access PUT returned a generic `404` mid-publish. Now the
  publisher lists existing products first and surfaces a structured
  `missing_upstream_product` warning per skipped agreement, naming the
  upstream that needs publishing first.
- **`fluid dmm list --format json`** — Rich console line-wrapping injected
  literal newlines into JSON string values, breaking `json.loads()` for
  pipe consumers. Now bypasses Rich for `--format json` and writes
  directly to stdout.
- **`generator: "fluid-forge-opds-provider"` payload literal** in the LF
  v4.1 wrapper renamed to `fluid-forge-odps-provider` for naming
  consistency with the canonical spec acronym. Wire-visible field;
  downstream consumers that keyed on the old string should accept both.

### Removed (dead code)
- `_generate_uuid_from_id` + `_CANONICAL_UUID_NAMESPACE` +
  `_LEGACY_UUID_NAMESPACE` + the `FLUID_LEGACY_UUID_NAMESPACE` env var
  escape hatch (added in Phase 8.2 as a thoughtful migration path) —
  **deleted**: zero callers in the codebase, no production path depends on
  it. If a future caller needs deterministic UUIDs from a product id, the
  helper is a 3-liner; re-introduce it with explicit wiring at that time.

### Changed (BEHAVIOR — read carefully)
- **`fluid generate standard` defaults reordered: Bitol ODPS v1.0.0 is now
  the center-stage `--format odps`.** Previously `--format odps` routed to
  the LF/ODPI v4.1 emitter (which itself silently fell back to a
  5-field degenerate placeholder because the underlying `cli.opds.run`
  symbol didn't exist). The fix:
  - `--format odps` → emits **Bitol Open Data Product Standard v1.0.0**
    (bare product YAML via `BitolOdpsProvider().render()`).
  - `--format odps-bitol` → explicit alias of `--format odps` (kept for
    callers that want disambiguation in CI logs).
  - `--format odps-v4.1` (NEW) → emits **LF/ODPI Open Data Product
    Specification v4.1** (bare JSON via `OdpsProvider().render()` with
    the `artifacts:` wrapper unwrapped).
  - `--format opds` → deprecated letter-swap alias of `--format odps-v4.1`
    (NOT `--format odps`) — emits a WARNING + the LF/ODPI v4.1 JSON.
    Reasoning: in this codebase `--format opds` has always *meant* the
    LF/ODPI export (the historical default of that flag), so back-compat
    callers keep getting the spec they actually consume.
  - `fluid export-opds` → also fixed; now emits a real LF/ODPI v4.1 JSON
    (was: same degenerate fallback). Logs a DEPRECATION warning pointing at
    `fluid generate standard --format odps-v4.1`.
  - All previously-emitted degenerate shapes (`{specVersion: "4.1", id,
    name, domain, owner}` and `{specVersion: "1.0", id, title, owner,
    domain, exposes[]}`) are GONE — every path now produces a real spec
    doc.
- **`fluid generate standard --list` output reordered Bitol-first**
  (`odps` → `odps-bitol` → `odcs` → `odps-v4.1` → `opds`). The README
  "Which ODPS?" section, AGENTS.md pipeline row, and the rich help
  formatter table are all reordered to lead with Bitol.

### Changed
- **ODPS / OPDS naming alignment with upstream specifications.** Resolved a
  long-standing letter-swap that treated the canonical acronym **ODPS**
  (Open Data Product Specification — Linux Foundation / ODPI v4.1) and the
  letter-swap **OPDS** as if they were separate standards in user-facing
  help text and emit-set listings. Two distinct standards do share the
  ODPS acronym (Bitol's *Standard* v1.0.0 and the LF *Specification* v4.1)
  and both are now disambiguated by full name everywhere the names appear.
  - `fluid odps` is the canonical subcommand; `fluid opds` remains as a
    documented deprecated alias.
  - `--spec odps-4.1` is the canonical id for the LF/ODPI spec;
    `--spec odpi-4.1` (which swapped the *spec* name for the *org* name) is
    accepted with a WARNING that points at the correct id.
  - `--format opds`, `--emit opds`, and the `OPDS_*` env vars
    (`OPDS_VERSION`, `OPDS_INCLUDE_BUILD_INFO`, `OPDS_INCLUDE_EXECUTION_DETAILS`,
    `OPDS_TARGET_PLATFORM`, `OPDS_VALIDATE_OUTPUT`) are accepted with a
    one-time WARNING that names the canonical ODPS form.
  - `pyproject.toml`: added `[odps]` extra as the canonical name; `[opds]`
    is kept as a back-compat empty alias that depends on `[odps]`. The
    `[all]` umbrella now references `[odps]`.
  - Internals: `SPEC_ODPS_4_1` / `ODPS_4_1_*_URL` are the canonical names
    in `fluid_build.cli.opds`; `SPEC_ODPI_4_1` / `ODPI_4_1_*_URL` remain
    as module-level back-compat aliases.
  - The `OdpsProvider.name` property and the deterministic UUID namespace
    string (`"fluid-forge-opds"`) are intentionally **unchanged** to
    preserve wire-format keys and deterministic IDs across releases.
  - Test coverage: every existing OPDS test (`tests/test_opds_*`,
    `tests/test_contract_compatibility_matrix.py`,
    `tests/forge/test_artifact_fanout.py`,
    `tests/test_pipeline_templates_branches.py`,
    `tests/test_forge_seed_import_matrix.py`,
    `tests/test_forge_copilot_seed.py`,
    `tests/cli/test_subcommand_dispatch_smoke.py`) re-runs green after the
    rename + back-compat aliases.
## [0.8.3] — 2026-05-25

First stable release on the `0.8.x` line after `v0.8.0`. Folds five
post-`v0.8.0` PRs — ODCS / Bitol ODPS bidirectional provider with
SSRF-hardened HTTP (#136), CLI dispatch + smoke hardening (#137),
unified catalog registry with world-class DataHub + Data Mesh Manager
lineage (#138), tier-0 import-hygiene SSRF gate with `import-linter`
contracts (#139), and the OpenTofu autogenerator that recasts
`fluid apply` for cloud providers as a contract compiler (#140) — and
ships the previously-pre-released `v0.8.3rc1` plugin extension points
+ v0.7.3 acquisition-pattern engine as stable. `pip install
data-product-forge` resolves to this line by default. Stacks on the
beta-tier v0.7.3 acquisition runners and the EngineRuntime registry
released in [0.8.2] below — no schema break vs `v0.8.0`.

### Added

- **OpenTofu autogenerator — `fluid apply` becomes a contract compiler
  for cloud providers** (#140). New `fluid_build/iac/` module: modular
  `IacProviderPlugin` per cloud (dbt-adapter pattern), built-in plugins
  for **AWS / GCP / Snowflake**. The cloud providers compile the
  contract to a deterministic OpenTofu `main.tf.json` and delegate
  apply / state / drift / idempotency to the `tofu` binary; `local`
  keeps its native apply. New CLI surface: `fluid generate iac
  <contract>` (review-only emit) and `fluid apply` auto-routes cloud
  providers through `iac.cutover.resolve_engine`. The plan-binding
  integrity gate from the native engine is replicated at
  `_apply_opentofu_engine.py::_verify_plan_binding_for_opentofu`. The
  brownfield `tofu import` path is wired for all three plugins via
  `discover_imports`. Architecture doc at `AUTOGEN_SPIKE.md`;
  three-tier coverage matrix at `HONESTLY_TESTED.md`.

- **Tier-0 SSRF gate + import-linter architecture contracts** (#139).
  The canonical post-DNS-resolution SSRF check
  (`_hostname_is_private` — RFC1918 + link-local 169.254.0.0/16 +
  loopback + reserved + IPv4-mapped IPv6 unwrap, fails closed on DNS
  errors) moved to the new `fluid_build/_net.py` tier-0 leaf so
  `observability/reporter.py` can use it without importing
  `build_runners` (closes the cycle that previously broke
  `cli/__init__.py` import). Two declarative `[tool.importlinter]`
  contracts in `pyproject.toml` gate the architecture in CI:
  (1) `observability ↛ build_runners`, (2) `_net` is tier-0 (no
  `fluid_build.*` upstreams). Wired into the pre-commit hook and a
  new `import-hygiene` CI job. Four subprocess-isolated regression
  tests in `tests/observability/test_import_hygiene.py`.

- **Unified catalog registry + world-class DataHub + DMM lineage**
  (#138). One registry (`fluid_build/build_runners/catalog_registrars/
  __init__.py::build_registrar`) instantiates every backend from a
  uniform `CatalogPublicationPayload`. DataHub emits canonical MCPs
  with full schema + ownership + tags + descriptions; Data Mesh
  Manager emits proper `SourceSystem` lineage links rather than the
  prior flat dataset list. The retired Glue + Snowflake Horizon
  publish registrars folded their metadata-enrichment into the IaC
  emit (PR #140) — single source of truth, full drift detection,
  zero out-of-band registrar writes.

- **ODCS + Bitol ODPS bidirectional provider + `fluid forge --seed-from`
  pre-processor** (#136). `BitolOdpsProvider` (registered as
  `odps_bitol`, alias `odps-standard`) emits the canonical Bitol
  fragments layout (1 ODPS doc + N sibling `<contractId>.odcs.yaml`
  files) and the linking invariant `port.contractId == odcs.id` is
  asserted in tests. `ContractResolver` resolves port `contractId`
  references through local probes + opt-in http(s) fetch with the
  full SSRF guard (see Security). `fluid opds import` accepts three
  entry shapes — single ODPS, directory bundle, lone ODCS — and
  `fluid opds export --spec bitol-1.0.0|odpi-4.1` dispatches between
  the two output specs. `fluid forge --seed-from <path>` accepts an
  ODCS contract, Bitol ODPS product, or a directory bundle as
  structural seed for the copilot. The ODCS provider was modularised
  under `providers/odcs/` with paired `to_fluid()` / `to_odcs()`
  mappers and per-level `odcs_passthrough` buckets for lossless
  round-trip; `roundtrip_check()` returns a structured diff used by
  tests and the forge ground-truth guard.

- **Plugin extension points** — three entry-point groups discovered
  via `importlib.metadata.entry_points()` (graduated from
  `v0.8.3rc1`):
  - `fluid_build.commands` — register `fluid <name>` subcommands at
    CLI bootstrap (`cli/bootstrap.py`).
  - `fluid_build.extension_validators` — validate `contract.extensions`
    sub-keys during `fluid validate`. Errors fold into the
    `ValidationResult` namespaced under `extensions.<ep-name>`.
  - `fluid_build.apply_hooks` — apply-time invariants during
    `fluid apply`. Plugins receive `copy.deepcopy(contract)` and
    cannot mutate the live reference.
  All three trap exceptions, pre-redact them via `redact_secret_text`,
  and report them as errors — they cannot crash the CLI. Companion
  packages `data-product-forge-sdk==0.9.0` (Beta) and
  `data-product-forge-custom-scaffold==0.1.0` (Beta) plug in via
  these hooks.

- **`--force-pattern-drift` flag on `fluid apply`** — override gate
  for the scaffold-pattern drift detector when an intentional
  re-scaffold needs to bypass it (graduated from `v0.8.3rc1`).

- **`contract.extensions` schema field** in `fluid-schema-0.7.3.json`
  — optional top-level object with `additionalProperties: true`,
  validated per-sub-key by extension-validator plugins.

- **`fluid stats` aggregator** with `--by provider/type/engine`,
  `--since <spec>`, and `--json` over `.fluid/agents/*/cost.json`
  (cross-run cost telemetry, useful for budget tracking).

### Security

- **`fluid_build.util.safe_http` — shared SSRF guard for every HTTP
  fetch** (#136). One factory (`safe_httpx_client`) routes all
  outbound http(s) calls through: scheme allowlist + private /
  loopback / link-local / CGNAT / 6to4 / NAT64 / ORCHIDv2 / IPv6-SR /
  RFC-TEST-NET filter + IPv4-mapped IPv6 unwrap (closes a Python
  3.10/3.11 bypass — stdlib `is_private` only recurses into
  IPv4-mapped in 3.12+) + reject-all on mixed-public+private DNS +
  connection-layer DNS pin (via httpx's `sni_hostname` extension) +
  `follow_redirects=False` default + streaming body cap (10 MiB).
  Migrated seven fetch surfaces in one pass: `ContractResolver`,
  `KafkaConnectRestClient` + its schema-registry client, the Airbyte
  REST client, all five catalog registrars (Glue / DataHub /
  OpenMetadata / Unity / Snowflake Horizon / DataMesh Manager), the
  Databricks auth-provider's API check, and the schema-manager remote
  fetcher. Borrow-before-build receipts (CIDR list from
  `requests-hardened` BSD-3; httpx DNS-pin from the maintainer-blessed
  `sni_hostname` extension docs) are in
  `fluid_build/util/safe_http.py`'s module header.

- **Plan-binding gate replicated in the OpenTofu engine** (#140).
  `_apply_opentofu_engine.py::_verify_plan_binding_for_opentofu`
  mirrors the native engine's stage-7 `bundleDigest` + `planDigest`
  verification — a tampered `plan.json` is rejected before any
  `tofu apply` for every cut-over cloud (AWS / GCP / Snowflake).
  `--no-verify-plan-binding` is the emergency escape hatch and logs
  at WARNING.

- **Operational hardening on the OpenTofu engine** (#140): per-tofu
  subprocess timeout (default 1800s, override via
  `FLUID_TOFU_TIMEOUT_SECONDS`); `require_tofu_version()` floor at
  1.6.0 (catches the silent `terraform`-on-PATH-as-`tofu` mixup);
  `--allow-data-loss` override now emits a WARNING log + a structured
  `opentofu_destructive_gate_override` event for CI log-scrapers.

- **Apply hooks receive `copy.deepcopy(contract)`** rather than the
  live reference (graduated from `v0.8.3rc1`). A buggy or malicious
  hook cannot mutate the contract the rest of apply or other hooks
  consume.

- **Plugin exception text and plugin-supplied error strings are
  pre-scrubbed with `redact_secret_text`** before reaching logs or
  the errors list (graduated from `v0.8.3rc1`). The
  `SecretRedactingFilter` only scrubs args bound to `password=%s`-style
  template tokens; plugin exceptions are free-form text that can
  carry credential-shaped substrings anywhere. Applied uniformly
  across `cli/apply.py`, `cli/validate.py`, `cli/bootstrap.py`.

- **`bootstrap.py` imports `redact_secret_text` at module top**
  rather than nested inside an except branch — closes a
  defense-in-depth gap surfaced by security review.

### Changed

- **BREAKING (caller API) — `allow_remote` defaults to `False`
  across CLI + library** (#136). `fluid opds import` and
  `fluid forge --seed-from` no longer fetch http(s) `contractId`
  references unless `--allow-remote` / `--seed-allow-remote` is
  passed explicitly. Python callers of
  `BitolOdpsProvider().import_contract(...)`,
  `BitolOdpsProvider().import_directory(...)`, `ContractResolver(...)`,
  and `forge_copilot_seed.load_seed(...)` must now pass
  `allow_remote=True` for the previous behaviour. `--no-remote` and
  `--seed-no-remote` remain as hidden no-op aliases.

- **CLI dispatch wired on `stats` and `generate-pipeline` subcommands**
  (#137). Both subparsers now carry `set_defaults(func=run)` so
  `fluid stats` and `fluid generate-pipeline` dispatch to the
  implementation instead of falling through to the
  no-subcommand-selected help guide. Pinned by a smoke test that
  parses every registered subparser and asserts the `func` attribute
  is set.

- **Catalog registrar retirement — Glue + Snowflake Horizon** (#140).
  `fluid_build/build_runners/catalog_registrars/{glue,snowflake_horizon}.py`
  (~514 LOC of boto3/HTTP push) deleted; the catalog metadata they
  used to write at publish time (table descriptions, per-column
  comments, FLUID classification tags, contract YAML) is folded into
  the IaC emit (`iac/providers/aws.py::_emit_glue` for Glue +
  `iac/providers/snowflake.py::_build_horizon_table_comment` for
  Horizon). One source of truth, full drift detection by `tofu plan`,
  no out-of-band registrar writes that fight IaC state. The
  `acquisitionCatalog.register` schema enum drops `glue` and
  `snowflake_horizon` accordingly; `datahub`, `openmetadata`, and
  `datamesh_manager` registrars remain (the first two have community
  Terraform providers we may adopt later; DMM has no provider).

- **ODCS schema validation default-on with warn-on-fail** (#136).
  Vendored ODCS v3.1.0 JSON Schema runs on every export by default
  (`ODCS_VALIDATE=true`); failures warn rather than raise. Hard fail
  via `ODCS_VALIDATE_STRICT=true` or by calling `validate_contract()`.

- **ODCS type table is now exhaustive against the FLUID 0.7.3 column-type
  enum** (79 types) (#136). Drift guard in
  `tests/providers/test_odcs_type_mapping.py` fails CI if a new FLUID
  schema type is missing from `_FLUID_TYPE_TO_ODCS_LOGICAL`.

- **Bitol ODPS input ports — three-source merge** (#136).
  `ports.py::to_odps()` walks `consumes[]` (FLUID 0.7.2 canonical),
  `builds[]` (SDP source streams), and `expects[]` (FLUID 0.7.1
  legacy) — de-duped by name. Uses upstream's
  `util.contract.consumes_to_canonical_ports` +
  `builds_to_canonical_input_ports` for the normalisation.

### Test coverage

- **40+ new IaC test files at `tests/iac/`** (#140) — three-tier
  ladder: Stage 1 unit + `tofu validate` (creds-free, every PR),
  Stage 2 Docker emulators (LocalStack for AWS + goccy/bigquery-emulator
  + fsouza/fake-gcs-server + gcloud pubsub for GCP, every PR), and
  Stage 3 real-cloud OIDC keyless (nightly + manual; ~30 tests on
  real AWS, ~19 on real GCP, ~40 on real Snowflake). New
  `.github/workflows/iac-tests.yml` runs the ladder; the existing
  `ci.yml` Stage 2 path skips LocalStack cleanly when the optional
  `LOCALSTACK_AUTH_TOKEN` secret isn't configured.

- **16 unit tests for the brownfield `discover_imports` shape**
  across AWS + GCP + Snowflake plugins, with documented hashicorp/{aws,
  google,snowflake} import-id formats; plus live brownfield tests for
  AWS + GCP (pre-create resources out-of-band, run apply, verify
  adoption).

- **4 subprocess-isolated import-hygiene regression tests** at
  `tests/observability/test_import_hygiene.py` (#139) — including the
  original failing import path, sys.modules introspection after
  `observability` load, and assertions that `fluid_build._net` does
  not pull any other `fluid_build.*` module.

- **`tests/test_cli_plugin_hooks.py`** — 19 tests pinning every
  behaviour on the plugin trust surface (graduated from
  `v0.8.3rc1`).

### Upstream issues filed / resolved

- [snowflakedb/terraform-provider-snowflake#4775](https://github.com/snowflakedb/terraform-provider-snowflake/issues/4775)
  — filed 2026-05-25, **resolved upstream same day** by maintainer
  @sfc-gh-kwasilewski. The `snowflake_tag_masking_policy_association`
  resource was deprecated in v0.99.0 and removed in v1.0.0; the
  binding moved INTO `snowflake_tag.masking_policies` (set of
  fully-qualified names). Trello card created for implementing
  this in the Snowflake IaC plugin.
- [goccy/bigquery-emulator#484](https://github.com/goccy/bigquery-emulator/issues/484)
  — filed 2026-05-25. `tofu apply` via the hashicorp/google provider
  crashes on dataset read-back ("Plugin did not respond"). Pinned as
  an xfailed test until upstream resolves.
- Two LocalStack Pro quirks (Lambda V2 docker-in-docker socket
  reachability + LF DataLakeAdmin GrantPermissions auth) — drafts at
  `docs/upstream-issues/localstack-*.md`. The public LocalStack repo
  was archived 2026-03-23 and the Pro repo is private; the drafts
  point operators at LocalStack's support portal and Slack.

### Notes

- Companion plugin ecosystem packages on PyPI:
  - `data-product-forge-sdk` (0.9.0, Beta) — reference ABCs +
    conformance harnesses for the three entry-point groups.
  - `data-product-forge-custom-scaffold` (0.1.0, Beta) — reference
    `CustomScaffold` engine; Jinja+YAML or Python-plugin bundles.
- All six ingestion engines (`duckdb`, `airbyte`, `meltano`, `dlt`,
  `kafka-connect`, `debezium`) remain GA — no schema break vs
  `v0.8.0`. The v0.7.3 acquisition-pattern engine documented in
  [0.8.2] graduates to stable in this line.
- Cloud-provider apply paths now route through the OpenTofu engine
  by default. Operationally this means: install `tofu` (≥ 1.6.0)
  alongside `data-product-forge`; otherwise `fluid apply` against
  AWS / GCP / Snowflake fails fast with a clear error. The `local`
  provider is unaffected.

## [0.8.3rc1] — 2026-05-12

Pre-release candidate of the first stable line after `v0.8.0`. Stacks the
v0.7.3 acquisition-pattern engine work (graduated from the `v0.8.2b1`
TestPyPI beta) on top of the new plugin extension points and their
trust-model hardening. **Published to PyPI as a pre-release** (`pip
install --pre data-product-forge` or `pip install
data-product-forge==0.8.3rc1`). The follow-on `v0.8.3` stable tag will
be cut after a downstream validation window; no functional changes are
expected between rc1 and stable.

### Added

- **Three plugin extension points** discovered via Python entry-points
  (`importlib.metadata.entry_points()`):
  - `fluid_build.commands` — register additional `fluid <name>`
    subcommands at CLI bootstrap (`cli/bootstrap.py`).
  - `fluid_build.extension_validators` — validate sub-keys of
    `contract.extensions` during `fluid validate` (`cli/validate.py`).
    Errors fold into the `ValidationResult` namespaced under
    `extensions.<ep-name>`.
  - `fluid_build.apply_hooks` — apply-time invariant checks (e.g.
    scaffold bundle digest drift) during `fluid apply` (`cli/apply.py`).
  All three follow a uniform contract: plugin exceptions are trapped,
  pre-redacted, and reported as errors — they cannot crash the CLI.
  Companion packages `data-product-forge-sdk==0.9.0` (Beta) and
  `data-product-forge-custom-scaffold==0.1.0` (Beta) ship reference
  ABCs, conformance harnesses, and a custom-scaffold engine that plug
  in via these hooks.

- **`--force-pattern-drift` flag on `fluid apply`** — override
  apply-hook drift errors (e.g. when a scaffold bundle digest moved
  underneath a development scenario). Errors downgrade to WARNINGs;
  apply proceeds.

- **`contract.extensions` schema field** in `fluid-schema-0.7.3.json`
  — optional top-level object with `additionalProperties: true`,
  validated per-sub-key by extension-validator plugins.

### Changed (security)

- **Apply hooks receive `copy.deepcopy(contract)`** rather than the live
  reference. A buggy or malicious hook cannot mutate the contract the
  rest of apply or other hooks consume. Pinned by
  `tests/test_cli_plugin_hooks.py::test_apply_hook_receives_deep_copy_of_contract`.

- **Plugin exception text and plugin-supplied error strings are
  pre-scrubbed with `redact_secret_text`** before reaching logs or the
  errors list. The `SecretRedactingFilter` only scrubs args bound to
  `password=%s`-style template tokens; plugin exceptions are free-form
  text that can carry credential-shaped substrings anywhere. Applied
  uniformly across `cli/apply.py`, `cli/validate.py`,
  `cli/bootstrap.py`. Pinned by `TestPluginErrorRedaction` (5 tests).

- **`bootstrap.py` imports `redact_secret_text` at module top**
  (was: nested try/except inside the except branch, which could have
  fallen back to unredacted logging if the import statement failed).
  Closes a defense-in-depth gap surfaced by the security review.

### Added (docs)

- **SECURITY.md** gains a "Plugin Trust Model" section: tables the
  three entry-point groups, names the defenses (crash containment,
  contract deep-copy, pre-redaction, override gate), names the
  deliberate non-defenses (no sandboxing, no timeout, no resource
  limits), routes plugin-side vs CLI-side vulnerability reports.

- **AGENTS.md** gains a "Plugin extension points" section documenting
  the three entry-point groups with hook signatures, failure model,
  and trust statement — for the AI-coding-agent audience.

### Test coverage

- **`tests/test_cli_plugin_hooks.py`** — 19 new tests across four
  classes that pin every behavior on the plugin trust surface:
  extension validators (4), apply hooks (6), bootstrap commands (4),
  plugin error redaction (5). Uses a `FakeEntryPoint` helper so tests
  don't need real installed packages.

### Notes

- This release stacks on top of the v0.7.3 acquisition-pattern engine
  work documented in [0.8.2] below — all six ingestion engines remain
  GA (`duckdb`, `airbyte`, `meltano`, `dlt`, `kafka-connect`,
  `debezium`).
- Companion plugin ecosystem packages on PyPI:
  - `data-product-forge-sdk` (0.9.0, Beta) — import path `fluid_sdk`,
    zero runtime dependencies.
  - `data-product-forge-custom-scaffold` (0.1.0, Beta) — reference
    `CustomScaffold` engine; Jinja+YAML or Python-plugin bundles.

## [0.8.2] — 2026-05-10

Beta release of the v0.7.3 acquisition-pattern engine runners and the
EngineRuntime registry across all CI emitters. Schema is GA; the
package is staged through a beta tag (`v0.8.2b1` → TestPyPI) so
downstream pipelines can validate the EngineRuntime wiring before
graduating to a stable `v0.8.2` on PyPI.

### Added

- **Data-product type vocabulary** (`metadata.productType`). v0.7.3
  introduces a Data Mesh-aligned classification that runs alongside the
  existing medallion `metadata.layer` field. Both vocabularies are
  first-class and accepted side-by-side; existing contracts using only
  `Bronze`/`Silver`/`Gold` validate unchanged. Canonical mapping:

  | medallion (`metadata.layer`) | Data Mesh (`metadata.productType`) | expansion                          |
  |------------------------------|------------------------------------|------------------------------------|
  | `Bronze`                     | `SDP`                              | Source-Aligned Data Product        |
  | `Silver`                     | `ADP`                              | Aggregated Data Product            |
  | `Gold`                       | `CDP`                              | Consumption-Aligned Data Product   |
  | `Platinum`                   | (no analogue)                      | —                                  |

  When a contract sets both fields they MUST agree; the validator emits a
  clear error otherwise. The discover emitter (`fluid init --discover`)
  populates both fields automatically. Helpers in
  `fluid_build.forge_datamodel.product_type` (`infer_product_type`,
  `infer_layer`, `validate_layer_product_type_consistency`) let
  downstream tooling normalize between the two vocabularies.

- **FLUID schema v0.7.3 — Source-Aligned Data Products.** First-class
  support for source-aligned (Bronze / SDP) data products that ingest
  external systems into the mesh.
  - New `acquisition` build pattern with full `$defs/acquisitionPattern`
    sub-schema (`source`, `sink`, `delivery`, `schemaEvolution`, `quality`,
    `cost`, `catalog`, `concurrency`, `imageSignature`, `deployment`).
  - **All six ingestion engines GA**: `duckdb`, `airbyte`, `meltano`, `dlt`,
    `kafka-connect`, `debezium` — every engine has a working runner, full
    test matrix (sources × sinks × modes × failure modes × capabilities ×
    deployment modes), and conforms to the public Runner Protocol via the
    conformance suite.
  - Three deployment modes (`embedded`, `bring-your-own`, `managed`) with
    three managed back-ends (`docker`, `kubernetes`, `terraform`).
  - Capability-based negotiation between contract and runner
    (`capabilities` array on builds; `RunnerCapability` enum on the public
    API).
  - Schema evolution semantics (`schemaPolicy`) with concrete per-policy +
    per-change decision matrix.
  - Delivery guarantees (`at_most_once | at_least_once | exactly_once`) +
    DLQ semantics (`dlq.maxRecordsBeforeAbort`, `dlq.alertOn`).
  - Cosign image-signature verification + SLSA provenance fields.
  - Top-level `retention` block (`runState`, `runLogs`, `lineage`, `dlq`).
  - Sovereignty extension: `dataResidency.region`,
    `dataResidency.prohibitTransferTo`.
  - `metadata.classification` enum + `metadata.experimental` array
    (feature gate).
  - All schema additions are additive and backward-compatible with v0.7.2;
    existing 0.7.x contracts validate unchanged.

- **`fluid_build.api` v1.0 — public extension contract.** Stable surface
  third-party runners and providers target. SemVer-governed via
  `__api_version__ = "1.0"` with a 2-minor-version deprecation window.
  Conformance suite available at
  `fluid_build.api.conformance.RunnerConformance`.

- **Common acquisition runtime** under `fluid_build/build_runners/` —
  `_state` (FileStateStore + single-flight lock), `_dlq`, `_retry`,
  `_idempotency`, `_schema_evolution`, `_fingerprint`, `_anomaly`
  (EWMA/IQR/exact), `_signature` (Cosign), `_cost` (budget gate),
  `_catalog` (registrar dispatch), `_lineage` (Null/Buffered/HTTP OL
  emitters), `_retention` (sweeper), and four pre-land hooks
  (`dlp_scan`, `tokenize_pii`, `quality_gate`, `emit_lineage_input`).

- **Six ingestion runners** under `fluid_build/build_runners/<engine>/`:
  - `duckdb` — zero-infra file/JDBC ingestion (CSV/Parquet/JSON/Postgres/MySQL/SQLite/HTTP).
  - `dlt` — Python-native sources, including custom `@dlt.source` modules + verified sources (filesystem, sql_database).
  - `meltano` — Singer protocol over subprocess; one runner unlocks 600+ Singer taps.
  - `airbyte` — REST mode against Airbyte OSS / Cloud + Cosign image signature verification (5-path verification matrix: signed/unsigned/wrong-key/SLSA-required-missing/SLSA-required-present).
  - `kafka-connect` — full connector lifecycle (create/get/update/delete/status) against a Kafka Connect cluster; JDBC + S3 + Salesforce + MongoDB sources; JDBC + S3 + Snowflake + Iceberg + BigQuery sinks.
  - `debezium` — CDC for Postgres / MySQL / MongoDB / SQL Server / Oracle; both Kafka Connect mode (preferred) and Debezium Server (embedded) mode; all 5 snapshot modes (`initial`, `schema_only`, `never`, `when_needed`, `always`).

- **Infrastructure layer** (`fluid_build/infra/`): three managed-mode artifact
  generators (Docker Compose, Helm with Flux-style HelmRelease CRs, OpenTofu
  modules). Pinned upstream Helm chart references; ExternalSecret + NetworkPolicy
  emission; sovereignty propagation into values overlays; deterministic bundle
  digests. **Hyperscaler-agnostic by construction** — no `boto3`, `google.cloud`,
  or `azure` imports anywhere in the layer (test-asserted).

- **Five catalog registrars** (`fluid_build/build_runners/catalog_registrars/`):
  DataHub (GMS REST), OpenMetadata, Databricks Unity Catalog, AWS Glue Catalog
  (HTTP — no boto3 dependency), Snowflake Horizon. Classifications propagate as
  glossary terms / PII tags / table parameters / column tags depending on target.

- **Authoring workflows**:
  - `fluid init --discover <uri>` — introspect Postgres / MySQL / filesystem
    sources and emit deterministic Bronze contracts.
  - `fluid import meltano <project>` / `fluid import airbyte <workspace>` /
    `fluid import dlt <pipeline>` / `fluid import singer <tap-cfg>` — convert
    foreign configs to FLUID contracts. Secrets are auto-redacted to `${ENV_VAR}`
    placeholders.

- **Day-2 operations** (`fluid_build/cli/ops/`). Run-record introspection
  is grouped under a `fluid runs` umbrella so the new commands don't
  collide with the existing top-level `fluid status / doctor / auth`
  which serve different (workspace-overview / system-diagnostic / cloud-
  auth) purposes.
  - `fluid runs status <product-id>` — last-N runs, freshness,
    error-rate-24h, last state, per-stream record counts.
  - `fluid runs logs <product-id> --component build|infra|server|worker|dlq`
    — component-scoped log fetch with `--grep` and JSON parsing.
  - `fluid runs diff <product-id> --build <id> --run-a <a> --run-b <b>`
    — schema and row-count delta between two runs.
  - `fluid retention sweep` — periodic cleanup with structured summary.
  - `fluid doctor --scope authoring|pipeline|ingestion|infra|catalog|all`
    — extends the existing `fluid doctor` with the acquisition-stack
    health report covering schema version, dispatcher integrity, runner
    module imports, optional extras, infra binaries, registrar imports.
  - `fluid secrets login|verify|rotate <secretRef>` — pipeline credential
    ops with keychain backend and probe-before-rotate semantics. Lives
    under its own `secrets` umbrella so it doesn't collide with the
    legacy cloud-provider `fluid auth`.

- **Typed CLI error catalog** (`fluid_build/cli/_errors.py`) — every
  user-facing error renders the five-field shape (`what / where / why /
  fix / doc`). 14 typed classes covering schema validation, capability
  mismatch, secret resolution, sovereignty, connectivity, partial failure,
  DLQ overflow, schema drift, budget, lock contention, replay staleness,
  missing extras, infra drift, residency, supply chain.

- **`fluid validate --probe`** flag for live external probes (off by
  default; pure schema validation otherwise).

- **End-to-end example** `examples/source-aligned-postgres-duckdb/` with
  `docker-compose.yml`, `seed.sql`, `Makefile`, `verify.py`. `make all`
  brings up Postgres, runs `fluid validate → apply`, and asserts row count
  + schema in the output Parquet. Verified end-to-end on Postgres 16.

- **Test infrastructure**:
  - Testcontainers fixtures for Postgres / MySQL / MongoDB.
  - respx mock servers for Airbyte / Kafka Connect / DataHub / OpenMetadata /
    Unity / Glue / Snowflake Horizon / Marquez.
  - Cosign mock with 5 signature scenarios (signed/unsigned/wrong-key/SLSA missing/SLSA present).
  - Synthetic Singer tap (`tap-fluid-fake`) for protocol-level tests.
  - Hypothesis property tests for state machine, locks, and schema evolution.

- **UX acceptance bar**:
  - Error-catalog completeness (15 typed error classes, all with `for_*`
    factories and five-field shape).
  - Engine uniformity: every runner declares the same Protocol surface,
    returns the same exit-code shape, and emits the same run-record JSON.
  - Performance budgets: validate < 3s, fingerprint < 500ms for 100 calls,
    schema evolution < 500ms for 100 resolves, doctor (all scopes) < 3s.
  - JSON output stability: error catalog snapshot, contract emitter snapshot.
  - Exit-code contract: 0 success, 1 user error, 2 partial, 3 transient, 4 internal.

### Changed

- `FluidContractValidator.__init__` and `ConformanceAgent.__init__` now
  default `fluid_version` to `FluidSchemaManager.latest_bundled_version()`
  (was hardcoded `"0.7.2"`). `FluidContractValidator.validate` honors the
  contract's own `fluidVersion` when present, so contracts emitted at one
  version validate against that version regardless of the validator's
  default.
- Dropped Python 3.9 support, raised the package baseline to Python 3.10,
  and expanded CI/package classifiers through Python 3.14.

### Fixed

## [0.8.0] — 2026-04-25

### Changed

- **`fluid compile` renamed to `fluid bundle --format yaml`.** The
  legacy `fluid compile` top-level command was removed when the
  11-stage pipeline landed (`cli/compile.py` → `cli/bundle.py`). The
  canonical bundle command is `fluid bundle --format tgz` (signed,
  content-addressable archive used as the root of trust by every
  downstream pipeline stage); `fluid bundle --format yaml` is the
  drop-in replacement for the old single-document compile behaviour.
  Any CI scripts, docs, or muscle memory targeting `fluid compile`
  needs a one-line update. See `examples/0.7.1/bitcoin-multifile/`
  for the updated usage pattern.
- **`policy-check` / `policy-compile` / `policy-apply` → `fluid policy
  {check,compile,apply}` subcommand group.** The three top-level
  hyphenated forms are easily confused (all start with `policy-`,
  all do different things). They're now grouped under a single
  `fluid policy` umbrella mirroring `fluid auth` / `fluid generate`.
  Legacy hyphenated forms remain registered for one release; plan
  to migrate before the next minor.
- **`fluid rollback --list`** — new read-only discovery flag for
  enumerating available snapshots before committing to a restore.
  Mirrors `terraform state list` / `git reflog`.

## [0.7.11] — 2026-04-16

Tooling release. No user-facing CLI behavior changes; ships a refactored
release pipeline and a few packaging fixes.

### Changed
- **Dynamic versioning via `setuptools-scm`.** The wheel's version is now derived from the git tag at build time (`v0.7.11a1` → wheel `0.7.11a1`). Removes the static `version = "..."` literal from `pyproject.toml`, the `__version__ = "..."` literal from `fluid_build/__init__.py`, and the `base_version` field from `fluid_build/build-manifest.yaml`. `fluid_build.__version__` now reads from `importlib.metadata.version("data-product-forge")`. Closes the version-drift bug that produced a `0.7.10` wheel for a `v0.7.10a1` tag.
- **Release pipeline restructured to sequential TestPyPI → PyPI promotion.** Every release tag is now published to TestPyPI first, install-verified in a fresh venv, and only promoted to real PyPI if the TestPyPI smoke test passes. Pre-release tags (`a*`/`b*`/`rc*`/`dev*`) stop after TestPyPI verify; stable tags continue to PyPI + a final PyPI install verify. Closes the gap where stable releases bypassed TestPyPI entirely.
- **Concurrency control** on the release workflow (`group: release, cancel-in-progress: false`) so two simultaneous tag pushes can't race PyPI uploads.
- **Pytest markers replaced inline `--deselect` flags.** Tests that fail in specific environments (GHA runner detection, Python 3.12 import-cycle issues) now carry explicit `@pytest.mark.skipif` / `@pytest.mark.xfail` markers in the test files themselves with a documented `reason=`. The release workflow's pytest invocation is back to a clean one-liner.
- **README image and LICENSE link rewritten to absolute GitHub URLs** so the PyPI / TestPyPI project pages render the Fluid Forge logo and the License badge links to the actual file (relative paths 404 on PyPI).

### Fixed
- **Version-mismatch verify-install failure** on pre-release tags (the root cause of the v0.7.10a1 incident) — fixed by the setuptools-scm migration above.

Patch release covering post-0.7.9 work that accumulated in the `Unreleased`
section. No breaking changes — everything here is additive, internal tidy-up,
or hardening.

**Heads-up for users: the PyPI distribution name is changing.** Starting with
0.7.10, the package publishes as **`data-product-forge`** instead of `fluid-
forge`. Update your install command:

```bash
# old
pip install fluid-forge

# new
pip install data-product-forge
```

The import path (`import fluid_build`), the CLI entry point (`fluid`), and all
internal provider identifiers (Snowflake query tags, Airflow DAG owners,
`managed-by` labels) stay as `fluid-forge` — those are runtime/audit
identifiers, not user-facing names. The CLI's Docker image on GHCR also aligns
with the GitHub repo name: `ghcr.io/agenticstiger/forge-cli`.

Also the first release published from the `Agenticstiger/forge-cli` repository
via the Trusted-Publishing release pipeline.

### Added
- **Master-schema validation on DMM publish (opt-in enforcement; backward-compatible across every bundled FLUID version).** `fluid datamesh-manager publish` (alias `fluid dmm publish`) now validates the loaded FLUID contract before constructing any provider payload, enforcing the CLI's role as master coordinator. **Validation honors the contract's own declared `fluidVersion`**: a 0.5.7 contract is validated against `fluid-schema-0.5.7.json`, a 0.7.1 contract against `fluid-schema-0.7.1.json`, a 0.7.2 contract against `fluid-schema-0.7.2.json`, and so on. Upgrading the CLI never invalidates a contract that was valid against its own version — the CLI coordinates publishes across the whole FLUID version range, not just the latest. **The default mode is `warn` — existing workflows are NOT affected: a contract that previously published will still publish, schema errors are logged, and the publish proceeds.** Users who want hard enforcement opt in via `--validation-mode strict`, which aborts the publish with a detailed error summary on any schema violation. Tests cover strict/warn modes, the valid-contract-in-strict-mode happy path, an end-to-end integration test that walks the full pipeline without mocking the provider, and a parametrized backward-compat suite that exercises strict-mode publish on every bundled FLUID version (0.5.7 / 0.7.1 / 0.7.2).
- **Migrated all 13 bundled templates to FLUID 0.7.2.** Mechanical migration of `builds[*].pattern` (`single-stage` → `embedded-logic`), `consumes[*]` legacy file-reference entries (removed — templates consume files via build SQL, not as upstream data products), `metadata` strict fields (dropped `sla`/`orchestration`/`environment`/`pattern`), DQ rule types (`validity` → `valid_values`, `consistency` → `accuracy`), DQ severities (`warning` → `warn`), operators (`=` → `==`), and trigger types (`scheduled` → `schedule`). All 13 templates now pass strict validation against `fluid-schema-0.7.2.json`.
- **`init --blank` scaffold now emits a valid 0.7.2 contract** (with a placeholder expose so the generated document passes the `exposes.minItems: 1` constraint out of the box).
- **ODPS input-port lineage** — `OdpsStandardProvider` now maps FLUID `consumes[]` entries to ODPS-Bitol `inputPorts`, so upstream data-product lineage is preserved when publishing to Entropy Data (datamesh-manager `provider_hint="odps"`).
- **Cross-version compatibility matrix** — new `tests/test_contract_compatibility_matrix.py` parameterizes a small set of golden fixture contracts (minimal 0.5.7/0.7.1/0.7.2, lineage 0.7.1/0.7.2) across schema validation and every export path (ODCS, official OPDS, ODPS-Bitol, DMM DPS dry-run, DMM ODPS dry-run). Guards against silent regressions across FLUID schema bumps.
- **`consumes_to_canonical_ports` / `get_owner` / `slugify_identifier` helpers** in `fluid_build/util/contract.py` — shared normalization used by both ODPS providers and the CLI init path.
- **FLUID 0.7.2 bundled schema** added to the fallback set in `FluidSchemaManager._discover_bundled_versions`, plus a new `latest_bundled_version()` classmethod so the "latest version" is resolved once and centrally.

### Changed
- **`fluid_build.cli.validate` exposes a public `run_on_contract_dict(...)` helper** (plus a public `output_text_results` alias) that other CLI commands can use to validate an already-loaded contract dict with identical UX to `fluid validate`. The DMM publish path is the first caller; the private `_output_text_results` name is no longer reached into.
- **`get_owner` precedence flipped** to the 0.7.2-canonical order: `metadata.owner` first, with top-level `owner` kept as the legacy fallback. Matches the master schema (which forbids top-level `owner` under `additionalProperties: false`).
- **`consumes_to_canonical_ports` now forwards the complete 0.7.2 `consumeRef` field set** (`versionConstraint`, `qosExpectations`, `requiredPolicies`, `tags`, `labels`) in addition to the legacy extension fields. Providers can forward any subset without re-parsing the raw contract. Non-mapping/non-list values on typed fields degrade to `None` for predictable downstream checks.
- **`slugify_identifier` leading-digit guard now applies to the fallback too**, so numeric or punctuation-only fallbacks still produce valid FLUID identifiers. Both input and fallback are slug-cleaned before the guard runs; if both collapse to empty, a single-character sentinel (`"x"`) is returned rather than an invalid identifier.
- **Scan-mode helper extraction and cleanup.** `generate_contracts_from_scan`, `apply_governance_policies`, and `show_migration_summary` now live in `fluid_build.cli.init_scan`, with `fluid_build.cli.init` re-exporting the public helpers for import stability. The temporary scan-mode `produces[]` fallback has been removed now that repo- and issue-level checks no longer show a live dependency.
- **Removed dead helpers** from `fluid_build/util/contract.py`: `get_expose_schema`, `get_expose_format`, `normalize_expose`, `normalize_contract`. These were silently wrong for 0.7.2 (reading fields that 0.7.2 `additionalProperties: false` rejects or producing outputs that don't satisfy the 0.7.2 `binding` shape), had zero production callers, and only existed to keep their own tests green. Associated test classes removed.
- **End-to-end publish integration test added** (`tests/cli/test_datamesh_manager.py::TestCmdPublishEndToEnd`) that walks through the full pipeline — on-disk 0.7.2 fixture → loader → master-schema validation → `DataMeshManagerProvider.apply(dry_run=True)` → payload assertions — without mocking the provider. Catches any wiring regression along the publish chain that unit tests with mocks would paper over.
- **ODPS input ports no longer fabricate `contractId` or default `required: True`.** Fields that were not explicitly declared on a `consumes[]` entry are omitted from the output rather than filled with synthetic defaults. Downstream consumers see only fields that point to real upstream identifiers, and pipelines are no longer implicitly marked as requiring every upstream.
- **Unified input-port extraction.** The official `OdpsProvider` and `OdpsStandardProvider` now share a single `consumes_to_canonical_ports` traversal (in `util/contract.py`) instead of reimplementing it side-by-side.
- **`OdpsStandardProvider` 0.7.x field tolerance** — reads both `expose.id`/`exposeId`, `expose.contract.schema`/`expose.schema`, `binding.platform`/`expose.provider`, `binding.location`/`expose.location`, and top-level `owner`/`description`/`domain`/`fluidVersion` as fallbacks for their `metadata.*` counterparts.
- **ODPS output ports now emit an `id` field** (previously only `name`). `contractId` on output ports is only populated when explicitly set on the expose — the DMM layer still overlays a deterministic `contractId` when publishing companion contracts.
- **`OdpsStandardProvider` raises `ProviderError`** (instead of `KeyError`) when an expose is missing both `id` and `exposeId`.
- **Malformed `consumes[]` entries are now skipped with a logged warning** rather than silently dropped.
- **CLI `init` blank mode** now uses `slugify_identifier()` to produce valid contract IDs from arbitrary project names (handles punctuation, leading digits, and non-ASCII).
- **Latest bundled FLUID version is resolved lazily** in `fluid_build/cli/init.py` and `fluid_build/cli/provider_init.py` via `_latest_fluid_version()` helpers instead of module-level constants computed at import time.
- **Templates bumped to FLUID 0.7.2.**

## [0.7.9] — 2026-04-06

### Added
- **Smart Forge first-run recovery** — bare `fluid forge` now distinguishes between an implicit entry and an explicit `--mode`, auto-starts copilot only when LLM prerequisites are already satisfied, and otherwise offers session-only AI setup or alternate creation-mode fallback instead of crashing.
- **Explicit extended doctor diagnostics** — `fluid doctor --extended` now runs optional workspace diagnostics via `scripts/diagnose.sh`, with `--comprehensive` supported as a compatibility alias.

### Changed
- **Doctor default behavior** — `fluid doctor` is now self-contained by default, showing a built-in summary, copilot readiness, and actionable next steps without probing workspace-only scripts unless `--extended` is requested.
- **Tooling and help alignment** — Make targets, bootstrap helpers, pipeline templates, and help text now reference the explicit extended-diagnostics flow so the CLI contract is consistent across entry points.
- **Release version bump** — promoted the CLI and build manifest version to `0.7.9`.

### Fixed
- **Forge missing-LLM onboarding** — missing LLM configuration now surfaces friendly recovery guidance and suggestions instead of raw error keys or a traceback during Forge startup.
- **Doctor missing-script messaging** — default doctor runs no longer end with `Diagnostic script not found or invalid: scripts/diagnose.sh`; that message is now replaced by a clear `Extended diagnostics are not installed in this checkout.` error only when `--extended` is explicitly requested.

## [0.7.8] — 2026-04-03

### Changed
- **Release version consistency** — aligned the runtime CLI version with package metadata so `fluid --version` and the published package both report `0.7.8`.
- **Release notes continuity** — added the `0.7.8` changelog section and compare link so the release history matches the version bump.

### Fixed
- **Windows timeout enforcement** — corrected the non-`SIGALRM` security timeout path so operations time out promptly instead of waiting for the worker to finish.
- **Regression coverage** — added a targeted test for the Windows timeout path to keep the fallback behavior stable.

## [0.7.7] — 2026-04-01

### Added
- **Forge copilot architecture refresh** — modularized the `fluid forge` copilot flow into focused runtime, context, UI, mode, and agent layers for easier iteration and maintenance.
- **Declarative domain-agent specs** — built-in YAML-backed agent specs now power domain guidance without hard-coding every interview path in Python.
- **Project-memory-aware copilot flow** — copilot generation now supports project-scoped memory and post-generation clarification loops to refine outputs with more context.

### Changed
- **Release version bump** — promoted the Forge CLI and companion Claude plugin assets to `0.7.7` to reflect the sizable copilot feature set landing in this release.

### Fixed
- **Test suite alignment with PR #8 merge** — updated tests to match new `_extract_sla_properties()` list-of-dicts return format, `_publish_odcs_per_expose()` keyword argument signature, and `dataContractId` format (`{product_id}.{expose_id}`)
- **Result URL domain** — test assertions updated to match `api.entropy-data.com` (from `app.entropy-data.com`)
- **License headers** — added Apache 2.0 headers to `tests/test_datamesh_manager_publish_spec.py` and `tests/test_odcs_sla_properties.py`

## [0.7.6] — 2026-03-31

### Fixed
- **Data Product payload conformance** — `PUT /api/dataproducts/{id}` now sends `dataProductSpecification: "0.0.1"`, root-level `id`, `info.title` (not `info.name`), and `info.owner` per Data Product Specification v0.0.1 schema
- **Output port server object** — output ports now send structured `server` objects (`account`, `database`, `table`) instead of flat `location` strings, matching the DPS schema
- **Result URLs** — publish result URLs now use the configured `api_url` instead of hardcoded `app.entropy-data.com`
- **`_cmd_publish()` signature bug** — all five `_cmd_*` functions in `datamesh_manager.py` now accept `(args, logger=None)` to match the CLI dispatcher in `__init__.py:432`

### Added
- **ODCS v3.1.0 data contract support** — `_build_data_contract_odcs()` generates Open Data Contract Standard v3.1.0 payloads (`apiVersion`, `kind: DataContract`, `team.name`, `description.purpose`, array-based `schema` with `logicalType` mapping)
- **`--contract-format {odcs,dcs}` CLI flag** — choose between ODCS v3.1.0 (default) and deprecated DCS 0.9.3 when publishing companion data contracts
- **`dataContractId` wiring** — output ports automatically include `dataContractId` linking to the companion contract when `--with-contract` is used
- **Archetype inference** — `info.archetype` auto-inferred from `metadata.layer` (Bronze→source-aligned, Silver→consumer-aligned, Gold→aggregate) when not explicitly set
- **SQL-to-ODCS type mapping** — `_odcs_logical_type()` maps 25+ SQL/FLUID types to ODCS logical types
- **86 tests** for `fluid dmm` subcommand (up from 40), covering DPS conformance, ODCS/DCS builders, server objects, format dispatch, and dataContractId wiring

### Deprecated
- DCS 0.9.3 data contract format — use `--contract-format dcs` for backward compatibility. Entropy Data removing DCS support after 2026-12-31.

## [0.7.1] — 2026-01-30

### Added
- Multi-provider Airflow DAG generation (`fluid generate-airflow`)
- GCP code generators: Airflow, Dagster, Prefect
- Snowflake code generators: Airflow, Dagster, Prefect
- AWS code generators: Airflow, Dagster, Prefect
- Circular dependency detection in contract validation
- Provider SDK extraction (`fluid-provider-sdk` v0.1.0)
- Plugin-based provider discovery via entry points
- Local provider with DuckDB backend
- Policy engine (check, compile, apply)
- Blueprint system for data product templates
- ODPS/ODCS standard export support
- Marketplace and catalog connectors
- Comprehensive GitHub Actions CI (Python 3.9–3.12, ruff, black, bandit, coverage)
- Apache 2.0 license headers on all source files
- CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
- Issue templates (bug report, feature request, provider request)
- PR template

### Performance
- Contract generation: 0.29–2.54ms per contract
- 828 tests passing at release

## [0.5.7] — 2025-08-15

### Added
- Initial public release
- Core CLI: `validate`, `plan`, `apply`
- GCP BigQuery provider
- Contract schema v0.5.7
- Basic Airflow DAG export

[Unreleased]: https://github.com/Agenticstiger/forge-cli/compare/v0.23.0...HEAD
[0.23.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.18.2...v0.19.0
[0.18.2]: https://github.com/Agenticstiger/forge-cli/compare/v0.18.1...v0.18.2
[0.18.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.7...v0.17.0
[0.16.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.6...v0.16.7
[0.16.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.5...v0.16.6
[0.16.5]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.4...v0.16.5
[0.16.4]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.3...v0.16.4
[0.16.3]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.2...v0.16.3
[0.16.2]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.1...v0.16.2
[0.16.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.15.3...v0.16.0
[0.15.3]: https://github.com/Agenticstiger/forge-cli/compare/v0.15.2...v0.15.3
[0.15.2]: https://github.com/Agenticstiger/forge-cli/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.11...v0.9.0
[0.8.11]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.10...v0.8.11
[0.8.10]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.9...v0.8.10
[0.8.9]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.8...v0.8.9
[0.8.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.0...v0.8.3
[0.8.8]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.7...v0.8.8
[0.8.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.8.0
[0.7.11]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.7.11
[0.7.10]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.9...v0.7.10
[0.7.9]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.8...v0.7.9
[0.7.8]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.7...v0.7.8
[0.7.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.1...v0.7.6
[0.7.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.5.7...v0.7.1
[0.5.7]: https://github.com/Agenticstiger/forge-cli/releases/tag/v0.5.7
