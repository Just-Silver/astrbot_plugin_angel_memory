from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from ..llm_memory.service.note_edit_service import NoteEditError, NoteEditService


@dataclass
class NoteWriteTool(FunctionTool):
    name: str = "angel_note_write"
    description: str = (
        "整篇覆盖已有笔记的正文，用于大范围重写；局部修改请用 angel_note_edit。"
        "content 是完整的新正文，会替换文件全部内容，不可撤销。"
        "不会新建笔记（用 angel_note_create）。建议先 angel_note_read 确认当前内容再覆盖。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "note_short_id": {
                    "type": "integer",
                    "description": "笔记短ID（数字ID）",
                    "minimum": 0,
                },
                "content": {
                    "type": "string",
                    "description": "完整的新正文（Markdown），会替换文件全部内容。",
                },
            },
            "required": ["note_short_id", "content"],
        }
    )

    def __post_init__(self):
        self.logger = logger

    async def run(
        self,
        event: AstrMessageEvent,
        note_short_id: int,
        content: str,
    ) -> str:
        self.logger.debug(
            f"{self.name} - LLM 调用: note_short_id={note_short_id} content_len={len(str(content or ''))}"
        )

        if not hasattr(event, "plugin_context") or event.plugin_context is None:
            return "错误：无法获取插件上下文。"
        plugin_context = event.plugin_context
        if plugin_context.get_component("note_service") is None:
            return "错误：笔记服务不可用，系统可能仍在初始化中。"

        service = NoteEditService(plugin_context)
        try:
            result = await service.overwrite(note_short_id, str(content or ""))
        except NoteEditError as exc:
            return f"错误：{exc}"
        except Exception as exc:
            self.logger.error(f"{self.name}: 覆盖笔记失败: {exc}", exc_info=True)
            return f"错误：覆盖笔记失败：{exc}"

        line_count = len(str(content or "").splitlines())
        lines = [
            "[angel_note_write]",
            f"note_short_id: {result['note_short_id']}",
            f"source_file_path: {result['relative_path']}",
            f"total_lines: {line_count}",
        ]
        if result.get("title"):
            lines.append(f"title: {result['title']}")
        lines.append(f"已覆盖正文并重建索引（切片 {result['chunks']} 段）。")
        return "\n".join(lines)
