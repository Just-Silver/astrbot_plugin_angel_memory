"""笔记删除与上传接口：文件、索引、命名冲突"""

import base64
import sqlite3
import sys
from pathlib import Path

import pytest
from quart import Quart

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_angel_memory.web_api.notes_api import NotesAPI  # noqa: E402


class FakePathManager:
    def __init__(self, raw_dir, db_path):
        self._raw_dir = raw_dir
        self._db_path = db_path

    def get_raw_dir(self):
        return self._raw_dir

    def get_simple_memory_db_path(self):
        return self._db_path


class FakeNoteService:
    def __init__(self):
        self.removed = []
        self.parsed = []
        self.remove_result = True

    def remove_file_data_by_file_id(self, file_id):
        self.removed.append(file_id)
        return self.remove_result

    def parse_and_store_file_sync(self, file_path, relative_path):
        self.parsed.append(relative_path)
        return 3, {}


class FakePluginContext:
    def __init__(self, raw_dir, db_path, note_service):
        self._path_manager = FakePathManager(raw_dir, db_path)
        self._note_service = note_service

    def get_component(self, name):
        if name == "note_service":
            return self._note_service
        return None

    def get_path_manager(self):
        return self._path_manager


def create_registry(db_path: Path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE note_index_records (
            source_id TEXT PRIMARY KEY,
            note_short_id INTEGER UNIQUE,
            file_id TEXT NOT NULL,
            source_file_path TEXT NOT NULL,
            heading_h1 TEXT,
            heading_h2 TEXT,
            heading_h3 TEXT,
            heading_h4 TEXT,
            heading_h5 TEXT,
            heading_h6 TEXT,
            total_lines INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """
    )
    for source_id, short_id, file_id, rel_path in rows:
        conn.execute(
            "INSERT INTO note_index_records VALUES (?,?,?,?,'','','','','','',10,0)",
            (source_id, short_id, file_id, rel_path),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def workspace(tmp_path):
    raw_dir = tmp_path / "raw"
    note_dir = raw_dir / ".angel" / "note"
    note_dir.mkdir(parents=True)
    db_path = tmp_path / "simple_memory.db"

    first = note_dir / "登录方案.md"
    first.write_text("# 登录方案\n旧内容\n", encoding="utf-8")
    second = note_dir / "部署记录.md"
    second.write_text("# 部署记录\n正文\n", encoding="utf-8")

    create_registry(
        db_path,
        [
            ("note_file_11", 1, "11", ".angel/note/登录方案.md"),
            ("note_file_12", 2, "12", ".angel/note/部署记录.md"),
        ],
    )

    note_service = FakeNoteService()
    plugin_context = FakePluginContext(raw_dir, db_path, note_service)

    api = NotesAPI(plugin_context)
    app = Quart(__name__)
    app.add_url_rule("/notes/delete", "delete", api.delete_notes, methods=["POST"])
    app.add_url_rule("/notes/upload", "upload", api.upload_preview, methods=["POST"])
    app.add_url_rule("/notes/upload/confirm", "confirm", api.upload_confirm, methods=["POST"])
    app.add_url_rule("/notes/recall", "recall", api.recall_note, methods=["POST"])

    return {
        "app": app,
        "raw_dir": raw_dir,
        "db_path": db_path,
        "note_service": note_service,
        "first": first,
        "second": second,
    }


async def post_json(app, url, payload):
    client = app.test_client()
    response = await client.post(url, json=payload)
    return response.status_code, await response.get_json()


class TestDeleteNotes:
    @pytest.mark.asyncio
    async def test_delete_removes_file_and_index(self, workspace):
        status, body = await post_json(workspace["app"], "/notes/delete", {"source_ids": ["note_file_11"]})

        assert status == 200
        assert body["deleted"] == ["note_file_11"]
        assert body["failed"] == []
        assert not workspace["first"].exists()
        assert workspace["second"].exists()
        assert workspace["note_service"].removed == ["11"]

    @pytest.mark.asyncio
    async def test_batch_delete_only_touches_selected_notes(self, workspace):
        status, body = await post_json(
            workspace["app"],
            "/notes/delete",
            {"source_ids": ["note_file_11", "note_file_12"]},
        )

        assert status == 200
        assert sorted(body["deleted"]) == ["note_file_11", "note_file_12"]
        assert not workspace["first"].exists()
        assert not workspace["second"].exists()

    @pytest.mark.asyncio
    async def test_unknown_source_id_is_reported_without_blocking_others(self, workspace):
        status, body = await post_json(
            workspace["app"],
            "/notes/delete",
            {"source_ids": ["note_file_missing", "note_file_11"]},
        )

        assert status == 200
        assert body["deleted"] == ["note_file_11"]
        assert body["failed"][0]["source_id"] == "note_file_missing"

    @pytest.mark.asyncio
    async def test_empty_source_ids_is_rejected(self, workspace):
        status, _body = await post_json(workspace["app"], "/notes/delete", {"source_ids": []})
        assert status == 400

    @pytest.mark.asyncio
    async def test_path_outside_raw_is_refused(self, workspace):
        conn = sqlite3.connect(workspace["db_path"])
        conn.execute(
            "INSERT INTO note_index_records VALUES ('note_file_99',99,'99','../outside.md','','','','','','',1,0)"
        )
        conn.commit()
        conn.close()

        status, body = await post_json(workspace["app"], "/notes/delete", {"source_ids": ["note_file_99"]})

        assert status == 200
        assert body["deleted"] == []
        assert body["failed"][0]["reason"] == "路径越界"

    @pytest.mark.asyncio
    async def test_index_cleanup_failure_is_reported_not_hidden(self, workspace):
        workspace["note_service"].remove_result = False

        status, body = await post_json(workspace["app"], "/notes/delete", {"source_ids": ["note_file_11"]})

        assert status == 200
        assert body["deleted"] == []
        assert "索引清理失败" in body["failed"][0]["reason"]


class TestUploadPreview:
    @pytest.mark.asyncio
    async def test_title_defaults_to_heading(self, workspace):
        content = "# 接口文档\n\n## 鉴权\nBearer Token\n"
        status, body = await post_json(
            workspace["app"],
            "/notes/upload",
            {
                "filename": "任意名字.md",
                "base64": base64.b64encode(content.encode("utf-8")).decode(),
            },
        )

        assert status == 200
        assert body["preview"]["title"] == "接口文档"
        assert body["preview"]["title_source"] == "h1"

    @pytest.mark.asyncio
    async def test_title_falls_back_to_filename_without_heading(self, workspace):
        content = "没有标题的正文\n第二行\n"
        status, body = await post_json(
            workspace["app"],
            "/notes/upload",
            {
                "filename": "备忘.md",
                "base64": base64.b64encode(content.encode("utf-8")).decode(),
            },
        )

        assert status == 200
        assert body["preview"]["title"] == "备忘"
        assert body["preview"]["title_source"] == "filename"

    @pytest.mark.asyncio
    async def test_unsupported_extension_is_rejected(self, workspace):
        status, _body = await post_json(
            workspace["app"],
            "/notes/upload",
            {"filename": "手册.pdf", "base64": base64.b64encode(b"%PDF").decode()},
        )
        assert status == 400

    @pytest.mark.asyncio
    async def test_gbk_file_is_decoded(self, workspace):
        content = "# 中文标题\n内容\n".encode("gbk")
        status, body = await post_json(
            workspace["app"],
            "/notes/upload",
            {"filename": "gbk.txt", "base64": base64.b64encode(content).decode()},
        )

        assert status == 200
        assert body["preview"]["title"] == "中文标题"
        assert body["preview"]["encoding"] == "gbk"


class TestUploadConfirm:
    @pytest.mark.asyncio
    async def test_changed_title_rewrites_heading_and_filename(self, workspace):
        status, body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {"filename": "笔记.md", "title": "新标题", "original_title": "旧标题", "content": "# 旧标题\n正文\n"},
        )

        assert status == 200
        saved = body["saved"]
        assert saved["relative_path"] == "upload/新标题.md"
        assert saved["title"] == "新标题"
        target = workspace["raw_dir"] / "upload" / "新标题.md"
        assert target.read_text(encoding="utf-8") == "# 新标题\n正文\n"
        assert workspace["note_service"].parsed == ["upload/新标题.md"]

    @pytest.mark.asyncio
    async def test_same_title_keeps_body_untouched(self, workspace):
        content = "# 已有一级标题\n正文\n"
        status, body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {"filename": "笔记.md", "title": "已有一级标题", "original_title": "已有一级标题", "content": content},
        )

        assert status == 200
        target = workspace["raw_dir"] / body["saved"]["relative_path"]
        assert target.read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_existing_name_gets_timestamp_suffix(self, workspace):
        upload_dir = workspace["raw_dir"] / "upload"
        upload_dir.mkdir(parents=True)
        existing = upload_dir / "接口文档.md"
        existing.write_text("# 旧版本\n", encoding="utf-8")

        status, body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {
                "filename": "接口文档.md",
                "title": "接口文档",
                "original_title": "接口文档",
                "content": "# 接口文档\n新版本\n",
            },
        )

        assert status == 200
        assert body["saved"]["relative_path"] != "upload/接口文档.md"
        assert existing.read_text(encoding="utf-8") == "# 旧版本\n"
        assert (workspace["raw_dir"] / body["saved"]["relative_path"]).exists()

    @pytest.mark.asyncio
    async def test_empty_content_is_rejected(self, workspace):
        status, _body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {"filename": "笔记.md", "title": "标题", "content": "   "},
        )
        assert status == 400

    @pytest.mark.asyncio
    async def test_unchanged_title_keeps_body_without_heading_untouched(self, workspace):
        content = "没有标题的正文\n第二行\n"
        status, body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {
                "filename": "备忘.md",
                "title": "备忘",
                "original_title": "备忘",
                "content": content,
            },
        )

        assert status == 200
        target = workspace["raw_dir"] / body["saved"]["relative_path"]
        assert target.read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_changed_title_does_not_touch_heading_inside_code_fence(self, workspace):
        content = "```bash\n# 安装依赖\nnpm i\n```\n正文\n"
        status, body = await post_json(
            workspace["app"],
            "/notes/upload/confirm",
            {
                "filename": "命令.md",
                "title": "常用命令",
                "original_title": "命令",
                "content": content,
            },
        )

        assert status == 200
        target = workspace["raw_dir"] / body["saved"]["relative_path"]
        written = target.read_text(encoding="utf-8")
        assert "# 安装依赖" in written
        assert written.startswith("# 常用命令\n\n```bash")

    @pytest.mark.asyncio
    async def test_over_size_upload_is_rejected(self, workspace):
        big = base64.b64encode(b"a" * (2 * 1024 * 1024 + 1)).decode()
        status, _body = await post_json(workspace["app"], "/notes/upload", {"filename": "big.md", "base64": big})
        assert status == 400


class TestRecallNoteParams:
    @pytest.mark.asyncio
    async def test_missing_short_id_returns_400_not_500(self, workspace):
        status, body = await post_json(workspace["app"], "/notes/recall", {})
        assert status == 400
        assert "note_short_id" in body["error"]

    @pytest.mark.asyncio
    async def test_null_offset_defaults_to_first_line(self, workspace):
        status, body = await post_json(workspace["app"], "/notes/recall", {"note_short_id": 1, "offset": None})
        assert status == 200
        assert body["actual_start_line"] == 1
