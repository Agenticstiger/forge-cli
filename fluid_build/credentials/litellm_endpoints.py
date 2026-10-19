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

"""Where litellm reads an LLM provider's endpoint, vendored.

``credentials.protected_env`` pairs these with ``FLUID_LLM_API_KEY`` only:
FLUID routes any provider litellm knows and passes it the key it resolved
(``FLUID_LLM_API_KEY``, else the keyring), which beats a provider key in the
project's files. A group in ``protected_env`` that lists one of the names
(``IBM watsonx``, ``OpenAI`` ...) pairs it by its own rule instead, and a name
litellm sends something else to (``LITELLM_PROXY_API_BASE``, the GitHub
Copilot ones) is protected outright. Any other ``<NAME>_API_BASE`` is the
project's own setting and follows the name-shape rule.

Vendored because importing litellm takes seconds and dotenv hydration runs on
every command. Generated from the litellm 1.83.14, 1.97.0, 1.105.0 and
1.106.0 wheels (FLUID's floor, the last release Python 3.10 can import, and the
two newest), as the union of:

* ``<PROVIDER>_API_BASE`` for every ``litellm.provider_list`` entry (``-``
  read as ``_``);
* every ``*_API_BASE`` name its provider code reads;
* every name its provider code reads through ``get_secret``,
  ``get_secret_str``, ``get_secret_bool``, ``os.getenv`` or ``os.environ``
  that ends in ``_URL``, ``_ENDPOINT``, ``_HOST``, ``_BASE``, ``_ACCOUNT_ID``
  or ``_SERVER``, except one an endpoint group already lists
  (``OPENAI_BASE_URL``, ``AZURE_AUTHORITY_HOST`` ...) and
  :data:`LITELLM_NOT_PROVIDER_ENDPOINTS`, the names that read like an
  endpoint but choose no provider host;
* every ``api_base_env`` of its JSON registry of OpenAI-compatible providers.

Provider code is everything outside ``proxy/``, ``integrations/``,
``caching/``, ``secret_managers/``, ``rust_bridge/`` and ``router_strategy/``,
whose endpoints get their own credentials.

``tests/test_litellm_endpoint_names.py`` fails when the installed litellm
reads such a name that neither this list, a group, nor
:data:`LITELLM_NOT_PROVIDER_ENDPOINTS` covers.
"""

from typing import FrozenSet

