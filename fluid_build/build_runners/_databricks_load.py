# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Land a build's Parquet file in the Unity Catalog table ``fluid apply`` created.

A Databricks binding is not a file destination. The acquisition runner used to treat its
``location.path`` (or ``out/``) as one: it wrote a LOCAL file and reported success with nothing in
Databricks. Now the build lands its file under ``.fluid/staging`` (after the runner's own masking,
which hashes, tokenizes, encrypts or masks columns before the data leaves the machine), and this
module moves it into the table in three steps:

1. upload the file to a Unity Catalog volume through the Files API,
   ``<volume>/<product>/<build>/<run id>/<file>.parquet``;
2. run ONE statement on ``DATABRICKS_WAREHOUSE_ID`` through the SQL Statement Execution API::

       INSERT OVERWRITE `c`.`s`.`t` (`col`, ...) SELECT `col`, ... FROM read_files('<staged file>', format => 'parquet')

   reading the one file this run uploaded, never the directory (a file someone else put there is not
   loaded), a full refresh that rewrites the table's rows and nothing else: the table's definition, grants,
   tags, row filter and column masks stay (``INSERT OVERWRITE`` is a data operation; ``CREATE OR
   REPLACE`` is the statement that rebuilds a table);
3. delete the staged files and their directory, whether the load worked or not. The volume must never
   keep landed data (masked, but still the product's data). A file that cannot be deleted fails the
   run and is named, so that someone deletes it. Ctrl-C (or any other interrupt) during the upload or
   the INSERT still deletes the files before it propagates. An INSERT the warehouse has already
   answered for is cancelled first; one still inside its first 30-second wait runs on, and Delta
   commits it whole or not at all. A run killed outright (SIGKILL, a CI runner torn down) cannot clean up, so each
   load first deletes what an earlier run of the same build left under
   ``<volume>/<product>/<build>/``, once all of it is over an hour old.

The table is the IaC's: a missing table is an error that names ``fluid apply``, and this module never
creates or alters one. The volume is named by ``DATABRICKS_STAGING_VOLUME``
(``/Volumes/<catalog>/<schema>/<volume>``), from the environment only, like
``DATABRICKS_WAREHOUSE_ID``: a contract that named a path could aim the upload anywhere the
credential reaches. Give the staging a volume of its own: the sweep of earlier runs deletes only
directories named like a run id that hold nothing but old Parquet files, but it runs under the
deploy credential. The client and the statement runner are the ones that enforce masks and row
filters (``iac/providers/databricks_enforce.py``), on the same pinned credential; no message carries
a credential.

Not proven without a workspace: what ``read_files`` and ``INSERT OVERWRITE`` accept on a managed table
carrying a mask and a row filter, and the Files API's limits. ``tests/iac/_fake_databricks.py`` stands
in for them.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from ..iac.provider_match import is_cloud
from ..iac.providers.databricks_access import quote as quote_ident
from ..iac.providers.databricks_access import quote_table, string_literal
from ..iac.providers.databricks_enforce import (
    DATABRICKS_EXTRA_INSTALL,
    WAREHOUSE_ENV,
    SqlRunner,
    StatementFailed,
    default_client,
    is_not_found,
    read_table,
    scrub,
)
from ..providers._sql_safety import validate_ident
from ..util.binding_paths import ENV_PLACEHOLDER_RE

#: The Unity Catalog volume the run's file is staged in: ``/Volumes/<catalog>/<schema>/<volume>``.
STAGING_VOLUME_ENV = "DATABRICKS_STAGING_VOLUME"

_VOLUME_FORM = "/Volumes/<catalog>/<schema>/<volume>"
_VOLUME_RE = re.compile(r"^/Volumes/([^/]+)/([^/]+)/([^/]+)/?$")
#: How old every file of an earlier run's staging directory must be before a load deletes it:
#: well past a load's own span (the INSERT is cancelled after 600 s), so a run of the same build
#: still in progress keeps its upload.
STALE_STAGING_SECONDS = 3600
#: The only mode a load supports: ``INSERT OVERWRITE`` replaces the table's rows.
LOAD_MODES = ("full_refresh",)
_SINK_FORMATS = ("parquet",)


class DatabricksLoadError(RuntimeError):
    """The landed file could not be loaded into the table, or loaded the wrong number of rows."""


@dataclass(frozen=True)
class StagingVolume:
    catalog: str
    schema: str
    volume: str

    @property
    def path(self) -> str:
        return f"/Volumes/{self.catalog}/{self.schema}/{self.volume}"


@dataclass(frozen=True)
class LoadTarget:
    """The table a Databricks expose lands in, named as the emitter creates it."""

    catalog: str
    schema: str
    table: str
    #: The contract's columns, in table order.
    columns: Tuple[str, ...]

    @property
    def full_name(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.table}"


def staging_volume(env: Mapping[str, str]) -> StagingVolume:
    """``DATABRICKS_STAGING_VOLUME`` as three identifiers, or a refusal that names the variable."""
    raw = str(env.get(STAGING_VOLUME_ENV) or "").strip()
    if not raw:
        raise DatabricksLoadError(
            f"{STAGING_VOLUME_ENV} is not set. The build stages its Parquet file in a Unity Catalog "
            f"volume before loading the table; set it to {_VOLUME_FORM}."
        )
    match = _VOLUME_RE.match(raw)
    parts = match.groups() if match else ()
    try:
        for part in parts:
            validate_ident(part)
    except ValueError:
        parts = ()
    if len(parts) != 3:
        raise DatabricksLoadError(
            f"{STAGING_VOLUME_ENV} is {raw[:120]!r}, which is not {_VOLUME_FORM}, with each part a "
            "plain identifier (letters, digits and underscores)."
        )
    return StagingVolume(*parts)


def run_prefix(volume: StagingVolume, product_id: Any, build_id: Any, run_id: Any) -> str:
    """The directory one run stages in: ``<volume>/<product>/<build>/<run id>``."""
    from .duckdb.runner import _path_part  # one safe path component, as the local staging uses

    parts = (_path_part(product_id), _path_part(build_id), _path_part(run_id))
    return "/".join((volume.path, *parts))


def _resolved(value: Any) -> Any:
    """``{{ env.NAME }}`` in a name, resolved as the runner resolves a bucket; unset ones stay."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    return ENV_PLACEHOLDER_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def databricks_load_target(
    binding: Mapping[str, Any], expose: Mapping[str, Any]
) -> Optional[LoadTarget]:
    """The table a binding loads into, or ``None`` when it is not a Databricks binding.

    Resolved with the emitter's own helpers, so the load names the catalog, schema, table and
    columns ``fluid apply`` created, and every name has been through the same identifier check.
    A Databricks binding that cannot be resolved raises :class:`DatabricksLoadError` with the
    emitter's reasons.
    """
    if not is_cloud(binding, "databricks"):
        return None
    from ..iac.providers import databricks as dbx

    where = f"exposes[{expose.get('exposeId')}]"
    raw_loc = binding.get("location")
    loc: Dict[str, Any] = dict(raw_loc) if isinstance(raw_loc, Mapping) else {}
    loc.update({k: _resolved(loc.get(k)) for k in ("database", "schema", "table")})
    refusals: List[Any] = []
    names = dbx._location_names(where, loc, refusals, validating=False)
    columns = dbx._columns(where, expose, refusals, validating=False)
    if refusals:
        raise DatabricksLoadError("; ".join(str(r) for r in refusals))
    return LoadTarget(
        catalog=str(names["database"]),
        schema=str(names["schema"]),
        table=str(names["table"]),
        columns=tuple(column["name"] for column in columns),
    )


def unsupported_reason(mode: str, sink_format: str, stream_count: int) -> Optional[str]:
    """Why this build cannot be loaded into a Databricks table, or ``None`` if it can.

    Checked before the build runs, so an unsupported shape fails by name instead of landing
    somewhere else and reporting success.
    """
    if stream_count != 1:
        return f"a Databricks binding loads exactly one stream; this build has {stream_count}"
    if sink_format not in _SINK_FORMATS:
        return f"a Databricks load takes {list(_SINK_FORMATS)}; the sink format is {sink_format!r}"
    if mode not in LOAD_MODES:
        return (
            f"a Databricks load is an INSERT OVERWRITE, so it supports modes {list(LOAD_MODES)}; "
            f"this build is {mode!r}"
        )
    return None


def insert_overwrite_statement(target: LoadTarget, source: str) -> str:
    """The one statement that loads the run's file: every name an identifier the emitter
    accepted, quoted; the path a Spark string literal (backslash escapes).

    The target columns are listed, so each value goes to the column of its name whatever order
    the live table keeps them in (a column added later by ``fluid apply`` sits at the end, not
    where the contract declares it); without the list Databricks inserts by position.
    """
    columns = ", ".join(quote_ident(column) for column in target.columns)
    return (
        f"INSERT OVERWRITE {quote_table(target.full_name)} ({columns}) SELECT {columns} "
        f"FROM read_files({string_literal(source)}, format => 'parquet')"
    )


#: A run directory's name: the acquisition runner's run id (``01`` + 16 Crockford base32
#: characters, ``_acquisition_common.generate_run_id``). The sweep touches nothing else.
_RUN_DIR_RE = re.compile(r"01[0-9A-HJKMNP-TV-Z]{16}")


def _stale_files(listed: Any, run_dir: str, cutoff_ms: float) -> Optional[List[str]]:
    """The files of an earlier run's directory listing, when it looks like what a load stages
    (Parquet files only, no subdirectory) and every file was last modified before
    ``cutoff_ms`` (epoch milliseconds, as the Files API reports ``last_modified``); ``None``
    otherwise: a file that is newer, undated or not Parquet, a subdirectory, or an empty
    directory (a run that has made it and is still uploading)."""
    files: List[str] = []
    if not listed:
        return None
    for entry in listed:
        entry = _mapping(entry)
        path = str(entry.get("path") or "")
        if entry.get("is_directory") or not path.startswith(run_dir + "/"):
            return None
        if not path.endswith(".parquet"):
            return None
        try:
            modified = float(entry.get("last_modified"))
        except (TypeError, ValueError):
            return None
        if modified >= cutoff_ms:
            return None
        files.append(path)
    return files


def _fs(kind: str, path: str) -> str:
    return f"/api/2.0/fs/{kind}{quote(path, safe='/')}"


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {scrub(exc)}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows_reported(doc: Mapping[str, Any]) -> Optional[Tuple[int, str]]:
    """The rows an INSERT says it wrote: ``num_inserted_rows``, else ``num_affected_rows``."""
    schema = _mapping(_mapping(doc.get("manifest")).get("schema"))
    names = [str(_mapping(c).get("name")) for c in schema.get("columns") or []]
    data = _mapping(doc.get("result")).get("data_array") or []
    if not data or not isinstance(data[0], list):
        return None
    for name in ("num_inserted_rows", "num_affected_rows"):
        if name in names and names.index(name) < len(data[0]):
            try:
                return int(data[0][names.index(name)]), name
            except (TypeError, ValueError):
                return None
    return None


class Landing:
    """A client, a runner and a staging volume, for loading one build's file."""

    def __init__(self, target: LoadTarget, volume: StagingVolume, client: Any, sql: SqlRunner):
        self.target = target
        self.volume = volume
        self._client = client
        self._sql = sql

    # ── the three steps ────────────────────────────────────────────────

    def _upload(self, path: str, prefix: str, remote: str) -> None:
        # One PUT to the Files API's own endpoint (the one databricks-sdk's FilesAPI.upload
        # calls), not WorkspaceClient.files.upload: from databricks-sdk 0.4x its FilesExt can
        # switch to multipart uploads through presigned cloud-storage URLs, a second host the
        # data would go to. The cost is the single PUT's 5 GiB limit per file.
        do = self._client.api_client.do
        try:
            do("PUT", _fs("directories", prefix))
        except Exception as exc:  # noqa: BLE001 — every transport failure is the upload's
            raise DatabricksLoadError(
                f"could not create {prefix} in the staging volume: {_failure(exc)}"
            ) from exc
        try:
            with open(path, "rb") as fh:
                do(
                    "PUT",
                    _fs("files", remote),
                    query={"overwrite": "true"},
                    headers={"Content-Type": "application/octet-stream"},
                    data=fh,
                )
        except Exception as exc:  # noqa: BLE001
            raise DatabricksLoadError(f"could not upload to {remote}: {_failure(exc)}") from exc

    def _count(self, full: str) -> int:
        doc = self._sql.execute(f"SELECT COUNT(*) FROM {quote_table(full)}")
        data = _mapping(doc.get("result")).get("data_array") or []
        try:
            return int(data[0][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise DatabricksLoadError(f"counting {full} returned no readable count") from exc

    def _insert(self, remote: str) -> Tuple[int, str]:
        full = self.target.full_name
        try:
            doc = self._sql.execute(insert_overwrite_statement(self.target, remote))
        except StatementFailed as exc:
            raise DatabricksLoadError(f"loading {full} failed: {exc}") from exc
        reported = _rows_reported(doc)
        if reported is not None:
            return reported
        # A full refresh leaves exactly the file's rows, so the table's count is the load's.
        try:
            return self._count(full), "count_after_load"
        except StatementFailed as exc:
            raise DatabricksLoadError(
                f"loaded {full}, but counting its rows afterwards failed: {exc}"
            ) from exc

    def _delete(self, prefix: str, remote: str) -> List[str]:
        """Delete the staged file and its directory; what could not be deleted, named."""
        do = self._client.api_client.do
        try:
            do("DELETE", _fs("files", remote))
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                return [f"{remote} ({_failure(exc)})"]
        try:
            do("DELETE", _fs("directories", prefix))
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                return [f"{prefix} ({_failure(exc)})"]
        return []

    def _list_dir(self, path: str) -> List[Any]:
        """Every entry of a volume directory, following ``next_page_token``."""
        entries: List[Any] = []
        token: Optional[str] = None
        for _ in range(100):
            page = _mapping(
                self._client.api_client.do(
                    "GET", _fs("directories", path), query={"page_token": token} if token else None
                )
            )
            entries += list(page.get("contents") or [])
            token = page.get("next_page_token") or None
            if not token:
                return entries
        raise DatabricksLoadError(f"the listing of {path} never ended")

    def _sweep_stale(self, build_dir: str, current: str, logger: logging.Logger) -> List[str]:
        """Delete what earlier runs of this build left in ``build_dir`` (a run killed before its
        own delete); the run directories deleted. Only a run directory whose every file is older
        than :data:`STALE_STAGING_SECONDS` is deleted: a run of the same build still in progress
        keeps its upload. Best effort: a failure is logged, not raised, because this run's own
        files are deleted by this run whatever happens here."""
        do = self._client.api_client.do
        swept: List[str] = []
        cutoff_ms = (time.time() - STALE_STAGING_SECONDS) * 1000
        try:
            runs = self._list_dir(build_dir)
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                logger.warning(
                    "databricks.load could not list %s for earlier runs' staged files: %s",
                    build_dir,
                    _failure(exc),
                )
            return swept
        for run in runs:
            run_dir = str(_mapping(run).get("path") or "").rstrip("/")
            if not _mapping(run).get("is_directory") or run_dir == current:
                continue
            name = run_dir[len(build_dir) + 1 :]
            if not run_dir.startswith(build_dir + "/") or not _RUN_DIR_RE.fullmatch(name):
                continue  # only a run directory, directly in this build's directory
            try:
                listed = self._list_dir(run_dir)
                files = _stale_files(listed, run_dir, cutoff_ms)
                if files is None:
                    continue  # a file there is recent, or undated: perhaps a run in progress
                for path in files:
                    do("DELETE", _fs("files", path))
                do("DELETE", _fs("directories", run_dir))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "databricks.load could not delete an earlier run's staged files in %s; "
                    "delete them: %s",
                    run_dir,
                    _failure(exc),
                )
                continue
            swept.append(run_dir)
            logger.warning(
                "databricks.load deleted staged files an earlier run left behind: %s", run_dir
            )
        return swept

    # ── the load ───────────────────────────────────────────────────────

    def load(
        self,
        path: str,
        *,
        product_id: Any,
        build_id: Any,
        run_id: Any,
        expected_rows: int,
        logger: logging.Logger,
    ) -> Dict[str, Any]:
        """Upload ``path``, replace the table's rows with it, delete the upload. Returns the facts.

        The table must exist. The staged files are deleted whether or not the load worked, and
        a load that wrote the wrong number of rows, or a file that cannot be deleted, fails.
        """
        full = self.target.full_name
        try:
            live = read_table(self._client, full)
        except Exception as exc:  # noqa: BLE001
            raise DatabricksLoadError(
                f"could not read {full} from Unity Catalog: {_failure(exc)}"
            ) from exc
        if live is None:
            raise DatabricksLoadError(
                f"{full} does not exist in Unity Catalog. The build loads the table fluid apply "
                "creates and never creates one itself: run fluid apply for this contract first."
            )
        prefix = run_prefix(self.volume, product_id, build_id, run_id)
        remote = f"{prefix}/{os.path.basename(path)}"
        swept = self._sweep_stale(prefix.rsplit("/", 1)[0], prefix, logger)
        loaded = 0
        rows_from = ""
        failure: Optional[DatabricksLoadError] = None
        try:
            self._upload(path, prefix, remote)
            loaded, rows_from = self._insert(remote)
            if loaded != expected_rows:
                raise DatabricksLoadError(
                    f"loaded {loaded} rows into {full}, but the landed file holds {expected_rows}"
                )
        except DatabricksLoadError as exc:
            failure = exc
        except Exception as exc:  # noqa: BLE001 — a failure must still reach the delete below
            failure = DatabricksLoadError(_failure(exc))
        except BaseException:
            # Ctrl-C or SystemExit mid-load (the statement runner has cancelled the INSERT): the
            # staged files still go before the interrupt propagates.
            left = self._delete(prefix, remote)
            if left:
                logger.error(
                    "databricks.load interrupted, and the staged files, which hold the landed "
                    "data, could not be deleted; delete them: %s",
                    "; ".join(left),
                )
            raise
        left = self._delete(prefix, remote)
        if left:
            # the paths lead the message: a caller that shortens it must not cut them off
            logger.error(
                "databricks.load could not delete the staged files, which hold the landed data; "
                "delete them: %s",
                "; ".join(left),
            )
        if failure is not None:
            if left:
                raise DatabricksLoadError(
                    f"could not delete the staged files, which hold the landed data; delete them: "
                    f"{'; '.join(left)}. The load had failed: {failure}"
                ) from None
            raise DatabricksLoadError(
                f"{failure} (the staged files in {prefix} were deleted)"
            ) from None
        if left:
            raise DatabricksLoadError(
                f"loaded {loaded} rows into {full}, but could not delete the staged files, which "
                f"hold the landed data; delete them: {'; '.join(left)}"
            )
        logger.info(
            "databricks.load table=%s rows=%d rows_from=%s staged_to=%s deleted=true",
            full,
            loaded,
            rows_from,
            prefix,
        )
        return {
            "table": full,
            "rows": loaded,
            "rows_from": rows_from,
            "staged_to": prefix,
            "staged_files_deleted": True,
            "earlier_runs_staging_deleted": swept,
        }


def open_landing(target: LoadTarget, *, env: Optional[Mapping[str, str]] = None) -> Landing:
    """A :class:`Landing` for ``target``, or a :class:`DatabricksLoadError` naming what is missing.

    Called before the build runs, so a missing volume, warehouse, credential or SDK refuses the
    run before anything is read or written.
    """
    env = os.environ if env is None else env
    volume = staging_volume(env)
    warehouse = str(env.get(WAREHOUSE_ENV) or "").strip()
    if not warehouse:
        raise DatabricksLoadError(
            f"{WAREHOUSE_ENV} is not set. The load runs on that SQL warehouse: set it to the id "
            "of a warehouse in the workspace DATABRICKS_HOST names."
        )
    try:
        client = default_client()
    except ImportError:
        raise DatabricksLoadError(
            f"loading into Databricks needs the databricks extra: {DATABRICKS_EXTRA_INSTALL}"
        ) from None
    except Exception as exc:  # noqa: BLE001 — a bad credential must not become a traceback
        raise DatabricksLoadError(
            f"could not create the Databricks client: {_failure(exc)}"
        ) from exc
    return Landing(target, volume, client, SqlRunner(client, warehouse))
