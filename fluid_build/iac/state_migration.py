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

"""Move a contract's state to its per-provider key, with OpenTofu's own migration.

The default remote state key gained the provider
(``fluid/<id>/<provider>/terraform.tfstate``, see :mod:`.backend`), so a
contract applied to aws and to gcp keeps two states instead of one that each
cloud's plan would read as the other's orphans. State a previous release
wrote at ``fluid/<id>/terraform.tfstate`` has to follow, or the first apply
after the upgrade plans every resource as new.

**The mechanism is OpenTofu's.** Nothing here reads, edits or writes a state
document's content. The copy is ``tofu init -force-copy`` (which implies
``-migrate-state``) in a scratch directory whose ``.terraform/`` recorded the
old backend and whose module names the new one: the backend-reconfiguration
path ``terraform init -migrate-state`` has always had, which locks both
states where the backend supports locking and leaves the source untouched
(OpenTofu ``internal/command/meta_backend_migrate.go``,
``backendMigrateState_s_s``). The copy is checked afterwards by reading it
back: the same resources, since OpenTofu gives a copy into an empty
destination a fresh lineage. Terragrunt's ``backend migrate`` wraps the same
step for a renamed unit; this is that idea without the wrapper.

**What the scratch ``tofu init`` installs.** OpenTofu's init installs every
provider the *state* names, not only the module's: a ``{"terraform": {}}``
module beside a state naming ``hashicorp/null`` installed the latest
``hashicorp/null`` (measured, tofu 1.12), and for an aws state that is the
latest ``hashicorp/aws``, not the pinned ``~> 5.0``. The probe runs on every
apply whose new key is still empty (every ``--dry-run`` while a move is
pending, every gcp run while the old key holds the aws state), so it installs
nothing: ``-plugin-dir`` names an empty directory, the init stops at its
provider step after its backend step recorded the old backend, and the state
is pulled from exactly that recorded backend (checked, never assumed).
Attribution needs the document, not the providers. Should a future OpenTofu
not record the backend first, the probe falls back to a plain init, which
may install, rather than fail. The apply's own ``.terraform/providers`` is
never the plugin directory: with a plugin cache configured its entries link
into the cache, and an init reading them as a mirror broke the workdir
(measured: "no package for hashicorp/aws 5.100.0 cached"). The copy itself,
once per contract, installs what the old state names at the plugin's own
pins (``required_providers`` of the IaC plugin), never the latest.

**Never lose it, never guess.** ``-force-copy`` overwrites a destination that
holds state (the same OpenTofu function skips its confirmation), so the copy
runs only when the new key holds none, checked once before deciding and again
just before copying. The old object is left where it was. Which provider a
state belongs to is read from its resources' own ``provider`` addresses
(``provider["registry.opentofu.org/hashicorp/aws"]``) against the provider
sources each IaC plugin requires:

* every resource under this provider's plugin → migrate it;
* every resource under exactly one other plugin (the gcp apply finding the
  aws state at the old key) → leave it, it is not this apply's;
* anything else (both clouds in one state, a provider no plugin emits, only
  shared utility providers) → refuse with a typed error naming both keys, so
  an operator decides. Moving the wrong state is the one mistake that cannot
  be undone by the next apply.

OpenTofu's built-in provider (``terraform.io/builtin/terraform``, which
``terraform_data`` belongs to) is left out of the attribution: it names no
cloud, so a state is attributed by its other resources.

A read-only caller (``fluid diff``, ``fluid verify --state-drift``) asks with
``migrate=False`` and reads the old key while the move is pending, so a drift
gate that runs before the first upgraded apply still sees the real state.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from . import runner
from .backend import backend_location

#: The new key already holds state: it is used and nothing moves.
CURRENT = "current"
#: Neither key holds a state with resources in it.
NOTHING = "nothing"
#: The old key holds another provider's state, which is left alone.
OTHER_PROVIDER = "other_provider"
#: The old key holds this provider's state and the caller asked not to move it.
PENDING = "pending"
#: The old key's state was copied to the new key.
MIGRATED = "migrated"

#: Provider sources a previous release wrote under a name the pins no longer
#: use: the Snowflake provider moved from ``Snowflake-Labs`` to ``snowflakedb``
#: at v2 (see ``versions.PROVIDER_PINS``).
_SOURCE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "snowflake": ("snowflake-labs/snowflake",),
}

#: ``provider["registry.opentofu.org/hashicorp/aws"]`` (optionally ``.alias``).
_PROVIDER_ADDR_RE = re.compile(r'provider\["([^"]+)"\]')

_LOG = logging.getLogger(__name__)


class StateMigrationError(RuntimeError):
    """The state could not be moved, or whose it is could not be told."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StateDoc:
    """What ``tofu state pull`` said is at one key."""

    lineage: str
    serial: int
    resources: Tuple[Mapping[str, Any], ...]

    @property
    def exists(self) -> bool:
        """A key with no state pulls as an empty lineage (measured, tofu 1.12)."""
        return bool(self.lineage)


