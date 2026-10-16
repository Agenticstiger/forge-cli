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

"""The pairing rule, case by case, through the code that sends the credential.

``credentials.protected_env.unpaired_endpoints`` lets a project dotenv file set
a third-party endpoint only next to a credential that is what every reader of
that endpoint sends. Each test here drives the real reader (an SDK, litellm, a
FLUID code path, or a stand-in CLI) against a loopback listener and asserts
the operator's credential never reaches the host the project chose:

* endpoint variables the SDKs FLUID runs read (botocore, litellm, huggingface_hub,
  requests), including ones no group lists;
* decoys: a project credential of the right service that the reader does not
  send, while the operator's keyring key, stored CLI login or exported
  ``FLUID_LLM_API_KEY`` is what goes out;
* the endpoint, the decoy and the operator's credential split across
  ``.env``, ``.env.<env>``, ``.env.local`` and ``FLUID_SECRETS_FILE``;
* how dotenv files are written (``export``, quotes, comments, CRLF, a BOM,
  duplicate keys, case);
* the setups that must keep working.

Requests leave only for loopback; every other name fails to resolve.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import reset_dotenv_load_log  # noqa: E402
from fluid_build.credentials.protected_env import (  # noqa: E402
    ENDPOINT_CREDENTIAL_GROUPS,
    is_protected_env_key,
    unpaired_endpoints,
)
from tests._dotenv_pairing_support import (  # noqa: E402
    DECOY_KEY,
    OPERATOR_KEY,
    PROJECT_KEY,
    Listener,
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
    send_if_loopback,
)

_OPERATOR_SECRET = "OPERATOR-FAKE-SECRET-321"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    refuse_outbound_dns(monkeypatch)
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


@pytest.fixture
def listener() -> Iterator[Listener]:
    server = Listener()
    yield server
    server.close()


@pytest.fixture
def operator_listener() -> Iterator[Listener]:
    server = Listener()
    yield server
    server.close()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _keyring_holds(monkeypatch: pytest.MonkeyPatch, provider: Optional[str]) -> None:
    """The operator saved ``OPERATOR_KEY`` for ``provider`` with ``fluid ai setup``."""
    import fluid_build.llm.providers as llm_providers

    monkeypatch.setattr(
        llm_providers,
        "_get_api_key_from_keyring",
        lambda name: OPERATOR_KEY if provider in (None, name) else None,
    )


@functools.lru_cache(maxsize=None)
def _litellm_reply_model_error() -> Optional[str]:
    """Why this litellm cannot build its reply model, or None when it can.

    litellm builds a ``Message`` for every completion. The last litellm that
    installs on Python 3.10 (1.97) leaves that pydantic model not fully
    defined under pydantic 2.14, so every ``litellm.completion`` raises
    APIConnectionError before it sends a request. Probed once per process.
    """
    try:
        from litellm.types.utils import Message

        Message(content="x", role="assistant")
    except Exception as exc:  # noqa: BLE001 — any failure to build it is the one we skip on
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return None


def _skip_where_litellm_cannot_reply() -> None:
    """Skip a test at the point it drives ``litellm.completion``, when the
    installed litellm and pydantic cannot build litellm's reply model: the
    call would fail before sending, so the listener could prove nothing."""
    error = _litellm_reply_model_error()
    if error is None:
        return
    from importlib.metadata import version

    pytest.skip(
        f"upstream incompatibility: litellm {version('litellm')} cannot build its Message "
        f"model with pydantic {version('pydantic')} ({error}), so litellm.completion "
        "fails before it sends anything"
    )


def _llm_call(provider: str, endpoint_var: str) -> None:
    """What ``fluid forge`` does with FLUID_LLM_BACKEND=litellm: resolve the key,
    hand it to litellm, and let litellm read the endpoint from the environment.

    litellm is called only for a loopback endpoint; a test that gets that far
    is skipped where litellm cannot build its reply
    (:func:`_skip_where_litellm_cannot_reply`). ``pytest.skip`` raises a
    BaseException, so ``send_if_loopback`` does not swallow it.
    """
    litellm = pytest.importorskip("litellm")
    from fluid_build.llm.litellm_backend import _litellm_model_for
    from fluid_build.llm.provider_resolve import _resolve_api_key

    key = _resolve_api_key(provider, os.environ)

    def complete() -> Any:
        _skip_where_litellm_cannot_reply()
        return litellm.completion(
            model=_litellm_model_for(provider, "test-model"),
            messages=[{"role": "user", "content": "ping"}],
            api_key=key,
            num_retries=0,
            timeout=5,
        )

    send_if_loopback(os.environ.get(endpoint_var), complete)


def _openai_sdk_call() -> None:
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or "none", max_retries=0)
    send_if_loopback(str(client.base_url), client.models.list)


def _dbt_output(platform: str) -> Dict[str, Any]:
    from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile

    profile = _build_generated_dbt_profile({"execution": {"runtime": {"platform": platform}}}, {})
    assert profile is not None
    (block,) = profile.values()
    return dict(block["outputs"][block["target"]])


# ── a. endpoint variables the SDKs FLUID runs read ────────────────────────


@pytest.mark.parametrize(
    "variable,service,call",
    [
        ("AWS_ENDPOINT_URL", "s3", lambda c: c.list_buckets()),
        ("AWS_ENDPOINT_URL_S3", "s3", lambda c: c.list_buckets()),
        ("aws_endpoint_url_s3", "s3", lambda c: c.list_buckets()),
        ("AWS_ENDPOINT_URL_STS", "sts", lambda c: c.get_caller_identity()),
        ("AWS_ENDPOINT_URL_GLUE", "glue", lambda c: c.get_databases()),
        (
            "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
            "bedrock-runtime",
            lambda c: c.invoke_model(modelId="m", body=b"{}"),
        ),
    ],
)
def test_aws_endpoint_urls_never_receive_the_operator_signature(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    variable: str,
    service: str,
    call: Any,
) -> None:
    # botocore reads AWS_ENDPOINT_URL[_<SERVICE>] when it builds a client and
    # signs every request with the access key id in the Authorization header.
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", OPERATOR_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _OPERATOR_SECRET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    (project / ".env").write_text(f"{variable}={listener.url}\n")

    hydrate_dotenv(project)
    client = boto3.client(service)
    send_if_loopback(client.meta.endpoint_url, lambda: call(client))

    assert listener.saw(OPERATOR_KEY) == []
    assert is_protected_env_key(variable)


def test_an_operator_exported_aws_endpoint_is_used(
    monkeypatch: pytest.MonkeyPatch, project: Path, operator_listener: Listener
) -> None:
    # The control for the test above: botocore does send the signature to an
    # endpoint the operator set, so an empty listener there means something.
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", OPERATOR_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _OPERATOR_SECRET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", operator_listener.url)

    hydrate_dotenv(project)
    client = boto3.client("s3")
    send_if_loopback(client.meta.endpoint_url, client.list_buckets)

    assert operator_listener.saw(OPERATOR_KEY) != []


def test_botocore_does_not_trust_a_project_ca_bundle(project: Path) -> None:
    botocore_session = pytest.importorskip("botocore.session")
    (project / "ca.pem").write_text("not a certificate\n")
    (project / ".env").write_text(f"AWS_CA_BUNDLE={project / 'ca.pem'}\n")

    hydrate_dotenv(project)

    assert botocore_session.get_session().get_config_variable("ca_bundle") is None


@pytest.mark.parametrize("variable", ["SSL_CERT_DIR", "NO_PROXY", "no_proxy"])
def test_tls_and_proxy_bypass_settings_stay_operator_only(project: Path, variable: str) -> None:
    # httpx reads SSL_CERT_DIR; requests, httpx, urllib and litellm read
    # NO_PROXY in either case.
    import urllib.request

    (project / ".env").write_text(f"{variable}={project}\n")

    hydrate_dotenv(project)

    assert variable not in os.environ
    assert "no" not in urllib.request.getproxies_environment()


def test_a_project_netrc_cannot_send_the_operator_s_netrc_password(
    project: Path, listener: Listener
) -> None:
    # requests reads NETRC to find the netrc file and sends the entry for the
    # request's host. A project netrc holds only what the project wrote; the
    # operator's ~/.netrc entries stay bound to their own hosts.
    requests = pytest.importorskip("requests")
    home = Path(os.environ["HOME"])
    (home / ".netrc").write_text(
        f"machine operator.example.test login operator password {OPERATOR_KEY}\n"
    )
    (project / "netrc").write_text(f"machine 127.0.0.1 login project password {PROJECT_KEY}\n")
    (project / ".env").write_text(f"NETRC={project / 'netrc'}\n")

    hydrate_dotenv(project)
    send_if_loopback(listener.url, lambda: requests.get(listener.url, timeout=5))

    assert listener.saw(OPERATOR_KEY) == []


@pytest.mark.parametrize("operator_has", ["FLUID_LLM_API_KEY", "keyring"])
def test_ollama_api_base_does_not_receive_the_key_fluid_resolves(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    operator_has: str,
) -> None:
    # litellm's ollama/ route reads OLLAMA_API_BASE and sends the api_key FLUID
    # resolved as a bearer token.
    if operator_has == "keyring":
        _keyring_holds(monkeypatch, "ollama")
    else:
        monkeypatch.setenv("FLUID_LLM_API_KEY", OPERATOR_KEY)
    (project / ".env").write_text(f"OLLAMA_API_BASE={listener.url}\n")

    hydrate_dotenv(project)
    _llm_call("ollama", "OLLAMA_API_BASE")

    assert listener.saw(OPERATOR_KEY) == []
    assert "OLLAMA_API_BASE" not in os.environ


@pytest.mark.parametrize(
    "provider,variable",
    [
        ("deepseek", "DEEPSEEK_API_BASE"),
        ("xai", "XAI_API_BASE"),
        ("openrouter", "OPENROUTER_API_BASE"),
        ("lm_studio", "LM_STUDIO_API_BASE"),
        ("hosted_vllm", "HOSTED_VLLM_API_BASE"),
    ],
)
@pytest.mark.parametrize("operator_has", ["FLUID_LLM_API_KEY", "keyring"])
def test_litellm_provider_api_bases_no_group_lists_are_paired_too(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    provider: str,
    variable: str,
    operator_has: str,
) -> None:
    # FLUID routes any provider litellm knows (``--llm-provider deepseek``),
    # and litellm reads <PROVIDER>_API_BASE for each. The project's own
    # <PROVIDER>_API_KEY is a decoy: the key FLUID resolves beats it.
    if operator_has == "keyring":
        _keyring_holds(monkeypatch, provider)
    else:
        monkeypatch.setenv("FLUID_LLM_API_KEY", OPERATOR_KEY)
    prefix = variable[: -len("_API_BASE")]
    (project / ".env").write_text(f"{variable}={listener.url}\n{prefix}_API_KEY={DECOY_KEY}\n")

    hydrate_dotenv(project)
    _llm_call(provider, variable)

    assert listener.saw(OPERATOR_KEY) == []
    assert variable not in os.environ


@pytest.mark.parametrize(
    "provider,lines,variable",
    [
        # the IAM endpoint watsonx posts the key FLUID passes to, and the host
        # that gets the IAM token
        (
            "watsonx",
            "WATSONX_IAM_URL={url}/identity/token\nWATSONX_URL={url}\nWATSONX_PROJECT_ID=p\n",
            "WATSONX_IAM_URL",
        ),
        ("datarobot", "DATAROBOT_ENDPOINT={url}\n", "DATAROBOT_ENDPOINT"),
        ("gradient_ai", "GRADIENT_AI_AGENT_ENDPOINT={url}\n", "GRADIENT_AI_AGENT_ENDPOINT"),
        # the token endpoint GigaChat posts the key FLUID passes to
        ("gigachat", "GIGACHAT_AUTH_URL={url}/oauth\n", "GIGACHAT_AUTH_URL"),
    ],
    ids=["watsonx", "datarobot", "gradient_ai", "gigachat"],
)
@pytest.mark.parametrize("operator_has", ["FLUID_LLM_API_KEY", "keyring"])
def test_litellm_hosts_named_otherwise_than_an_api_base_are_paired_too(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    provider: str,
    lines: str,
    variable: str,
    operator_has: str,
) -> None:
    # litellm's provider code also chooses a host from names that are not
    # <PROVIDER>_API_BASE, and sends the key FLUID resolved there.
    if operator_has == "keyring":
        _keyring_holds(monkeypatch, provider)
    else:
        monkeypatch.setenv("FLUID_LLM_API_KEY", OPERATOR_KEY)
    (project / ".env").write_text(lines.format(url=listener.url))

    hydrate_dotenv(project)
    _llm_call(provider, variable)

    assert listener.saw(OPERATOR_KEY) == []
    assert variable not in os.environ


@pytest.mark.parametrize("credential", ["WATSONX_ZENAPIKEY", "WATSONX_TOKEN"])
@pytest.mark.parametrize("variable", ["WX_URL", "WML_URL", "WATSONX_URL"])
def test_a_project_watsonx_host_never_gets_the_operator_s_watsonx_token(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    credential: str,
    variable: str,
) -> None:
    # litellm's watsonx/ route sends WATSONX_TOKEN, else WATSONX_ZENAPIKEY,
    # from the environment ahead of an IAM token for the key FLUID passes, so
    # the project's own FLUID_LLM_API_KEY does not pair the host.
    monkeypatch.setenv(credential, OPERATOR_KEY)
    (project / ".env").write_text(
        f"FLUID_LLM_API_KEY={PROJECT_KEY}\n{variable}={listener.url}\nWATSONX_PROJECT_ID=p\n"
    )

    hydrate_dotenv(project)
    _llm_call("watsonx", variable)

    assert listener.saw(OPERATOR_KEY) == []
    assert variable not in os.environ


def test_a_project_copilot_url_never_gets_litellm_s_stored_github_login(
    project: Path, listener: Listener
) -> None:
    # litellm's github_copilot/ route fetches its Copilot key with the GitHub
    # login it stored (~/.config/litellm/github_copilot/access-token), whatever
    # key FLUID passes: a project FLUID_LLM_API_KEY pairs nothing there.
    stored = Path(os.environ["HOME"]) / ".config" / "litellm" / "github_copilot"
    stored.mkdir(parents=True)
    (stored / "access-token").write_text(OPERATOR_KEY)
    (project / ".env").write_text(
        f"FLUID_LLM_API_KEY={PROJECT_KEY}\n"
        f"GITHUB_COPILOT_API_KEY_URL={listener.url}/copilot_internal/v2/token\n"
    )

    hydrate_dotenv(project)
    _llm_call("github_copilot", "GITHUB_COPILOT_API_KEY_URL")

    assert listener.saw(OPERATOR_KEY) == []
    assert "GITHUB_COPILOT_API_KEY_URL" not in os.environ


def test_a_project_copilot_api_base_never_gets_litellm_s_stored_copilot_key(
    project: Path, listener: Listener
) -> None:
    # litellm's github_copilot/ route sends the Copilot key it stored
    # (~/.config/litellm/github_copilot/api-key.json), whatever key FLUID
    # passes, to the api_base argument, else the endpoint that file names,
    # else GITHUB_COPILOT_API_BASE (llms/github_copilot/chat/transformation.py,
    # litellm 1.83.14 to 1.105.0). It picks the host before it reads or mints
    # the key, so a first call, or a stored key that names no endpoint, leaves
    # the choice to the variable: a project FLUID_LLM_API_KEY pairs nothing.
    stored = Path(os.environ["HOME"]) / ".config" / "litellm" / "github_copilot"
    stored.mkdir(parents=True)
    # expires 2100-01-01, so litellm sends it without refreshing it
    (stored / "api-key.json").write_text(
        json.dumps({"token": OPERATOR_KEY, "expires_at": 4102444800})
    )
    (project / ".env").write_text(
        f"FLUID_LLM_API_KEY={PROJECT_KEY}\nGITHUB_COPILOT_API_BASE={listener.url}\n"
    )

    hydrate_dotenv(project)
    _llm_call("github_copilot", "GITHUB_COPILOT_API_BASE")

    assert listener.saw(OPERATOR_KEY) == []
    assert "GITHUB_COPILOT_API_BASE" not in os.environ


_HF_WHOAMI = """
import os, sys
endpoint = os.environ.get("HF_ENDPOINT", "")
if endpoint.startswith("http://127.0.0.1"):
    from huggingface_hub import HfApi
    try:
        HfApi().whoami()
    except Exception:
        pass
