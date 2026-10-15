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

"""Which market config file ``fluid market`` reads, and whose it is.

``fluid market`` reads the operator's ``~/.fluid/market.yaml`` (or ``.yml``)
and, when there is none, ``market.yaml`` (or ``.yml``) in the working
directory. That second file arrives with the checkout being built, so its
catalog blocks get the project-config trust rule (``config_manager``): a
connector may send what such a block carries itself, but never pair an
endpoint it chose with the operator's environment, never start a command it
names, and its endpoint must pass the Command Center host gate. The blocks are
marked here (:class:`CheckoutMarketBlock`); each connector enforces the rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Tuple

#: The sections of the market config that are not catalog blocks.
_NOT_CATALOG_BLOCKS = frozenset({"catalogs", "defaults", "cache", "output"})


class CheckoutMarketBlock(Dict[str, Any]):
    """A catalog block from ``market.yaml`` in the working directory: the checkout's.

    A plain dict to every consumer. ``checkout_file`` names the file, relative
    to the working directory.
    """

    checkout_file: str = ""

    def __init__(self, block: Mapping[str, Any], checkout_file: str) -> None:
        super().__init__(block)
        self.checkout_file = checkout_file


def market_config_files() -> Iterator[Tuple[Path, bool]]:
    """The market config files to try, in order, each with whether it is the checkout's.

    With no home directory there is no operator file, and the working
    directory is never read in its place as the operator's.
    """
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    if home is not None:
        yield home / ".fluid" / "market.yaml", False
        yield home / ".fluid" / "market.yml", False
    yield Path("market.yaml"), True
    yield Path("market.yml"), True


def checkout_blocks(file_config: Any, path: Path, from_checkout: bool) -> Dict[str, str]:
    """``{catalog: file}`` for every catalog block a checkout file sets; ``{}`` for the operator's."""
    if not from_checkout or not isinstance(file_config, dict):
        return {}
    return {
        str(name): str(path)
        for name, block in file_config.items()
        if isinstance(block, dict) and name not in _NOT_CATALOG_BLOCKS
    }


def mark_checkout_blocks(
    config: Dict[str, Any], marked: Mapping[str, str], env_config: Mapping[str, Any]
) -> None:
    """Make each block in ``marked`` a :class:`CheckoutMarketBlock` in ``config``.

    Not a catalog the environment configured: ``_load_env_config`` sets its
    endpoint and its credential together, so the endpoint is the operator's.
    """
    for name, source in marked.items():
        block = config.get(name)
        if name not in env_config and isinstance(block, dict):
            config[name] = CheckoutMarketBlock(block, source)


__all__ = [
    "CheckoutMarketBlock",
    "checkout_blocks",
    "mark_checkout_blocks",
    "market_config_files",
]