@dataclass(frozen=True)
class StateReconciliation:
    """What :func:`reconcile_state_key` found and did."""

    outcome: str
    #: Where the state was before the provider joined the key.
    legacy: Mapping[str, Any]
    #: Where it is now (the key the apply uses).
    current: Mapping[str, Any]
    resources: int = 0
    detail: str = ""

    @property
    def read_from(self) -> Mapping[str, Any]:
        """The backend a read-only caller should read: the old one while pending."""
        return self.legacy if self.outcome == PENDING else self.current

    def summary(self) -> str:
        """One line for the apply's output."""
        old = backend_location(self.legacy)
        new = backend_location(self.current)
        if self.outcome == MIGRATED:
            return (
                f"moved {self.resources} resource(s) from {old} to {new} with "
                "`tofu init -migrate-state`; the old object is left in place"
            )
        if self.outcome == PENDING:
            return f"read from {old}: `fluid apply` moves it to {new}"
        if self.outcome == OTHER_PROVIDER:
            return f"{old} holds {self.detail}, not this provider's; left in place"
        return ""


def plugin_sources(provider: str) -> FrozenSet[str]:
    """The provider sources (``namespace/type``, lower case) ``provider`` emits."""
    from .registry import get_iac_plugin

    plugin = get_iac_plugin(provider)
    required = getattr(plugin, "required_providers", None) or {}
    sources = {str(spec.get("source", "")).lower() for spec in required.values()}
    sources.update(_SOURCE_ALIASES.get(provider, ()))
    return frozenset(s for s in sources if s)


def state_sources(resources: Iterable[Mapping[str, Any]]) -> FrozenSet[str]:
    """``namespace/type`` of every provider the state's resources name.

    OpenTofu's built-in provider (``terraform.io/builtin/terraform``: the
    ``terraform_data`` resource, the ``terraform_remote_state`` data source)
    is not one: it ships inside the binary, any module can use it, and it
    says nothing about which cloud a state is. The GCP plugin writes a
    ``terraform_data`` beside a partitioned table (the trigger that replaces
    it when its partitioning changes), and counted as a provider no plugin
    emits it made every such gcp state "ambiguous", refusing the move and
    with it every apply, dry-run and diff on the product.
    """
    found = set()
    for resource in resources:
        match = _PROVIDER_ADDR_RE.search(str(resource.get("provider") or ""))
        if not match:
            # A resource with no readable provider address is itself a reason
            # not to decide: keep it visible to the classification.
            found.add("<unreadable>")
            continue
        parts = match.group(1).lower().split("/")
        if _is_builtin(parts):
            continue
        found.add("/".join(parts[-2:]))
    return frozenset(found)


