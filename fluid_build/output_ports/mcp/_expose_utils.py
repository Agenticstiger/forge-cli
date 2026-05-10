# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Expose-handling utilities shared between the MCP dispatcher and CLI.

These helpers do not depend on the MCP wire protocol or the
Anthropic SDK. They live here so the SDK-bound dispatcher stays
focused on protocol handling, and so CLI code (`fluid mcp output-port
list / doctor / serve`) can use them without importing the SDK.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


def find_expose(contract: Mapping[str, Any], expose_id: Optional[str]) -> Mapping[str, Any]:
    """Return an ``expose`` block from the contract.

    When ``expose_id`` is ``None`` and the contract has exactly one
    expose, that expose is returned automatically — the common case
    for single-expose contracts. When the contract has multiple
    exposes and no id is given, a ``ValueError`` lists them so the
    operator can pick.

    The error message is agentPort-aware: when more than one expose
    carries an ``agentPort.kind=mcp`` (or legacy ``mcp``) block, the
    eligible candidates are listed separately so the operator
    immediately sees which ``--expose-id`` choices the gateway can
    actually serve.

    When an id IS given but doesn't match, the error message lists
    the ids that DO exist so an operator can fix the typo without
    re-reading the contract.
    """
    available: List[str] = []
    matches: List[Mapping[str, Any]] = []
    agent_eligible: List[str] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        candidate = expose.get("exposeId")
        if isinstance(candidate, str):
            available.append(candidate)
            agent_port = expose.get("agentPort") or {}
            has_agent_port = (
                isinstance(agent_port, dict) and agent_port.get("kind") == "mcp"
            ) or bool(expose.get("mcp"))
            if has_agent_port:
                agent_eligible.append(candidate)
            if expose_id is not None and candidate == expose_id:
                return expose
            matches.append(expose)
    if expose_id is None:
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError("Contract has no exposes")
        if agent_eligible:
            eligible_hint = (
                f" Agent-eligible exposes (carry agentPort.kind=mcp or "
                f"legacy mcp block): {agent_eligible}."
            )
        else:
            eligible_hint = (
                " None of these exposes carry agentPort.kind=mcp; the "
                "gateway will refuse to serve a non-eligible expose. "
                "Add an `agentPort: { kind: mcp }` block to the expose "
                "you want to serve."
            )
        raise ValueError(
            f"Contract has {len(matches)} exposes; pass --expose-id to "
            f"pick one. Available: {available}.{eligible_hint}"
        )
    raise ValueError(
        f"exposeId {expose_id!r} not found in contract; available: " f"{available or 'none'}"
    )


def resolve_expose_paths(
    expose: Mapping[str, Any], *, contract_dir: Optional[Path]
) -> Mapping[str, Any]:
    """Return a copy of ``expose`` with relative ``binding.location.path``
    resolved against ``contract_dir``.

    Lets example contracts ship a ``path: ./customers.csv`` next to
    the YAML without forcing operators to author absolute paths.
    Absolute paths and missing paths pass through unchanged.
    """
    if contract_dir is None:
        return expose
    binding = expose.get("binding") or {}
    location = binding.get("location") or {}
    raw_path = location.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return expose
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return expose
    resolved = (contract_dir / candidate).resolve()
    new_location = dict(location)
    new_location["path"] = str(resolved)
    new_binding = dict(binding)
    new_binding["location"] = new_location
    new_expose = dict(expose)
    new_expose["binding"] = new_binding
    return new_expose


