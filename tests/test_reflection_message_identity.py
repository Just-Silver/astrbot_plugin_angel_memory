from __future__ import annotations

import json
import logging
import sys
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

from astrbot_plugin_angel_memory.core.deepmind import DeepMind
from astrbot_plugin_angel_memory.core.utils.small_model_prompt_builder import (
    SmallModelPromptBuilder,
)


class _ReflectionHarness:
    logger = logging.getLogger("reflection-identity-test")
    prompt_builder = SmallModelPromptBuilder

    _is_missing_user_identity = staticmethod(DeepMind._is_missing_user_identity)
    _get_event_sender_identity = DeepMind._get_event_sender_identity
    _log_missing_user_identity = DeepMind._log_missing_user_identity
    _dedupe_and_sort_chat_records = DeepMind._dedupe_and_sort_chat_records
    _split_chat_snapshot = DeepMind._split_chat_snapshot
    _build_reflection_records_for_turn = DeepMind._build_reflection_records_for_turn

    def _extract_message_text(self, event) -> str:
        return event.get_message_outline()


class _NativeEvent:
    unified_msg_origin = "aiocqhttp:group:100"

    def __init__(self, sender_id: str, sender_name: str, text: str):
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._text = text

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_message_outline(self) -> str:
        return self._text


def test_native_reflection_records_use_event_getters():
    harness = _ReflectionHarness()
    event = _NativeEvent("123456", "小明", "我在写 AstrBot 插件。")

    records = harness._build_reflection_records_for_turn(
        event,
        SimpleNamespace(completion_text="收到。"),
    )

    user_record = next(record for record in records if record["role"] == "user")
    assert user_record["sender_id"] == "123456"
    assert user_record["sender_name"] == "小明"


def test_native_reflection_skips_user_message_without_identity(caplog):
    harness = _ReflectionHarness()
    event = _NativeEvent("", "", "我在写 AstrBot 插件。")

    with caplog.at_level(logging.ERROR, logger="reflection-identity-test"):
        records = harness._build_reflection_records_for_turn(
            event,
            SimpleNamespace(completion_text="收到。"),
        )

    assert all(record["role"] != "user" for record in records)
    assert "检测到异常消息，该消息不存在用户和ID" in caplog.text


def test_angelheart_reflection_skips_user_message_without_identity(caplog):
    harness = _ReflectionHarness()
    event = SimpleNamespace(
        angelheart_context=json.dumps(
            {
                "chat_records": [
                    {
                        "role": "user",
                        "content": "异常消息",
                        "sender_id": "user",
                        "sender_name": "用户",
                        "timestamp": 1.0,
                        "is_processed": True,
                    },
                    {
                        "role": "user",
                        "content": "有效消息",
                        "sender_id": "123456",
                        "sender_name": "小明",
                        "timestamp": 2.0,
                        "is_processed": False,
                    },
                ]
            },
            ensure_ascii=False,
        )
    )

    with caplog.at_level(logging.ERROR, logger="reflection-identity-test"):
        records = harness._build_reflection_records_for_turn(
            event,
            SimpleNamespace(completion_text="收到。"),
        )

    assert [record["content"] for record in records] == ["有效消息"]
    assert "检测到异常消息，该消息不存在用户和ID" in caplog.text


def _angelheart_event(payload: dict):
    return SimpleNamespace(angelheart_context=json.dumps(payload, ensure_ascii=False))


def _user(content: str, sender_id: str, timestamp: float) -> dict:
    return {
        "role": "user",
        "content": content,
        "sender_id": sender_id,
        "sender_name": f"用户{sender_id}",
        "timestamp": timestamp,
    }


def _assistant(content: str, timestamp: float) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "sender_id": "assistant",
        "sender_name": "助理",
        "timestamp": timestamp,
    }


