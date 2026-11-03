"""A spec hook whose command is over the length limit is named to the user.

The limit stays at 4,096 characters, the document validator's payload cap. A
longer command does not run, and the log and the session-start notice name the
hook (event and matcher), the length, the limit and the fix. A hook dropped for
any other reason keeps its own message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner

_LIMIT = 4096


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **_kw: seen.append(text),
    )
    return seen


def _write_spec(agents_dir: Path, name: str, hooks) -> None:
    spec = {"name": name, "prompt": "p", "hooks": hooks}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _prepare(agent: str, *, is_new: bool = True):
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), provider, agent, is_new=is_new
        )
    )


def _cmd(length: int) -> str:
    return "echo " + "x" * (length - 5)


def test_the_limit_is_the_document_validators_cap():
    from kiro_crew.agent_materialization.kiro_hooks import _MAX_HOOK_PAYLOAD_LEN

    assert spec_hooks.MAX_SPEC_HOOK_COMMAND_LEN == _LIMIT == _MAX_HOOK_PAYLOAD_LEN


@pytest.mark.parametrize("length", [6, _LIMIT - 1, _LIMIT])
def test_a_command_up_to_the_limit_runs_with_no_notice(agents_dir, notices, length):
    _write_spec(
        agents_dir, "a1", {"preToolUse": [{"matcher": "execute_bash", "command": _cmd(length)}]}
    )
    hooks, unreadable, _cwd = _prepare("a1")
    assert [len(h.command) for h in hooks] == [length]
    assert unreadable is False
    assert notices == []


@pytest.mark.parametrize(
    "hooks",
    [
        {"preToolUse": [{"matcher": "execute_bash", "command": _cmd(_LIMIT + 1)}]},
        [
            {
                "trigger": "PreToolUse",
                "matcher": "execute_bash",
                "action": {"type": "command", "command": _cmd(_LIMIT + 1)},
            }
        ],
    ],
    ids=["object-form", "document-form"],
)
def test_one_over_the_limit_is_refused_and_named(agents_dir, notices, caplog, hooks):
    _write_spec(agents_dir, "a1", hooks)
    with caplog.at_level(logging.WARNING, logger=spec_hooks.logger.name):
        out, unreadable, _cwd = _prepare("a1")
    assert out == []
    assert unreadable is False
    assert len(notices) == 1
    notice = notices[0]
    assert "PreToolUse" in notice or "preToolUse" in notice
    assert "matcher execute_bash" in notice
    assert "4,097 characters" in notice
    assert "4,096 characters" in notice
    assert "script file" in notice
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "4,097 characters" in logged
    assert "4,096-character limit" in logged
    assert "execute_bash" in logged
    assert "script file" in logged


@pytest.mark.parametrize("trigger", ["preToolUse", "pretooluse", "agentSpawn", "stop"])
def test_a_trigger_alias_still_gets_the_notice(agents_dir, notices, trigger):
    _write_spec(
        agents_dir,
        "a1",
        [{"trigger": trigger, "action": {"type": "command", "command": _cmd(_LIMIT + 1)}}],
    )
    hooks, _unreadable, _cwd = _prepare("a1")
    assert hooks == []
    assert len(notices) == 1
    assert f"{trigger}: 4,097 characters" in notices[0]


def test_an_agent_action_is_not_blamed_on_command_length(agents_dir, notices):
    _write_spec(
        agents_dir,
        "a1",
        [
            {
                "trigger": "Stop",
                "action": {"type": "agent", "prompt": "p", "command": _cmd(_LIMIT + 1)},
            }
        ],
    )
    _prepare("a1")
    assert notices == []


def test_the_notice_shows_once_per_session(agents_dir, notices):
    _write_spec(agents_dir, "a1", {"stop": [{"command": _cmd(_LIMIT + 1)}]})
    _prepare("a1", is_new=True)
    assert len(notices) == 1
    assert "stop: 4,097 characters" in notices[0]
    assert "matcher" not in notices[0]
    notices.clear()
    _prepare("a1", is_new=False)
    assert notices == []


def test_long_hooks_are_counted_and_a_short_one_still_runs(agents_dir, notices):
    long_ = [{"command": _cmd(_LIMIT + 10 + i)} for i in range(4)]
    _write_spec(agents_dir, "a1", {"stop": [*long_, {"command": "ok.sh"}]})
    hooks, _unreadable, _cwd = _prepare("a1")
    assert [h.command for h in hooks] == ["ok.sh"]
    assert len(notices) == 1
    assert "4 hooks do not run" in notices[0]
    assert "and 1 more" in notices[0]


@pytest.mark.parametrize("command", ["", "   ", None, 42])
def test_other_unusable_commands_keep_their_own_message(agents_dir, notices, caplog, command):
    _write_spec(agents_dir, "a1", {"preToolUse": [{"command": command}]})
    with caplog.at_level(logging.WARNING, logger=spec_hooks.logger.name):
        hooks, _unreadable, _cwd = _prepare("a1")
    assert hooks == []
    assert notices == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "no usable command" in logged
    assert "limit" not in logged
