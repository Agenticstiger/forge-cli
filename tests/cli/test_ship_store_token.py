# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship`` with a response-wrapped store token: one unwrap serves every stage.

A wrapped token (``FLUID_VAULT_WRAPPED_TOKEN``) unwraps once, and ship runs each
stage in its own process, so before this ship only the first stage that needed
the store could use it. Ship now unwraps it before the first stage, hands each
stage the token through a pipe (``FLUID_VAULT_SHIP_TOKEN_FD`` names it; on
Windows the token travels in ``FLUID_VAULT_SHIP_TOKEN``), and revokes it when
the run ends.

``subprocess.run`` is replaced by :class:`StageProcesses`. It plays each stage
that needs the store as a new ``fluid`` process: the environment ship gave it,
its own copy of each descriptor ship passed, no memory of an unwrap, and the
first step of ``main()``. The stage then asks
for a token the way state encryption does (``_vault.token_for``), and its run
ends as a real stage's does. The store is an ``httpx.MockTransport`` that opens
a wrapper once, as OpenBao does; no test can reach a network.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from unittest import mock

import httpx
import pytest

from fluid_build.cli import ship
from fluid_build.observability import secret_redactor
from fluid_build.secrets import _ship_token, _vault, run
from fluid_build.secrets.errors import SecretResolveError
from fluid_build.secrets.run import RunState

LOG = logging.getLogger("test_ship_store_token")

ADDR = "https://vault.example:8200"
WRAPPED = "s.wrapping-token-1111"
MINTED = "s.run-token-2222"
OPERATOR = "s.operator-token-3333"
PROJECT = "s.project-token-4444"

UNWRAP = "/v1/sys/wrapping/unwrap"
REVOKE = "/v1/auth/token/revoke-self"

_STORE_ENV = (
    "FLUID_VAULT_WRAPPED_TOKEN",
    "FLUID_VAULT_SHIP_TOKEN",
    "FLUID_VAULT_SHIP_TOKEN_FD",
    "FLUID_VAULT_JWT_ROLE",
    "FLUID_VAULT_JWT",
    "FLUID_VAULT_JWT_FILE",
    "FLUID_VAULT_JWT_MOUNT",
    "FLUID_RUN_SECRETS_FILE",
    "BAO_ADDR",
    "VAULT_ADDR",
    "BAO_TOKEN",
    "VAULT_TOKEN",
    "BAO_NAMESPACE",
    "VAULT_NAMESPACE",
    "BAO_CACERT",
    "VAULT_CACERT",
    "FLUID_RUN_ID",
    "FLUID_ENV",
    "APPLY_MODE",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "TF_BUILD",
)


class Store:
    """OpenBao as far as these tests need it: a wrapper opens once."""

    def __init__(self, unwrap_status: int = 200) -> None:
        self.unwrap_status = unwrap_status
        self.minted = MINTED  # the token a successful unwrap answers with
        self.requests: List[Dict[str, Any]] = []

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("x-vault-token")
        self.requests.append({"path": request.url.path, "token": token})
        if request.url.path == UNWRAP:
            first = token == WRAPPED and self.count(UNWRAP) == 1
            if first and self.unwrap_status == 200:
                return httpx.Response(200, json={"auth": {"client_token": self.minted}})
            return httpx.Response(400, json={"errors": ["wrapping token is not valid"]})
        if request.url.path == REVOKE:
            return httpx.Response(204)
        return httpx.Response(404, json={"errors": []})

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r["path"] == path)

    def revokes(self) -> List[Optional[str]]:
        return [r["token"] for r in self.requests if r["path"] == REVOKE]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterable[None]:
    for name in _STORE_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_vault, "_UNWRAPPED", set())
    monkeypatch.setattr(_ship_token, "_TAKEN", None)
    registered = set(secret_redactor._known_secret_set)
    run._close_orphan()
    yield
    run._close_orphan()
    with secret_redactor._registry_lock:
        secret_redactor._known_secret_set.clear()
        secret_redactor._known_secret_set.update(registered)
        secret_redactor._known_secrets = tuple(sorted(registered, key=lambda s: (-len(s), s)))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()

    def no_network(self: socket.socket, *args: object, **kwargs: object) -> None:
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    def stub_client(base_url: str = "", **kwargs: Any) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            transport=httpx.MockTransport(fake.dispatch),
            headers=kwargs.get("headers"),
            follow_redirects=False,
        )

    monkeypatch.setattr("fluid_build.util.safe_http.safe_httpx_client", stub_client)
    monkeypatch.setenv("BAO_ADDR", ADDR)
    return fake


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    (project / "contract.fluid.yaml").write_text("fluidVersion: '0.7.5'\nid: x.y\n")
    return project


