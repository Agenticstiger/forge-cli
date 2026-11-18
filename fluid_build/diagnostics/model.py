# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The versioned diagnostic model: one finding, where it was written, and what it is about.

A :class:`Diagnostic` is one error, warning or note of ``fluid validate``, with the
file, line and column it was written at (after ``$ref`` fragments and the
``--env`` overlay are composed) and the related places that explain it: the root
contract that includes a fragment, the base contract an overlay patches. A
:class:`DiagnosticReport` holds them, under a ``version``.

Versioning: ``major.minor``. A minor release only adds fields; a reader written
for ``1.0`` reads every ``1.x`` document and ignores the keys it does not know
(:meth:`DiagnosticReport.from_dict` does). A new major is a breaking change, and
:meth:`DiagnosticReport.from_dict` refuses it rather than misread it.

The shape follows prior art: the Language Server Protocol's ``Diagnostic``
(``range``, ``code``, ``codeDescription``, ``relatedInformation``), rustc's JSON
diagnostics (a primary span, related ones), and SARIF 2.1.0 (``locations``,
``relatedLocations``), which :mod:`fluid_build.diagnostics.render` writes.

What a diagnostic carries is names and places: never a value from the contract
beyond what the message already said, never a resolved secret, and never a fix
a tool could apply on its own (a routing, permission or type change is the
author's call).

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: The version of the document this module writes.
VERSION = "1.0"
#: The severities, as SARIF names its levels.
SEVERITIES = ("error", "warning", "note")


class UnsupportedDiagnosticsVersion(ValueError):
    """A diagnostics document of a major version this reader does not know."""


@dataclass(frozen=True)
class SourceLocation:
    """A place in a file: ``file`` as shown to the reader, ``line`` and ``column`` 1-based.

    ``line`` and ``column`` are ``None`` when the file gives none (a text file
    a ``$ref`` read whole, or JSON YAML cannot place).
    """

    file: str
    line: Optional[int] = None
    column: Optional[int] = None

    def __str__(self) -> str:
        if self.line is None:
            return self.file
        if self.column is None:
            return f"{self.file}:{self.line}"
        return f"{self.file}:{self.line}:{self.column}"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"file": self.file}
        if self.line is not None:
            out["line"] = self.line
        if self.column is not None:
            out["column"] = self.column
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceLocation":
        return cls(file=str(data["file"]), line=data.get("line"), column=data.get("column"))


@dataclass(frozen=True)
class RelatedLocation:
    """Another place that explains a diagnostic, and what it is."""

    location: SourceLocation
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"location": self.location.to_dict(), "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelatedLocation":
        return cls(
            location=SourceLocation.from_dict(data["location"]), message=str(data["message"])
        )


@dataclass(frozen=True)
class Diagnostic:
    """One finding.

    ``code`` is the catalog slug (``ERR_CONTRACT_SCHEMA_TYPE``), ``path`` the RFC 6901
    pointer into the composed contract, ``port`` the id of the expose or consume
    the finding sits under, ``location`` where it was written (``None`` when no
    file answers for it).
    """

    code: str
    severity: str
    message: str
    path: str
    location: Optional[SourceLocation] = None
    related: Tuple[RelatedLocation, ...] = ()
    product: Optional[str] = None
    environment: Optional[str] = None
    port: Optional[str] = None
    fix: Optional[str] = None
    docs_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "location": self.location.to_dict() if self.location else None,
            "related": [item.to_dict() for item in self.related],
            "product": self.product,
            "environment": self.environment,
            "port": self.port,
        }
        if self.fix:
            out["fix"] = self.fix
        if self.docs_url:
            out["docs_url"] = self.docs_url
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Diagnostic":
        location = data.get("location")
        return cls(
            code=str(data["code"]),
            severity=str(data["severity"]),
            message=str(data["message"]),
            path=str(data.get("path", "")),
            location=SourceLocation.from_dict(location) if location else None,
            related=tuple(RelatedLocation.from_dict(item) for item in data.get("related") or ()),
            product=data.get("product"),
            environment=data.get("environment"),
            port=data.get("port"),
            fix=data.get("fix"),
            docs_url=data.get("docs_url"),
        )


@dataclass(frozen=True)
class DiagnosticReport:
    """Every diagnostic of one run, under the model's ``version``."""

    diagnostics: Tuple[Diagnostic, ...] = ()
    version: str = VERSION
    #: Unknown keys a newer minor version wrote, kept so a reader can pass them on.
    extra: Dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "items": [d.to_dict() for d in self.diagnostics]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DiagnosticReport":
        """Read a document of any ``1.x`` version: unknown keys are ignored, never an error."""
        version = str(data.get("version", ""))
        if version.split(".", 1)[0] != VERSION.split(".", 1)[0]:
            raise UnsupportedDiagnosticsVersion(
                f"diagnostics version {version or '(none)'} is not supported: "
                f"this reader reads {VERSION.split('.', 1)[0]}.x"
            )
        known = {"version", "items"}
        items: List[Diagnostic] = [Diagnostic.from_dict(item) for item in data.get("items") or ()]
        return cls(
            diagnostics=tuple(items),
            version=version,
            extra={k: v for k, v in data.items() if k not in known},
        )