def list_exposes(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return a lightweight summary of every expose in ``contract``.

    Used by ``fluid mcp output-port list`` to show operators what's
    available without parsing the full YAML themselves. Each entry
    carries the bare minimum a human needs to pick: ``exposeId``,
    ``kind``, ``title``, the binding's ``platform``/``format``, and
    a short engine-readable ``tableReference``.
    """
    out: List[Dict[str, Any]] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        binding = expose.get("binding") or {}
        location = binding.get("location") or {}
        table_reference = _format_table_reference(binding, location)
        out.append(
            {
                "exposeId": expose.get("exposeId"),
                "kind": expose.get("kind"),
                "title": expose.get("title"),
                "platform": binding.get("platform"),
                "format": binding.get("format"),
                "tableReference": table_reference,
                "hasSemantics": bool(expose.get("semantics")),
                "hasMcpOverrides": bool(expose.get("mcp")),
                "hasAgentPort": bool(expose.get("agentPort")),
            }
        )
    return out


def _format_table_reference(binding: Mapping[str, Any], location: Mapping[str, Any]) -> str:
    """Best-effort, human-readable engine reference for the
    ``list`` summary. Driver-specific quoting is NOT applied — this
    string is for display, not execution."""
    fmt = str(binding.get("format") or "")
    if fmt == "bigquery_table":
        parts = [
            location.get("project"),
            location.get("dataset"),
            location.get("table"),
        ]
        return ".".join(p for p in parts if p) or "<unknown>"
    if fmt == "snowflake_table":
        parts = [
            location.get("database") or location.get("dataset"),
            location.get("schema"),
            location.get("table"),
        ]
        return ".".join(p for p in parts if p) or "<unknown>"
    if fmt in {"csv", "parquet", "json"}:
        path = location.get("path") or location.get("table") or "<unknown>"
        return str(path)
    return str(location.get("table") or location.get("name") or "<unknown>")


_ENGINE_HINT_PATTERNS: List[Tuple[str, str]] = [
    (
        "does not exist or not authorized",
        "engine reports the table is missing or the role can't see it. "
        "Check binding.location.{database,schema,table} (Snowflake / "
        "BigQuery: project.dataset.table) and confirm the connection's "
        "role/credentials grant SELECT on the bound table.",
    ),
    (
        "Object not found",
        "engine reports the bound object does not exist. Verify the "
        "binding.location values against the contract's expose block.",
    ),
    (
        "Access Denied",
        "engine refused the query for permissions. Check that the "
        "connection's role/service-account has SELECT on the bound "
        "table; column-level grants may also be required when the "
        "expose carries policy.authz.columnRestrictions.",
    ),
    (
        "Authentication failed",
        "Snowflake authentication failed. Verify SNOWFLAKE_ACCOUNT, "
        "SNOWFLAKE_USER, and one of SNOWFLAKE_PASSWORD / "
        "SNOWFLAKE_PRIVATE_KEY_PATH in the server's environment.",
    ),
    (
        "could not be authenticated",
        "BigQuery authentication failed. Verify "
        "GOOGLE_APPLICATION_CREDENTIALS or the active gcloud auth "
        "context can call the bound project.",
    ),
    (
        "Catalog Error",
        "DuckDB couldn't resolve the bound table. Verify "
        "binding.location.path points at a readable file and "
        "binding.location.table matches the file's logical name.",
    ),
]


def _annotate_engine_error(exc: BaseException, *, expose: Mapping[str, Any]) -> str:
    """Attach an actionable hint to a raw engine exception.

    Driver clients (Snowflake, BigQuery, DuckDB) return long,
    technical error strings that an LLM agent or operator has to
    decode. We pattern-match the most common shapes and append a
    one-line "what to fix" hint with the relevant binding fields,
    so the wire response is structured help instead of raw stack
    text.
    """
    raw = str(exc)
    expose_id = str(expose.get("exposeId") or "<unknown>")
    binding = expose.get("binding") or {}
    binding_summary = f"platform={binding.get('platform')} format={binding.get('format')}"
    for needle, hint in _ENGINE_HINT_PATTERNS:
        if needle.lower() in raw.lower():
            return f"{raw}\n\nHint: {hint} " f"(expose={expose_id}, {binding_summary})"
    return raw


def _summarise_arguments(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip large or sensitive blobs from an argument dict before
    audit logging.

    Long SQL bodies are truncated to keep the audit document small,
    AND every value is routed through forge-cli's
    :func:`fluid_build.observability.secret_redactor.redact_value` so
    a caller-supplied filter literal that happens to look like a
    credential (JWT / Stripe key / GitHub token / bearer / k=v
    assignment) lands masked rather than raw.

    The redactor is imported lazily so importing this utility module
    in a no-deps context (e.g. linting an isolated file) does not
    require the observability package to be installed.
    """
    try:
        from fluid_build.observability.secret_redactor import redact_value
    except ImportError:  # pragma: no cover - defensive

        def redact_value(v: Any) -> Any:  # type: ignore[no-redef]
            return v

    summary: Dict[str, Any] = {}
    for key, value in arguments.items():
        if key == "sql" and isinstance(value, str) and len(value) > 256:
            redacted = redact_value(value[:256])
            summary[key] = (
                redacted if isinstance(redacted, str) else value[:256]
            ) + "... [truncated]"
        else:
            summary[key] = redact_value(value)
    return summary


def _jsonable(value: Any) -> Any:
    """Coerce a contract fragment into JSON-serialisable form.

    YAML loads can return ``OrderedDict`` / ``datetime`` /
    ``decimal.Decimal``; ``json.dumps(default=str)`` handles them, but
    we apply an explicit pass for stable test output.
    """
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value