class StageProcesses:
    """Stands in for ``subprocess.run``: each stage of the run as its own process."""

    def __init__(
        self,
        store_stages: Iterable[str] = ("diff", "apply"),
        rcs: Optional[Dict[str, int]] = None,
        interrupt: Optional[str] = None,
    ) -> None:
        self.store_stages = set(store_stages)
        self.rcs = rcs or {}
        self.interrupt = interrupt
        self.envs: Dict[str, Dict[str, str]] = {}  # the environment each stage started with
        self.inherited: Dict[str, Dict[str, str]] = {}  # what its own subprocesses would get
        self.tokens: Dict[str, str] = {}
        self.errors: Dict[str, SecretResolveError] = {}

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        argv = cmd[3:]
        stage = argv[1] if argv[0] == "generate" else argv[0]
        env = kwargs.get("env")
        assert isinstance(env, dict)
        self.envs[stage] = dict(env)
        if stage == "bundle":  # what the real stages leave for the next ones
            Path("runtime").mkdir(exist_ok=True)
            Path("runtime/bundle.tgz").write_bytes(b"x")
        elif stage == "plan":
            Path("runtime/plan.json").write_text("{}")
        elif stage == "artifacts":
            Path("dist/artifacts").mkdir(parents=True, exist_ok=True)
        if stage == self.interrupt:
            raise KeyboardInterrupt  # a second Ctrl-C while the stage runs
        rc = self.rcs.get(stage, 0)
        if stage in self.store_stages:
            rc = self._new_process(stage, env, kwargs.get("pass_fds", ())) or rc
        return subprocess.CompletedProcess(cmd, rc)

    def _new_process(self, stage: str, env: Dict[str, str], fds: Iterable[int] = ()) -> int:
        env = dict(env)
        for fd in fds:  # a child gets its own copy of each descriptor it inherits
            if env.get(_ship_token.SHIP_TOKEN_FD_ENV) == str(fd):
                env[_ship_token.SHIP_TOKEN_FD_ENV] = str(os.dup(fd))
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(_vault, "_UNWRAPPED", set()),
            mock.patch.object(_ship_token, "_TAKEN", None),
        ):
            _ship_token.take()  # the first thing main() does
            self.inherited[stage] = dict(os.environ)
            state = RunState()
            try:
                self.tokens[stage] = _vault.token_for(None, _vault.load_config(None), state)
            except SecretResolveError as exc:
                self.errors[stage] = exc
                return 1
            finally:
                state.close()  # the stage's run ends: it revokes what it obtained itself
        return 0


def parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    ship.register(sub)
    return parser.parse_args(["ship", *argv])