LITELLM_ENDPOINT_NAMES: FrozenSet[str] = frozenset(
    {
        "A2A_AGENT_API_BASE",
        "A2A_API_BASE",
        "AGENTCORE_GATEWAY_URL",
        "AI21_API_BASE",
        "AI21_CHAT_API_BASE",
        "AIHUBMIX_API_BASE",
        "AIML_API_BASE",
        "AIOHTTP_OPENAI_API_BASE",
        "ALEPH_ALPHA_API_BASE",
        "AMAZON_NOVA_API_BASE",
        "ANTHROPIC_API_BASE",
        "ANTHROPIC_AWS_API_BASE",
        "ANTHROPIC_AWS_BASE_URL",
        "ANTHROPIC_TEXT_API_BASE",
        "ANYSCALE_API_BASE",
        "APERTIS_API_BASE",
        "APISERPENT_API_BASE",
        "ARK_API_BASE",
        "ASSEMBLYAI_API_BASE",
        "AUTO_ROUTER_API_BASE",
        "AWS_POLLY_API_BASE",
        "AWS_TEXTRACT_API_BASE",
        "AZURE_AI_API_BASE",
        "AZURE_API_BASE",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_SPEECH_API_BASE",
        "AZURE_TEXT_API_BASE",
        "BASETEN_API_BASE",
        "BEDROCK_API_BASE",
        "BEDROCK_MANTLE_API_BASE",
        "BFL_API_BASE",
        "BLACK_FOREST_LABS_API_BASE",
        "BRAVE_API_BASE",
        "BYTEZ_API_BASE",
        "CEREBRAS_API_BASE",
        "CHARITY_ENGINE_API_BASE",
        "CHATGPT_API_BASE",
        "CHUTES_API_BASE",
        "CLARIFAI_API_BASE",
        "CLOUDFLARE_API_BASE",
        "CODESTRAL_API_BASE",
        "COGNITION_API_BASE",
        "COHERE_API_BASE",
        "COHERE_CHAT_API_BASE",
        "COMETAPI_API_BASE",
        "COMETAPI_BASE_URL",
        "COMPACTIFAI_API_BASE",
        "CORTECS_API_BASE",
        "CRUSOE_API_BASE",
        "CRW_API_BASE",
        "CURSOR_API_BASE",
        "CUSTOM_API_BASE",
        "CUSTOM_OPENAI_API_BASE",
        "DARKBLOOM_API_BASE",
        "DASHSCOPE_API_BASE",
        "DATABRICKS_API_BASE",
        "DATAFORSEO_API_BASE",
        "DATAROBOT_API_BASE",
        "DATAROBOT_ENDPOINT",
        "DEEPGRAM_API_BASE",
        "DEEPINFRA_API_BASE",
        "DEEPSEEK_ANTHROPIC_API_BASE",
        "DEEPSEEK_API_BASE",
        "DEFAULT_API_BASE",
        "DOCKER_MODEL_RUNNER_API_BASE",
        "DOTPROMPT_API_BASE",
        "DUCKDUCKGO_API_BASE",
        "EDENAI_API_BASE",
        "ELEVENLABS_API_BASE",
        "EMPIRIOLABS_API_BASE",
        "EMPOWER_API_BASE",
        "EXA_API_BASE",
        "FAL_AI_API_BASE",
        "FAL_AI_QUEUE_API_BASE",
        "FEATHERLESS_AI_API_BASE",
        "FEATHERLESS_API_BASE",
        "FIRECRAWL_API_BASE",
        "FIREWORKS_AI_API_BASE",
        "FIREWORKS_API_BASE",
        "FRIENDLIAI_API_BASE",
        "FRIENDLI_API_BASE",
        "GALADRIEL_API_BASE",
        "GDC_API_BASE",
        "GEMINI_API_BASE",
        "GIGACHAT_API_BASE",
        "GIGACHAT_AUTH_URL",
        "GITHUB_API_BASE",
        "GITHUB_COPILOT_ACCESS_TOKEN_URL",
        "GITHUB_COPILOT_API_BASE",
        "GITHUB_COPILOT_API_KEY_URL",
        "GITHUB_COPILOT_DEVICE_CODE_URL",
        "GOOGLE_PSE_API_BASE",
        "GRADIENT_AI_AGENT_ENDPOINT",
        "GRADIENT_AI_API_BASE",
        "GROQ_API_BASE",
        "HELICONE_API_BASE",
        "HEROKU_API_BASE",
        "HF_API_BASE",
        "HOSTED_VLLM_API_BASE",
        "HUGGINGFACE_API_BASE",
        "HUMANLOOP_API_BASE",
        "HYPERBOLIC_API_BASE",
        "INCEPTION_API_BASE",
        "INFINITY_API_BASE",
        "JINA_AI_API_BASE",
        "LAMBDA_AI_API_BASE",
        "LAMBDA_API_BASE",
        "LANGFLOW_API_BASE",
        "LANGFUSE_API_BASE",
        "LANGGRAPH_API_BASE",
        "LEMONADE_API_BASE",
        "LIBERTAI_API_BASE",
        "LINKUP_API_BASE",
        "LITELLM_AGENT_API_BASE",
        "LITELLM_PROXY_API_BASE",
        "LLAMAFILE_API_BASE",
        "LLAMA_API_BASE",
        "LM_STUDIO_API_BASE",
        "MANUS_API_BASE",
        "MARITALK_API_BASE",
        "META_API_BASE",
        "META_LLAMA_API_BASE",
        "MILVUS_API_BASE",
        "MINIMAX_API_BASE",
        "MISTRAL_API_BASE",
        "MISTRAL_AZURE_API_BASE",
        "MODELSCOPE_API_BASE",
        "MONGODB_API_BASE",
        "MOONSHOT_API_BASE",
        "MORPH_API_BASE",
        "NADIR_API_BASE",
        "NANO_GPT_API_BASE",
        "NEBIUS_API_BASE",
        "NEOSANTARA_API_BASE",
        "NIMBLE_API_BASE",
        "NLP_CLOUD_API_BASE",
        "NOVITA_API_BASE",
        "NSCALE_API_BASE",
        "NVIDIA_NIM_API_BASE",
        "NVIDIA_RIVA_API_BASE",
        "OCI_API_BASE",
        "OLLAMA_API_BASE",
        "OLLAMA_CHAT_API_BASE",
        "OOBABOOGA_API_BASE",
        "OPENAI_API_BASE",
        "OPENAI_CHATGPT_API_BASE",
        "OPENAI_LIKE_API_BASE",
        "OPENROUTER_API_BASE",
        "OPEN_SANDBOX_API_BASE",
        "OVHCLOUD_API_BASE",
        "PARALLEL_AI_API_BASE",
        "PARASAIL_API_BASE",
        "PERPLEXITY_API_BASE",
        "PETALS_API_BASE",
        "PG_VECTOR_API_BASE",
        "PINSTRIPES_API_BASE",
        "POE_API_BASE",
        "PREDIBASE_API_BASE",
        "PRISM_API_BASE",
        "PUBLICAI_API_BASE",
        "QWENCLOUD_API_BASE",
        "QWEN_AI_PLATFORM_API_BASE",
        "RAGFLOW_API_BASE",
        "RECRAFT_API_BASE",
        "REDUCTO_API_BASE",
        "REKA_API_BASE",
        "REPLICATE_API_BASE",
        "RUNWAYML_API_BASE",
        "S3_VECTORS_API_BASE",
        "SAGEMAKER_API_BASE",
        "SAGEMAKER_CHAT_API_BASE",
        "SAGEMAKER_NOVA_API_BASE",
        "SAIL_API_BASE",
        "SAMBANOVA_API_BASE",
        "SAP_API_BASE",
        "SCALEWAY_API_BASE",
        "SCX_AI_API_BASE",
        "SCX_API_BASE",
        "SEARCHAPI_API_BASE",
        "SEARXNG_API_BASE",
        "SERPER_API_BASE",
        "SNOWFLAKE_ACCOUNT_ID",
        "SNOWFLAKE_API_BASE",
        "SONIOX_API_BASE",
        "STABILITY_API_BASE",
        "STRANDS_DECIDER_API_BASE",
        "SYNTHETIC_API_BASE",
        "TAVILY_API_BASE",
        "TENCENT_ANTHROPIC_API_BASE",
        "TENCENT_API_BASE",
        "TENSORMESH_API_BASE",
        "TENSORMESH_SERVERLESS_BASE_URL",
        "TEXT_COMPLETION_CODESTRAL_API_BASE",
        "TEXT_COMPLETION_INCEPTION_API_BASE",
        "TEXT_COMPLETION_OPENAI_API_BASE",
        "TINYFISH_API_BASE",
        "TOGETHER_AI_API_BASE",
        "TOPAZ_API_BASE",
        "TRANSCRIBE_API_BASE",
        "TRITON_API_BASE",
        "TYPESAFE_API_BASE",
        "V0_API_BASE",
        "VALKEY_API_BASE",
        "VERCEL_AI_GATEWAY_API_BASE",
        "VERTEXAI_API_BASE",
        "VERTEX_AI_API_BASE",
        "VERTEX_AI_BETA_API_BASE",
        "VERTEX_API_BASE",
        "VLLM_API_BASE",
        "VOLCENGINE_API_BASE",
        "VOYAGE_API_BASE",
        "WANDB_API_BASE",
        "WATSONX_API_BASE",
        "WATSONX_IAM_URL",
        "WATSONX_TEXT_API_BASE",
        "WATSONX_URL",
        "WML_URL",
        "WX_URL",
        "XAI_API_BASE",
        "XAI_OAUTH_API_BASE",
        "XIAOMI_MIMO_API_BASE",
        "XINFERENCE_API_BASE",
        "YOUCOM_API_BASE",
        "ZAI_API_BASE",
    }
)

