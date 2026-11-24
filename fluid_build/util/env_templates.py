# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One reading of ``{{ env.NAME }}`` for every stage that renders a contract.

Four resolvers used to read the placeholder and disagreed about a variable that
is not set: one left the text as written, two rendered nothing, one refused. A
contract then deployed to a bucket named ``{{ env.S3_BUCKET }}``, or to a path
missing its first half, and the command exited 0. This is the one place that
decides.

The rules, for every caller:

* A name is **missing** when the variable is unset or empty. :func:`render_contract`
  raises :class:`EnvTemplateError`, which lists every missing name and where it is
  written, before anything is deployed.
* A stage that deliberately leaves the placeholder for a later one says so by
  name: ``unset=UNSET_DEFER``. The call sites that do are the publish and export
  paths (a catalog shows the template), the preload of a contract embedded in a
  plan (each build renders it strictly when it runs), and the validation report.
  ``tests/test_env_templates_defer_sites.py`` pins the list, so nothing passes
  through unresolved by accident.
* A credential-shaped name (:func:`~fluid_build.observability.secret_redactor.is_sensitive_key_name`)
  is never rendered, whatever the mode: the rendered contract is written to
  ``main.tf.json``, to state, to a table COMMENT or to a catalog. It stays as
  written, with one warning per name, and is not "missing": policy refuses it.

Borrowed from the strict modes of ``envsubst -no-unset`` (collect every missing
name, then fail), Docker Compose's ``${VAR:?}`` (unset or empty is an error) and
Flux's ``--strict``; their "resolved later is always an explicit marker" is the
named ``UNSET_DEFER``. Stdlib only, so the light CLI path may import it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from fluid_build._errors import FluidUserError, doc_url

#: ``{{ env.NAME }}`` in a string. The one pattern: it takes any run of
#: non-space characters as the name, so a name that is not an identifier is
#: looked up (and reported) as written instead of passing through unseen.
ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.(\S+?)\s*\}\}")

#: An unset or empty variable is an error (:class:`EnvTemplateError`).
UNSET_ERROR = "error"
#: The stage leaves the placeholder of an unset or empty variable as written, for a later stage
#: (which refuses it, if it is the one that deploys).
UNSET_DEFER = "defer"

#: A credential-shaped name is left as written (the default: a rendered contract is written to files,
#: state and catalogs).
SENSITIVE_REFUSE = "refuse"
#: A credential-shaped name renders like any other. For the build runners, which hand a connection's
#: ``password: "{{ env.PGPASSWORD }}"`` to the engine that connects; where a rendered value may go is the
#: secret policy's to judge there.
SENSITIVE_RENDER = "render"

#: How many field paths the error names before it counts the rest.
_PATHS_SHOWN = 6

_DEFAULT_LOG = logging.getLogger("fluid.env_templates")


@dataclass
class EnvTemplateError(FluidUserError):
    """``{{ env.NAME }}`` names variables that are unset or empty here."""

    code: str = "EnvTemplateError"


def _missing_error(fields: Mapping[str, List[str]], stage: Optional[str]) -> EnvTemplateError:
    names = sorted(fields)
    shown: List[str] = []
    for name in names:
        for place in fields[name][:2]:
            shown.append(f"{name} at {place}" if place else name)
    total = sum(len(places) for places in fields.values())
    if len(shown) > _PATHS_SHOWN:
        shown = shown[:_PATHS_SHOWN] + [f"and {total - _PATHS_SHOWN} more places"]
    return EnvTemplateError(
        what=(
            f"The contract needs {'an environment variable that is' if len(names) == 1 else 'environment variables that are'} "
            f"not set or empty here: {', '.join(names)}"
        ),
        where="; ".join(shown) or None,
        why=(
            "{{ env.NAME }} renders from the environment of the process that runs this "
            f"command{f' ({stage})' if stage else ''}. With no value it would deploy a name "
            "the contract never wrote (an empty one, or the literal placeholder) and still "
            "exit 0."
        ),
        fix=(
            f"Export {', '.join(names)} before the command (an empty value counts as unset), "
            "or correct the placeholder in the contract."
        ),
        doc=doc_url(),
        extras={
            "missing": names,
            "fields": {name: list(places) for name, places in fields.items()},
        },
    )