def ship_run(argv: List[str], stages: StageProcesses, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(ship.subprocess, "run", stages)
    return ship.run(parse(argv), LOG)


# ── one unwrap, every stage ──────────────────────────────────────────────────


def test_every_stage_that_needs_the_store_gets_the_token_of_one_unwrap(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses()
    assert ship_run(["--yes"], stages, monkeypatch) == 0
    assert stages.errors == {}
    assert stages.tokens == {"diff": MINTED, "apply": MINTED}
    assert store.count(UNWRAP) == 1
    # Revoked once, by ship, after the last stage: no stage revoked it.
    assert store.revokes() == [MINTED]
    assert store.requests[-1]["path"] == REVOKE


def test_no_stage_gets_the_wrapper_and_no_subprocess_of_a_stage_gets_the_token(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses()
    assert ship_run(["--yes"], stages, monkeypatch) == 0
    assert stages.envs  # every stage ran
    for stage, env in stages.envs.items():
        assert "FLUID_VAULT_WRAPPED_TOKEN" not in env, stage
        # The token is in no variable at all: only the number of its pipe is.
        assert not [name for name, value in env.items() if MINTED in value], stage
        assert "FLUID_VAULT_SHIP_TOKEN" not in env, stage
        assert env["FLUID_VAULT_SHIP_TOKEN_FD"].isdigit(), stage
    for stage, env in stages.inherited.items():
        assert not [name for name, value in env.items() if MINTED in value], stage
        assert not [name for name in env if name.startswith("FLUID_VAULT_SHIP")], stage


def test_the_ship_token_beats_a_bao_token_and_a_jwt_login_in_the_stage(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A BAO_TOKEN in a stage's environment (the operator's, or one a project
    # file loaded) and a JWT role are both read after the token ship handed it.
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    monkeypatch.setenv("BAO_TOKEN", PROJECT)
    monkeypatch.setenv("FLUID_VAULT_JWT_ROLE", "ci-role")
    monkeypatch.setenv("FLUID_VAULT_JWT", "jwt-material")
    stages = StageProcesses()
    assert ship_run(["--yes"], stages, monkeypatch) == 0
    assert stages.tokens == {"diff": MINTED, "apply": MINTED}
    assert [r["path"] for r in store.requests] == [UNWRAP, REVOKE]
    assert stages.envs["apply"]["BAO_TOKEN"] == PROJECT  # left as it was


# ── revoked when ship ends ───────────────────────────────────────────────────


def test_the_token_is_revoked_once_after_a_failed_stage(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses(rcs={"apply": 3})
    assert ship_run(["--yes"], stages, monkeypatch) == 1
    assert "verify" not in stages.envs
    assert store.revokes() == [MINTED]
    assert store.requests[-1]["path"] == REVOKE


def test_the_token_is_revoked_after_ctrl_c_in_a_stage(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses(interrupt="verify")
    assert ship_run(["--yes"], stages, monkeypatch) == 130
    assert stages.tokens == {"diff": MINTED, "apply": MINTED}
    assert store.revokes() == [MINTED]
    assert store.requests[-1]["path"] == REVOKE


def test_the_token_is_revoked_when_ship_itself_is_interrupted(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)

    def interrupted(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(ship, "_finish", interrupted)
    stages = StageProcesses()
    with pytest.raises(KeyboardInterrupt):
        ship_run(["--yes"], stages, monkeypatch)
    assert stages.tokens == {"diff": MINTED, "apply": MINTED}
    assert store.revokes() == [MINTED]
    assert store.requests[-1]["path"] == REVOKE


# ── refusals, and runs that do not unwrap ────────────────────────────────────


def test_a_refused_unwrap_stops_the_run_before_any_stage(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    store.unwrap_status = 400
    stages = StageProcesses()
    caplog.set_level(logging.ERROR)
    assert ship_run(["--yes"], stages, monkeypatch) == 2
    assert stages.envs == {}
    assert store.revokes() == []
    assert "no stage ran" in caplog.text
    assert "exposed" in caplog.text
    assert "Fix: Ask the runner for a fresh wrapped token" in caplog.text
    assert "refused. the store refused" not in caplog.text  # said once, plainly
    assert WRAPPED not in caplog.text
    assert not (work / "runtime" / "ship-report.json").exists()


def test_explain_never_unwraps(work: Path, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses()
    assert ship_run(["--explain"], stages, monkeypatch) == 0
    assert store.requests == []


def test_an_operator_token_is_handed_on_unchanged_and_never_revoked(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAO_TOKEN", OPERATOR)
    stages = StageProcesses()
    assert ship_run(["--yes"], stages, monkeypatch) == 0
    assert stages.tokens == {"diff": OPERATOR, "apply": OPERATOR}
    for env in stages.envs.values():
        assert env["BAO_TOKEN"] == OPERATOR
        assert not [name for name in env if name.startswith("FLUID_VAULT_SHIP")]
    assert store.requests == []


def test_a_wrapped_token_in_one_stage_alone_still_unwraps_and_revokes_there(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One fluid process given the wrapper, as a single `fluid apply` is: it
    # unwraps it and revokes the token when its run ends, as before.
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses()
    assert stages._new_process("apply", dict(os.environ)) == 0
    assert stages.tokens == {"apply": MINTED}
    assert [r["path"] for r in store.requests] == [UNWRAP, REVOKE]


def test_a_run_of_one_stage_hands_that_stage_the_wrapper_as_before(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One stage per CI step: `fluid ship --stage 7` leaves the wrapper to its
    # stage, so the steps before it, which never reach the store, cannot spend it.
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    (work / "runtime").mkdir()
    (work / "runtime" / "bundle.tgz").write_bytes(b"x")
    (work / "runtime" / "plan.json").write_text("{}")
    stages = StageProcesses(store_stages=("apply",))
    assert ship_run(["--stage", "7", "--yes"], stages, monkeypatch) == 0
    assert list(stages.envs) == ["apply"]
    assert stages.envs["apply"]["FLUID_VAULT_WRAPPED_TOKEN"] == WRAPPED
    assert not [name for name in stages.envs["apply"] if name.startswith("FLUID_VAULT_SHIP")]
    assert stages.tokens == {"apply": MINTED}
    assert [r["path"] for r in store.requests] == [UNWRAP, REVOKE]  # both by the stage


# ── a stage takes the token first, and from nowhere but its pipe ─────────────


def _pipe_holding(data: bytes) -> int:
    """The read end of a pipe that holds ``data``, its write end closed."""
    read_end, write_end = os.pipe()
    os.write(write_end, data)
    os.close(write_end)
    return read_end


def _main_first_step(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Optional[str]]:
    """Run main() up to its first call before this change, and what it saw there."""
    from fluid_build import cli

    seen: Dict[str, Optional[str]] = {}

    class Stop(Exception):
        pass

    def first_step() -> None:
        seen["fd"] = os.environ.get("FLUID_VAULT_SHIP_TOKEN_FD")
        seen["env"] = os.environ.get("FLUID_VAULT_SHIP_TOKEN")
        seen["taken"] = _ship_token.take()
        raise Stop

    monkeypatch.setattr(cli, "_force_utf8_streams", first_step)
    with pytest.raises(Stop):
        cli.main(["--version"])
    return seen


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_main_reads_the_ship_token_from_its_pipe_before_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_end = _pipe_holding(MINTED.encode())
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(read_end))
    assert _main_first_step(monkeypatch) == {"fd": None, "env": None, "taken": MINTED}
    with pytest.raises(OSError):
        os.fstat(read_end)  # read once, then closed


def test_main_takes_the_windows_variable_out_of_the_environment_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN", MINTED)
    assert _main_first_step(monkeypatch) == {"fd": None, "env": None, "taken": MINTED}


def test_a_blank_ship_token_is_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN", "  ")
    assert _ship_token.take() is None
    assert "FLUID_VAULT_SHIP_TOKEN" not in os.environ


def test_the_report_never_holds_the_token(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    assert ship_run(["--yes"], StageProcesses(), monkeypatch) == 0
    text = (work / "runtime" / "ship-report.json").read_text()
    assert MINTED not in text and WRAPPED not in text
    assert json.loads(text)["exit_code"] == 0


# ── the pipe ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
@pytest.mark.parametrize(
    "number",
    ["0", "1", "2", "-5", "abc", "", "99999", "\u00b2", "99999999999999999999", "9" * 5000],
)
def test_a_ship_token_fd_that_names_no_pipe_of_ours_gives_no_token(
    number: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", number)
    assert _ship_token.take() is None
    assert "FLUID_VAULT_SHIP_TOKEN_FD" not in os.environ


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_a_regular_file_is_not_read_as_the_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token"
    path.write_text(MINTED, encoding="utf-8")
    with open(path, "rb") as handle:
        monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(handle.fileno()))
        assert _ship_token.take() is None


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
@pytest.mark.parametrize(
    "data", [b"", b"two words", b"s.token\x00", "s.t\u00f6ken".encode(), b"\xff\xfe"]
)
def test_a_pipe_that_holds_no_token_gives_none(
    data: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(_pipe_holding(data)))
    assert _ship_token.take() is None


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_a_pipe_that_holds_more_than_a_token_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ship_token, "_MAX_TOKEN_BYTES", 64)
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(_pipe_holding(b"s." + b"x" * 100)))
    assert _ship_token.take() is None


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_a_pipe_still_open_for_writing_never_blocks_the_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_end, write_end = os.pipe()
    try:
        monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(read_end))
        assert _ship_token.take() is None  # returns at once, empty
    finally:
        os.close(write_end)


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_both_ends_of_the_pipe_are_closed_when_the_stage_has_ended() -> None:
    with _ship_token.handed_to_child(MINTED, {"FLUID_VAULT_SHIP_TOKEN": "stale"}) as (env, fds):
        (read_end,) = fds
        assert env["FLUID_VAULT_SHIP_TOKEN_FD"] == str(read_end)
        assert "FLUID_VAULT_SHIP_TOKEN" not in env  # a stale one never rides along
        assert not [name for name, value in env.items() if MINTED in value]
    with pytest.raises(OSError):
        os.fstat(read_end)


def test_on_windows_the_token_travels_in_the_variable(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_ship_token, "_pipes_supported", lambda: False)
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    stages = StageProcesses()
    assert ship_run(["--yes"], stages, monkeypatch) == 0
    assert stages.tokens == {"diff": MINTED, "apply": MINTED}
    for stage, env in stages.envs.items():
        assert env["FLUID_VAULT_SHIP_TOKEN"] == MINTED, stage
        assert "FLUID_VAULT_SHIP_TOKEN_FD" not in env, stage
    for stage, env in stages.inherited.items():
        assert not [name for name, value in env.items() if MINTED in value], stage
    assert store.revokes() == [MINTED]


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_a_real_stage_process_reads_the_pipe_and_its_children_see_nothing(
    tmp_path: Path,
) -> None:
    import sys

    result = tmp_path / "result.json"
    stage = (
        "import json, os, subprocess, sys\n"
        "from fluid_build.secrets._ship_token import take\n"
        "token = take()\n"
        "child = subprocess.run([sys.executable, '-c', "
        "'import os, sys; print([n for n, v in os.environ.items() "
        "if n.startswith(\"FLUID_VAULT_SHIP\") or sys.argv[1] in v])', token or 'none'], "
        "capture_output=True, text=True, check=True).stdout.strip()\n"
        "json.dump({'token': token, 'child_sees': child, "
        "'env': sorted(n for n in os.environ if n.startswith('FLUID_VAULT_SHIP'))}, "
        "open(sys.argv[1], 'w', encoding='utf-8'))\n"
    )
    rc = ship._start_stage(
        [sys.executable, "-c", stage, str(result)], dict(os.environ), None, MINTED
    )
    assert rc == 0
    seen = json.loads(result.read_text(encoding="utf-8"))
    assert seen == {"token": MINTED, "child_sees": "[]", "env": []}


def test_a_token_ship_cannot_hand_on_stops_the_run_and_is_revoked(
    work: Path, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED)
    store.minted = "s.two words"  # a stage would drop it, so ship refuses it first
    stages = StageProcesses()
    caplog.set_level(logging.ERROR)
    assert ship_run(["--yes"], stages, monkeypatch) == 2
    assert stages.envs == {}
    assert "cannot be handed to the stages" in caplog.text
    assert store.revokes() == ["s.two words"]  # ship unwrapped it, so ship revokes it


@pytest.mark.parametrize("token", ["s.two words", "", "s.t\u00f6ken", "s." + "x" * (16 * 1024)])
def test_a_token_the_stage_would_drop_is_never_written_to_its_pipe(token: str) -> None:
    assert not _ship_token.can_hand(token)
    with pytest.raises(ValueError):
        with _ship_token.handed_to_child(token, {}):
            pass


@pytest.mark.skipif(os.name == "nt", reason="a pipe is inherited on POSIX only")
def test_a_pipe_on_a_low_descriptor_moves_above_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    # With stdio closed, os.pipe() returns 0, 1 or 2, which a stage refuses to read.
    # Raising the floor makes every new descriptor "low", without closing stdio here.
    import fcntl

    monkeypatch.setattr(_ship_token, "_LOWEST_FD", 200)
    with _ship_token.handed_to_child(MINTED, {}) as (env, fds):
        (read_end,) = fds
        assert read_end >= 200 and env["FLUID_VAULT_SHIP_TOKEN_FD"] == str(read_end)
        child_copy = fcntl.fcntl(read_end, fcntl.F_DUPFD, 200)  # what the stage inherits
        monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN_FD", str(child_copy))
        assert _ship_token.take() == MINTED
