"""笔记编辑服务：落盘、越界、外部改动检测、索引同步"""

import os

import pytest

from llm_memory.service import note_edit_service as service_module
from llm_memory.service.note_edit_service import NoteEditError, NoteEditService
from llm_memory.utils.text_edit import EditError


class FakePathManager:
    def __init__(self, raw_dir):
        self._raw_dir = raw_dir

    def get_raw_dir(self):
        return self._raw_dir


class FakeMemorySqlManager:
    def __init__(self, rows):
        self._rows = rows

    async def get_note_index_by_short_id(self, short_id):
        return self._rows.get(short_id)


class FakeNoteService:
    def __init__(self):
        self.calls = []

    def parse_and_store_file_sync(self, file_path, relative_path):
        self.calls.append(relative_path)
        return 2, {}


class FakePluginContext:
    def __init__(self, raw_dir, rows, note_service):
        self._path_manager = FakePathManager(raw_dir)
        self._memory_sql_manager = FakeMemorySqlManager(rows)
        self._note_service = note_service

    def get_component(self, name):
        if name == "memory_sql_manager":
            return self._memory_sql_manager
        if name == "note_service":
            return self._note_service
        return None

    def get_path_manager(self):
        return self._path_manager


@pytest.fixture
def note_file(tmp_path):
    raw_dir = tmp_path / "raw"
    notes_dir = raw_dir / ".angel" / "note"
    notes_dir.mkdir(parents=True)
    path = notes_dir / "登录方案.md"
    path.write_text("# 登录方案\n\n## 背景\n旧内容\n", encoding="utf-8", newline="")
    return raw_dir, path


def build_service(raw_dir, rows, note_service):
    return NoteEditService(FakePluginContext(raw_dir, rows, note_service))


class TestResolve:
    @pytest.mark.asyncio
    async def test_unknown_short_id_is_rejected(self, note_file):
        raw_dir, _path = note_file
        service = build_service(raw_dir, {}, FakeNoteService())
        with pytest.raises(NoteEditError) as exc:
            await service.resolve(999)
        assert "未找到" in str(exc.value)

    @pytest.mark.asyncio
    async def test_path_outside_raw_is_rejected(self, note_file):
        raw_dir, _path = note_file
        rows = {1: {"source_file_path": "../outside.md", "file_id": "9"}}
        service = build_service(raw_dir, rows, FakeNoteService())
        with pytest.raises(NoteEditError) as exc:
            await service.resolve(1)
        assert "越界" in str(exc.value)


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_updates_file_and_resyncs_index(self, note_file):
        raw_dir, path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        note_service = FakeNoteService()
        service = build_service(raw_dir, rows, note_service)

        result = await service.edit(1, "旧内容", "新内容")

        assert path.read_text(encoding="utf-8") == "# 登录方案\n\n## 背景\n新内容\n"
        assert result["replacements"] == 1
        assert result["chunks"] == 2
        assert note_service.calls == [".angel/note/登录方案.md"]

    @pytest.mark.asyncio
    async def test_edit_reports_new_title_when_heading_changes(self, note_file):
        raw_dir, _path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        service = build_service(raw_dir, rows, FakeNoteService())

        result = await service.edit(1, "# 登录方案", "# 登录方案（修订）")

        assert result["title"] == "登录方案（修订）"

    @pytest.mark.asyncio
    async def test_missing_old_string_leaves_file_untouched(self, note_file):
        raw_dir, path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        note_service = FakeNoteService()
        service = build_service(raw_dir, rows, note_service)

        with pytest.raises(EditError):
            await service.edit(1, "不存在的段落", "x")

        assert "旧内容" in path.read_text(encoding="utf-8")
        assert note_service.calls == []

    @pytest.mark.asyncio
    async def test_external_change_after_read_is_rejected(self, note_file, monkeypatch):
        raw_dir, path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        service = build_service(raw_dir, rows, FakeNoteService())

        original_apply = service_module.apply_edit

        def racing_apply(source, old_string, new_string, replace_all=False):
            result = original_apply(source, old_string, new_string, replace_all)
            bumped = path.stat().st_mtime_ns + 5_000_000_000
            os.utime(path, ns=(bumped, bumped))
            return result

        monkeypatch.setattr(service_module, "apply_edit", racing_apply)

        with pytest.raises(NoteEditError) as exc:
            await service.edit(1, "旧内容", "新内容")

        assert "已被其他写入改动" in str(exc.value)
        assert "旧内容" in path.read_text(encoding="utf-8")


class TestOverwrite:
    @pytest.mark.asyncio
    async def test_overwrite_replaces_whole_body(self, note_file):
        raw_dir, path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        note_service = FakeNoteService()
        service = build_service(raw_dir, rows, note_service)

        result = await service.overwrite(1, "# 新标题\n整篇重写\n")

        assert path.read_text(encoding="utf-8") == "# 新标题\n整篇重写\n"
        assert result["title"] == "新标题"
        assert note_service.calls == [".angel/note/登录方案.md"]

    @pytest.mark.asyncio
    async def test_overwrite_with_identical_content_is_rejected(self, note_file):
        raw_dir, path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        service = build_service(raw_dir, rows, FakeNoteService())

        with pytest.raises(NoteEditError) as exc:
            await service.overwrite(1, path.read_text(encoding="utf-8"))

        assert "没有变化" in str(exc.value)

    @pytest.mark.asyncio
    async def test_empty_content_is_rejected(self, note_file):
        raw_dir, _path = note_file
        rows = {1: {"source_file_path": ".angel/note/登录方案.md", "file_id": "9"}}
        service = build_service(raw_dir, rows, FakeNoteService())

        with pytest.raises(NoteEditError):
            await service.overwrite(1, "   \n")