def _is_builtin(parts: List[str]) -> bool:
    """``terraform.io/builtin/<type>``: a provider compiled into OpenTofu itself."""
    return len(parts) >= 3 and parts[-3] == "terraform.io" and parts[-2] == "builtin"


def classify(resources: Iterable[Mapping[str, Any]], provider: str) -> Tuple[str, str]:
    """``(verdict, detail)``: ``"mine"``, ``"other"`` or ``"ambiguous"``.

    ``detail`` names the owner for ``"other"`` and the reason for
    ``"ambiguous"``. See the module docstring for the rule.
    """
    resources = list(resources)
    sources = state_sources(resources)
    if resources and not sources:
        return "ambiguous", "only OpenTofu built-in resources, which name no cloud"
    by_plugin = _sources_by_plugin(provider)
    known = frozenset().union(*by_plugin.values())
    unknown = sources - known
    if unknown:
        return "ambiguous", "providers no forge-cli IaC plugin emits: " + ", ".join(sorted(unknown))
    owners = _owners(sources, by_plugin)
    if owners == {provider} and sources <= by_plugin[provider]:
        return "mine", provider
    if len(owners) == 1 and provider not in owners:
        (owner,) = owners
        if sources <= by_plugin[owner]:
            return "other", f"the {owner} provider's state ({', '.join(sorted(sources))})"
    if len(owners) > 1:
        return "ambiguous", "resources of several clouds (" + ", ".join(sorted(owners)) + ")"
    return "ambiguous", "only providers no single cloud owns (" + ", ".join(sorted(sources)) + ")"


def other_clouds(resources: Iterable[Mapping[str, Any]], provider: str) -> FrozenSet[str]:
    """The IaC plugins other than ``provider`` whose own resources the state holds.

    A resource counts for a plugin when its provider source is one no other
    plugin emits (``hashicorp/google`` is gcp's; ``hashicorp/null`` is no
    one's), the rule :func:`classify` attributes a state by.
    """
    by_plugin = _sources_by_plugin(provider)
    return frozenset(_owners(state_sources(resources), by_plugin) - {provider})


def _sources_by_plugin(provider: str) -> Dict[str, FrozenSet[str]]:
    from .registry import IAC_PLUGINS

    by_plugin = {name: plugin_sources(name) for name in IAC_PLUGINS}
    by_plugin.setdefault(provider, plugin_sources(provider))
    return by_plugin


def _owners(sources: FrozenSet[str], by_plugin: Mapping[str, FrozenSet[str]]) -> Set[str]:
    """Plugins owning a source in ``sources`` that no other plugin emits."""
    owners: Set[str] = set()
    for name, mine in by_plugin.items():
        exclusive = mine - frozenset().union(*(s for n, s in by_plugin.items() if n != name))
        if sources & exclusive:
            owners.add(name)
    return owners


