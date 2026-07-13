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

"""Stackable prompt overlays for the LLM-backed Forge copilot.

An *overlay* is a small, declarative YAML file that patches one or more
labelled prompt sections (``replace`` / ``append`` / ``prepend``) and may
ship extra ``validator_rules`` that the generated contract must satisfy.
Unlike a ``--prompt-profile`` (single-name, single-swap directory) overlays
**stack left-to-right**::

    fluid forge --prompt-overlay base-hardening,pii-lockdown,tenant-a

Prior art / borrowed shapes (see PR body for receipts):

* **Kustomize overlays / Helm values layering** — the base+overlay model
  where each overlay is a partial patch and later overlays win on conflict.
  We adopt the same ``replace`` (scalar swap) / ``append`` semantics and add
  ``prepend`` (Kustomize < v3.8.0's array-prepend behaviour) as an explicit
  mode rather than a version quirk.
* **ed25519 detached signatures (`cryptography`)** — the same 32-byte key /
  64-byte signature primitive used elsewhere in this repo's crypto surface.
* **Layered-prompt injection defence (OWASP LLM cheat-sheet, MS/Mindgard)** —
  the *anchor-sentence integrity check*: trusted, load-bearing directives are
  fingerprinted and re-checked after untrusted layers compose, and the compose
  is rejected if a layer dropped one. This is the anti-malicious-overlay guard.

Security posture (untrusted input):

* Overlay files are treated as **untrusted**. Names are slug-validated and the
  resolved realpath must sit directly under a known overlays directory
  (path-traversal containment, mirroring the #359 profile loader).
* YAML is parsed with :func:`fluid_build.util.safe_yaml.load_yaml_safe`
  (``yaml.safe_load`` + billion-laughs alias cap) — never ``yaml.load``.
* Section ids are validated against :class:`SectionId`; an unknown id fails
  loudly at load rather than silently no-op'ing.
* ``FLUID_OVERLAY_STRICT=1`` rejects any overlay without a valid ed25519
  signature. A *present-but-invalid* signature is rejected in every mode
  (tamper detection), strict or not.
* The anchor guard rejects any overlay stack whose composition drops a
  load-bearing anchor sentence (e.g. "Return strict JSON only.").
"""

from __future__ import annotations

__all__ = [
    "ANCHOR_SENTENCES",
    "OverlaySection",
    "PromptOverlay",
    "PromptOverlayError",
    "SectionId",
    "ValidatorRule",
    "activate_prompt_overlays",
    "apply_overlays_to_guidance",
    "available_prompt_overlays",
    "clear_trusted_overlay_keys",
    "enforce_anchor_integrity",
    "load_overlay",
    "overlay_stack_fingerprint",
    "overlay_validator_rule_dicts",
    "register_trusted_overlay_key",
    "resolve_overlay_names",
    "sign_overlay_dict",
]

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.util.safe_yaml import load_yaml_safe

_LOG = logging.getLogger("fluid.cli.forge.prompt_overlays")

# Bundled overlays ship next to the profiles under agent_specs/.
_OVERLAYS_DIR: Path = Path(__file__).with_name("agent_specs") / "prompt_overlays"

# Overlay names must be simple slugs — no path separators, no traversal, no
# leading dot/underscore. First line of defence against
# ``--prompt-overlay ../../etc``; :func:`_resolve_overlay_path` adds a
# resolved-realpath containment check as the second.
_OVERLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_ALLOWED_MODES = ("replace", "append", "prepend")

# The load-bearing anchor sentences that MUST survive overlay composition.
# These live in the overlay-reachable default guidance sections (see
# ``forge_copilot_prompts`` — the strict-JSON / no-secrets directives are
# loaded from ``_defaults/response_contract.yaml``, and the restrictive
# agent-policy default). A malicious overlay that ``replace``s a section and
# drops one of these is rejected by :func:`enforce_anchor_integrity`.
ANCHOR_SENTENCES: Tuple[str, ...] = (
    "Return strict JSON only.",
    "Never include secrets, access tokens, raw sample values, or verbatim file contents.",
    "Default to restrictive settings: canStore=false",
)


