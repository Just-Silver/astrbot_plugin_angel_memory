"""笔记标题识别与改写"""

from llm_memory.utils.note_text import extract_h1, rewrite_title


class TestExtractH1:
    def test_returns_first_heading(self):
        assert extract_h1("# 登录方案\n\n## 背景\n") == "登录方案"

    def test_returns_empty_without_heading(self):
        assert extract_h1("没有标题\n正文\n") == ""

    def test_ignores_heading_inside_code_fence(self):
        content = "```\n# 安装依赖\n```\n# 真标题\n正文\n"
        assert extract_h1(content) == "真标题"

    def test_ignores_heading_when_only_fence_has_one(self):
        content = "```bash\n# 安装依赖\nnpm i\n```\n正文\n"
        assert extract_h1(content) == ""

    def test_ignores_hash_without_space(self):
        assert extract_h1("#标签\n正文\n") == ""


class TestRewriteTitle:
    def test_replaces_existing_heading(self):
        assert rewrite_title("# 旧标题\n正文\n", "新标题") == "# 新标题\n正文\n"

    def test_prepends_heading_when_missing(self):
        assert rewrite_title("正文\n", "新标题") == "# 新标题\n\n正文\n"

    def test_prepends_with_crlf_file(self):
        assert rewrite_title("正文\r\n", "新标题") == "# 新标题\r\n\r\n正文\r\n"

    def test_keeps_crlf_on_replaced_heading(self):
        assert rewrite_title("# 旧标题\r\n正文\r\n", "新标题") == "# 新标题\r\n正文\r\n"

    def test_does_not_touch_heading_inside_fence(self):
        content = "```\n# 注释\n```\n# 真标题\n正文\n"
        assert rewrite_title(content, "新标题") == "```\n# 注释\n```\n# 新标题\n正文\n"
