# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A :class:`~fluid_build.diagnostics.model.DiagnosticReport` as text lines and as SARIF 2.1.0.

Text is compiler style, so an editor or a terminal can jump to the place::

    contract.fluid.yaml:12:7: error ERR_CONTRACT_SCHEMA_ENUM: metadata.layer: ...
      related: contract.fluid.yaml:3:1: exposes[0] in the root contract

SARIF follows the 2.1.0 OASIS standard and what GitHub code scanning reads: one
run, a ``rules[]`` entry per code (its catalog advice as ``help``), each result's
``locations`` and ``relatedLocations`` relative to ``%SRCROOT%`` (the directory
``fluid validate`` ran in), and a ``partialFingerprints`` entry that does not
move when lines are added above the finding. No result carries ``fixes``: a
tool that applies them would edit routing, permissions or types on its own.
No ``snippet`` either, so no contract value leaves in the report.
"""

from __future__ import annotations

import hashlib
from typing import AbstractSet, Any, Dict, List, Optional, Set
from urllib.parse import quote

from fluid_build.diagnostics.model import Diagnostic, DiagnosticReport, SourceLocation

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "fluid validate"
INFORMATION_URI = "https://github.com/Agenticstiger/forge-cli"
#: ``%SRCROOT%``: file names are relative to the directory the command ran in.
SRCROOT = "%SRCROOT%"


def text_lines(
    report: DiagnosticReport, *, severities: Optional[AbstractSet[str]] = None
) -> List[str]:
    """One line per diagnostic (``severities`` only, when given), then its related lines."""
    lines: List[str] = []
    for item in report.diagnostics:
        if severities is not None and item.severity not in severities:
            continue
        where = f"{item.location}: " if item.location else ""
        lines.append(f"{where}{item.severity} {item.code}: {item.message}")
        for related in item.related:
            lines.append(f"  related: {related.location}: {related.message}")
    return lines


def _title(code: str) -> str:
    words = code.removeprefix("ERR_").replace("_", " ").lower()
    return words[:1].upper() + words[1:]


def _rule(item: Diagnostic, fixes: AbstractSet[Optional[str]]) -> Dict[str, Any]:
    """The rule for ``item``'s code; its advice is the fix only when every result shares it.

    A check can give a fix per place; each result then carries its own in
    ``properties.fix``, and the rule says what the code means.
    """
    text = item.fix if len(fixes) == 1 and item.fix else _title(item.code)
    rule: Dict[str, Any] = {
        "id": item.code,
        "name": "".join(part.capitalize() for part in item.code.removeprefix("ERR_").split("_")),
        "shortDescription": {"text": _title(item.code)},
        "fullDescription": {"text": text},
        "help": {"text": text},
        "defaultConfiguration": {"level": item.severity},
    }
    if item.docs_url:
        rule["helpUri"] = item.docs_url
    return rule


def _physical(location: SourceLocation) -> Dict[str, Any]:
    # A URI reference: a space or a non-ASCII letter in a file name is percent-encoded.
    uri = quote(location.file, safe="/")
    out: Dict[str, Any] = {"artifactLocation": {"uri": uri, "uriBaseId": SRCROOT}}
    if location.line is not None:
        region: Dict[str, Any] = {"startLine": location.line}
        if location.column is not None:
            region["startColumn"] = location.column
        out["region"] = region
    return out


def _fingerprint_key(item: Diagnostic) -> str:
    file = item.location.file if item.location else ""
    parts = (item.code, file, item.path, item.port or "", item.environment or "", item.message)
    return "\0".join(parts)


def _fingerprint(key: str, occurrence: int) -> str:
    """Stable across runs and line moves: the code, the file, the pointer, the port
    and the message.

    Two keys missing from one object differ by message only, so fixing one does
    not move the other. Findings identical in all of those also count how many
    before them were, so a reader that dedupes by fingerprint keeps each.
    """
    text = key if occurrence == 0 else f"{key}\0{occurrence}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _result(item: Diagnostic, rule_index: int, fingerprint: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ruleId": item.code,
        "ruleIndex": rule_index,
        "level": item.severity,
        "message": {"text": item.message},
        "partialFingerprints": {"fluidFinding/v1": fingerprint},
        "properties": {
            "path": item.path,
            "product": item.product,
            "environment": item.environment,
            "port": item.port,
        },
    }
    if item.fix:
        result["properties"]["fix"] = item.fix
    if item.location is not None:
        location: Dict[str, Any] = {"physicalLocation": _physical(item.location)}
        if item.path:
            location["logicalLocations"] = [{"fullyQualifiedName": item.path, "kind": "member"}]
        result["locations"] = [location]
    related = [
        {"id": i, "physicalLocation": _physical(r.location), "message": {"text": r.message}}
        for i, r in enumerate(item.related, 1)
    ]
    if related:
        result["relatedLocations"] = related
    return result


def to_sarif(report: DiagnosticReport, *, version: str) -> Dict[str, Any]:
    """``report`` as one SARIF 2.1.0 log (a ``dict`` to serialise as JSON)."""
    rules: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}
    results: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    fixes: Dict[str, Set[Optional[str]]] = {}
    for item in report.diagnostics:
        fixes.setdefault(item.code, set()).add(item.fix)
    for item in report.diagnostics:
        if item.code not in index:
            index[item.code] = len(rules)
            rules.append(_rule(item, fixes[item.code]))
        key = _fingerprint_key(item)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        results.append(_result(item, index[item.code], _fingerprint(key, occurrence)))
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": version,
                        "informationUri": INFORMATION_URI,
                        "rules": rules,
                    }
                },
                # PyYAML counts columns in code points, not UTF-16 code units.
                "columnKind": "unicodeCodePoints",
                "properties": {"diagnosticsVersion": report.version},
                "results": results,
            }
        ],
    }
