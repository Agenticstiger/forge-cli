# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow asset URIs for a contract's exposes, and for the datasets a trigger names.

An expose is named the way Airflow's own providers name the same storage: the AIP-60 URI that
each provider's ``create_asset`` factory builds (``airflow/providers/<p>/assets/*.py``).

* ``aws`` with ``location.bucket``: ``s3://<bucket>/<path>`` (amazon).
* ``gcp`` with ``location.project``, ``dataset`` and ``table``:
  ``bigquery://<project>/<dataset>/<table>`` (google).
* ``gcp`` with ``location.bucket``: ``gs://<bucket>/<path>`` (google).
* ``snowflake``: ``snowflake://<account>/<database>/<schema>/<table>``.
* ``databricks`` with ``location.host``: ``databricks://<host>/<catalog>/<schema>/<table>``.
* ``postgres``, ``pgvector``: ``postgres://<host>:<port>/<database>/<schema>/<table>``.
* ``kafka``, ``confluent``: ``kafka://<first bootstrap server>/<topic>`` (apache-kafka).
* ``local`` with an absolute ``location.path``: ``file://<path>`` (common-io).

Each provider also converts its URI to OpenLineage's dataset naming
(openlineage.io/docs/spec/naming), so a forge DAG, a dbt DAG that astronomer-cosmos renders and a
hand-written DAG that land the same table name the same asset. An aws expose with no
``location.path`` lives at ``<database>/<table>/`` under its bucket, the warehouse prefix
``providers/aws/util/warehouse.py::normalize_location`` gives it. On Databricks the catalog is
``location.database``, as ``iac/providers/databricks.py`` reads it.

A URI is written in the form Airflow's ``_sanitize_uri`` reduces it to: a lower-case scheme, no
trailing slash, no user info, query or fragment, and the default port on a postgres URI. The
string in the DAG is then the asset's key on Airflow 2 and 3 alike, and Airflow 3, which refuses
a DAG whose provider URI is malformed, loads it.

A value the worker fills in at run time (``{{ env.X }}``, ``${X}``) cannot name an asset: the URI
is fixed when the DAG is generated. Neither can a relative local path, nor a binding that names
no address. Such an expose gets no asset, and :func:`expose_asset_uri` says why.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

#: Airflow 3 caps an asset URI at 1500 characters (Airflow 2 at 3000).
MAX_URI = 1500

#: A bucket, project, account, database, schema or table name, as a URI can carry it unescaped.
_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,254}")
#: One object-key segment (``=`` for Hive-style partition directories).
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.=~+\-]{1,255}")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,252})(?::[0-9]{1,5})?")
_TOPIC_RE = re.compile(r"[A-Za-z0-9._\-]{1,249}")
_PLACEHOLDERS = ("{{", "{%", "{#", "${")
#: The characters RFC 3986 lets a URI hold: no quote, backslash, brace, space or control.
_URI_CHARS_RE = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

#: Provider schemes and the number of path parts their sanitizer requires.
#: Read from each provider's ``sanitize_uri``: Airflow 2 warns about a URI that breaks it, and
#: Airflow 3 refuses the DAG that declares it.
_SCHEME_PARTS = {"bigquery": 2, "snowflake": 3, "databricks": 3, "postgres": 3}
#: Provider schemes whose sanitizer requires a host (or bucket) first.
_HOST_FIRST = frozenset({"s3", "gs", "kafka", *_SCHEME_PARTS})