#: Names litellm reads that end like an endpoint but choose no host a provider
#: request, or the key FLUID resolved, goes to; each read where litellm uses it
#: (litellm 1.83.14 to 1.105.0). The drift test skips them.
LITELLM_NOT_PROVIDER_ENDPOINTS: FrozenSet[str] = frozenset(
    {
        # a connection-pool size (llms/custom_httpx/http_handler.py)
        "AIOHTTP_CONNECTOR_LIMIT_PER_HOST",
        # header values sent to OpenRouter's and Vercel's own hosts (main.py)
        "OR_SITE_URL",
        "VERCEL_SITE_URL",
        # litellm's own downloads of public JSON or RSS (__init__.py): no
        # credential goes there, and no entry of them names a host
        "LITELLM_ANTHROPIC_BETA_HEADERS_URL",
        "LITELLM_AUTOROUTER_PRESETS_URL",
        "LITELLM_BLOG_POSTS_URL",
        "LITELLM_MODEL_COST_MAP_URL",
        # callback loggers, litellm's cache and its tracing receiver, which
        # run only when configured and send their own credentials, which the
        # name-shape rule pairs (LANGTRACE_API_KEY, LOGFIRE_TOKEN,
        # REDIS_PASSWORD, CLICKHOUSE_PASSWORD)
        "LANGTRACE_API_HOST",
        "LOGFIRE_BASE_URL",
        "REDIS_HOST",
        "REDIS_URL",
        "CLICKHOUSE_URL",
        "CLICKHOUSE_READER_URL",
        # a proxy guardrail's setting (types/proxy/guardrails)
        "ZSCALER_AI_GUARD_URL",
        # account ids that fill a path under the vendor's fixed host
        # (https://api.cloudflare.com/client/v4/accounts/<id>/...,
        # <FIREWORKS_API_BASE>/v1/accounts/<id>/models)
        "CLOUDFLARE_ACCOUNT_ID",
        "FIREWORKS_ACCOUNT_ID",
        # OpenAI workload identity: the account the openai SDK exchanges the
        # operator's identity token for at OpenAI's own token endpoint
        "OPENAI_SERVICE_ACCOUNT_ID",
    }
)