def test_angelheart_snapshot_without_processed_flag_yields_records():
    """天使之心 2.x 快照不再带 is_processed，反思仍要能拿到本轮记录。"""
    harness = _ReflectionHarness()
    event = _angelheart_event(
        {
            "chat_records": [
                _user("第一句", "10001", 1.0),
                _assistant("第一答", 2.0),
                _user("第二句", "10002", 3.0),
            ]
        }
    )

    records = harness._build_reflection_records_for_turn(
        event,
        SimpleNamespace(completion_text="第二答"),
    )

    assert records, "2.x 快照必须能产出记录，否则反思永远不会入池"
    assert "第二句" in [record["content"] for record in records]


def test_angelheart_snapshot_batch_excludes_previous_assistant():
    """按 historical_count 切出的批次只保留当前连续块，不含上一轮助理回复。"""
    harness = _ReflectionHarness()
    event = _angelheart_event(
        {
            "chat_records": [
                _user("第一句", "10001", 1.0),
                _assistant("第一答", 2.0),
                _user("第二句", "10002", 3.0),
            ],
            "historical_count": 2,
        }
    )

    records = harness._build_reflection_records_for_turn(
        event,
        SimpleNamespace(completion_text="第二答"),
    )

    contents = [record["content"] for record in records]
    assert "第二句" in contents
    assert "第一答" not in contents


def test_angelheart_snapshot_batch_falls_back_when_trailing_assistant():
    """快照以助理回复结尾时批次会切空，必须兜底而不是丢弃本轮。"""
    harness = _ReflectionHarness()
    event = _angelheart_event(
        {
            "chat_records": [
                _user("第一句", "10001", 1.0),
                _assistant("第一答", 2.0),
            ]
        }
    )

    records = harness._build_reflection_records_for_turn(
        event,
        SimpleNamespace(completion_text="第一答"),
    )

    assert records, "切空时必须兜底保留内容"


def test_angelheart_legacy_snapshot_keeps_processed_semantics():
    """带 is_processed 的旧快照仍按旧语义筛选，行为不回退。"""
    harness = _ReflectionHarness()
    event = _angelheart_event(
        {
            "chat_records": [
                {
                    **_user("已处理", "10001", 1.0),
                    "is_processed": True,
                },
                {
                    **_user("待处理", "10002", 2.0),
                    "is_processed": False,
                },
            ]
        }
    )

    records = harness._build_reflection_records_for_turn(
        event,
        SimpleNamespace(completion_text="收到"),
    )

    contents = [record["content"] for record in records]
    assert "已处理" in contents
    assert "待处理" in contents


def test_reflection_input_uses_current_response_as_latest_assistant():
    """快照里只有旧回复时，反思提示词的“最新回复”必须取本轮 response。"""
    harness = _ReflectionHarness()
    event = _angelheart_event(
        {
            "chat_records": [
                _user("第一句", "10001", 1.0),
                _assistant("上一轮的旧回复", 2.0),
                _user("第二句", "10002", 3.0),
            ]
        }
    )
    event.angelmemory_context = None

    reflection_input = _build_reflection_input(harness, event, "本轮真正的新回复")

    assert reflection_input.latest_assistant_text == "本轮真正的新回复"
    assert reflection_input.latest_user_text == "第二句"


def _build_reflection_input(harness, event, completion_text):
    """驱动 _build_reflection_input，跳过与本次断言无关的依赖。"""
    import asyncio

    harness.prompt_builder = SmallModelPromptBuilder
    harness._extract_message_text = harness._extract_message_text

    async def _scope(_event):
        return "public"

    async def _persona(_event):
        return ""

    harness.plugin_context = SimpleNamespace(
        resolve_memory_scope_from_event=_scope,
        get_event_persona_name=_persona,
    )
    harness._build_reflection_input = DeepMind._build_reflection_input.__get__(harness)

    return asyncio.run(
        harness._build_reflection_input(
            event,
            SimpleNamespace(completion_text=completion_text),
            "aiocqhttp:group:100",
        )
    )
