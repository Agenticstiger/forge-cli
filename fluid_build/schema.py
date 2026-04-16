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

# fluid_build/schema.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# --------- Small helpers ---------
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")
_NAME_PATTERN = re.compile(r"^.{1,256}$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_EMAIL_PATTERN = re.compile(r"^[^@]+@[^@]+\.[^@]+$")

_LAYERS = {"Bronze", "Silver", "Gold", "Platinum"}
_PATTERNS = {"declarative", "hybrid-reference", "embedded-logic", "logical-mapping"}


def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _is_obj(x: Any) -> bool:
    return isinstance(x, dict)


def _is_list(x: Any) -> bool:
    return isinstance(x, list)


def _req(obj: Dict[str, Any], key: str, errors: List[str], path: str) -> Any:
    if key not in obj:
        errors.append(f"{path}: missing required property '{key}'")
        return None
    return obj[key]


def _opt(obj: Dict[str, Any], key: str, default=None) -> Any:
    return obj.get(key, default)


def _assert(cond: bool, msg: str, errors: List[str]) -> None:
    if not cond:
        errors.append(msg)


def _check_id(s: Any, path: str, errors: List[str]) -> None:
    _assert(_is_str(s), f"{path} must be string", errors)
    if _is_str(s):
        _assert(bool(_ID_PATTERN.match(s)), f"{path} must match pattern ^[a-zA-Z0-9_.-]+$", errors)


def _check_version(s: Any, path: str, errors: List[str]) -> None:
    _assert(_is_str(s), f"{path} must be string", errors)
    if _is_str(s):
        _assert(bool(_VERSION_PATTERN.match(s)), f"{path} must match semver X.Y[.Z]", errors)


def _check_email(s: Any, path: str, errors: List[str]) -> None:
    _assert(_is_str(s), f"{path} must be string (email)", errors)
    if _is_str(s):
        _assert(bool(_EMAIL_PATTERN.match(s)), f"{path} must be a valid email", errors)


def _check_column(col: Any, path: str, errors: List[str]) -> None:
    _assert(_is_obj(col), f"{path} must be object", errors)
    if not _is_obj(col):
        return
    name = _req(col, "name", errors, f"{path}")
    ctype = _req(col, "type", errors, f"{path}")
    if name is not None:
        _assert(_is_str(name), f"{path}.name must be string", errors)
    if ctype is not None:
        _assert(_is_str(ctype), f"{path}.type must be string", errors)


def _check_location(loc: Any, path: str, errors: List[str]) -> None:
    _assert(_is_obj(loc), f"{path} must be object", errors)
    if not _is_obj(loc):
        return
    fmt = _req(loc, "format", errors, path)
    props = _req(loc, "properties", errors, path)
    if fmt is not None:
        _assert(_is_str(fmt), f"{path}.format must be string", errors)
    if props is not None:
        _assert(_is_obj(props), f"{path}.properties must be object", errors)


def _check_expose(ex: Any, idx: int, errors: List[str]) -> None:
    path = f"exposes[{idx}]"
    _assert(_is_obj(ex), f"{path} must be object", errors)
    if not _is_obj(ex):
        return
    _id = _req(ex, "id", errors, path)
    _type = _req(ex, "type", errors, path)
    _loc = _req(ex, "location", errors, path)
    _schema = _req(ex, "schema", errors, path)

    if _id is not None:
        _check_id(_id, f"{path}.id", errors)
    if _type is not None:
        _assert(_is_str(_type), f"{path}.type must be string", errors)
    if _loc is not None:
        _check_location(_loc, f"{path}.location", errors)
    if _schema is not None:
        _assert(_is_list(_schema), f"{path}.schema must be array", errors)
        if _is_list(_schema):
            for i, col in enumerate(_schema):
                _check_column(col, f"{path}.schema[{i}]", errors)


def _check_consumes(consumes: Any, errors: List[str]) -> None:
    _assert(_is_list(consumes), "consumes must be an array", errors)
    if not _is_list(consumes):
        return
    for i, c in enumerate(consumes):
        path = f"consumes[{i}]"
        _assert(_is_obj(c), f"{path} must be object", errors)
        if not _is_obj(c):
            continue
        cid = _req(c, "id", errors, path)
        ref = _req(c, "ref", errors, path)
        if cid is not None:
            _check_id(cid, f"{path}.id", errors)
        if ref is not None:
            _assert(_is_str(ref), f"{path}.ref must be string", errors)


def _check_metadata(md: Any, errors: List[str]) -> None:
    _assert(_is_obj(md), "metadata must be object", errors)
    if not _is_obj(md):
        return
    layer = _req(md, "layer", errors, "metadata")
    owner = _req(md, "owner", errors, "metadata")
    if layer is not None:
        _assert(layer in _LAYERS, f"metadata.layer must be one of {sorted(_LAYERS)}", errors)
    if owner is not None:
        if _is_str(owner):
            _check_email(owner, "metadata.owner", errors)
        elif _is_obj(owner):
            team = _req(owner, "team", errors, "metadata.owner")
            if team is not None:
                _assert(_is_str(team), "metadata.owner.team must be string", errors)
            email = _opt(owner, "email")
            if email is not None:
                _check_email(email, "metadata.owner.email", errors)
        else:
            _assert(False, "metadata.owner must be string (email) or object", errors)


def _check_build(build: Any, errors: List[str]) -> None:
    _assert(_is_obj(build), "build must be object", errors)
    if not _is_obj(build):
        return
    tr = _req(build, "transformation", errors, "build")
    if tr is None or not _is_obj(tr):
        return
    pattern = _req(tr, "pattern", errors, "build.transformation")
    engine = _req(tr, "engine", errors, "build.transformation")
    props = _req(tr, "properties", errors, "build.transformation")
    if pattern is not None:
        _assert(
            pattern in _PATTERNS,
            f"build.transformation.pattern must be one of {sorted(_PATTERNS)}",
            errors,
        )
    if engine is not None:
        _assert(_is_str(engine), "build.transformation.engine must be string", errors)
    if props is not None:
        _assert(_is_obj(props), "build.transformation.properties must be object", errors)

    # Conditional validation (the v0.4.0 if/then semantics)
    if _is_str(pattern) and _is_obj(props):
        if pattern == "hybrid-reference":
            model = _req(props, "model", errors, "build.transformation.properties")
            if model is not None:
                _assert(
                    _is_str(model), "build.transformation.properties.model must be string", errors
                )
            vars_ = _opt(props, "vars")
            if vars_ is not None:
                _assert(
                    _is_obj(vars_), "build.transformation.properties.vars must be object", errors
                )
        elif pattern == "declarative":
            # Optional keys but types must be correct if present
            if "from" in props:
                _assert(_is_str(props["from"]), "declarative.from must be string", errors)
            if "joins" in props:
                _assert(_is_list(props["joins"]), "declarative.joins must be array", errors)
            if "filters" in props:
                _assert(_is_list(props["filters"]), "declarative.filters must be array", errors)
            if "select" in props:
                _assert(_is_list(props["select"]), "declarative.select must be array", errors)
        elif pattern == "embedded-logic":
            sql = _req(props, "sql", errors, "build.transformation.properties")
            if sql is not None:
                _assert(_is_str(sql), "embedded-logic.sql must be string", errors)
            lang = _opt(props, "language")
            if lang is not None:
                _assert(
                    lang in {"sql", "flink_sql", "pyspark", "scala"},
                    "embedded-logic.language invalid",
                    errors,
                )
        elif pattern == "logical-mapping":
            srcs = _req(props, "sources", errors, "build.transformation.properties")
            steps = _req(props, "steps", errors, "build.transformation.properties")
            if srcs is not None:
                _assert(_is_list(srcs), "logical-mapping.sources must be array", errors)
            if steps is not None:
                _assert(_is_list(steps), "logical-mapping.steps must be array", errors)


# --------- Public API used by CLI ---------
def validate_contract(contract: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate a FLUID v0.4.0 contract.

    Returns:
        (ok, error_string_or_none)
    """
    errors: List[str] = []

    # Top-level required
    fv = _req(contract, "fluidVersion", errors, "$")
    k = _req(contract, "kind", errors, "$")
    cid = _req(contract, "id", errors, "$")
    name = _req(contract, "name", errors, "$")
    domain = _req(contract, "domain", errors, "$")
    md = _req(contract, "metadata", errors, "$")
    exposes = _req(contract, "exposes", errors, "$")

    # Types & patterns
    if fv is not None:
        _check_version(fv, "fluidVersion", errors)
    if k is not None:
        _assert(_is_str(k), "kind must be string", errors)
    if cid is not None:
        _check_id(cid, "id", errors)
    if name is not None:
        _assert(_is_str(name), "name must be string", errors)
        if _is_str(name):
            _assert(bool(_NAME_PATTERN.match(name)), "name must be 1..256 chars", errors)
    if domain is not None:
        _check_id(domain, "domain", errors)
    if md is not None:
        _check_metadata(md, errors)

    # consumes (optional)
    consumes = _opt(contract, "consumes")
    if consumes is not None:
        _check_consumes(consumes, errors)

    # build (optional but common)
    build = _opt(contract, "build")
    if build is not None:
        _check_build(build, errors)

    # exposes (required)
    if exposes is not None:
        _assert(_is_list(exposes), "exposes must be array", errors)
        if _is_list(exposes):
            if len(exposes) == 0:
                errors.append("exposes must contain at least one item")
            for i, ex in enumerate(exposes):
                _check_expose(ex, i, errors)

    if errors:
        # Keep message compact but complete (one-per-line)
        return False, "\n".join(errors[:200])  # hard cap to avoid flooding
    return True, None
