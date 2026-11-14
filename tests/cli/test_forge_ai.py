# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid forge`` AI mode driven by the shipped skills (``cli/forge_ai.py``).

No live agent, LLM or subprocess: ``call_llm``, ``_run_agent``, ``_run_interactive`` and
``_run_validate`` are the seams, and each test replaces the ones it reaches.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import fluid_build.cli.forge_ai as forge_ai
from fluid_build.llm.providers import CopilotGenerationError

VALID = """\
fluidVersion: 0.7.6
kind: DataProduct
id: sales.orders
name: Orders
"""


def _args(**kw: Any) -> SimpleNamespace:
    base: Dict[str, Any] = {
        "context": "Orders from orders.csv, owned by ana@example.com, daily at 02:00.",
        "target_dir": None,
        "refine": None,
        "from_product": None,
        "from_product_list": None,
        "from_workspace": None,
        "provider": None,
        "data_product_type": None,
        "transform_engine": None,
        "non_interactive": True,
        "agent": False,
        "yes": True,
        "dry_run": False,
        "forge_agent_mode": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_FORGE_AGENT_MODE", raising=False)
    monkeypatch.delenv("FLUID_FORGE_NO_PREVIEW", raising=False)
    return tmp_path


def _config(provider: str = "anthropic", model: str = "claude-sonnet") -> SimpleNamespace:
    return SimpleNamespace(provider=provider, model=model, agent_mode="envelope")


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_request_defaults_to_the_working_directory(workspace: Path) -> None:
    request = forge_ai.build_request(_args(provider="local", data_product_type="SDP"))
    assert request.contract_path == (workspace / "contract.fluid.yaml").resolve()
    assert request.shown_path() == "contract.fluid.yaml"
    assert request.facts == ["--provider local", "--data-product-type SDP"]
    assert "ana@example.com" in request.intent


def test_request_reads_context_from_a_file(workspace: Path) -> None:
    (workspace / "intent.txt").write_text("Daily revenue by region.\n", encoding="utf-8")
    request = forge_ai.build_request(_args(context="intent.txt", target_dir="products/rev"))
    assert request.intent == "Daily revenue by region."
    assert request.shown_path() == str(Path("products/rev/contract.fluid.yaml"))


def test_request_refuses_to_overwrite_a_contract(workspace: Path) -> None:
    (workspace / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.build_request(_args())
    assert exc.value.event == "forge_contract_exists"
    assert any("--refine" in s for s in exc.value.suggestions)


def test_refine_targets_the_existing_contract(workspace: Path) -> None:
    (workspace / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
    request = forge_ai.build_request(_args(refine="contract.fluid.yaml"))
    assert request.refine and request.contract_path.read_text(encoding="utf-8") == VALID


def test_refine_of_a_missing_contract_is_a_typed_error(workspace: Path) -> None:
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.build_request(_args(refine="nope.fluid.yaml"))
    assert exc.value.event == "forge_refine_contract_missing"


def test_from_product_accepts_a_file_or_a_folder(workspace: Path) -> None:
    for name in ("orders_raw", "customers_raw"):
        product = workspace / "products" / name
        product.mkdir(parents=True)
        (product / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
    request = forge_ai.build_request(
        _args(
            target_dir="products/enriched",
            from_product=["products/orders_raw/contract.fluid.yaml", "products/customers_raw"],
        )
    )
    assert [p.parent.name for p in request.upstreams] == ["orders_raw", "customers_raw"]


def test_an_unknown_upstream_is_a_typed_error(workspace: Path) -> None:
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.build_request(_args(from_product=["no.such.product"]))
    assert exc.value.event == "forge_upstream_not_found"


# ---------------------------------------------------------------------------
# Prompts and answers
# ---------------------------------------------------------------------------


def test_system_prompt_is_the_skills_text(workspace: Path) -> None:
    from fluid_build.agent_skills import SKILLS_DIR

    authoring = (SKILLS_DIR / "authoring-fluid-contracts" / "SKILL.md").read_text(encoding="utf-8")
    composing = (SKILLS_DIR / "composing-fluid-data-products" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    fresh = forge_ai.skills_system_prompt(forge_ai.build_request(_args()))
    assert authoring in fresh
    assert "contract-schema.md" in fresh
    assert composing not in fresh
    (workspace / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
    refine = forge_ai.skills_system_prompt(
        forge_ai.build_request(_args(refine="contract.fluid.yaml"))
    )
    assert composing in refine


def test_user_prompt_carries_the_request_and_the_rules(workspace: Path) -> None:
    upstream = workspace / "up" / "contract.fluid.yaml"
    upstream.parent.mkdir()
    upstream.write_text(VALID, encoding="utf-8")
    request = forge_ai.build_request(_args(provider="gcp", from_product=["up"]))
    prompt = forge_ai.user_prompt(request)
    assert "ana@example.com" in prompt
    assert "--provider gcp" in prompt
    assert "id: sales.orders" in prompt
    assert "Never invent an owner, email" in prompt


def test_repair_prompt_feeds_the_errors_back() -> None:
    prompt = forge_ai.repair_prompt("BASE", "fluidVersion: 0.7.6\n", ["exposes: required"])
    assert prompt.startswith("BASE")
    assert "- exposes: required" in prompt
    assert "fluidVersion: 0.7.6" in prompt


@pytest.mark.parametrize(
    "answer",
    [
        f"Here it is:\n```yaml\n{VALID}```\nDone.",
        f"```\n{VALID}```",
        json.dumps({"contract_yaml": VALID}),
        json.dumps({"contract_yaml": f"```yaml\n{VALID}```"}),
        VALID,
    ],
)
def test_extract_contract_yaml_accepts_each_answer_shape(answer: str) -> None:
    assert forge_ai.extract_contract_yaml(answer).strip() == VALID.strip()


def test_extract_prefers_the_block_with_fluid_version() -> None:
    answer = f"```yaml\nnote: example\n```\n\n```yaml\n{VALID}```"
    assert forge_ai.extract_contract_yaml(answer).startswith("fluidVersion")


def test_yaml_mapping_error() -> None:
    assert forge_ai.yaml_mapping_error(VALID) is None
    assert "not a YAML mapping" in (forge_ai.yaml_mapping_error("- a\n- b\n") or "")
    assert "not valid YAML" in (forge_ai.yaml_mapping_error("a: [\n") or "")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_runs_the_installed_fluid_offline(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[List[str]] = []
    monkeypatch.setattr(forge_ai, "_fluid_command", lambda: ["/venv/bin/fluid"])

    def fake(argv: List[str], *, cwd: Path):
        calls.append(argv)
        assert cwd == workspace
        return 1, json.dumps({"valid": False, "errors": ["metadata.owner: required"]}), ""

    monkeypatch.setattr(forge_ai, "_run_validate", fake)
    outcome = forge_ai.validate_contract(workspace / "c.fluid.yaml", workspace=workspace)
    assert calls == [
        [
            "/venv/bin/fluid",
            "validate",
            str(workspace / "c.fluid.yaml"),
            "--offline",
            "--format",
            "json",
        ]
    ]
    assert not outcome.valid and outcome.errors == ["metadata.owner: required"]


def test_validate_reports_a_crash_as_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forge_ai, "_fluid_command", lambda: ["fluid"])
    monkeypatch.setattr(forge_ai, "_run_validate", lambda argv, cwd: (2, "", "boom\nTraceback"))
    outcome = forge_ai.validate_contract(workspace / "c.yaml", workspace=workspace)
    assert not outcome.valid and outcome.errors == ["boom", "Traceback"]


# ---------------------------------------------------------------------------
# One call with the skills
# ---------------------------------------------------------------------------


def _stub_llm(monkeypatch: pytest.MonkeyPatch, answers: List[str]) -> List[str]:
    prompts: List[str] = []

    def call_llm(provider: Any, config: Any, system: str, user: str, **_: Any) -> str:
        assert "authoring-fluid-contracts" in system or "FLUID" in system
        prompts.append(user)
        return answers.pop(0)

    import fluid_build.llm.providers as providers

    monkeypatch.setattr(providers, "get_llm_provider", lambda name: object())
    monkeypatch.setattr(providers, "call_llm", call_llm)
    return prompts


def test_one_call_repairs_until_valid_and_writes_a_receipt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = _stub_llm(monkeypatch, [f"```yaml\n{VALID}```", f"```yaml\n{VALID}```"])
    outcomes = [
        forge_ai.ValidationOutcome(False, ["exposes: required"]),
        forge_ai.ValidationOutcome(True, []),
    ]
    monkeypatch.setattr(forge_ai, "validate_contract", lambda path, workspace: outcomes.pop(0))
    rc = forge_ai.one_call(forge_ai.build_request(_args()), _config(), args=_args())
    assert rc == 0
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == VALID
    assert len(prompts) == 2 and "- exposes: required" in prompts[1]
    receipts = list(workspace.glob(".fluid/agents/*/cost.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["provider"] == "anthropic"


def test_one_call_gives_up_after_max_attempts_and_keeps_a_draft(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, [VALID] * forge_ai.MAX_ATTEMPTS)
    monkeypatch.setattr(
        forge_ai,
        "validate_contract",
        lambda path, workspace: forge_ai.ValidationOutcome(False, ["owner: required"]),
    )
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.one_call(forge_ai.build_request(_args()), _config(), args=_args())
    assert exc.value.event == "forge_contract_invalid"
    assert not (workspace / "contract.fluid.yaml").exists()
    drafts = list(workspace.glob(".fluid/agents/*/contract.draft.yaml"))
    assert len(drafts) == 1 and any(str(drafts[0]) in s for s in exc.value.suggestions)


def test_one_call_restores_the_original_when_refine_fails(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = VALID + "description: keep me\n"
    (workspace / "contract.fluid.yaml").write_text(original, encoding="utf-8")
    _stub_llm(monkeypatch, ["not: [yaml"] * forge_ai.MAX_ATTEMPTS)
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: pytest.fail("never valid YAML")
    )
    args = _args(refine="contract.fluid.yaml")
    with pytest.raises(CopilotGenerationError):
        forge_ai.one_call(forge_ai.build_request(args), _config(), args=args)
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == original


def test_one_call_dry_run_writes_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_llm(monkeypatch, [VALID])
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    args = _args(dry_run=True)
    assert forge_ai.one_call(forge_ai.build_request(args), _config(), args=args) == 0
    assert not (workspace / "contract.fluid.yaml").exists()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _route(monkeypatch: pytest.MonkeyPatch, provider: str, args: SimpleNamespace, tty: bool):
    import fluid_build.llm.providers as providers

    monkeypatch.setattr(providers, "preflight_llm_provider", lambda a: None)
    monkeypatch.setattr(providers, "resolve_llm_config", lambda a: _config(provider))
    monkeypatch.setattr(forge_ai, "_interactive", lambda a: tty)
    seen: Dict[str, Any] = {}
    for name in ("hand_off", "drive_headless"):
        monkeypatch.setattr(
            forge_ai,
            name,
            lambda agent, req, cfg, args, console, _n=name: seen.setdefault(_n, agent) and 0,
        )
    monkeypatch.setattr(
        forge_ai,
        "one_call",
        lambda req, cfg, args, console: seen.setdefault("one_call", cfg.provider) and 0,
    )
    forge_ai.run_ai_mode(args)
    return seen


def test_routing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _route(monkeypatch, "claude-code", _args(non_interactive=False), True) == {
        "hand_off": "claude-code"
    }
    assert _route(monkeypatch, "claude-code", _args(), False) == {"drive_headless": "claude-code"}
    assert _route(monkeypatch, "codex", _args(), False) == {"drive_headless": "codex"}
    assert _route(monkeypatch, "kiro", _args(), False) == {"one_call": "kiro"}
    assert _route(monkeypatch, "anthropic", _args(), False) == {"one_call": "anthropic"}
    assert _route(monkeypatch, "claude-code", _args(forge_agent_mode="envelope"), False) == {
        "one_call": "claude-code"
    }


def test_headless_run_without_a_description_is_a_typed_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CopilotGenerationError) as exc:
        _route(monkeypatch, "anthropic", _args(context=None), False)
    assert exc.value.event == "forge_ai_needs_context"


# ---------------------------------------------------------------------------
# Coding agents
# ---------------------------------------------------------------------------


def test_claude_headless_argv_is_the_measured_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fluid_build.llm.coding_agent as ca
    from fluid_build.agent_skills import PACK_DIR

    monkeypatch.setattr(
        ca,
        "_cli_help",
        lambda binary: "--setting-sources --strict-mcp-config "
        "--no-session-persistence --plugin-dir",
    )
    argv = forge_ai.claude_argv("claude", _config("claude-code", "sonnet"), headless=True)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--plugin-dir") + 1] == str(PACK_DIR)
    assert "--setting-sources=" in argv and "--strict-mcp-config" in argv
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Bash(fluid validate *)" in tools and f"Read(/{PACK_DIR}/**)" in tools
    assert not any("apply" in t or "publish" in t or t == "Bash(fluid forge *)" for t in tools)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "authoring-fluid-contracts" in argv[argv.index("--append-system-prompt") + 1]


def test_claude_interactive_prompt_precedes_variadic_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import fluid_build.llm.coding_agent as ca

    monkeypatch.setattr(ca, "_cli_help", lambda binary: "--plugin-dir")
    argv = forge_ai.claude_argv(
        "claude", _config("claude-code", "claude-code"), headless=False, prompt="P"
    )
    assert argv[1] == "P" and "-p" not in argv and "--model" not in argv


def test_codex_headless_runs_in_its_workspace_sandbox(workspace: Path) -> None:
    argv = forge_ai.codex_argv("codex", _config("codex", "codex"), workspace, "P", headless=True)
    assert argv == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "P",
    ]


def _stub_agent(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Dict[str, Any]:
    import fluid_build.llm.coding_agent as ca

    seen: Dict[str, Any] = {}
    monkeypatch.setattr(forge_ai, "_binary", lambda agent: "/bin/" + agent)
    monkeypatch.setattr(forge_ai, "_agent_env", lambda agent: {"PATH": "/bin"})
    monkeypatch.setattr(ca, "_cli_help", lambda binary: "--plugin-dir")

    def run_agent(argv: List[str], *, stdin: Any, timeout: int, cwd: str, env: Dict[str, str]):
        seen.update(argv=argv, stdin=stdin, cwd=cwd)
        if kw.get("write", True):
            (Path(cwd) / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
        return kw.get("rc", 0), kw.get("stdout", ""), ""

    monkeypatch.setattr(ca, "_run_agent", run_agent)
    return seen


def test_headless_claude_writes_validates_and_records_cost(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {
        "result": "done",
        "total_cost_usd": 0.21,
        "usage": {"input_tokens": 9, "output_tokens": 4},
    }
    seen = _stub_agent(monkeypatch, stdout=json.dumps(report))
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    request = forge_ai.build_request(_args())
    rc = forge_ai.drive_headless("claude-code", request, _config("claude-code"), args=_args())
    assert rc == 0
    assert seen["cwd"] == str(workspace) and "contract.fluid.yaml" in seen["stdin"]
    assert "nobody will answer questions" in seen["stdin"]
    cost = json.loads(next(workspace.glob(".fluid/agents/*/cost.json")).read_text(encoding="utf-8"))
    assert cost["total_usd"] == 0.21 and cost["total_tokens"] == 13


def test_headless_agent_error_is_typed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_agent(
        monkeypatch, write=False, stdout=json.dumps({"is_error": True, "result": "safeguards"})
    )
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert exc.value.event == "coding_agent_failed" and "safeguards" in exc.value.message


def test_agent_that_writes_nothing_fails_the_gate(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch, write=False)
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "codex", forge_ai.build_request(_args()), _config("codex"), args=_args()
        )
    assert exc.value.event == "forge_contract_missing"


def test_hand_off_opens_the_agent_then_validates(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}
    monkeypatch.setattr(forge_ai, "_binary", lambda agent: "/bin/kiro-cli")
    monkeypatch.setattr(forge_ai, "_agent_env", lambda agent: {})

    def run_interactive(argv: List[str], *, cwd: Path, env: Dict[str, str]) -> int:
        seen["argv"] = argv
        (cwd / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
        return 0

    monkeypatch.setattr(forge_ai, "_run_interactive", run_interactive)
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    request = forge_ai.build_request(_args(context=None, non_interactive=False))
    assert forge_ai.hand_off("kiro", request, _config("kiro"), args=_args()) == 0
    assert seen["argv"][:2] == ["/bin/kiro-cli", "chat"]
    assert "Ask me what it reads" in seen["argv"][2]
    assert "nobody will answer" not in seen["argv"][2]


def test_invalid_agent_contract_reports_the_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    monkeypatch.setattr(
        forge_ai,
        "validate_contract",
        lambda path, workspace: forge_ai.ValidationOutcome(False, ["builds: required"]),
    )
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "codex", forge_ai.build_request(_args()), _config("codex"), args=_args()
        )
    assert exc.value.event == "forge_contract_invalid"
    assert "fluid validate: builds: required" in exc.value.suggestions
