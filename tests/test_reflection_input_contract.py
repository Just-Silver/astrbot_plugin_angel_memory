"""反思缓冲持久化与记忆创建去重的业务行为测试。"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
from pathlib import Path


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot-test")

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


_install_astrbot_stubs()

PACKAGE_NAME = "astrbot_plugin_angel_memory"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[PACKAGE_NAME] = package

from astrbot_plugin_angel_memory.llm_memory.components.memory_sql_manager import (
    MemorySqlManager,
)


def _make_manager() -> MemorySqlManager:
    return MemorySqlManager(db_path=Path(tempfile.mkdtemp()) / "test.db")


def _create_action(judgment: str) -> dict:
    return {
        "action": "create",
        "memory": {
            "type": "知识记忆",
            "judgment": judgment,
            "reasoning": "测试背景",
            "tags": ["测试标签"],
        },
    }


def test_create_action_skips_duplicate_judgment_in_same_scope():
    """同一 scope 下重复归纳出相同论断时，不应产生第二条记忆。"""
    manager = _make_manager()

    first = asyncio.run(
        manager.process_feedback(memory_actions=[_create_action("用户喜欢喝乌龙茶")])
    )
    second = asyncio.run(
        manager.process_feedback(memory_actions=[_create_action("用户喜欢喝乌龙茶")])
    )

    assert len(first) == 1
    assert second == [], "重复论断必须被去重，否则反思越频繁记忆越膨胀"


def test_create_action_keeps_distinct_judgment():
    """不同论断必须照常创建，去重不能误杀。"""
    manager = _make_manager()

    created = asyncio.run(
        manager.process_feedback(
            memory_actions=[
                _create_action("用户喜欢喝乌龙茶"),
                _create_action("用户讨厌香菜"),
            ]
        )
    )

    assert len(created) == 2