class SectionId(str, Enum):
    """Stable identifiers for the overlay-addressable prompt sections.

    The *value* of each member is the guidance-map key it patches (see
    ``forge_copilot_prompts._active_guidance``), so an overlay section id maps
    one-to-one onto a composable guidance block. The enum is the contract: an
    overlay referencing an id not in this set is rejected at load time.
    """

    SOVEREIGNTY = "sovereignty"
    AGENT_POLICY = "agent_policy"
    UPSTREAM_SQL = "upstream_sql"
    TECHNIQUE_MANDATE = "technique_mandate"
    RESPONSE_CONTRACT = "response_contract"
    CLARIFICATION = "clarification"
    EVALUATION = "evaluation"


_VALID_SECTION_IDS = frozenset(item.value for item in SectionId)


class PromptOverlayError(ValueError):
    """Raised for an unknown / unsafe / rejected prompt overlay.

    Covers: an unsafe name or traversal escape, an unknown overlay, a malformed
    overlay file, a dropped anchor sentence, and a signature policy violation
    (unsigned under strict mode, or a tampered signature in any mode). The CLI
    turns this into a clear, non-silent error — never a fall-back to defaults.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverlaySection:
    """One patch operation against a labelled prompt section."""

    section_id: str
    mode: str
    text: str

    def as_tuple(self) -> Tuple[str, str, str]:
        return (self.section_id, self.mode, self.text)


@dataclass(frozen=True)
class ValidatorRule:
    """An overlay-supplied post-generation check on the emitted contract.

    Exactly the predicates that are set are enforced. All are evaluated against
    the emitted contract: ``*_regex`` against its canonical JSON serialisation,
    ``*_field`` against a dotted-path lookup. A rule with no predicate is a
    load-time error (a rule that can never fire is almost certainly a mistake).
    """

    id: str
    message: str
    forbid_regex: Optional[str] = None
    require_regex: Optional[str] = None
    require_field: Optional[str] = None
    forbid_field: Optional[str] = None

    def as_dict(self) -> Dict[str, str]:
        out: Dict[str, str] = {"id": self.id, "message": self.message}
        for key in ("forbid_regex", "require_regex", "require_field", "forbid_field"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    def as_tuple(self) -> Tuple:
        return (
            self.id,
            self.message,
            self.forbid_regex or "",
            self.require_regex or "",
            self.require_field or "",
            self.forbid_field or "",
        )


@dataclass(frozen=True)
class PromptOverlay:
    """A loaded, validated overlay ready to stack."""

    name: str
    sections: Tuple[OverlaySection, ...] = ()
    validator_rules: Tuple[ValidatorRule, ...] = ()
    signed: bool = False
    digest: str = ""

    def as_tuple(self) -> Tuple:
        """Canonical, order-stable tuple used for the stack fingerprint."""
        return (
            self.name,
            tuple(s.as_tuple() for s in self.sections),
            tuple(r.as_tuple() for r in self.validator_rules),
        )


# ---------------------------------------------------------------------------
# Loading (path-traversal-safe, safe_load only)
# ---------------------------------------------------------------------------


def _overlay_search_dirs() -> List[Path]:
    """Overlay directories in priority order (user-home shadow, then bundled).

    A tenant can drop ``<name>.yaml`` into ``<user-home>/agent_specs/
    prompt_overlays/`` to add an overlay without forking the package, mirroring
    the per-tenant ``_defaults`` shadow. Only existing directories are returned.
    """
    dirs: List[Path] = []
    # Function-local import keeps ``paths`` off any cold-path import graph.
    from fluid_build.paths import user_home

    user_dir = user_home() / "agent_specs" / "prompt_overlays"
    if user_dir.is_dir():
        dirs.append(user_dir)
    if _OVERLAYS_DIR.is_dir():
        dirs.append(_OVERLAYS_DIR)
    return dirs


def available_prompt_overlays() -> List[str]:
    """Return the sorted names of discoverable overlays (bundled + user-home)."""
    names: set = set()
    for directory in _overlay_search_dirs():
        for path in directory.glob("*.yaml"):
            stem = path.stem
            if _OVERLAY_NAME_RE.match(stem) and not stem.startswith((".", "_")):
                names.add(stem)
    return sorted(names)


def _resolve_overlay_path(name: str) -> Path:
    """Validate *name* and resolve it to a contained ``<dir>/<name>.yaml``.

    Raises :class:`PromptOverlayError` on an unsafe name, a resolved path that
    escapes its overlays directory (symlink / traversal), or an unknown
    overlay. Never silently falls back.
    """
    if not name or not _OVERLAY_NAME_RE.match(name):
        raise PromptOverlayError(
            f"invalid prompt overlay name {name!r}: names must match "
            r"[A-Za-z0-9][A-Za-z0-9._-]* (no path separators or traversal)"
        )
    for directory in _overlay_search_dirs():
        try:
            root = directory.resolve()
        except OSError:
            continue
        candidate = directory / f"{name}.yaml"
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        # Defence in depth: the resolved file must sit directly under the
        # overlays root. Rejects a symlinked file that would otherwise turn the
        # overlay dir into an arbitrary-file-read primitive.
        if resolved.parent != root:
            raise PromptOverlayError(
                f"prompt overlay {name!r} escapes the overlays directory {root}"
            )
        return resolved
    available = ", ".join(available_prompt_overlays()) or "(none bundled)"
    raise PromptOverlayError(
        f"unknown prompt overlay {name!r}. Available overlays: {available}. "
        f"Add one under {_OVERLAYS_DIR}{Path('/')}<name>.yaml."
    )


def _parse_sections(raw: object, *, name: str) -> Tuple[OverlaySection, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise PromptOverlayError(f"overlay {name!r}: 'sections' must be a list.")
    parsed: List[OverlaySection] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PromptOverlayError(f"overlay {name!r}: section[{index}] must be a mapping.")
        section_id = str(item.get("section") or "").strip()
        mode = str(item.get("mode") or "").strip().lower()
        text = item.get("text")
        if section_id not in _VALID_SECTION_IDS:
            raise PromptOverlayError(
                f"overlay {name!r}: section[{index}] has unknown section id "
                f"{section_id!r}. Allowed: {sorted(_VALID_SECTION_IDS)}."
            )
        if mode not in _ALLOWED_MODES:
            raise PromptOverlayError(
                f"overlay {name!r}: section[{index}] mode {mode!r} must be one of "
                f"{list(_ALLOWED_MODES)}."
            )
        if not isinstance(text, str):
            raise PromptOverlayError(f"overlay {name!r}: section[{index}] 'text' must be a string.")
        parsed.append(OverlaySection(section_id=section_id, mode=mode, text=text))
    return tuple(parsed)


def _parse_validator_rules(raw: object, *, name: str) -> Tuple[ValidatorRule, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise PromptOverlayError(f"overlay {name!r}: 'validator_rules' must be a list.")
    parsed: List[ValidatorRule] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PromptOverlayError(
                f"overlay {name!r}: validator_rules[{index}] must be a mapping."
            )
        rule_id = str(item.get("id") or "").strip()
        message = str(item.get("message") or "").strip()
        if not rule_id or not message:
            raise PromptOverlayError(
                f"overlay {name!r}: validator_rules[{index}] needs an 'id' and a 'message'."
            )
        predicates = {
            "forbid_regex": item.get("forbid_regex"),
            "require_regex": item.get("require_regex"),
            "require_field": item.get("require_field"),
            "forbid_field": item.get("forbid_field"),
        }
        clean = {
            k: str(v).strip() for k, v in predicates.items() if isinstance(v, str) and v.strip()
        }
        if not clean:
            raise PromptOverlayError(
                f"overlay {name!r}: validator_rules[{index}] ({rule_id!r}) needs at least one of "
                "forbid_regex / require_regex / require_field / forbid_field."
            )
        # Compile regex predicates now so a malformed pattern fails at load,
        # not deep inside the generation loop.
        for key in ("forbid_regex", "require_regex"):
            if key in clean:
                try:
                    re.compile(clean[key])
                except re.error as exc:
                    raise PromptOverlayError(
                        f"overlay {name!r}: validator_rules[{index}] ({rule_id!r}) has an "
                        f"invalid {key} pattern: {exc}"
                    ) from exc
        parsed.append(ValidatorRule(id=rule_id, message=message, **clean))
    return tuple(parsed)


def load_overlay(name: str) -> PromptOverlay:
    """Load, validate, and (optionally) signature-check a single overlay.

    Path-traversal-safe name resolution, ``safe_load``-only parsing, section-id
    validation, and regex pre-compilation all happen here. The *signature*
    block is parsed but its policy verdict (accept / reject) is applied by
    :func:`activate_prompt_overlays`, which knows the strict-mode flag.
    """
    path = _resolve_overlay_path(name)
    try:
        raw = load_yaml_safe(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptOverlayError(f"overlay {name!r}: could not read {path}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — UnsafeYamlError / YAMLError
        raise PromptOverlayError(f"overlay {name!r}: invalid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PromptOverlayError(f"overlay {name!r}: top level must be a mapping.")

    declared_name = str(raw.get("name") or name).strip() or name
    sections = _parse_sections(raw.get("sections"), name=name)
    rules = _parse_validator_rules(raw.get("validator_rules"), name=name)
    if not sections and not rules:
        raise PromptOverlayError(
            f"overlay {name!r} is empty: it must declare at least one section or validator_rule."
        )

    signed, digest = _verify_overlay_signature(raw, overlay_name=name)
    return PromptOverlay(
        name=declared_name,
        sections=sections,
        validator_rules=rules,
        signed=signed,
        digest=digest,
    )


# ---------------------------------------------------------------------------
# ed25519 signing
# ---------------------------------------------------------------------------
#
# Trusted public keys are supplied out-of-band (they are NOT read from the
# overlay file — that would let an attacker sign with their own key). Sources,
# highest priority first:
#   1. process registry via ``register_trusted_overlay_key`` (tests / embedders)
#   2. ``FLUID_OVERLAY_PUBLIC_KEYS`` env: comma-separated ``keyid=<b64>`` or
#      bare ``<b64>`` entries (a bare entry matches any key_id).
_TRUSTED_KEYS: Dict[Optional[str], List[bytes]] = {}


def register_trusted_overlay_key(public_key_bytes: bytes, *, key_id: Optional[str] = None) -> None:
    """Register a trusted ed25519 public key (32 raw bytes) for verification."""
    _TRUSTED_KEYS.setdefault(key_id, [])
    if public_key_bytes not in _TRUSTED_KEYS[key_id]:
        _TRUSTED_KEYS[key_id].append(public_key_bytes)


def clear_trusted_overlay_keys() -> None:
    """Drop all process-registered trusted keys (test isolation hook)."""
    _TRUSTED_KEYS.clear()


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _env_trusted_keys() -> Dict[Optional[str], List[bytes]]:
    raw = os.environ.get("FLUID_OVERLAY_PUBLIC_KEYS", "").strip()
    out: Dict[Optional[str], List[bytes]] = {}
    if not raw:
        return out
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key_id: Optional[str]
        if "=" in entry:
            key_id, b64 = entry.split("=", 1)
            key_id = key_id.strip() or None
        else:
            key_id, b64 = None, entry
        try:
            out.setdefault(key_id, []).append(_b64decode(b64.strip()))
        except Exception:  # noqa: BLE001 — a malformed env key is simply skipped
            _LOG.warning("Skipping malformed FLUID_OVERLAY_PUBLIC_KEYS entry")
    return out


def _candidate_keys(key_id: Optional[str]) -> List[bytes]:
    """Return every trusted public key eligible to verify a *key_id* signature.

    An untagged trusted key (registered with ``key_id=None`` / a bare env
    entry) is eligible for any signature; a tagged key only for its own id.
    """
    merged: Dict[Optional[str], List[bytes]] = {}
    for source in (_env_trusted_keys(), _TRUSTED_KEYS):
        for kid, keys in source.items():
            merged.setdefault(kid, [])
            for key in keys:
                if key not in merged[kid]:
                    merged[kid].append(key)
    candidates: List[bytes] = []
    if key_id is not None and key_id in merged:
        candidates.extend(merged[key_id])
    candidates.extend(merged.get(None, []))
    return candidates


def _canonical_overlay_bytes(raw: Mapping) -> bytes:
    """Deterministic signable payload: the overlay body minus its signature."""
    body = {k: v for k, v in raw.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _verify_overlay_signature(raw: Mapping, *, overlay_name: str) -> Tuple[bool, str]:
    """Verify the overlay's signature block if present.

    Returns ``(signed, digest)`` where *digest* is a sha1 of the canonical
    signable payload (used in provenance / logging). Raises
    :class:`PromptOverlayError` when a signature block is present but does not
    verify against any trusted key (tamper detection — enforced in every mode).
    Absence of a signature block is allowed here; the strict-mode gate lives in
    :func:`activate_prompt_overlays`.
    """
    payload = _canonical_overlay_bytes(raw)
    digest = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
    sig = raw.get("signature")
    if not isinstance(sig, Mapping) or not sig.get("value"):
        return False, digest

    algo = str(sig.get("algorithm") or "ed25519").strip().lower()
    if algo != "ed25519":
        raise PromptOverlayError(
            f"overlay {overlay_name!r}: unsupported signature algorithm {algo!r} (only ed25519)."
        )
    try:
        sig_bytes = _b64decode(str(sig["value"]))
    except Exception as exc:  # noqa: BLE001
        raise PromptOverlayError(
            f"overlay {overlay_name!r}: signature value is not valid base64."
        ) from exc

    key_id = sig.get("key_id")
    key_id = str(key_id).strip() if isinstance(key_id, str) and key_id.strip() else None
    candidates = _candidate_keys(key_id)
    if not candidates:
        raise PromptOverlayError(
            f"overlay {overlay_name!r}: no trusted overlay public key registered for "
            f"key_id {key_id!r} (set FLUID_OVERLAY_PUBLIC_KEYS or register_trusted_overlay_key)."
        )

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    for pub in candidates:
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig_bytes, payload)
            return True, digest
        except InvalidSignature:
            continue
        except Exception:  # noqa: BLE001 — malformed key bytes, try the next
            continue
    raise PromptOverlayError(
        f"overlay {overlay_name!r}: signature verification failed "
        "(tampered overlay or wrong signing key)."
    )


def sign_overlay_dict(
    body: Mapping, private_key, *, key_id: Optional[str] = None
) -> Dict[str, object]:
    """Return *body* with an ed25519 ``signature`` block appended.

    Helper for tooling / tests: signs the canonical payload (body minus any
    existing signature) and returns a new dict ready to dump to YAML.
    *private_key* is a ``cryptography`` ``Ed25519PrivateKey``.
    """
    signable = {k: v for k, v in body.items() if k != "signature"}
    payload = json.dumps(
        signable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    signature = private_key.sign(payload)
    signed = dict(signable)
    signed["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


# ---------------------------------------------------------------------------
# Composition + anchor integrity
# ---------------------------------------------------------------------------


def apply_overlays_to_guidance(
    base_map: Mapping[str, str], overlays: Sequence[PromptOverlay]
) -> Dict[str, str]:
    """Apply the overlay stack left-to-right onto a copy of *base_map*.

    Each overlay sees the result of the previous one, so the last overlay wins
    on ``replace`` conflicts and ``append`` / ``prepend`` accumulate in order.
    """
    merged: Dict[str, str] = dict(base_map)
    for overlay in overlays:
        for section in overlay.sections:
            current = merged.get(section.section_id, "")
            if section.mode == "replace":
                merged[section.section_id] = section.text
            elif section.mode == "append":
                merged[section.section_id] = current + section.text
            elif section.mode == "prepend":
                merged[section.section_id] = section.text + current
    return merged


def enforce_anchor_integrity(
    base_map: Mapping[str, str],
    overlaid_map: Mapping[str, str],
    anchors: Sequence[str] = ANCHOR_SENTENCES,
) -> None:
    """Reject the stack if it dropped a load-bearing anchor sentence.

    Compares the concatenated section text before and after overlay
    composition: any anchor present in the base but missing after overlays is a
    dropped load-bearing instruction (the classic malicious-overlay move) and
    raises :class:`PromptOverlayError`.
    """
    base_blob = "\n".join(base_map.get(key, "") for key in sorted(base_map))
    over_blob = "\n".join(overlaid_map.get(key, "") for key in sorted(overlaid_map))
    dropped = [a for a in anchors if a in base_blob and a not in over_blob]
    if dropped:
        raise PromptOverlayError(
            "prompt overlay dropped load-bearing anchor sentence(s): "
            + "; ".join(repr(a) for a in dropped)
            + ". An overlay may add or reinforce guidance but must not remove a "
            "load-bearing directive."
        )


def overlay_stack_fingerprint(overlays: Sequence[PromptOverlay]) -> str:
    """Return ``SHA1`` over the (order-preserving) sorted overlay tuples.

    Empty stack ⇒ ``""`` so the runtime cache key equals the legacy key and no
    cache is invalidated for existing users. Order is captured via the position
    index so ``a,b`` and ``b,a`` (which compose differently under ``replace``)
    fingerprint differently, while the sort keeps the digest deterministic.
    """
    if not overlays:
        return ""
    indexed = sorted((index, overlay.as_tuple()) for index, overlay in enumerate(overlays))
    blob = json.dumps(indexed, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Activation (orchestrates load → signature policy → anchor guard → state)
# ---------------------------------------------------------------------------


def resolve_overlay_names(spec: object) -> List[str]:
    """Flatten a CLI/env overlay spec into an ordered list of names.

    Accepts ``None``, a comma-separated string, or a list of (possibly
    comma-separated) strings (argparse ``append``). Order is preserved;
    duplicates are dropped keeping first occurrence.
    """
    tokens: List[str] = []
    items: Sequence
    if spec is None:
        return []
    if isinstance(spec, str):
        items = [spec]
    elif isinstance(spec, (list, tuple)):
        items = spec
    else:
        return []
    for item in items:
        if not isinstance(item, str):
            continue
        for part in item.split(","):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return tokens


def _strict_mode_enabled(strict: Optional[bool]) -> bool:
    if strict is not None:
        return strict
    return os.environ.get("FLUID_OVERLAY_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def activate_prompt_overlays(
    spec: object, *, strict: Optional[bool] = None
) -> Tuple[PromptOverlay, ...]:
    """Load, policy-check, anchor-guard, and activate an overlay stack.

    *spec* is the raw CLI/env value (string, list, or ``None``). Returns the
    activated overlay tuple (empty when *spec* resolves to nothing). Raises
    :class:`PromptOverlayError` on any load / signature / anchor failure —
    fail-fast, before any prompt is built or contract written.

    Activation order matters: call this AFTER ``set_prompt_profile`` so the
    anchor guard's base guidance already reflects the active profile.
    """
    from fluid_build.cli import forge_copilot_prompts as prompts

    names = resolve_overlay_names(spec)
    if not names:
        prompts.set_prompt_overlays((), "")
        return ()

    strict_on = _strict_mode_enabled(strict)
    overlays: List[PromptOverlay] = []
    for name in names:
        overlay = load_overlay(name)
        if strict_on and not overlay.signed:
            raise PromptOverlayError(
                f"overlay {name!r} is unsigned and FLUID_OVERLAY_STRICT=1 requires a valid "
                "ed25519 signature. Sign the overlay or unset FLUID_OVERLAY_STRICT."
            )
        overlays.append(overlay)

    overlay_tuple = tuple(overlays)
    # Anchor guard: compose against the CURRENT base guidance (bundled + shadow
    # + domain + profile, WITHOUT overlays) and reject any dropped anchor.
    base_map = prompts.base_guidance_without_overlays()
    overlaid_map = apply_overlays_to_guidance(base_map, overlay_tuple)
    enforce_anchor_integrity(base_map, overlaid_map)

    prompts.set_prompt_overlays(overlay_tuple, overlay_stack_fingerprint(overlay_tuple))
    return overlay_tuple


def overlay_validator_rule_dicts(overlays: Sequence[PromptOverlay]) -> List[Dict[str, str]]:
    """Flatten every overlay's validator rules into plain dicts (stack order)."""
    return [rule.as_dict() for overlay in overlays for rule in overlay.validator_rules]
