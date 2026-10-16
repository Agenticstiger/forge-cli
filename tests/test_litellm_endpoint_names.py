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

"""The endpoint names no group lists that litellm reads, and only those.

FLUID routes any provider litellm knows and hands it the key it resolved
(``FLUID_LLM_API_KEY``, else the keyring), so a project file may set such a
provider's endpoint only next to a ``FLUID_LLM_API_KEY`` of its own. That is
every name litellm's provider code reads to choose where a request goes:
``<PROVIDER>_API_BASE``, and the hosts, URLs, endpoints and account ids it
reads under other names (``WATSONX_URL``, ``DATAROBOT_ENDPOINT``,
``SNOWFLAKE_ACCOUNT_ID`` ...). Any other ``<NAME>_API_BASE`` is the project's
own setting: it follows the name-shape rule like ``ACME_HOST``. The names are
vendored (``credentials/litellm_endpoints.py``) so hydration never imports
litellm; the drift test below fails when the installed litellm reads one that
neither the list, a group, nor the documented list of names that choose no
provider host (``LITELLM_NOT_PROVIDER_ENDPOINTS``) covers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, Set

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from fluid_build.credentials.protected_env import (  # noqa: E402
    ENDPOINT_CREDENTIAL_GROUPS,
    _matches,
    is_protected_env_key,
    unpaired_endpoints,
)
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    drop_new_names,
    hermetic_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    before = hermetic_env(monkeypatch, tmp_path / "home")
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


def test_a_project_s_own_api_base_loads_with_its_own_key(tmp_path: Path) -> None:
    # An in-house REST service; litellm has no "myapp" provider.
    (tmp_path / ".env").write_text(
        f"MYAPP_API_BASE=https://api.myapp.test\nMYAPP_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert os.environ["MYAPP_API_BASE"] == "https://api.myapp.test"
    assert os.environ["MYAPP_API_KEY"] == PROJECT_KEY


def test_a_project_api_base_still_follows_the_name_shape_rule() -> None:
    # The operator's own MYAPP_API_KEY would go to the project's host.
    refused = unpaired_endpoints(
        {"MYAPP_API_BASE": "https://evil.test"}, {"MYAPP_API_KEY": OPERATOR_KEY}
    )

    assert refused["MYAPP_API_BASE"].operator_credentials == ("MYAPP_API_KEY",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "DEEPSEEK_API_BASE",
        "XAI_API_BASE",
        "LM_STUDIO_API_BASE",
        "HOSTED_VLLM_API_BASE",
        "TOGETHER_AI_API_BASE",
        # litellm's JSON registry of OpenAI-compatible providers
        "CRUSOE_API_BASE",
        "TENSORMESH_SERVERLESS_BASE_URL",
        "ANTHROPIC_AWS_BASE_URL",
        "COMETAPI_BASE_URL",
    ],
)
def test_a_litellm_provider_endpoint_pairs_only_with_fluid_llm_api_key(endpoint: str) -> None:
    prefix = re.sub(r"_(API_BASE|BASE_URL)$", "", endpoint)
    decoy = {endpoint: "https://llm.project.test", f"{prefix}_API_KEY": PROJECT_KEY}
    paired = {endpoint: "https://llm.project.test", "FLUID_LLM_API_KEY": PROJECT_KEY}

    assert unpaired_endpoints(decoy, {})[endpoint].pairs_with == ("FLUID_LLM_API_KEY",)
    assert unpaired_endpoints(paired, {}) == {}


# Names litellm reads to choose a provider's host that are not an
# ``<NAME>_API_BASE``: the IAM endpoint watsonx posts the key to and the hosts
# that get its IAM token, the host built from a Snowflake account id, API base
# fallbacks, GigaChat's token endpoint, Azure Document Intelligence's OCR
# endpoint and the AgentCore search gateway (litellm 1.83.14 to 1.105.0).
_HOST_NAMES = [
    "WATSONX_IAM_URL",
    "WATSONX_URL",
    "WX_URL",
    "WML_URL",
    "SNOWFLAKE_ACCOUNT_ID",
    "DATAROBOT_ENDPOINT",
    "GRADIENT_AI_AGENT_ENDPOINT",
    "GIGACHAT_AUTH_URL",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
    "AGENTCORE_GATEWAY_URL",
]
# litellm's github_copilot/ route: where it sends the Copilot key it stored
# when that key names no endpoint, where it fetches that key with the GitHub
# login it stored, where its login flow sends the operator, and where it keeps
# that login and the key it mints.
_COPILOT_NAMES = [
    "GITHUB_COPILOT_API_BASE",
    "GITHUB_COPILOT_API_KEY_URL",
    "GITHUB_COPILOT_ACCESS_TOKEN_URL",
    "GITHUB_COPILOT_DEVICE_CODE_URL",
    "GITHUB_COPILOT_TOKEN_DIR",
    "GITHUB_COPILOT_ACCESS_TOKEN_FILE",
    "GITHUB_COPILOT_API_KEY_FILE",
]


@pytest.mark.parametrize("endpoint", _HOST_NAMES + _COPILOT_NAMES)
def test_a_litellm_host_setting_alone_does_not_load(tmp_path: Path, endpoint: str) -> None:
    (tmp_path / ".env").write_text(f"{endpoint}=https://llm.project.test\n")

    hydrate_dotenv(tmp_path)

    assert endpoint not in os.environ


@pytest.mark.parametrize("endpoint", _HOST_NAMES)
def test_a_litellm_host_loads_with_the_project_s_own_fluid_llm_api_key(
    tmp_path: Path, endpoint: str
) -> None:
    (tmp_path / ".env").write_text(
        f"{endpoint}=https://llm.project.test\nFLUID_LLM_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert os.environ[endpoint] == "https://llm.project.test"


@pytest.mark.parametrize("endpoint", _COPILOT_NAMES)
def test_litellm_s_github_copilot_settings_never_load_from_a_project_file(
    tmp_path: Path, endpoint: str
) -> None:
    # The route sends the GitHub login litellm stored, never the key FLUID
    # passes, so nothing a project file sets can pair these.
    (tmp_path / ".env").write_text(
        f"{endpoint}=https://llm.project.test\n"
        f"FLUID_LLM_API_KEY={PROJECT_KEY}\nGITHUB_TOKEN={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert endpoint not in os.environ
    assert is_protected_env_key(endpoint)


@pytest.mark.parametrize(
    "endpoint", ["WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL", "WATSONX_IAM_URL"]
)
@pytest.mark.parametrize("credential", ["WATSONX_TOKEN", "WATSONX_ZENAPIKEY"])
def test_a_watsonx_host_is_refused_while_the_operator_holds_a_watsonx_token(
    endpoint: str, credential: str
) -> None:
    # litellm's watsonx/ route sends WATSONX_TOKEN, else WATSONX_ZENAPIKEY,
    # from the environment ahead of an IAM token for the key FLUID passes.
    refused = unpaired_endpoints(
        {endpoint: "https://llm.project.test", "FLUID_LLM_API_KEY": PROJECT_KEY},
        {credential: OPERATOR_KEY},
    )

    assert refused[endpoint].operator_credentials == (credential,)


def test_hydration_does_not_import_litellm(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_BASE=https://llm.project.test\n")
    code = (
        "import sys, pathlib\n"
        "from fluid_build.cli._common import hydrate_dotenv\n"
        f"hydrate_dotenv(pathlib.Path({str(tmp_path)!r}))\n"
        "print('litellm' in sys.modules)\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "False"


# ── drift: the vendored names against the installed litellm ────────────────

# Code litellm runs for the proxy server, its guardrails, loggers, caches and
# secret managers; their endpoints get their own credentials, never the key
# FLUID resolved for a provider.
_NOT_PROVIDER_CODE = (
    "proxy/",
    "integrations/",
    "caching/",
    "secret_managers/",
    "rust_bridge/",
    "router_strategy/",
)
_READ = re.compile(r"""["']([A-Z][A-Z0-9_]*_API_BASE)["']""")
# A get_secret* / os.getenv / os.environ read of a name ending like a host, a
# URL, an endpoint or an account id (the host Snowflake's is part of).
_ENV_READ = re.compile(
    r"""(?:get_secret(?:_str|_bool)?|os\.environ\.get|os\.getenv|os\.environ\[)\s*\(?\s*"""
    r"""(?:secret_name\s*=\s*)?["']"""
    r"""([A-Z][A-Z0-9_]*(?:_URL|_ENDPOINT|_HOST|_BASE|_ACCOUNT_ID|_SERVER))["']"""
)


def _registry_names(value: object, out: Set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "api_base_env":
                out.update([item] if isinstance(item, str) else item)
            _registry_names(item, out)
    elif isinstance(value, list):
        for item in value:
            _registry_names(item, out)


def _what_litellm_reads(root: Path) -> Dict[str, str]:
    """Each endpoint name litellm's provider code reads, with a file reading it."""
    found: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".py", ".json") or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            registry: Set[str] = set()
            try:
                _registry_names(json.loads(text), registry)
            except ValueError:
                pass
            for name in registry:
                found.setdefault(name, relative)
        if relative.startswith(_NOT_PROVIDER_CODE):
            continue
        for match in _READ.finditer(text):
            found.setdefault(match.group(1), relative)
        if path.suffix == ".py":
            for match in _ENV_READ.finditer(text):
                found.setdefault(match.group(1), relative)
    return found


def _listed_by_a_group(name: str) -> bool:
    return any(_matches(name, group.endpoints) for group in ENDPOINT_CREDENTIAL_GROUPS)


def test_the_names_that_choose_no_provider_host_are_not_listed() -> None:
    from fluid_build.credentials.litellm_endpoints import (
        LITELLM_ENDPOINT_NAMES,
        LITELLM_NOT_PROVIDER_ENDPOINTS,
    )

    assert LITELLM_NOT_PROVIDER_ENDPOINTS
    assert not LITELLM_NOT_PROVIDER_ENDPOINTS & LITELLM_ENDPOINT_NAMES
    assert not {n for n in LITELLM_NOT_PROVIDER_ENDPOINTS if _listed_by_a_group(n)}


def test_the_vendored_names_cover_the_installed_litellm() -> None:
    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("litellm not installed")
    import litellm

    from fluid_build.credentials.litellm_endpoints import (
        LITELLM_ENDPOINT_NAMES,
        LITELLM_NOT_PROVIDER_ENDPOINTS,
    )

    root = Path(list(spec.submodule_search_locations)[0])
    providers = {str(getattr(p, "value", p)) for p in litellm.provider_list}
    wanted = {p.upper().replace("-", "_") + "_API_BASE": "litellm.provider_list" for p in providers}
    wanted.update(_what_litellm_reads(root))

    missing = {
        name: where
        for name, where in wanted.items()
        if name not in LITELLM_ENDPOINT_NAMES
        and name not in LITELLM_NOT_PROVIDER_ENDPOINTS
        and not _listed_by_a_group(name)
    }
    assert missing == {}, (
        f"litellm {getattr(litellm, '__version__', '?')} reads endpoint names "
        "fluid_build/credentials/litellm_endpoints.py does not list; add each to "
        "LITELLM_ENDPOINT_NAMES, or, after reading where litellm sends it, to "
        "LITELLM_NOT_PROVIDER_ENDPOINTS with why it chooses no provider host: "
        f"{json.dumps(missing, indent=1, sort_keys=True)}"
    )
