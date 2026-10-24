"""精确文本编辑算法测试（无 IO，纯函数）"""

import pytest

from llm_memory.utils.text_edit import EditError, apply_edit


class TestExactMatch:
    def test_replaces_single_occurrence(self):
        source = "# 登录方案\n\n## 背景\n旧内容\n"
        edited, count = apply_edit(source, "旧内容", "新内容")
        assert count == 1
        assert edited == "# 登录方案\n\n## 背景\n新内容\n"

    def test_rejects_duplicate_match_without_replace_all(self):
        source = "A\nB\nA\n"
        with pytest.raises(EditError) as exc:
            apply_edit(source, "A", "C")
        assert "2 处" in str(exc.value)

    def test_replace_all_replaces_every_occurrence(self):
        source = "A\nB\nA\n"
        edited, count = apply_edit(source, "A", "C", replace_all=True)
        assert count == 2
        assert edited == "C\nB\nC\n"

    def test_missing_text_reports_not_found(self):
        with pytest.raises(EditError) as exc:
            apply_edit("# 标题\n正文\n", "不存在的段落", "x")
        assert "找不到" in str(exc.value)

    def test_identical_strings_rejected(self):
        with pytest.raises(EditError) as exc:
            apply_edit("abc", "abc", "abc")
        assert "没有变化" in str(exc.value)

    def test_empty_old_string_rejected(self):
        with pytest.raises(EditError):
            apply_edit("abc", "", "x")

    def test_multiline_block_replaced_as_whole(self):
        source = "## A\n第一行\n第二行\n## B\n"
        edited, count = apply_edit(source, "第一行\n第二行", "合并成一行")
        assert count == 1
        assert edited == "## A\n合并成一行\n## B\n"

    def test_adjacent_occurrences_not_double_counted(self):
        source = "aaaa"
        with pytest.raises(EditError) as exc:
            apply_edit(source, "aa", "b")
        assert "2 处" in str(exc.value)


class TestNormalizedMatch:
    def test_curly_quotes_match_straight_quotes(self):
        source = '他说“明天上线”\n'
        edited, count = apply_edit(source, '他说"明天上线"', '他说"下周上线"')
        assert count == 1
        assert edited == '他说"下周上线"\n'

    def test_dash_variants_match(self):
        source = "范围 1—10 天\n"
        edited, count = apply_edit(source, "范围 1-10 天", "范围 1-30 天")
        assert count == 1
        assert edited == "范围 1-30 天\n"

    def test_fullwidth_space_matches(self):
        source = "关键词\u3000空格\n"
        edited, count = apply_edit(source, "关键词 空格", "关键词 空行")
        assert count == 1
        assert edited == "关键词 空行\n"


class TestLineMatch:
    def test_trailing_whitespace_difference_matches(self):
        source = "# 标题\n  缩进内容   \n结尾\n"
        edited, count = apply_edit(source, "  缩进内容\n", "  缩进内容\n新增一行\n")
        assert count == 1
        assert "新增一行" in edited
        assert edited.endswith("结尾\n")

    def test_line_match_replaces_whole_line(self):
        source = "第一行   \n第二行\n"
        edited, count = apply_edit(source, "第一行  \n", "替换\n")
        assert count == 1
        assert edited == "替换\n第二行\n"


class TestLineEndings:
    def test_crlf_file_stays_crlf(self):
        source = "# 标题\r\n旧内容\r\n尾行\r\n"
        edited, count = apply_edit(source, "旧内容", "新内容")
        assert count == 1
        assert edited == "# 标题\r\n新内容\r\n尾行\r\n"

    def test_lf_file_stays_lf(self):
        source = "# 标题\n旧内容\n"
        edited, _ = apply_edit(source, "旧内容", "新内容")
        assert edited == "# 标题\n新内容\n"
        assert "\r" not in edited

    def test_crlf_old_string_from_lf_view_matches(self):
        """模型读到的是 LF 视图，传入的 old_string 也是 LF"""
        source = "第一段\r\n第二段\r\n"
        edited, count = apply_edit(source, "第一段\n第二段", "合并")
        assert count == 1
        assert edited == "合并\r\n"
