"""反思缓冲在 DeepMind 层的恢复与消费语义。"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot-test")

    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    event.AstrMessageEvent = AstrMessageEvent

    provider = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        pass

    provider.ProviderRequest = ProviderRequest

    core_pkg = types.ModuleType("astrbot.core")
    agent_pkg = types.ModuleType("astrbot.core.agent")
    message = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        def __init__(self, text: str = ""):
            self.text = text

    message.TextPart = TextPart

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.provider"] = provider
    sys.modules["astrbot.core"] = core_pkg
    sys.modules["astrbot.core.agent"] = agent_pkg
    sys.modules["astrbot.core.agent.message"] = message


_install_astrbot_stubs()

PACKAGE_NAME = "astrbot_plugin_angel_memory"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[PACKAGE_NAME] = package

from astrbot_plugin_angel_memory.core.deepmind import DeepMind, ReflectionInput
from astrbot_plugin_angel_memory.llm_memory.components.memory_sql_manager import (
    MemorySqlManager,
)

SESSION = "aiocqhttp:group:100"


def _make_deepmind(db_path: Path) -> DeepMind:
    """构造只带反思缓冲所需依赖的 DeepMind 实例。"""
    manager = MemorySqlManager(db_path=db_path)
    deepmind = DeepMind.__new__(DeepMind)
    deepmind.logger = logging.getLogger("reflection-buffer-test")
    deepmind._reflection_state_lock = asyncio.Lock()
    deepmind._reflection_states = {}
    deepmind.plugin_context = SimpleNamespace(
        get_component=lambda name: manager if name == "memory_sql_manager" else None
    )
    return deepmind


def _input(text: str) -> ReflectionInput:
    return ReflectionInput(
        session_id=SESSION,
        memory_scope="public",
        persona_name="测试",
        latest_user_text=text,
        latest_assistant_text="收到",
    )


def test_buffer_is_persisted_and_restored_after_restart():
    """未消费的缓冲在进程重启后必须能读回，否则静默丢掉整批对话。"""
    db_path = Path(tempfile.mkdtemp()) / "test.db"

    first = _make_deepmind(db_path)
    asyncio.run(
        first._persist_reflection_state(
            SESSION,
            {
                "pending_turns": 4,
                "last_activity_at": 999.0,
                "records": [{"role": "user", "content": "第一句", "sender_id": "10001", "timestamp": 1.0}],
                "latest_input": _input("第一句"),
            },
        )
    )

    second = _make_deepmind(db_path)
    asyncio.run(second._restore_reflection_state(SESSION))

    state = second._reflection_states[SESSION]
    assert state["pending_turns"] == 4
    assert state["last_activity_at"] == 999.0
    assert [r["content"] for r in state["records"]] == ["第一句"]
    assert state["latest_input"].latest_user_text == "第一句"


def test_restore_does_not_regress_in_memory_progress():
    """内存里已有更高进度时，恢复不得把它抹回去。"""
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    deepmind = _make_deepmind(db_path)
    deepmind._reflection_states[SESSION] = {
        "pending_turns": 5,
        "last_activity_at": 100.0,
        "records": [],
        "latest_input": None,
        "processing": False,
    }

    asyncio.run(
        deepmind._persist_reflection_state(
            SESSION,
            {
                "pending_turns": 2,
                "last_activity_at": 50.0,
                "records": [],
                "latest_input": None,
            },
        )
    )
    asyncio.run(deepmind._restore_reflection_state(SESSION))

    assert deepmind._reflection_states[SESSION]["pending_turns"] == 5


def test_consumed_buffer_is_cleared_from_storage():
    """消费成功后缓冲必须从存储层清空，避免重启后又反思同一批。"""
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    deepmind = _make_deepmind(db_path)
    asyncio.run(
        deepmind._persist_reflection_state(
            SESSION,
            {
                "pending_turns": 6,
                "last_activity_at": 1.0,
                "records": [],
                "latest_input": None,
            },
        )
    )

    asyncio.run(
        deepmind._sync_reflection_state(
            SESSION,
            {"pending_turns": 0, "last_activity_at": 1.0, "records": [], "latest_input": None},
            consumed=True,
        )
    )

    manager = deepmind.plugin_context.get_component("memory_sql_manager")
    assert manager.load_reflection_buffer(SESSION) is None


def test_missing_storage_degrades_without_raising():
    """存储层不可用时只降级为内存态，不得阻断反思。"""
    deepmind = DeepMind.__new__(DeepMind)
    deepmind.logger = logging.getLogger("reflection-buffer-test")
    deepmind._reflection_state_lock = asyncio.Lock()
    deepmind._reflection_states = {}
    deepmind.plugin_context = SimpleNamespace(get_component=lambda name: None)

    asyncio.run(deepmind._restore_reflection_state(SESSION))
    asyncio.run(
        deepmind._persist_reflection_state(
            SESSION,
            {"pending_turns": 1, "last_activity_at": 1.0, "records": [], "latest_input": None},
        )
    )

    assert deepmind._reflection_states == {}
