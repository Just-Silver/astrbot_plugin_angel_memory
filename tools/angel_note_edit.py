from dataclasses import dataclass, field

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from ..llm_memory.service.note_edit_service import NoteEditError, NoteEditService
from ..llm_memory.utils.text_edit import EditError


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


@dataclass
class NoteEditTool(FunctionTool):
    name: str = "angel_note_edit"
    description: str = (
        "编辑已有笔记：把 old_string 精确替换为 new_string，两者必须不同。"
        "默认 old_string 必须只出现一次；出现多次时补充更多上下文使其唯一，或设 replace_all=true 替换全部。"
        "old_string 必须与笔记原文逐字一致（含缩进、空行、行尾换行），"
        "且不要包含 angel_note_read 返回的元信息头（note_short_id / source_file_path / total_lines 那几行）。"
        "匹配失败时会依次忽略弯引号、破折号、全角空格，再忽略每行行尾空白的差异重试；仍失败则报错且不写盘。"
        "你看到的笔记内容通常只是切片，不是全文。编辑前请先调用 angel_note_read 读取完整正文，再从正文中摘取 old_string。"
        '笔记标题是正文中第一处以 "# " 开头的行（代码块里的 # 不算），改标题就替换那一行。'
        "本工具不能新建笔记（用 angel_note_create），也不能整篇覆盖（用 angel_note_write）。"
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
                "old_string": {
                    "type": "string",
                    "description": ("要被替换的原文。必须与笔记正文逐字一致，不含 angel_note_read 的元信息头。"),
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本，必须与 old_string 不同。",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": ("是否替换所有出现位置。默认 false 时 old_string 必须只出现一次。"),
                    "default": False,
                },
            },
            "required": ["note_short_id", "old_string", "new_string"],
        }
    )

    def __post_init__(self):
        self.logger = logger

    async def run(
        self,
        event: AstrMessageEvent,
        note_short_id: int,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        self.logger.debug(
            f"{self.name} - LLM 调用: note_short_id={note_short_id} "
            f"old_len={len(str(old_string or ''))} new_len={len(str(new_string or ''))}"
        )

        if not hasattr(event, "plugin_context") or event.plugin_context is None:
            return "错误：无法获取插件上下文。"
        plugin_context = event.plugin_context
        if plugin_context.get_component("note_service") is None:
            return "错误：笔记服务不可用，系统可能仍在初始化中。"

        service = NoteEditService(plugin_context)
        try:
            result = await service.edit(
                note_short_id,
                str(old_string or ""),
                str(new_string or ""),
                _as_bool(replace_all),
            )
        except (EditError, NoteEditError) as exc:
            return f"错误：{exc}"
        except Exception as exc:
            self.logger.error(f"{self.name}: 编辑笔记失败: {exc}", exc_info=True)
            return f"错误：编辑笔记失败：{exc}"

        lines = [
            "[angel_note_edit]",
            f"note_short_id: {result['note_short_id']}",
            f"source_file_path: {result['relative_path']}",
            f"replacements: {result['replacements']}",
        ]
        if result.get("title_changed") and result.get("title"):
            lines.append(f"title: {result['title']}")
        lines.append(f"已更新正文并重建索引（切片 {result['chunks']} 段）。")
        return "\n".join(lines)