def reconcile_state_key(
    *,
    workdir: Path,
    current: Mapping[str, Any],
    legacy: Mapping[str, Any],
    provider: str,
    env: Mapping[str, str],
    migrate: bool,
    logger: Optional[logging.Logger] = None,
) -> StateReconciliation:
    """Make sure ``current`` holds this provider's state, moving it from ``legacy``.

    ``workdir`` is the apply's own workdir, already ``tofu init``-ed on
    ``current``: the new key is read there, so an apply whose state is
    already at the new key pays one ``tofu state pull`` and nothing else.
    Only a new key with no state brings the scratch directory (and its
    ``tofu init -plugin-dir``, which installs nothing) in; only a move
    installs, at the plugin's pins.
    ``current`` and ``legacy`` are ``terraform.backend`` blocks
    (:func:`.backend.parse_backend`). Returns what was found; raises
    :class:`StateMigrationError` when the old state cannot be attributed or
    the copy fails or does not verify. ``migrate=False`` reports
    :data:`PENDING` instead of copying.
    """
    log = logger or _LOG
    current_dir = Path(workdir)
    now = _pull(current_dir, env)
    if now.exists:
        return StateReconciliation(CURRENT, legacy, current)
    with tempfile.TemporaryDirectory(prefix="fluid-state-") as tmp:
        old = _probe(Path(tmp), legacy, env)
        if not old.exists or not old.resources:
            return StateReconciliation(NOTHING, legacy, current)
        verdict, detail = classify(old.resources, provider)
        if verdict == "other":
            return StateReconciliation(OTHER_PROVIDER, legacy, current, len(old.resources), detail)
        if verdict != "mine":
            raise StateMigrationError(
                "state_migration_ambiguous",
                f"{backend_location(legacy)} holds {detail}, and {backend_location(current)} "
                f"holds no state, so it cannot be told whether it is the {provider} apply's "
                "and nothing was moved. Move it yourself (`tofu init -migrate-state` from a "
                "directory configured with the old key), or name the key this apply should "
                "use explicitly in --state-backend / FLUID_STATE_BACKEND",
            )
        if not migrate:
            return StateReconciliation(PENDING, legacy, current, len(old.resources))

        # Looked at again right before the copy: -force-copy would overwrite
        # a state another job wrote since the first probe.
        again = _pull(current_dir, env)
        if again.exists:
            if again.resources == old.resources:
                return StateReconciliation(CURRENT, legacy, current)
            raise StateMigrationError(
                "state_migration_raced",
                f"{backend_location(current)} received a different state while this apply "
                f"was about to move {backend_location(legacy)} there; nothing was moved",
            )
        # The copy starts from a directory initialised on the old key, whose
        # module pins the providers the old state names to the plugin's own
        # versions (see the module docstring).
        move_dir = Path(tmp) / "move"
        pins = plugin_pins(provider, state_sources(old.resources))
        _write_backend(move_dir, legacy, pins)
        first = runner.tofu_init(str(move_dir), env=env)
        if not first.ok:
            raise StateMigrationError(
                "state_migration_failed",
                f"could not initialise on {backend_location(legacy)} to move it: "
                + _tail(first.stderr or first.stdout),
            )
        _write_backend(move_dir, current, pins)
        init = runner.tofu_init(str(move_dir), env=env, force_copy=True)
        if not init.ok:
            raise StateMigrationError(
                "state_migration_failed",
                "`tofu init -force-copy` could not copy the state: "
                + _tail(init.stderr or init.stdout),
            )
        # Verified on content, not lineage: OpenTofu 1.12 writes the copy into
        # an empty destination under a fresh lineage and serial 1 (measured
        # against an S3 backend), so only the resources can be compared.
        moved = _pull(current_dir, env)
        if moved.resources != old.resources:
            raise StateMigrationError(
                "state_migration_unverified",
                f"after the copy {backend_location(current)} holds "
                f"{len(moved.resources)} resource(s) that are not the "
                f"{len(old.resources)} copied from {backend_location(legacy)}; the old "
                "object is untouched",
            )
        result = StateReconciliation(MIGRATED, legacy, current, len(old.resources))
        log.warning("state_migrated: %s", result.summary())
        return result