"""


def test_hf_endpoint_does_not_receive_the_stored_hugging_face_token(
    project: Path, listener: Listener
) -> None:
    # huggingface_hub reads HF_ENDPOINT when imported and sends HF_TOKEN, else
    # the token ``hf auth login`` stored under HF_HOME (~/.cache/huggingface).
    pytest.importorskip("huggingface_hub")
    token = Path(os.environ["HOME"]) / ".cache" / "huggingface" / "token"
    token.parent.mkdir(parents=True)
    token.write_text(OPERATOR_KEY)
    (project / ".env").write_text(f"HF_ENDPOINT={listener.url}\n")

    hydrate_dotenv(project)
    subprocess.run(
        [sys.executable, "-c", _HF_WHOAMI], env=dict(os.environ), timeout=120, check=False
    )

    assert listener.saw(OPERATOR_KEY) == []
    assert "HF_ENDPOINT" not in os.environ


def test_hf_endpoint_loads_with_the_project_s_own_token(project: Path, listener: Listener) -> None:
    pytest.importorskip("huggingface_hub")
    token = Path(os.environ["HOME"]) / ".cache" / "huggingface" / "token"
    token.parent.mkdir(parents=True)
    token.write_text(OPERATOR_KEY)
    (project / ".env").write_text(f"HF_ENDPOINT={listener.url}\nHF_TOKEN={PROJECT_KEY}\n")

    hydrate_dotenv(project)
    subprocess.run(
        [sys.executable, "-c", _HF_WHOAMI], env=dict(os.environ), timeout=120, check=False
    )

    assert listener.saw(OPERATOR_KEY) == []
    assert listener.saw(PROJECT_KEY) != []


@pytest.mark.parametrize(
    "endpoint",
    [
        "GCE_METADATA_HOST",
        "GCE_METADATA_IP",
        "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
        "DATABRICKS_CONFIG_FILE",
    ],
)
def test_sdk_locations_outside_the_image_pair_like_their_service(endpoint: str) -> None:
    # google-auth, the google-cloud clients and databricks-sdk read these;
    # none is installed here, so the rule itself is what is pinned: the
    # operator's default credentials or ~/.databrickscfg would answer.
    assert endpoint in unpaired_endpoints({endpoint: "h.test"}, {})
    pairing = (
        "DATABRICKS_TOKEN"
        if endpoint.startswith("DATABRICKS")
        else "GOOGLE_APPLICATION_CREDENTIALS"
    )
    assert unpaired_endpoints({endpoint: "h.test", pairing: "p"}, {}) == {}


# ── b. decoys: a project credential the reader does not send ──────────────


@pytest.mark.parametrize(
    "provider,endpoint,decoy",
    [
        # the codex CLI's key, which _resolve_api_key never reads
        ("openai", "OPENAI_BASE_URL", "CODEX_API_KEY"),
        # Claude Code's names, which litellm's anthropic/ route ignores
        ("anthropic", "ANTHROPIC_BASE_URL", "CLAUDE_API_KEY"),
        ("anthropic", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"),
        ("anthropic", "ANTHROPIC_API_BASE", "CLAUDE_CODE_OAUTH_TOKEN"),
        # google-genai's name, which _resolve_api_key never reads
        ("gemini", "GEMINI_API_BASE", "GOOGLE_GENAI_API_KEY"),
        # provider keys litellm reads only when FLUID passes no api_key
        ("groq", "GROQ_API_BASE", "GROQ_API_KEY"),
        ("cohere", "COHERE_API_BASE", "COHERE_API_KEY"),
        ("cohere", "COHERE_API_BASE", "CO_API_KEY"),
        ("github", "GITHUB_API_BASE", "GITHUB_TOKEN"),
        ("azure", "AZURE_API_BASE", "AZURE_API_KEY"),
        ("databricks", "DATABRICKS_API_BASE", "DATABRICKS_TOKEN"),
    ],
)
def test_a_decoy_credential_does_not_pair_an_llm_endpoint_with_the_keyring_key(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    provider: str,
    endpoint: str,
    decoy: str,
) -> None:
    _keyring_holds(monkeypatch, provider)
    monkeypatch.setenv("AZURE_API_VERSION", "2024-06-01")
    (project / ".env").write_text(f"{endpoint}={listener.url}\n{decoy}={DECOY_KEY}\n")

    hydrate_dotenv(project)
    _llm_call(provider, endpoint)

    assert listener.saw(OPERATOR_KEY) == []
    assert endpoint not in os.environ


@pytest.mark.parametrize(
    "provider,endpoint,credential",
    [
        ("openai", "OPENAI_BASE_URL", "OPENAI_API_KEY"),
        ("openai", "OPENAI_API_BASE", "FLUID_LLM_API_KEY"),
        ("anthropic", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
        ("gemini", "GEMINI_API_BASE", "GEMINI_API_KEY"),
        ("gemini", "GEMINI_API_BASE", "GOOGLE_API_KEY"),
        ("github", "GITHUB_API_BASE", "GITHUB_API_KEY"),
        ("groq", "GROQ_API_BASE", "FLUID_LLM_API_KEY"),
        ("deepseek", "DEEPSEEK_API_BASE", "FLUID_LLM_API_KEY"),
    ],
)
def test_the_credential_the_reader_sends_pairs_its_endpoint(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    provider: str,
    endpoint: str,
    credential: str,
) -> None:
    # Even with a keyring key saved, these come first, so the project's own
    # key is the one that goes to the project's host.
    _keyring_holds(monkeypatch, provider)
    (project / ".env").write_text(f"{endpoint}={listener.url}\n{credential}={PROJECT_KEY}\n")

    hydrate_dotenv(project)
    _llm_call(provider, endpoint)

    assert os.environ[endpoint] == listener.url
    assert listener.saw(OPERATOR_KEY) == []
    assert listener.saw(PROJECT_KEY) != []


@pytest.mark.parametrize(
    "provider,endpoint", [("groq", "GROQ_API_BASE"), ("openai", "OPENAI_BASE_URL")]
)
def test_the_harness_sees_a_key_sent_to_an_operator_endpoint(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_listener: Listener,
    provider: str,
    endpoint: str,
) -> None:
    # The control for the decoy tests: litellm does read these endpoints from
    # the environment and send the keyring key there.
    _keyring_holds(monkeypatch, provider)
    monkeypatch.setenv(endpoint, operator_listener.url)

    hydrate_dotenv(project)
    _llm_call(provider, endpoint)

    assert operator_listener.saw(OPERATOR_KEY) != []


def test_an_exported_fluid_llm_api_key_refuses_a_project_openai_endpoint(
    project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener
) -> None:
    # _resolve_api_key prefers FLUID_LLM_API_KEY to the project's OPENAI_API_KEY.
    monkeypatch.setenv("FLUID_LLM_API_KEY", OPERATOR_KEY)
    (project / ".env").write_text(f"OPENAI_BASE_URL={listener.url}\nOPENAI_API_KEY={DECOY_KEY}\n")

    hydrate_dotenv(project)
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(OPERATOR_KEY) == []
    assert "OPENAI_BASE_URL" not in os.environ


_FAKE_CODEX = """#!{python}
# codex exec, as far as credentials go: OPENAI_BASE_URL is where it sends,
# CODEX_API_KEY is the key (blank counts as unset), else the login stored in
# $CODEX_HOME/auth.json.
import json, os, pathlib, urllib.request
base = os.environ.get("OPENAI_BASE_URL", "")
key = (os.environ.get("CODEX_API_KEY") or "").strip()
if not key:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    key = json.loads(pathlib.Path(home, "auth.json").read_text())["tokens"]["access_token"]
if base.startswith("http://127.0.0.1"):
    request = urllib.request.Request(
        base.rstrip("/") + "/responses", data=b"{{}}", headers={{"Authorization": "Bearer " + key}}
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except Exception:
        pass
print(json.dumps({{"contract": {{}}}}))
"""


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path(os.environ["HOME"])
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": OPERATOR_KEY}})
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    codex = bindir / "codex"
    codex.write_text(_FAKE_CODEX.format(python=sys.executable))
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _run_codex() -> None:
    from fluid_build.llm.coding_agent import get_coding_agent_provider
    from fluid_build.llm.providers import LlmConfig

    config = LlmConfig(provider="codex", model="codex", endpoint="", api_key=None)
    try:
        get_coding_agent_provider("codex").invoke_blocking(config, "system", "user")
    except Exception:  # noqa: BLE001 — the listener answers 401
        pass


def test_codex_does_not_send_its_stored_login_to_a_project_endpoint(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener
) -> None:
    # A local OpenAI-compatible server with its own key is a valid pair for
    # FLUID's OpenAI calls, but codex sends its stored login, not
    # OPENAI_API_KEY, to OPENAI_BASE_URL.
    _install_fake_codex(tmp_path, monkeypatch)
    (project / ".env").write_text(
        f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)
    _run_codex()

    assert os.environ["OPENAI_BASE_URL"] == f"{listener.url}/v1"
    assert listener.saw(OPERATOR_KEY) == []


def test_codex_uses_a_project_endpoint_with_the_project_s_codex_key(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener
) -> None:
    _install_fake_codex(tmp_path, monkeypatch)
    (project / ".env").write_text(
        f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
        f"CODEX_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)
    _run_codex()

    assert listener.saw(OPERATOR_KEY) == []
    assert listener.saw(PROJECT_KEY) == ["POST /v1/responses"]


@pytest.mark.parametrize("blank", ["", '""', '"   "'], ids=["empty", "quoted-empty", "whitespace"])
def test_a_blank_project_codex_key_does_not_keep_a_project_endpoint(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener, blank: str
) -> None:
    # The endpoint pairs with OPENAI_API_KEY for FLUID's own calls; codex
    # treats a blank CODEX_API_KEY as unset and sends its stored login.
    from fluid_build.llm.coding_agent import _AGENT_SPECS, _agent_environment

    _install_fake_codex(tmp_path, monkeypatch)
    (project / ".env").write_text(
        f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY={PROJECT_KEY}\nCODEX_API_KEY={blank}\n"
    )

    hydrate_dotenv(project)
    _run_codex()

    assert os.environ["OPENAI_BASE_URL"] == f"{listener.url}/v1"
    assert "OPENAI_BASE_URL" not in _agent_environment(_AGENT_SPECS["codex"])
    assert listener.saw(OPERATOR_KEY) == []


class _OsNamed:
    """``os`` as ``protected_env`` sees it, with only ``name`` replaced."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(os, attr)


def _on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Variable names compare as on Windows (``protected_env`` decides that)."""
    import fluid_build.credentials.protected_env as protected_env

    monkeypatch.setattr(protected_env, "os", _OsNamed("nt"))


def _rekey_environ_as_windows() -> None:
    """``os.environ`` as Windows stores it: under the upper-cased name."""
    for key in list(os.environ):
        if key != key.upper():
            os.environ[key.upper()] = os.environ.pop(key)


@pytest.mark.parametrize("spelling", ["openai_base_url", "OpenAI_Base_Url"])
def test_on_windows_codex_drops_a_project_endpoint_in_any_spelling(
    project: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    from fluid_build.llm.coding_agent import _AGENT_SPECS, _agent_environment

    _on_windows(monkeypatch)
    (project / ".env").write_text(
        f"{spelling}=http://llm.project.test/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)
    _rekey_environ_as_windows()

    assert os.environ["OPENAI_BASE_URL"] == "http://llm.project.test/v1"
    assert "OPENAI_BASE_URL" not in _agent_environment(_AGENT_SPECS["codex"])


@pytest.mark.parametrize("spelling", ["codex_api_key", "Codex_Api_Key"])
def test_on_windows_a_project_codex_key_in_any_spelling_keeps_the_endpoint(
    project: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    from fluid_build.llm.coding_agent import _AGENT_SPECS, _agent_environment

    _on_windows(monkeypatch)
    (project / ".env").write_text(
        f"OPENAI_BASE_URL=http://llm.project.test/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
        f"{spelling}={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)
    _rekey_environ_as_windows()

    env = _agent_environment(_AGENT_SPECS["codex"])
    assert env["OPENAI_BASE_URL"] == "http://llm.project.test/v1"
    assert env["CODEX_API_KEY"] == PROJECT_KEY


def test_on_posix_a_lower_case_project_codex_key_is_another_variable(
    project: Path,
) -> None:
    # codex reads CODEX_API_KEY; on POSIX codex_api_key is not it.
    from fluid_build.llm.coding_agent import _AGENT_SPECS, _agent_environment

    (project / ".env").write_text(
        f"OPENAI_BASE_URL=http://llm.project.test/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
        f"codex_api_key={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)

    assert "OPENAI_BASE_URL" not in _agent_environment(_AGENT_SPECS["codex"])


@pytest.mark.parametrize(
    "decoy", ["DATABRICKS_CLIENT_SECRET", "DATABRICKS_API_KEY", "DATABRICKS_PASSWORD"]
)
def test_databricks_auth_check_does_not_send_the_keyring_token_to_a_project_host(
    project: Path, monkeypatch: pytest.MonkeyPatch, decoy: str
) -> None:
    # cli/_auth_provider_impls.py check_auth: the host from DATABRICKS_HOST,
    # the token from DATABRICKS_TOKEN, else the keyring.
    from fluid_build.cli.auth import AuthResult, AuthStatus, DatabricksAuthProvider

    sent: List[tuple] = []

    def validate(self: Any, host: str, token: str) -> AuthResult:
        sent.append((host, token))
        return AuthResult(provider="databricks", status=AuthStatus.NOT_AUTHENTICATED)

    keyring = {"host": "https://dbx.operator.test", "token": OPERATOR_KEY}
    monkeypatch.setattr(DatabricksAuthProvider, "_validate_via_api", validate)
    monkeypatch.setattr(
        DatabricksAuthProvider, "_get_keyring_credential", lambda self, key: keyring.get(key)
    )
    (project / ".env").write_text(
        f"DATABRICKS_HOST=https://dbx.project.test\n{decoy}={DECOY_KEY}\n"
    )

    hydrate_dotenv(project)
    asyncio.run(DatabricksAuthProvider({}, logging.getLogger("test")).check_auth())

    assert ("https://dbx.project.test", OPERATOR_KEY) not in sent


@pytest.mark.parametrize(
    "decoy,keyring",
    [
        ("SNOWFLAKE_PAT", {"password": OPERATOR_KEY}),
        ("SNOWFLAKE_TOKEN", {"password": OPERATOR_KEY}),
        ("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", {"password": OPERATOR_KEY}),
        ("SNOWFLAKE_OAUTH_CLIENT_SECRET", {"password": OPERATOR_KEY}),
        # a password of its own loses to the keyring's OAuth token: the
        # provider prefers a key path, then a token, then a password
        ("SNOWFLAKE_PASSWORD", {"oauth_token": OPERATOR_KEY}),
        ("PASSWORD", {"oauth_token": OPERATOR_KEY}),
    ],
)
def test_a_snowflake_decoy_does_not_pair_the_account_with_a_keyring_credential(
    project: Path, monkeypatch: pytest.MonkeyPatch, decoy: str, keyring: Dict[str, str]
) -> None:
    from fluid_build.credentials.resolver import BaseCredentialResolver
    from fluid_build.providers.snowflake.util.config import get_connection_params

    monkeypatch.setattr(
        BaseCredentialResolver, "_get_from_keyring", lambda self, key: keyring.get(key)
    )
    (project / ".env").write_text(
        f"SNOWFLAKE_ACCOUNT=project-acct\nSNOWFLAKE_USER=svc\n{decoy}={DECOY_KEY}\n"
    )

    hydrate_dotenv(project)
    try:
        params = get_connection_params(project_root=project, environment="dev")
    except ValueError:  # no account: the project's was refused
        return

    if params.get("account") == "project-acct":
        assert OPERATOR_KEY not in params.values(), sorted(params)


def test_a_project_snowflake_key_path_pairs_the_account(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key path outranks every other method, so a keyring password or
    # token is never what goes out.
    from fluid_build.credentials.resolver import BaseCredentialResolver
    from fluid_build.providers.snowflake.util.config import get_connection_params

    keyring = {"password": OPERATOR_KEY, "oauth_token": OPERATOR_KEY}
    monkeypatch.setattr(
        BaseCredentialResolver, "_get_from_keyring", lambda self, key: keyring.get(key)
    )
    (project / "key.p8").write_text("not a key\n")
    (project / ".env").write_text(
        "SNOWFLAKE_ACCOUNT=project-acct\nSNOWFLAKE_USER=svc\n"  # pragma: allowlist secret
        f"SNOWFLAKE_PRIVATE_KEY_PATH={project / 'key.p8'}\n"
    )

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert params["account"] == "project-acct"
    assert params["private_key_path"] == str(project / "key.p8")
    assert OPERATOR_KEY not in params.values()


@dataclass
class _PostgresCredentials:
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None


def _fake_dlt(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = types.SimpleNamespace(credentials_type=lambda: _PostgresCredentials)
    destination = types.SimpleNamespace(spec=lambda: spec)
    fake = types.ModuleType("dlt")
    fake.destinations = types.SimpleNamespace(postgres=lambda: destination)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dlt", fake)


def _dlt_bridge() -> Dict[str, Optional[str]]:
    # build_runners/dlt/destinations.py copies FLUID's resolved credentials
    # into DESTINATION__POSTGRES__CREDENTIALS__*, keeping any already set.
    from fluid_build.build_runners.dlt.destinations import _dlt_introspect

    _dlt_introspect(
        platform="postgres",
        credentials={"host": "db.operator.test", "user": "operator", "password": OPERATOR_KEY},
        binding={},
        contract={},
        product_id="p",
    )
    section = "DESTINATION__POSTGRES__CREDENTIALS__"
    return {field: os.environ.get(section + field) for field in ("HOST", "PASSWORD")}


@pytest.mark.parametrize(
    "decoy",
    [
        "DESTINATION__SNOWFLAKE__CREDENTIALS__PASSWORD",
        "DESTINATION__POSTGRES__CREDENTIALS__TOKEN",
        "DESTINATION__POSTGRES__CREDENTIALS__PRIVATE_KEY",
    ],
)
def test_the_dlt_bridge_never_pairs_a_project_host_with_the_operator_password(
    project: Path, monkeypatch: pytest.MonkeyPatch, decoy: str
) -> None:
    _fake_dlt(monkeypatch)
    (project / ".env").write_text(
        f"DESTINATION__POSTGRES__CREDENTIALS__HOST=pg.project.test\n{decoy}={DECOY_KEY}\n"
    )

    hydrate_dotenv(project)
    sent = _dlt_bridge()

    assert not (sent["HOST"] == "pg.project.test" and sent["PASSWORD"] == OPERATOR_KEY), sent


def test_the_dlt_bridge_keeps_a_project_host_with_its_own_password(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_dlt(monkeypatch)
    (project / ".env").write_text(
        "DESTINATION__POSTGRES__CREDENTIALS__HOST=pg.project.test\n"
        f"DESTINATION__POSTGRES__CREDENTIALS__PASSWORD={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)

    assert _dlt_bridge() == {"HOST": "pg.project.test", "PASSWORD": PROJECT_KEY}


@pytest.mark.parametrize(
    "switch,where",
    [
        ("REDSHIFT_USE_IAM", "project"),
        ("REDSHIFT_IAM_PROFILE", "project"),
        ("AWS_PROFILE", "project"),
        ("AWS_PROFILE", "operator"),
    ],
)
def test_redshift_iam_auth_never_sends_temporary_credentials_to_a_project_host(
    project: Path, monkeypatch: pytest.MonkeyPatch, switch: str, where: str
) -> None:
    # With IAM auth dbt-redshift gets a temporary database password with the
    # operator's AWS credentials and sends it to the host; the project's own
    # REDSHIFT_PASSWORD is not used.
    lines = [
        "REDSHIFT_HOST=rs.project.test",
        f"REDSHIFT_PASSWORD={DECOY_KEY}",
        "REDSHIFT_CLUSTER_ID=c1",
    ]
    if where == "project":
        lines.append(f"{switch}=on")
    else:
        monkeypatch.setenv(switch, "operator-profile")
    (project / ".env").write_text("\n".join(lines) + "\n")

    hydrate_dotenv(project)
    output = _dbt_output("redshift")

    assert not (output.get("method") == "iam" and output["host"] == "rs.project.test"), output


# ── c. the endpoint, the decoy and the operator's credential in separate files


_SPLITS = [
    (".env", ".env.local"),
    (".env.local", ".env"),
    (".env", ".env.dev"),
    (".env.dev", ".env"),
    (".env.dev", ".env.local"),
    (".env.local", ".env.dev"),
]


@pytest.mark.parametrize("endpoint_file,decoy_file", _SPLITS)
@pytest.mark.parametrize(
    "secrets",
    [
        f"FLUID_LLM_API_KEY={OPERATOR_KEY}\n",
        f"OPENAI_API_KEY={OPERATOR_KEY}\n",
        # an empty value still wins the export, and the keyring key goes out
        "OPENAI_API_KEY=\n",
    ],
    ids=["fluid-key", "same-name", "empty-same-name"],
)
def test_a_split_across_files_does_not_pair_with_the_operator_s_credential(
    tmp_path: Path,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    endpoint_file: str,
    decoy_file: str,
    secrets: str,
) -> None:
    _keyring_holds(monkeypatch, "openai")
    secrets_file = tmp_path / "operator.env"
    secrets_file.write_text(secrets)
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets_file))
    (project / endpoint_file).write_text(f"OPENAI_BASE_URL={listener.url}\n")
    with (project / decoy_file).open("a") as fh:
        fh.write(f"OPENAI_API_KEY={DECOY_KEY}\n")

    hydrate_dotenv(project, environment="dev")
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(OPERATOR_KEY) == []
    assert "OPENAI_BASE_URL" not in os.environ


@pytest.mark.parametrize("endpoint_file,decoy_file", _SPLITS)
def test_a_project_key_in_another_project_file_still_pairs(
    project: Path, listener: Listener, endpoint_file: str, decoy_file: str
) -> None:
    (project / endpoint_file).write_text(f"OPENAI_BASE_URL={listener.url}\n")
    with (project / decoy_file).open("a") as fh:
        fh.write(f"OPENAI_API_KEY={PROJECT_KEY}\n")

    hydrate_dotenv(project, environment="dev")
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(PROJECT_KEY) != []


# ── d. how the files are written ──────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "export OPENAI_BASE_URL={url}/v1",
        'OPENAI_BASE_URL="{url}/v1"',
        "OPENAI_BASE_URL='{url}/v1'",
        "OPENAI_BASE_URL={url}/v1  # local server",
        "OPENAI_BASE_URL={url}/v1\r",
        "﻿OPENAI_BASE_URL={url}/v1",
        "Openai_Base_Url={url}/v1",
        "openai_base_url={url}/v1",
    ],
    ids=[
        "export",
        "double-quoted",
        "single-quoted",
        "comment",
        "crlf",
        "bom",
        "mixed-case",
        "lower",
    ],
)
def test_every_spelling_of_an_endpoint_is_paired_alike(
    project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener, line: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    (project / ".env").write_bytes((line.format(url=listener.url) + "\nOTHER=1\n").encode())

    hydrate_dotenv(project)
    _openai_sdk_call()
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(OPERATOR_KEY) == []
    assert "OPENAI_BASE_URL" not in {key.upper() for key in os.environ}


@pytest.mark.parametrize(
    "decoy_lines",
    [
        # POSIX readers look up OPENAI_API_KEY only
        ["openai_api_key={decoy}"],
        ["Openai_Api_Key={decoy}"],
        # the later line wins and is empty
        ["OPENAI_API_KEY={decoy}", "OPENAI_API_KEY="],
        ["OPENAI_API_KEY=   "],
        # a BOM makes the first name another variable
        ["﻿OPENAI_API_KEY={decoy}"],
    ],
    ids=["lower", "mixed-case", "duplicate-emptied", "blank", "bom"],
)
def test_a_project_key_readers_do_not_see_does_not_pair(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: Listener,
    decoy_lines: List[str],
) -> None:
    _keyring_holds(monkeypatch, "openai")
    lines = [line.format(decoy=DECOY_KEY) for line in decoy_lines]
    lines.append(f"OPENAI_BASE_URL={listener.url}")
    (project / ".env").write_bytes(("\n".join(lines) + "\n").encode())

    hydrate_dotenv(project)
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(OPERATOR_KEY) == []


@pytest.mark.parametrize("operator_value", ["", "   "], ids=["empty", "whitespace"])
def test_an_operator_s_blank_key_keeps_the_project_s_key_out(
    project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener, operator_value: str
) -> None:
    # The operator's variable wins the export even when blank, so the
    # project's key never reaches os.environ and FLUID falls back to the
    # keyring: the endpoint must not load either.
    _keyring_holds(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", operator_value)
    (project / ".env").write_text(f"OPENAI_API_KEY={DECOY_KEY}\nOPENAI_BASE_URL={listener.url}\n")

    hydrate_dotenv(project)
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.saw(OPERATOR_KEY) == []
    assert "OPENAI_BASE_URL" not in os.environ


def test_on_windows_any_spelling_of_the_key_is_the_key() -> None:
    project = {"openai_api_key": PROJECT_KEY, "OPENAI_BASE_URL": "http://h.test"}

    assert "OPENAI_BASE_URL" in unpaired_endpoints(project, {}, case_insensitive_env=False)
    assert unpaired_endpoints(project, {}, case_insensitive_env=True) == {}


# ── f. setups that must keep working ──────────────────────────────────────


def test_a_local_openai_compatible_server_with_a_dummy_key(
    project: Path, listener: Listener
) -> None:
    # Ollama's or LM Studio's OpenAI endpoint, no key of the operator's anywhere.
    (project / ".env").write_text(f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY=lm-studio\n")

    hydrate_dotenv(project)
    _openai_sdk_call()
    _llm_call("openai", "OPENAI_BASE_URL")

    assert [r for r in listener.saw("lm-studio")] == ["GET /v1/models", "POST /v1/chat/completions"]


def test_a_docker_compose_postgres_dotenv(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pydantic_settings")
    from fluid_build.build_runners._credentials import PostgresCredentials
    from fluid_build.output_ports.mcp.drivers.postgres import PostgresDriver

    calls: List[Dict[str, Any]] = []
    fake = types.ModuleType("psycopg")
    fake.connect = lambda **opts: calls.append(opts) or types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    (project / ".env").write_text(
        "POSTGRES_HOST=db\nPOSTGRES_PORT=5432\nPOSTGRES_USER=app\n"
        f"POSTGRES_PASSWORD={PROJECT_KEY}\nPOSTGRES_DB=shop\n"
    )

    hydrate_dotenv(project)
    expose = {
        "exposeId": "orders",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "shop", "table": "orders"},
        },
    }
    PostgresDriver(expose=expose, contract={"id": "p"})._get_connection()

    assert PostgresCredentials().host == "db"
    (opts,) = calls
    assert (opts["host"], opts["password"]) == ("db", PROJECT_KEY)


def test_ci_exporting_both_endpoint_and_key(
    project: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener, operator_listener: Listener
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", operator_listener.url)
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    (project / ".env").write_text(f"OPENAI_BASE_URL={listener.url}\nFLUID_LOG_LEVEL=INFO\n")

    hydrate_dotenv(project)
    _llm_call("openai", "OPENAI_BASE_URL")

    assert listener.requests == []
    assert operator_listener.saw(OPERATOR_KEY) == ["POST /chat/completions"]
    assert os.environ["FLUID_LOG_LEVEL"] == "INFO"


def test_an_operator_exporting_only_the_endpoint(
    project: Path, monkeypatch: pytest.MonkeyPatch, operator_listener: Listener
) -> None:
    # The operator chose the host; the project supplies the key for it.
    monkeypatch.setenv("OPENAI_BASE_URL", operator_listener.url)
    (project / ".env").write_text(f"OPENAI_API_KEY={PROJECT_KEY}\n")

    hydrate_dotenv(project)
    _llm_call("openai", "OPENAI_BASE_URL")

    assert operator_listener.saw(PROJECT_KEY) == ["POST /chat/completions"]


def test_every_ambient_group_names_what_pairs_it() -> None:
    for group in ENDPOINT_CREDENTIAL_GROUPS:
        if group.ambient:
            assert group.paired_by, group.service
            assert all(path for path in group.paired_by), group.service