def placeholder_names(value: Any) -> List[str]:
    """The variable names ``value`` names, each once, in the order first written.

    Walks strings, lists and dicts; anything else names nothing.
    """
    found: Dict[str, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if "{{" in node:
                for name in ENV_TEMPLATE_RE.findall(node):
                    found.setdefault(name.strip(), None)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(value)
    return list(found)


def render_text(
    text: str, environ: Optional[Mapping[str, str]] = None, *, strip: bool = True
) -> str:
    """``text`` with each ``{{ env.NAME }}`` replaced by the variable's value, leaving the
    placeholder of an unset variable as written (the :data:`UNSET_DEFER` rule).

    The single-string primitive for a caller that decides for itself what an
    The single-string primitive for a caller that decides for itself what an
    unrendered placeholder means (a provider's optional field with a default). A
    string with ``{{`` in it comes back stripped, as it always did, unless ``strip``
    is false.
    """
    if not isinstance(text, str) or "{{" not in text:
        return text
    env = os.environ if environ is None else environ

    def sub(match: "re.Match[str]") -> str:
        value = env.get(match.group(1).strip())
        return match.group(0) if value is None else value

    out = ENV_TEMPLATE_RE.sub(sub, text)
    return out.strip() if strip else out


def render_contract(
    value: Any,
    *,
    unset: str = UNSET_ERROR,
    sensitive: str = SENSITIVE_REFUSE,
    environ: Optional[Mapping[str, str]] = None,
    purpose: Optional[str] = None,
    stage: Optional[str] = None,
    strip: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """``value`` with every ``{{ env.NAME }}`` in its strings rendered, through lists and dicts.

    ``unset`` says what an unset or empty variable is: :data:`UNSET_ERROR` (the
    default) raises one :class:`EnvTemplateError` naming every missing variable
    and the fields that write it; :data:`UNSET_DEFER` leaves the placeholder as
    written. Nothing else decides: the caller names the mode.

    A credential-shaped name is never rendered in either ``unset`` mode unless the
    caller says ``sensitive=SENSITIVE_RENDER`` (see the module docstring); the
    warning says where the rendered ``value`` goes, as ``purpose``. ``stage`` names the command for the error. ``strip`` keeps the
    historical ``.strip()`` of a string that holds ``{{``; a caller that renders
    paths or SQL turns it off. ``value`` is not changed.
    """
    if unset not in (UNSET_ERROR, UNSET_DEFER):
        raise ValueError(f"unset must be {UNSET_ERROR!r} or {UNSET_DEFER!r}, not {unset!r}")
    if sensitive not in (SENSITIVE_REFUSE, SENSITIVE_RENDER):
        raise ValueError(
            f"sensitive must be {SENSITIVE_REFUSE!r} or {SENSITIVE_RENDER!r}, not {sensitive!r}"
        )
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    env = os.environ if environ is None else environ
    log = logger or _DEFAULT_LOG
    warned: set[str] = set()
    missing: Dict[str, List[str]] = {}

    def refuse_sensitive(name: str) -> None:
        if name in warned:
            return
        warned.add(name)
        log.warning(
            "Refusing to resolve sensitive-looking env placeholder '{{ env.%s }}' in contract "
            "body; leaving it literal. %s: a credential must not ride along. If this value is "
            "not a secret, rename the variable outside the password/secret/token/key family.",
            name,
            purpose or "The rendered contract is written to files, state and catalogs",
        )

    def render(text: str, where: str) -> str:
        if "{{" not in text:
            return text

        def sub(match: "re.Match[str]") -> str:
            name = match.group(1).strip()
            if sensitive == SENSITIVE_REFUSE and is_sensitive_key_name(name):
                refuse_sensitive(name)
                return match.group(0)
            rendered = env.get(name)
            if rendered:
                return rendered
            missing.setdefault(name, []).append(where)
            return match.group(0)

        out = ENV_TEMPLATE_RE.sub(sub, text)
        return out.strip() if strip else out

    def walk(node: Any, where: str) -> Any:
        if isinstance(node, dict):
            return {
                key: walk(item, f"{where}.{key}" if where else str(key))
                for key, item in node.items()
            }
        if isinstance(node, list):
            return [walk(item, f"{where}[{index}]") for index, item in enumerate(node)]
        if isinstance(node, str):
            return render(node, where)
        return node

    rendered_value = walk(value, "")
    if missing and unset == UNSET_ERROR:
        raise _missing_error(missing, stage)
    return rendered_value


def missing_fields(value: Any, environ: Optional[Mapping[str, str]] = None) -> Dict[str, List[str]]:
    """The variables ``value`` names that are unset or empty here, each with the fields that
    write it (``exposes[0].binding.location.bucket``), sorted by name.

    Credential-shaped names are left out: they are never rendered, so they are
    not waiting for a value. What ``fluid validate`` warns about; the same
    variables :func:`render_contract` refuses.
    """
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    env = os.environ if environ is None else environ
    found: Dict[str, List[str]] = {}

    def walk(node: Any, where: str) -> None:
        if isinstance(node, str):
            if "{{" in node:
                for raw in ENV_TEMPLATE_RE.findall(node):
                    name = raw.strip()
                    if not is_sensitive_key_name(name) and not env.get(name):
                        found.setdefault(name, []).append(where)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(item, f"{where}.{key}" if where else str(key))

    walk(value, "")
    return {name: found[name] for name in sorted(found)}


__all__ = [
    "ENV_TEMPLATE_RE",
    "EnvTemplateError",
    "SENSITIVE_REFUSE",
    "SENSITIVE_RENDER",
    "UNSET_DEFER",
    "UNSET_ERROR",
    "missing_fields",
    "placeholder_names",
    "render_contract",
    "render_text",
]
