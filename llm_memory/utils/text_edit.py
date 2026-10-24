"""精确文本编辑：匹配与替换算法（纯函数，无 IO）。

三级降级匹配，语义对齐 opencode 的 edit 工具：
1. 精确匹配
2. 归一化匹配（弯引号、破折号、特殊空格）
3. 行级匹配（忽略每行行尾空白）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

CRLF = "\r\n"

_SINGLE_CHAR_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
    "\u2002": " ", "\u2003": " ", "\u2004": " ", "\u2005": " ",
    "\u2006": " ", "\u2007": " ", "\u2008": " ", "\u2009": " ",
    "\u200a": " ",
}

_SINGLE_CHAR_TABLE = str.maketrans(_SINGLE_CHAR_MAP)


class EditError(Exception):
    """匹配或替换失败；消息可直接返回给模型。"""


@dataclass(frozen=True)
class Match:
    start: int
    end: int


@dataclass(frozen=True)
class _Line:
    start: int
    text: str
    end: int
    has_newline: bool


def normalize_for_match(value: str) -> str:
    """把排版变体归一成同一字符；逐字符 1:1 映射，偏移量保持不变。"""
    return value.translate(_SINGLE_CHAR_TABLE)


def is_pure_crlf(text: str) -> bool:
    total = text.count("\n")
    return total > 0 and text.count(CRLF) == total


def _find_occurrences(content: str, search: str) -> List[Match]:
    matches: List[Match] = []
    offset = 0
    while True:
        found = content.find(search, offset)
        if found == -1:
            return matches
        matches.append(Match(found, found + len(search)))
        offset = found + len(search)


def _split_lines(content: str) -> List[_Line]:
    lines: List[_Line] = []
    position = 0
    length = len(content)
    while position < length:
        newline = content.find("\n", position)
        if newline == -1:
            lines.append(_Line(position, content[position:], length, False))
            break
        lines.append(_Line(position, content[position:newline], newline + 1, True))
        position = newline + 1
    return lines


def _normalized_line(text: str) -> str:
    return normalize_for_match(text.rstrip())


def _find_line_occurrences(content: str, search: str) -> List[Match]:
    trailing_newline = search.endswith("\n")
    expected = search.split("\n")
    if trailing_newline:
        expected.pop()
    if not expected:
        return []

    lines = _split_lines(content)
    if len(lines) < len(expected):
        return []

    expected_keys = [_normalized_line(item) for item in expected]
    matches: List[Match] = []
    for index in range(len(lines) - len(expected) + 1):
        window = lines[index:index + len(expected)]
        if any(_normalized_line(line.text) != key for line, key in zip(window, expected_keys)):
            continue
        last = window[-1]
        if trailing_newline and not last.has_newline:
            continue
        end = last.end if trailing_newline else last.start + len(last.text.rstrip())
        candidate = Match(window[0].start, end)
        if matches and candidate.start < matches[-1].end:
            continue
        matches.append(candidate)
    return matches


def _locate(view: str, search: str) -> List[Match]:
    matches = _find_occurrences(view, search)
    if matches:
        return matches

    normalized_view = normalize_for_match(view)
    normalized_search = normalize_for_match(search)
    if normalized_search != search or normalized_view != view:
        matches = _find_occurrences(normalized_view, normalized_search)
        if matches:
            return matches

    if "\n" in search or search.rstrip() != search:
        return _find_line_occurrences(view, search)
    return []


def apply_edit(
    source: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[str, int]:
    """把 source 中的 old_string 替换为 new_string。

    Returns:
        (新内容, 替换处数)

    Raises:
        EditError: 参数无效、找不到、多处匹配或内容未变化。
    """
    if not old_string:
        raise EditError(
            "old_string 不能为空。新建笔记请用 angel_note_create，整篇覆盖请用 angel_note_write。"
        )
    if old_string == new_string:
        raise EditError("没有变化：old_string 与 new_string 相同。")

    ending = CRLF if is_pure_crlf(source) else "\n"
    view = source.replace(CRLF, "\n")
    search = old_string.replace(CRLF, "\n")
    replacement = new_string.replace(CRLF, "\n")

    matches = _locate(view, search)
    if not matches:
        raise EditError(
            "在笔记中找不到 old_string。它必须与原文完全一致，包括缩进和空行。"
        )
    if len(matches) > 1 and not replace_all:
        raise EditError(
            f"找到 {len(matches)} 处匹配，但默认只允许一处。"
            "请补充更多上下文使其唯一，或设 replace_all=true。"
        )

    selected = matches if replace_all else matches[:1]
    edited = view
    for match in reversed(selected):
        edited = edited[: match.start] + replacement + edited[match.end:]

    if edited == view:
        raise EditError("没有变化：这次编辑没有改动文件。")

    if ending == CRLF:
        edited = edited.replace("\n", CRLF)
    return edited, len(selected)
