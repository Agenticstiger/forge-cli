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
    assert _route(monkeypatch, "codex", _args(), False) == {"one_call": "codex"}
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


_HELP = "--setting-sources --strict-mcp-config --no-session-persistence --plugin-dir"


def test_claude_headless_argv_grants_the_contract_file_and_nothing_wider(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca
    from fluid_build.agent_skills import PACK_DIR

    monkeypatch.setattr(ca, "_cli_help", lambda binary: _HELP)
    request = forge_ai.build_request(_args(target_dir="products/orders"))
    argv = forge_ai.claude_argv("claude", _config("claude-code", "sonnet"), request, headless=True)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--plugin-dir") + 1] == str(PACK_DIR)
    assert "--setting-sources=" in argv and "--strict-mcp-config" in argv
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    contract = str(Path("products/orders/contract.fluid.yaml"))
    assert f"Edit(./{contract})" in tools
    assert f"Bash(fluid validate {contract} --offline)" in tools
    assert f"Read(/{PACK_DIR}/**)" in tools
    # No rule lets the agent write elsewhere: no Edit wildcard, and no fluid stage with a
    # wildcard (bundle/plan/diff --out, validate --report all take absolute paths).
    assert not any(t.startswith("Edit(") and "*" in t for t in tools)
    assert not any(t.startswith("Bash(fluid") and "*" in t for t in tools)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "authoring-fluid-contracts" in argv[argv.index("--append-system-prompt") + 1]


def test_headless_claude_refuses_without_its_isolation_flags(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    monkeypatch.setattr(ca, "_cli_help", lambda binary: "--plugin-dir")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.claude_argv(
            "claude", _config("claude-code"), forge_ai.build_request(_args()), headless=True
        )
    assert exc.value.event == "coding_agent_too_old"
    assert "--setting-sources" in exc.value.message


def test_an_absolute_contract_path_takes_the_double_slash_rule(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    request = forge_ai.AuthoringRequest(
        intent="x", workspace=tmp_path / "ws", contract_path=outside / "contract.fluid.yaml"
    )
    assert forge_ai.agent_rules(request)[0] == f"Edit(/{outside / 'contract.fluid.yaml'})"


def test_claude_interactive_prompt_precedes_variadic_flags(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    monkeypatch.setattr(ca, "_cli_help", lambda binary: "--plugin-dir")
    request = forge_ai.build_request(_args())
    argv = forge_ai.claude_argv(
        "claude", _config("claude-code", "claude-code"), request, headless=False, prompt="P"
    )
    assert argv[1] == "P" and "-p" not in argv and "--model" not in argv


def test_codex_opens_interactively_in_the_workspace(workspace: Path) -> None:
    argv = forge_ai.codex_argv("codex", _config("codex", "codex"), workspace, "P")
    assert argv == ["codex", "-C", str(workspace), "P"]


def _stub_agent(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Dict[str, Any]:
    import fluid_build.llm.coding_agent as ca

    seen: Dict[str, Any] = {}
    monkeypatch.setattr(forge_ai, "_binary", lambda agent: "/bin/" + agent)
    monkeypatch.setattr(forge_ai, "_agent_env", lambda agent: {"PATH": "/bin"})
    monkeypatch.setattr(ca, "_cli_help", lambda binary: _HELP)

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
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
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
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert exc.value.event == "forge_contract_invalid"
    assert "fluid validate: builds: required" in exc.value.suggestions


def test_headless_run_makes_the_target_folder_first(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    _stub_agent(monkeypatch)
    made: Dict[str, bool] = {}

    def run_agent(argv: List[str], *, stdin: Any, timeout: int, cwd: str, env: Dict[str, str]):
        target = Path(cwd) / "products" / "new"
        made["dir"] = target.is_dir()
        (target / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(ca, "_run_agent", run_agent)
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    request = forge_ai.build_request(_args(target_dir="products/new"))
    assert (
        forge_ai.drive_headless("claude-code", request, _config("claude-code"), args=_args()) == 0
    )
    assert made["dir"]


@pytest.mark.parametrize(
    "path, safe",
    [
        ("products/orders/contract.fluid.yaml", True),
        ("/abs/products/contract.fluid.yaml", True),
        ("my products/contract.fluid.yaml", False),
        ("a,b/contract.fluid.yaml", False),
        ("x(1)/contract.fluid.yaml", False),
        ("p*/contract.fluid.yaml", False),
        ("C:\\work\\contract.fluid.yaml", False),
    ],
)
def test_rule_safe(path: str, safe: bool) -> None:
    assert forge_ai.rule_safe(path) is safe


def test_a_path_no_rule_can_carry_gets_no_rules_and_no_headless_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    monkeypatch.setattr(ca, "_cli_help", lambda binary: _HELP)
    request = forge_ai.build_request(_args(target_dir="my products/orders"))
    assert forge_ai.agent_rules(request) == []
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.claude_argv("claude", _config("claude-code"), request, headless=True)
    assert exc.value.event == "forge_contract_path_unsupported"
    # Interactive still opens, with the read-only base rules only: the user approves edits.
    argv = forge_ai.claude_argv(
        "claude", _config("claude-code"), request, headless=False, prompt="P"
    )
    assert not any(t.startswith("Edit(") for t in argv[argv.index("--allowedTools") + 1].split(","))


def test_a_failed_agent_refine_puts_the_original_back_and_keeps_a_draft(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = VALID + "description: keep me\n"
    (workspace / "contract.fluid.yaml").write_text(original, encoding="utf-8")
    _stub_agent(monkeypatch)  # the agent overwrites the contract with VALID
    monkeypatch.setattr(
        forge_ai,
        "validate_contract",
        lambda path, workspace: forge_ai.ValidationOutcome(False, ["builds: required"]),
    )
    args = _args(refine="contract.fluid.yaml")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(args), _config("claude-code"), args=args
        )
    assert exc.value.event == "forge_contract_invalid"
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == original
    drafts = list(workspace.glob(".fluid/agents/*/contract.draft.yaml"))
    assert len(drafts) == 1 and drafts[0].read_text(encoding="utf-8") == VALID


def test_a_failed_fresh_agent_run_leaves_no_invalid_contract(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    monkeypatch.setattr(
        forge_ai,
        "validate_contract",
        lambda path, workspace: forge_ai.ValidationOutcome(False, ["builds: required"]),
    )
    with pytest.raises(CopilotGenerationError):
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert not (workspace / "contract.fluid.yaml").exists()
    assert list(workspace.glob(".fluid/agents/*/contract.draft.yaml"))


def test_an_agent_that_deletes_the_refined_contract_gets_it_restored(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    (workspace / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
    _stub_agent(monkeypatch, write=False)

    def deleting_agent(argv, *, stdin, timeout, cwd, env):
        (Path(cwd) / "contract.fluid.yaml").unlink()
        return 0, "", ""

    monkeypatch.setattr(ca, "_run_agent", deleting_agent)
    args = _args(refine="contract.fluid.yaml")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(args), _config("claude-code"), args=args
        )
    assert exc.value.event == "forge_contract_missing"
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == VALID


def test_headless_timeout_is_a_typed_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import fluid_build.llm.coding_agent as ca

    _stub_agent(monkeypatch)

    def slow(argv, *, stdin, timeout, cwd, env):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(ca, "_run_agent", slow)
    monkeypatch.setenv("FLUID_FORGE_AGENT_TIMEOUT_SECONDS", "7")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert exc.value.event == "coding_agent_timeout" and "7s" in exc.value.message


def test_declining_the_one_call_contract_restores_the_original(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = VALID + "description: keep me\n"
    (workspace / "contract.fluid.yaml").write_text(original, encoding="utf-8")
    _stub_llm(monkeypatch, [VALID])
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    monkeypatch.setattr(forge_ai, "_interactive", lambda a: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    args = _args(refine="contract.fluid.yaml", yes=False, non_interactive=False)
    assert forge_ai.one_call(forge_ai.build_request(args), _config(), args=args) == 1
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == original


def test_an_upstream_outside_the_workspace_gets_an_exact_read_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    other = tmp_path / "other" / "contract.fluid.yaml"
    other.parent.mkdir(parents=True)
    ws.mkdir()
    other.write_text(VALID, encoding="utf-8")
    monkeypatch.chdir(ws)
    request = forge_ai.build_request(_args(from_product=[str(other)]))
    assert f"Read(/{other.resolve().as_posix()})" in forge_ai.agent_rules(request)


def test_ci_is_scaffolded_after_a_kept_contract_but_not_under_agent(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.cli.forge as forge_cli

    calls: List[Path] = []
    monkeypatch.setattr(
        forge_cli, "_scaffold_ci_pipeline", lambda a, t, *rest, **kw: calls.append(t)
    )
    request = forge_ai.build_request(_args())
    forge_ai._scaffold_ci(request, _args(ci="github_actions"), console=None)
    forge_ai._scaffold_ci(request, _args(agent=True), console=None)
    assert calls == [request.target_dir]


def test_validate_runs_the_real_fluid_cli(workspace: Path) -> None:
    """No stub: the installed ``fluid validate --offline --format json``, as forge runs it."""
    from fluid_build.agent_skills import SKILLS_DIR
    from fluid_build.agent_skills.hooks.validate_contract import find_fluid

    if find_fluid() is None:
        pytest.skip("no `fluid` console script in this environment")
    bad = workspace / "bad.fluid.yaml"
    bad.write_text("fluidVersion: 0.7.6\nkind: DataProduct\n", encoding="utf-8")
    outcome = forge_ai.validate_contract(bad, workspace=workspace)
    assert not outcome.valid and outcome.errors
    good = workspace / "good.fluid.yaml"
    example = (
        SKILLS_DIR / "authoring-fluid-contracts" / "references" / "example.contract.fluid.yaml"
    )
    good.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    assert forge_ai.validate_contract(good, workspace=workspace).valid


# ---------------------------------------------------------------------------
# Answers, prompts and intents the first version misread
# ---------------------------------------------------------------------------


def test_extract_skips_a_shell_block_before_the_contract() -> None:
    answer = f"Then run:\n```bash\nfluid validate contract.fluid.yaml\n```\n\n```yaml\n{VALID}```"
    assert forge_ai.extract_contract_yaml(answer).strip() == VALID.strip()


def test_extract_prefers_the_yaml_block_to_a_shell_block_naming_fluid_version() -> None:
    answer = f"```bash\ngrep fluidVersion contract.fluid.yaml\n```\n```yaml\n{VALID}```"
    assert forge_ai.extract_contract_yaml(answer).strip() == VALID.strip()


def test_extract_ignores_inline_triple_backticks_before_the_contract() -> None:
    answer = f"Use ```fluid validate``` after.\n```yaml\n{VALID}```"
    assert forge_ai.extract_contract_yaml(answer).strip() == VALID.strip()


def test_extract_reads_a_fence_indented_inside_a_list() -> None:
    body = "".join(f"    {line}\n" for line in VALID.splitlines())
    answer = f"1. The contract:\n    ```yaml\n{body}    ```\n"
    assert forge_ai.extract_contract_yaml(answer).strip() == VALID.strip()


@pytest.mark.parametrize(
    "text", ["~10M events/day from Kafka, owned by data@example.com", "a\x00b"]
)
def test_free_text_that_looks_like_a_path_stays_the_intent(workspace: Path, text: str) -> None:
    assert forge_ai._read_intent(text) == text


def test_the_agent_prompt_leads_with_its_own_line(workspace: Path) -> None:
    request = forge_ai.build_request(_args(context="/compact the orders feed"))
    for prompt in (
        forge_ai.agent_prompt(request, interactive=False),
        forge_ai.interactive_argv("claude-code", "/bin/claude", _config("claude-code"), request)[1],
    ):
        assert prompt.splitlines()[0] == "FLUID data product request:"
        assert "/compact the orders feed" in prompt


def test_a_refine_prompt_carries_the_whole_contract(workspace: Path) -> None:
    long = VALID + "".join(f"# note {i}\n" for i in range(forge_ai.UPSTREAM_MAX_CHARS // 8))
    (workspace / "contract.fluid.yaml").write_text(long, encoding="utf-8")
    request = forge_ai.build_request(_args(refine="contract.fluid.yaml"))
    assert long.rstrip() in forge_ai.user_prompt(request)


# ---------------------------------------------------------------------------
# Records: one folder per run, a receipt on every paid exit, nothing left behind
# ---------------------------------------------------------------------------


def _runs(root: Path) -> List[Path]:
    return sorted(root.glob(".fluid/agents/*"))


def test_a_failed_one_call_keeps_its_draft_and_receipt_in_one_run_folder(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, [VALID] * forge_ai.MAX_ATTEMPTS)
    monkeypatch.setattr(
        forge_ai,
        "validate_contract",
        lambda path, workspace: forge_ai.ValidationOutcome(False, ["owner: required"]),
    )
    with pytest.raises(CopilotGenerationError):
        forge_ai.one_call(forge_ai.build_request(_args()), _config(), args=_args())
    (run,) = _runs(workspace)
    assert (run / "contract.draft.yaml").is_file()
    cost = json.loads((run / "cost.json").read_text(encoding="utf-8"))
    assert cost["provider"] == "anthropic" and cost["mode"] == "llm"


def test_a_dry_run_into_a_new_folder_leaves_nothing_behind(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, [VALID])
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    args = _args(dry_run=True, target_dir="products/new")
    assert forge_ai.one_call(forge_ai.build_request(args), _config(), args=args) == 0
    assert not (workspace / "products").exists() and not (workspace / ".fluid").exists()


def test_a_declined_contract_removes_its_new_folder_and_keeps_the_receipt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, [VALID])
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    monkeypatch.setattr(forge_ai, "_interactive", lambda a: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    args = _args(target_dir="products/new", yes=False, non_interactive=False)
    assert forge_ai.one_call(forge_ai.build_request(args), _config(), args=args) == 1
    assert not (workspace / "products").exists()
    (run,) = _runs(workspace)
    assert (run / "cost.json").is_file()


def test_a_kept_contract_records_its_run_in_the_product_folder(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, [VALID])
    monkeypatch.setattr(
        forge_ai, "validate_contract", lambda path, workspace: forge_ai.ValidationOutcome(True)
    )
    args = _args(target_dir="products/new")
    assert forge_ai.one_call(forge_ai.build_request(args), _config(), args=args) == 0
    product = workspace / "products" / "new"
    assert (product / "contract.fluid.yaml").read_text(encoding="utf-8") == VALID
    (run,) = _runs(product)
    assert (run / "cost.json").is_file() and not _runs(workspace)


def test_a_timed_out_agent_run_restores_the_workspace_and_keeps_a_draft(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import fluid_build.llm.coding_agent as ca

    _stub_agent(monkeypatch)

    def slow(argv, *, stdin, timeout, cwd, env):
        (Path(cwd) / "contract.fluid.yaml").write_text("fluidVersion: 0.7.6\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(ca, "_run_agent", slow)
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert exc.value.event == "coding_agent_timeout"
    assert not (workspace / "contract.fluid.yaml").exists()
    (run,) = _runs(workspace)
    draft = run / "contract.draft.yaml"
    assert draft.read_text(encoding="utf-8") == "fluidVersion: 0.7.6\n"
    assert str(draft) in exc.value.message and (run / "cost.json").is_file()


def test_a_failed_agent_run_puts_the_refined_contract_back_and_records_its_cost(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = VALID + "description: keep me\n"
    (workspace / "contract.fluid.yaml").write_text(original, encoding="utf-8")
    report = {"is_error": True, "result": "rate limited", "total_cost_usd": 0.05}
    _stub_agent(monkeypatch, rc=1, stdout=json.dumps(report))  # it overwrites with VALID
    args = _args(refine="contract.fluid.yaml")
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(args), _config("claude-code"), args=args
        )
    assert exc.value.event == "coding_agent_failed" and "rate limited" in exc.value.message
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == original
    (run,) = _runs(workspace)
    assert (run / "contract.draft.yaml").read_text(encoding="utf-8") == VALID
    assert json.loads((run / "cost.json").read_text(encoding="utf-8"))["total_usd"] == 0.05


def test_an_interrupted_agent_run_puts_the_refined_contract_back(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    original = VALID + "description: keep me\n"
    (workspace / "contract.fluid.yaml").write_text(original, encoding="utf-8")
    _stub_agent(monkeypatch)

    def interrupted(argv, *, stdin, timeout, cwd, env):
        (Path(cwd) / "contract.fluid.yaml").write_text("fluidVersion: 0.7.6\n", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(ca, "_run_agent", interrupted)
    args = _args(refine="contract.fluid.yaml")
    with pytest.raises(KeyboardInterrupt):
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(args), _config("claude-code"), args=args
        )
    assert (workspace / "contract.fluid.yaml").read_text(encoding="utf-8") == original
    (draft,) = workspace.glob(".fluid/agents/*/contract.draft.yaml")
    assert draft.read_text(encoding="utf-8") == "fluidVersion: 0.7.6\n"


def test_a_failed_agent_run_in_a_new_folder_removes_the_folder(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.llm.coding_agent as ca

    _stub_agent(monkeypatch)

    def failing(argv, *, stdin, timeout, cwd, env):
        (Path(cwd) / "products" / "new" / "contract.fluid.yaml").write_text(VALID, encoding="utf-8")
        return 1, "", "boom"

    monkeypatch.setattr(ca, "_run_agent", failing)
    args = _args(target_dir="products/new")
    with pytest.raises(CopilotGenerationError):
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(args), _config("claude-code"), args=args
        )
    assert not (workspace / "products").exists()
    (run,) = _runs(workspace)
    assert (run / "contract.draft.yaml").is_file() and (run / "cost.json").is_file()


def test_an_agent_that_writes_nothing_still_gets_a_receipt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch, write=False, stdout=json.dumps({"total_cost_usd": 0.02}))
    with pytest.raises(CopilotGenerationError) as exc:
        forge_ai.drive_headless(
            "claude-code", forge_ai.build_request(_args()), _config("claude-code"), args=_args()
        )
    assert exc.value.event == "forge_contract_missing"
    (run,) = _runs(workspace)
    assert json.loads((run / "cost.json").read_text(encoding="utf-8"))["total_usd"] == 0.02


# ---------------------------------------------------------------------------
# fluid init's AI path
# ---------------------------------------------------------------------------


def test_init_with_nobody_to_describe_the_product_scaffolds_the_template(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    from argparse import Namespace

    import fluid_build.cli.forge as forge_cli
    from fluid_build.cli import init as init_mod

    scaffolded: List[str] = []

    def template_mode(args: Any, logger: Any) -> int:
        scaffolded.append(args.template)
        return 0

    monkeypatch.setattr(init_mod, "_setup_ai_mode_llm", lambda args: "ready")
    monkeypatch.setattr(init_mod, "template_mode", template_mode)
    monkeypatch.setattr(forge_ai, "_interactive", lambda a: False)
    monkeypatch.setattr(
        forge_cli, "run_ai_copilot_mode", lambda a, l: pytest.fail("AI mode needs a description")
    )
    assert init_mod._ai_mode(Namespace(name="demo", yes=True), logging.getLogger("t")) == 0
    assert scaffolded == ["customer-360"]