def _write_backend(
    workdir: Path,
    backend: Mapping[str, Any],
    required_providers: Optional[Mapping[str, Any]] = None,
) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    terraform: Dict[str, Any] = {"backend": dict(backend)}
    if required_providers:
        terraform["required_providers"] = dict(required_providers)
    doc = {"terraform": terraform}
    (workdir / "main.tf.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def plugin_pins(provider: str, sources: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """``provider``'s ``required_providers`` entries for the given ``namespace/type`` sources."""
    from .registry import get_iac_plugin

    wanted = {str(s).lower() for s in sources}
    required = getattr(get_iac_plugin(provider), "required_providers", None) or {}
    return {
        name: dict(spec)
        for name, spec in required.items()
        if str(spec.get("source", "")).lower() in wanted
    }


def _probe(tmp: Path, backend: Mapping[str, Any], env: Mapping[str, str]) -> StateDoc:
    """``backend``'s state, read with nothing installed (see the module docstring).

    ``tofu init -plugin-dir`` with an empty directory: a state that names a
    provider stops the init after the backend step, and the state is read
    only when the init is shown to have recorded ``backend``. Otherwise a
    plain init in a fresh directory is tried, as before; its failure is the
    error.
    """
    empty = tmp / "no-providers"
    empty.mkdir(parents=True, exist_ok=True)
    probe_dir = tmp / "legacy"
    _write_backend(probe_dir, backend)
    init = runner.tofu_init(str(probe_dir), env=env, plugin_dir=str(empty))
    if init.ok or records_backend(probe_dir, backend):
        return _pull(probe_dir, env)
    plain_dir = tmp / "legacy-plain"
    _write_backend(plain_dir, backend)
    init = runner.tofu_init(str(plain_dir), env=env)
    if not init.ok:
        raise StateMigrationError(
            "state_migration_probe_failed",
            f"could not read {backend_location(backend)}: " + _tail(init.stderr or init.stdout),
        )
    return _pull(plain_dir, env)


def read_state(workdir: Path, env: Mapping[str, str]) -> StateDoc:
    """The state ``workdir``'s initialised backend holds (``tofu state pull``).

    Raises :class:`StateMigrationError` (``state_migration_probe_failed``)
    when it cannot be read; the error carries stderr only, never the
    document.
    """
    return _pull(Path(workdir), env)


def _pull(workdir: Path, env: Mapping[str, str]) -> StateDoc:
    result = runner.tofu_state_pull(str(workdir), env=env)
    if not result.ok:
        # stderr only: stdout of `state pull` is the state document, whose
        # attributes can hold secrets, and must never reach an error message.
        raise StateMigrationError(
            "state_migration_probe_failed",
            "`tofu state pull` failed: " + (_tail(result.stderr) or f"exit {result.returncode}"),
        )
    return parse_state(result.stdout)


def parse_state(text: str) -> StateDoc:
    """A ``tofu state pull`` document. Empty output is no state."""
    if not (text or "").strip():
        return StateDoc("", 0, ())
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateMigrationError(
            "state_migration_probe_failed", f"`tofu state pull` printed no JSON ({exc.msg})"
        ) from None
    if not isinstance(doc, dict):
        raise StateMigrationError(
            "state_migration_probe_failed", "`tofu state pull` printed JSON that is not a state"
        )
    resources = doc.get("resources") or []
    return StateDoc(
        lineage=str(doc.get("lineage") or ""),
        serial=int(doc.get("serial") or 0),
        resources=tuple(r for r in resources if isinstance(r, dict)),
    )


def recorded_backend(workdir: Path) -> Optional[Dict[str, Any]]:
    """The backend ``tofu init`` last recorded in ``workdir``, as ``{type: config}``.

    ``.terraform/terraform.tfstate`` keeps it as ``{"backend": {"type": ...,
    "config": {...}}}``. ``None`` when there is none or it cannot be read.
    """
    path = workdir / ".terraform" / "terraform.tfstate"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backend = doc.get("backend") if isinstance(doc, dict) else None
    if not isinstance(backend, dict) or not backend.get("type"):
        return None
    config = backend.get("config") if isinstance(backend.get("config"), dict) else {}
    return {str(backend["type"]): config}


def records_backend(workdir: Path, backend: Mapping[str, Any]) -> bool:
    """True when ``workdir``'s recorded backend is ``backend`` (bucket and key/prefix).

    Only the fields forge-cli writes are compared: OpenTofu records every
    backend attribute, most of them null.
    """
    recorded = recorded_backend(workdir)
    if not recorded or set(recorded) != set(backend):
        return False
    (kind,) = tuple(backend)
    want = backend[kind] or {}
    have = recorded[kind] or {}
    return all(have.get(k) == v for k, v in want.items())


def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text