class NotAddressable(ValueError):
    """An expose, or a trigger's dataset, that no asset URI can name."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotAddressable(f"it has no {field}")
    if any(mark in value for mark in _PLACEHOLDERS):
        raise NotAddressable(
            f"its {field} {value!r} is filled in on the worker at run time, and an asset URI is "
            "fixed when the DAG is generated"
        )
    return value.strip()


def _matching(value: Any, field: str, pattern: "re.Pattern[str]", what: str) -> str:
    text = _text(value, field)
    if not pattern.fullmatch(text):
        raise NotAddressable(f"its {field} {text!r} {what}")
    return text


def _name(location: Mapping[str, Any], key: str) -> str:
    return _matching(
        location.get(key),
        f"location.{key}",
        _NAME_RE,
        "holds characters an asset URI would have to escape",
    )


def _host(value: Any, field: str = "location.host") -> str:
    return _matching(value, field, _HOST_RE, "is not a host[:port]")


def _key(path: Any, field: str) -> str:
    """An object key: segments of letters, digits and ``_.=~+-``, no leading or trailing ``/``."""
    text = _text(path, field).strip("/")
    segments = text.split("/") if text else []
    if any(s in (".", "..") or not _SEGMENT_RE.fullmatch(s) for s in segments):
        raise NotAddressable(
            f"its {field} {path!r} holds an empty, '.' or '..' segment, or characters an asset "
            "URI would have to escape"
        )
    return "/".join(segments)


def _object_store(scheme: str, location: Mapping[str, Any]) -> str:
    bucket = _name(location, "bucket")
    if location.get("path") is None and location.get("database") and location.get("table"):
        key = f"{_name(location, 'database')}/{_name(location, 'table')}"
    else:
        key = _key(location.get("path"), "location.path")
    return f"{scheme}://{bucket}/{key}"


def _tables(scheme: str, first: str, location: Mapping[str, Any], *keys: str) -> str:
    return f"{scheme}://{first}/" + "/".join(_name(location, key) for key in keys)


def _aws(location: Mapping[str, Any]) -> str:
    if location.get("bucket") is None:
        raise NotAddressable(
            "an aws expose is an asset through its S3 location, and it has no location.bucket"
        )
    return _object_store("s3", location)


def _gcp(location: Mapping[str, Any]) -> str:
    if location.get("dataset") is None and location.get("table") is None:
        return _object_store("gs", location)
    return _tables("bigquery", _name(location, "project"), location, "dataset", "table")


def _snowflake(location: Mapping[str, Any]) -> str:
    return _tables("snowflake", _name(location, "account"), location, "database", "schema", "table")


def _databricks(location: Mapping[str, Any]) -> str:
    host = _host(location.get("host"))
    return _tables("databricks", host, location, "database", "schema", "table")


def _postgres(location: Mapping[str, Any]) -> str:
    host = _host(location.get("host"))
    host = host if ":" in host else f"{host}:5432"
    return _tables("postgres", host, location, "database", "schema", "table")


def _kafka(location: Mapping[str, Any]) -> str:
    servers = _text(location.get("host"), "location.host")
    topic = _matching(
        location.get("topic"), "location.topic", _TOPIC_RE, "is not a Kafka topic name"
    )
    return f"kafka://{_host(servers.split(',')[0].strip())}/{topic}"


def _local(location: Mapping[str, Any]) -> str:
    path = _text(location.get("path"), "location.path")
    if not path.startswith("/"):
        raise NotAddressable(
            f"its location.path {path!r} is relative to the project directory, which is "
            "somewhere else on each worker"
        )
    key = _key(path, "location.path")
    if not key:
        raise NotAddressable("its location.path names no file")
    return f"file:///{key}"


#: ``binding.platform`` -> the URI of an expose there.
_URI_BUILDERS: Dict[str, Callable[[Mapping[str, Any]], str]] = {
    "aws": _aws,
    "gcp": _gcp,
    "snowflake": _snowflake,
    "databricks": _databricks,
    "postgres": _postgres,
    "pgvector": _postgres,
    "kafka": _kafka,
    "confluent": _kafka,
    "local": _local,
}


def expose_asset_uri(expose: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    """``(uri, "")`` for an expose that names an asset, else ``(None, why not)``."""
    binding = expose.get("binding")
    binding = binding if isinstance(binding, Mapping) else {}
    location = binding.get("location")
    location = location if isinstance(location, Mapping) else {}
    platform = binding.get("platform")
    if not isinstance(platform, str) or not platform:
        return None, "its binding names no platform"
    builder = _URI_BUILDERS.get(platform.strip().lower())
    if builder is None:
        return None, f"no Airflow provider names a {platform!r} binding"
    try:
        uri = builder(location)
    except NotAddressable as exc:
        return None, str(exc)
    if len(uri) > MAX_URI:
        return None, f"its asset URI would be longer than Airflow's {MAX_URI} characters"
    return uri, ""


def _refuse_unsafe(raw: Any) -> str:
    """``raw`` when it can be a URI fixed in a DAG file at all."""
    if not isinstance(raw, str) or not raw:
        raise NotAddressable("a dataset URI must be a non-empty string")
    if len(raw) > MAX_URI:
        raise NotAddressable(f"dataset URI is longer than Airflow's {MAX_URI} characters")
    if any(mark in raw for mark in _PLACEHOLDERS):
        raise NotAddressable(
            f"dataset URI {raw!r} is filled in on the worker at run time, and a schedule is "
            "fixed when the DAG is generated"
        )
    if not _URI_CHARS_RE.fullmatch(raw):
        raise NotAddressable(
            f"dataset URI {raw!r} holds a character a URI cannot (RFC 3986): a space, quote, "
            "backslash, brace or non-ASCII letter; percent-encode it"
        )
    return raw


def _provider_netloc(raw: str, scheme: str, netloc: str, path: str) -> str:
    """The host part of a provider URI its sanitizer accepts, or what it refuses."""
    if scheme in _HOST_FIRST and not netloc:
        raise NotAddressable(f"dataset URI {raw!r}: a {scheme}:// URI names a host first")
    parts = path.split("/")[1:]
    expected = _SCHEME_PARTS.get(scheme)
    if expected is not None and (len(parts) != expected or "" in parts):
        raise NotAddressable(
            f"dataset URI {raw!r}: a {scheme}:// URI has {expected} path parts after its host"
        )
    if scheme in ("kafka", "file") and path == "/":
        raise NotAddressable(f"dataset URI {raw!r}: a {scheme}:// URI names a path")
    if scheme == "postgres":
        # As the postgres provider's sanitizer reads it: no port is 5432, and a port that is
        # not a number makes it raise.
        try:
            port = urlsplit(f"//{netloc}").port
        except ValueError:
            raise NotAddressable(f"dataset URI {raw!r}: its port is not a number") from None
        return f"{netloc.rstrip(':')}:5432" if port is None else netloc
    return netloc


def normalize_dataset_uri(raw: Any) -> str:
    """A dataset URI from ``trigger.datasets``, normalised as Airflow normalises it.

    Raises :class:`NotAddressable` for a value Airflow would refuse or read differently: an
    empty, non-ASCII or spaced value, a placeholder, the reserved ``airflow`` scheme, user info
    (a password in a DAG file), a query or fragment, or a provider URI its sanitizer refuses.
    A value with no scheme is a plain name to Airflow, and an ``x-`` scheme opts out of its
    checks; both pass as written.
    """
    _refuse_unsafe(raw)
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise NotAddressable(f"dataset URI {raw!r} is not a URI ({exc})") from None
    scheme = parts.scheme.lower()
    if (not scheme and not parts.netloc) or scheme.startswith("x-"):
        return raw
    if scheme == "airflow":
        raise NotAddressable(f"dataset URI {raw!r}: Airflow reserves the airflow scheme")
    if "@" in parts.netloc:
        raise NotAddressable(
            f"dataset URI {raw!r} carries user info; name the asset without credentials"
        )
    if parts.query or parts.fragment or "?" in raw or "#" in raw:
        raise NotAddressable(f"dataset URI {raw!r} takes no query or fragment")
    scheme = "postgres" if scheme == "postgresql" else scheme
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, _provider_netloc(raw, scheme, parts.netloc, path), path, "", ""))


__all__ = ["MAX_URI", "NotAddressable", "expose_asset_uri", "normalize_dataset_uri"]
