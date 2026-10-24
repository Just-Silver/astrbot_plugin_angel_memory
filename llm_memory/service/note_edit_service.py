"""笔记编辑的落盘与索引同步。

负责：短 ID 定位、raw/ 越界校验、原子写、外部改动检测、写后重建索引。
匹配与替换算法在 llm_memory.utils.text_edit。
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from ..utils.note_text import extract_h1
from ..utils.text_edit import EditError, apply_edit

SUPPORTED_SUFFIXES = {".md", ".txt"}

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NoteEditError(Exception):
    """定位或落盘失败；消息可直接返回给模型。"""


def _lock_for(abs_path: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(abs_path)
        if lock is None:
            lock = threading.Lock()
            _locks[abs_path] = lock
        return lock


def _read_preserving_newlines(path: Path) -> str:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class NoteEditService:
    def __init__(self, plugin_context):
        self.plugin_context = plugin_context
        self.logger = logger

    # ---------- 定位 ----------

    async def resolve(self, note_short_id: int) -> Dict[str, Any]:
        """按短 ID 取笔记记录，并校验文件位于 raw/ 内。"""
        try:
            short_id = int(note_short_id)
        except (TypeError, ValueError):
            raise NoteEditError("note_short_id 必须是整数。")

        memory_sql_manager = self.plugin_context.get_component("memory_sql_manager")
        if memory_sql_manager is None:
            raise NoteEditError("memory_sql_manager 不可用。")

        row = await memory_sql_manager.get_note_index_by_short_id(short_id)
        if not row:
            raise NoteEditError(f"未找到 note_short_id={short_id} 对应的笔记。")

        rel_path = str(row.get("source_file_path") or "").replace("\\", "/").strip().lstrip("/")
        if not rel_path:
            raise NoteEditError(f"note_short_id={short_id} 的记录缺少文件路径。")

        raw_root = Path(self.plugin_context.get_path_manager().get_raw_dir()).resolve()
        abs_path = (raw_root / rel_path).resolve()
        if raw_root not in abs_path.parents:
            raise NoteEditError("路径越界。")
        if abs_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise NoteEditError("只支持编辑 .md 与 .txt 笔记。")
        if not abs_path.exists():
            raise NoteEditError(f"文件不存在：{rel_path}")

        return {
            "note_short_id": short_id,
            "file_id": str(row.get("file_id") or ""),
            "relative_path": rel_path,
            "abs_path": str(abs_path),
        }

    # ---------- 编辑 ----------

    async def edit(
        self,
        note_short_id: int,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Dict[str, Any]:
        # 参数错误优先于定位错误报出，避免模型顺着「笔记不存在」改方向
        if not str(old_string or ""):
            raise EditError(
                "old_string 不能为空。新建笔记请用 angel_note_create，整篇覆盖请用 angel_note_write。"
            )
        if old_string == new_string:
            raise EditError("没有变化：old_string 与 new_string 相同。")

        target = await self.resolve(note_short_id)
        content, count, chunks, previous_title = await asyncio.to_thread(
            self._edit_sync,
            target,
            old_string,
            new_string,
            replace_all,
        )
        title = extract_h1(content)
        return {
            **target,
            "replacements": count,
            "title": title,
            "title_changed": title != previous_title,
            "chunks": chunks,
        }

    async def overwrite(self, note_short_id: int, content: str) -> Dict[str, Any]:
        if not str(content or "").strip():
            raise NoteEditError("content 不能为空。")
        target = await self.resolve(note_short_id)
        saved, chunks = await asyncio.to_thread(self._overwrite_sync, target, content)
        return {
            **target,
            "replacements": 0,
            "title": extract_h1(saved),
            "chunks": chunks,
        }

    # ---------- 线程内实现 ----------

    def _edit_sync(
        self,
        target: Dict[str, Any],
        old_string: str,
        new_string: str,
        replace_all: bool,
    ):
        abs_path = Path(target["abs_path"])
        with _lock_for(str(abs_path)):
            before = abs_path.stat()
            source = _read_preserving_newlines(abs_path)
            edited, count = apply_edit(source, old_string, new_string, replace_all)
            self._assert_unchanged(abs_path, before.st_mtime_ns)
            atomic_write_text(abs_path, edited)
            chunks = self._resync(target)
            previous_title = extract_h1(source)
        return edited, count, chunks, previous_title

    def _overwrite_sync(self, target: Dict[str, Any], content: str):
        abs_path = Path(target["abs_path"])
        with _lock_for(str(abs_path)):
            before = abs_path.stat()
            current = _read_preserving_newlines(abs_path)
            if current == content:
                raise NoteEditError("没有变化：新内容与当前正文一致。")
            self._assert_unchanged(abs_path, before.st_mtime_ns)
            atomic_write_text(abs_path, content)
            chunks = self._resync(target)
        return content, chunks

    def _assert_unchanged(self, abs_path: Path, expected_mtime_ns: int) -> None:
        current = abs_path.stat().st_mtime_ns
        if current != expected_mtime_ns:
            raise NoteEditError("文件已被其他写入改动，请重新读取后再编辑。")

    def _resync(self, target: Dict[str, Any]) -> int:
        note_service = self.plugin_context.get_component("note_service")
        if note_service is None:
            self.logger.warning("笔记服务不可用，跳过索引同步")
            return 0
        try:
            chunks, _timings = note_service.parse_and_store_file_sync(
                target["abs_path"], target["relative_path"]
            )
            return int(chunks)
        except Exception as exc:
            self.logger.error(
                f"笔记索引同步失败 path={target['relative_path']}: {exc}", exc_info=True
            )
            raise NoteEditError(f"内容已写入，但索引更新失败：{exc}")


__all__ = [
    "EditError",
    "NoteEditError",
    "NoteEditService",
    "atomic_write_text",
    "extract_h1",
]
