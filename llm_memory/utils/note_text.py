"""笔记标题的识别与改写。

一级标题只认围栏代码块之外的 "# " 行：笔记里贴 shell 命令时，
代码块中的 "# 安装依赖" 不能被当成文档标题。
"""

from __future__ import annotations

from typing import List, Optional

from .text_edit import is_pure_crlf

_FENCE_MARKERS = ("```", "~~~")


def find_h1_line_index(lines: List[str]) -> Optional[int]:
    """返回第一处一级标题所在行号；找不到返回 None。"""
    in_fence = False
    fence_marker = ""
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r")
        stripped = line.lstrip()
        for marker in _FENCE_MARKERS:
            if stripped.startswith(marker):
                if not in_fence:
                    in_fence = True
                    fence_marker = marker
                elif marker == fence_marker:
                    in_fence = False
                break
        else:
            if in_fence:
                continue
            if stripped.startswith("# ") and stripped[2:].strip():
                return index
    return None


def extract_h1(content: str) -> str:
    """取正文的一级标题文本；没有则返回空串。"""
    lines = str(content or "").split("\n")
    index = find_h1_line_index(lines)
    if index is None:
        return ""
    return lines[index].rstrip("\r").lstrip()[2:].strip()


def rewrite_title(content: str, title: str) -> str:
    """把正文的一级标题改成 title；没有标题则在文件头补一行。

    保持原文件的行尾风格，未涉及的行一个字节都不动。
    """
    lines = str(content or "").split("\n")
    index = find_h1_line_index(lines)
    if index is not None:
        carriage = "\r" if lines[index].endswith("\r") else ""
        lines[index] = f"# {title}{carriage}"
        return "\n".join(lines)

    carriage = "\r" if is_pure_crlf(content) else ""
    separator = f"{carriage}\n{carriage}\n"
    return f"# {title}{separator}{content}"


__all__ = ["extract_h1", "find_h1_line_index", "rewrite_title"]
