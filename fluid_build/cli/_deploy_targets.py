# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --artifacts LOCK --deploy-targets FILE``: where each sealed
external file goes in one environment, sealed into the release so ``fluid runs deploy`` copies
exactly what the release names, to exactly where it says.

The file names the account the targets belong to and one target per artifact and place::

    deploy:
      account: "123456789012"     # the AWS account every target bucket belongs to
      region: eu-north-1          # optional: the targets' region (default: the profile's)
      targets:
        - artifact: orders-dags   # a name the --artifacts lock lists
          to: s3://acme-airflow/dags/orders_dag.py

The run refuses a file it cannot seal before any stage runs: a field the format does not allow,
an account that is not 12 digits, an artifact the lock does not list, a ``to`` that is not an
``s3://bucket/key`` object (a prefix is not), two targets with one ``to``, or a file larger than a
single copy takes (5 GiB). The release records ``identity["deploy"]``: the account, the region and
each target with the artifact's kind, release path and sha256 (from the lock), ordered with every
Airflow DAG last. It is per environment, so it is part of the release's ``identity_digest`` and
not of its ``code_digest``: the releases of one commit for two environments share the code and
differ in where it goes.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import _ship_release as release

#: What ``--deploy-targets`` reads when the flag is absent (only beside ``--release-dir``).
TARGETS_ENV = "RELEASE_DEPLOY_TARGETS"
#: Targets one file may list, and bytes of the file itself.
MAX_TARGETS = 256
MAX_FILE_BYTES = 1_048_576
#: The largest object one CopyObject (or PutObject) writes.
MAX_OBJECT_BYTES = 5 * 1024**3
#: Copied last: a DAG folder picks up a change within seconds, so it must find every other
#: file of the release already in place.
DAG_KIND = "airflow-dag"
_TOP_KEYS = {"deploy"}
_KEYS = {"account", "region", "targets"}
_TARGET_KEYS = {"artifact", "to"}
_ACCOUNT = re.compile(r"[0-9]{12}")
_REGION = re.compile(r"[a-z]{2}(-[a-z0-9]+){1,3}-[0-9]{1,2}")


class TargetsError(release.ReleaseError):
    """The deploy targets cannot be sealed into a release: the run refuses them."""


def order_key(target: Mapping[str, Any]) -> Any:
    """Every target but an Airflow DAG first, DAGs last; then by where it goes."""
    return (target.get("kind") == DAG_KIND, str(target.get("to")))


def parse_targets(data: Any, where: str, entries: Sequence[Any]) -> Dict[str, Any]:
    """``identity["deploy"]`` for ``data`` (the parsed file), checked against the lock's
    ``entries`` (``_release_lock.LockEntry``). Pure: reads no file.

    Raises:
        TargetsError: a key, value or target the release cannot seal.
    """
    if not isinstance(data, Mapping):
        raise TargetsError(f"{where}: expected a mapping with a deploy block")
    unknown = sorted(str(k) for k in set(data) - _TOP_KEYS)
    if unknown:
        raise TargetsError(f"{where}: unsupported field(s) {unknown}")
    block = data.get("deploy")
    if not isinstance(block, Mapping):
        raise TargetsError(f"{where}: deploy: expected a mapping with account and targets")
    unknown = sorted(str(k) for k in set(block) - _KEYS)
    if unknown:
        raise TargetsError(f"{where}: deploy: unsupported field(s) {unknown}")
    account = block.get("account")
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise TargetsError(
            f'{where}: deploy.account: expected the 12-digit AWS account id, quoted ("0123...")'
        )
    region = block.get("region")
    if region is not None and (not isinstance(region, str) or not _REGION.fullmatch(region)):
        raise TargetsError(f"{where}: deploy.region: expected an AWS region name (eu-north-1)")
    items = block.get("targets")
    if not isinstance(items, list) or not items:
        raise TargetsError(f"{where}: deploy.targets: expected a list of one target or more")
    if len(items) > MAX_TARGETS:
        raise TargetsError(
            f"{where}: deploy.targets: lists {len(items)} targets, at most {MAX_TARGETS}"
        )
    by_name = {e.name: e for e in entries}
    targets: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for i, item in enumerate(items):
        at = f"{where}: deploy.targets[{i}]"
        target = _target(item, at, by_name)
        if target["to"] in seen:
            raise TargetsError(
                f"{at}: {target['to']} is also targets[{seen[target['to']]}]'s; one object holds "
                "one file"
            )
        seen[target["to"]] = i
        targets.append(target)
    out: Dict[str, Any] = {"account": account, "targets": sorted(targets, key=order_key)}
    if region is not None:
        out["region"] = region
    return out


def _target(item: Any, where: str, by_name: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, Mapping):
        raise TargetsError(f"{where}: expected a mapping with artifact and to")
    unknown = sorted(str(k) for k in set(item) - _TARGET_KEYS)
    if unknown:
        raise TargetsError(f"{where}: unsupported field(s) {unknown}")
    name = item.get("artifact")
    if not isinstance(name, str) or name not in by_name:
        listed = ", ".join(sorted(by_name)) or "none"
        raise TargetsError(
            f"{where}.artifact: expected a name the --artifacts lock lists ({listed})"
        )
    to = item.get("to")
    if not isinstance(to, str) or not is_object_uri(to):
        raise TargetsError(
            f"{where}.to: expected s3://bucket/key, the object the file is copied to (a key, "
            "not a prefix ending in /)"
        )
    entry = by_name[name]
    return {
        "artifact": name,
        "kind": entry.kind,
        "path": entry.release_path,
        "digest": {"sha256": entry.sha256},
        "to": to,
    }


def is_object_uri(uri: str) -> bool:
    """Whether ``uri`` is ``s3://bucket/key`` naming one object (``util.s3.is_object_uri``)."""
    from fluid_build.util import s3

    return s3.is_object_uri(uri)


def load(path: str, external: Sequence[Any]) -> Dict[str, Any]:
    """``identity["deploy"]`` from the targets file at ``path``, checked against the sealed
    external files (``_release_lock.ExternalArtifact``), each targeted one no larger than one
    copy writes.

    Raises:
        TargetsError: the file cannot be read, or names anything the release cannot seal.
    """
    import yaml

    from fluid_build._contract_loader import CLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.util.safe_yaml import load_yaml_safe, read_bounded_text

    where = f"--deploy-targets {path}"
    try:
        checked = validate_cli_path(path, mode="read", file_type="deploy targets")
        data = load_yaml_safe(
            read_bounded_text(checked, MAX_FILE_BYTES), unique_keys=True, name=checked.name
        )
    except CLIError as exc:
        raise TargetsError(f"{where}: {exc.message}") from None
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise TargetsError(f"{where}: cannot read it: {exc}") from None
    deploy = parse_targets(data, where, [a.entry for a in external])
    local: Dict[str, Optional[Path]] = {a.entry.name: a.local for a in external}
    for target in deploy["targets"]:
        file = local.get(target["artifact"])
        try:
            size = file.stat().st_size if file is not None else 0
        except OSError as exc:
            raise TargetsError(f"{where}: cannot read {target['artifact']}: {exc}") from None
        if size > MAX_OBJECT_BYTES:
            raise TargetsError(
                f"{where}: {target['artifact']} is larger than 5 GiB, more than one copy "
                "writes; deploy it with your own tooling"
            )
    return deploy
