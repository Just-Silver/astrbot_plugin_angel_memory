"""
笔记相关 API
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from quart import jsonify, request

from ..llm_memory.service.note_edit_service import atomic_write_text
from ..llm_memory.utils.note_text import extract_h1, rewrite_title

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class NotesAPI:
    KEYWORD_SEARCH_MAX_CANDIDATES = 500

    def __init__(self, plugin_context):
        self.plugin_context = plugin_context

    def _get_central_conn(self) -> sqlite3.Connection | None:
        path_manager = self.plugin_context.get_path_manager()
        db_path = str(path_manager.get_simple_memory_db_path())
        if not os.path.exists(db_path):
            return None
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_raw_dir(self) -> Path:
        path_manager = self.plugin_context.get_path_manager()
        return path_manager.get_raw_dir()

    def _load_note_registry_map(self, paths: list[str], file_ids: list[str]) -> dict[str, dict[str, Any]]:
        """按路径和 file_id 读取笔记注册表，用于补齐切片搜索结果。"""
        normalized_paths = sorted(
            {str(path or "").replace("\\", "/").strip().lstrip("/") for path in paths if str(path or "").strip()}
        )
        normalized_file_ids = sorted({str(file_id or "").strip() for file_id in file_ids if str(file_id or "").strip()})
        if not normalized_paths and not normalized_file_ids:
            return {}

        conn = self._get_central_conn()
        if not conn:
            return {}

        try:
            clauses: list[str] = []
            params: list[Any] = []
            if normalized_paths:
                placeholders = ",".join(["?" for _ in normalized_paths])
                clauses.append(f"source_file_path IN ({placeholders})")
                params.extend(normalized_paths)
            if normalized_file_ids:
                placeholders = ",".join(["?" for _ in normalized_file_ids])
                clauses.append(f"file_id IN ({placeholders})")
                params.extend(normalized_file_ids)
            where_sql = " OR ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT
                    source_id, IFNULL(note_short_id, -1) AS note_short_id,
                    file_id, source_file_path,
                    IFNULL(heading_h1, '') AS heading_h1,
                    IFNULL(heading_h2, '') AS heading_h2,
                    IFNULL(heading_h3, '') AS heading_h3,
                    IFNULL(heading_h4, '') AS heading_h4,
                    IFNULL(heading_h5, '') AS heading_h5,
                    IFNULL(heading_h6, '') AS heading_h6,
                    IFNULL(total_lines, 0) AS total_lines,
                    updated_at,
                    '' AS tags_text
                FROM note_index_records
                WHERE {where_sql}
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            path = str(item.get("source_file_path") or "").replace("\\", "/").strip().lstrip("/")
            file_id = str(item.get("file_id") or "").strip()
            if path:
                result[f"path:{path}"] = item
            if file_id:
                result[f"file:{file_id}"] = item
        return result

    async def browse_notes(self):
        """分页浏览笔记索引（使用切片搜索）"""
        keyword = request.args.get("keyword", "")
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(1, int(request.args.get("page_size", 20))))
        offset = (page - 1) * page_size

        # 如果有关键词，优先使用切片搜索
        if keyword.strip():
            search_engine = self.plugin_context.get_component("note_chunk_search")
            if search_engine is not None:
                try:
                    requested_end = offset + page_size
                    search_limit = min(
                        self.KEYWORD_SEARCH_MAX_CANDIDATES,
                        max(page_size * 3, requested_end + 1),
                    )
                    results = await search_engine.search_async(
                        query=keyword.strip(),
                        limit=search_limit,
                        max_chunks_per_file=1,
                    )
                    has_more = len(results) > requested_end
                    paged = results[offset : offset + page_size]
                    registry_map = self._load_note_registry_map(
                        [str(r.get("source_file_path") or "") for r in paged],
                        [str(r.get("file_id") or "") for r in paged],
                    )
                    items = []
                    for r in paged:
                        source_file_path = str(r.get("source_file_path") or "").replace("\\", "/").strip().lstrip("/")
                        file_id = str(r.get("file_id") or "").strip()
                        registry = (
                            registry_map.get(f"path:{source_file_path}") or registry_map.get(f"file:{file_id}") or {}
                        )
                        items.append(
                            {
                                "source_id": registry.get("source_id", ""),
                                "note_short_id": int(registry.get("note_short_id", -1) or -1),
                                "source_file_path": source_file_path,
                                "file_id": file_id,
                                "heading_h1": registry.get("heading_h1", ""),
                                "heading_h2": registry.get("heading_h2", ""),
                                "heading_h3": registry.get("heading_h3", ""),
                                "heading_h4": registry.get("heading_h4", ""),
                                "heading_h5": registry.get("heading_h5", ""),
                                "heading_h6": registry.get("heading_h6", ""),
                                "total_lines": int(registry.get("total_lines", 0) or 0),
                                "updated_at": registry.get("updated_at"),
                                "tags_text": registry.get("tags_text", ""),
                                "chunk_index": r.get("chunk_index", 0),
                                "line_start": r.get("line_start", 0),
                                "line_end": r.get("line_end", 0),
                                "content": str(r.get("content") or "")[:200],
                                "score": float(r.get("score", 0.0)),
                            }
                        )
                    total = offset + len(items) + (1 if has_more else 0)
                    return jsonify(
                        {
                            "items": items,
                            "total": total,
                            "page": page,
                            "page_size": page_size,
                            "total_pages": math.ceil(total / page_size) if total > 0 else 1,
                            "search_mode": "chunk",
                            "has_more": has_more,
                            "total_is_estimated": has_more or search_limit >= self.KEYWORD_SEARCH_MAX_CANDIDATES,
                        }
                    )
                except Exception as e:
                    logger.warning(f"切片搜索失败，降级到 SQL 浏览: {e}")

        conn = self._get_central_conn()
        if not conn:
            return jsonify({"items": [], "total": 0, "page": page, "page_size": page_size})

        try:
            where_sql = ""
            params: list[Any] = []
            if keyword.strip():
                kw = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where_sql = "WHERE nir.source_file_path LIKE ? ESCAPE '\\'"
                like = f"%{kw}%"
                params.append(like)

            # 总数
            sql_count = f"""
                SELECT COUNT(*) AS cnt
                FROM note_index_records nir
                {where_sql}
            """
            cur = conn.cursor()
            cur.execute(sql_count, params)
            total = int(cur.fetchone()["cnt"])

            # 数据
            sql = f"""
                SELECT
                    nir.source_id, IFNULL(nir.note_short_id, -1) AS note_short_id,
                    nir.file_id, nir.source_file_path,
                    IFNULL(nir.heading_h1, '') AS heading_h1,
                    IFNULL(nir.heading_h2, '') AS heading_h2,
                    IFNULL(nir.heading_h3, '') AS heading_h3,
                    IFNULL(nir.heading_h4, '') AS heading_h4,
                    IFNULL(nir.heading_h5, '') AS heading_h5,
                    IFNULL(nir.heading_h6, '') AS heading_h6,
                    IFNULL(nir.total_lines, 0) AS total_lines,
                    nir.updated_at,
                    '' AS tags_text
                FROM note_index_records nir
                {where_sql}
                ORDER BY nir.updated_at DESC, nir.source_file_path ASC
                LIMIT ? OFFSET ?
            """
            cur.execute(sql, params + [page_size, offset])
            items = [dict(row) for row in cur.fetchall()]

            return jsonify(
                {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": math.ceil(total / page_size) if total > 0 else 1,
                }
            )
        except Exception as e:
            return jsonify({"items": [], "total": 0, "error": str(e)}), 500
        finally:
            conn.close()

    async def recall_note(self):
        """模拟 angel_note_read"""
        data = await request.get_json(silent=True) or {}
        try:
            note_short_id = int(data.get("note_short_id", -1))
        except (TypeError, ValueError):
            note_short_id = -1
        if note_short_id < 0:
            return jsonify({"error": "note_short_id 必填，且必须是不小于 0 的整数"}), 400
        try:
            offset = int(data.get("offset") or 1)
        except (TypeError, ValueError):
            offset = 1
        offset = max(1, offset)
        try:
            limit = int(data.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, limit)

        conn = self._get_central_conn()
        if not conn:
            return jsonify({"error": "数据库不可用"}), 500

        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT source_file_path FROM note_index_records WHERE note_short_id = ? LIMIT 1",
                (note_short_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": f"未找到 note_short_id={note_short_id}"}), 404

            rel_path = str(row["source_file_path"] or "").replace("\\", "/").strip().lstrip("/")
            raw_dir = self._get_raw_dir()
            target = (raw_dir / rel_path).resolve()
            raw_root = raw_dir.resolve()

            if raw_root not in target.parents and target != raw_root:
                return jsonify({"error": "路径越界"}), 400

            if not target.exists():
                return jsonify({"error": f"文件不存在: {rel_path}"}), 404

            text = target.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            total_lines = len(lines)

            actual_start = min(max(1, offset), total_lines) if total_lines > 0 else 0
            actual_end = min(actual_start + max(1, limit) - 1, total_lines) if total_lines > 0 else 0
            content = "\n".join(lines[actual_start - 1 : actual_end]) if total_lines > 0 else ""

            return jsonify(
                {
                    "content": content,
                    "note_short_id": note_short_id,
                    "source_file_path": rel_path,
                    "total_lines": total_lines,
                    "actual_start_line": actual_start,
                    "actual_end_line": actual_end,
                }
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    # ---------- 删除 ----------

    def _resolve_raw_path(self, rel_path: str) -> Path | None:
        """把相对路径解析到 raw/ 内；越界返回 None。"""
        raw_dir = self._get_raw_dir()
        target = (raw_dir / str(rel_path).replace("/", os.sep)).resolve()
        raw_root = raw_dir.resolve()
        if raw_root not in target.parents:
            return None
        return target

    def _delete_notes_sync(self, source_ids: list[str]) -> tuple[list[str], list[dict[str, str]]]:
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        note_service = self.plugin_context.get_component("note_service")
        if note_service is None:
            return [], [{"source_id": sid, "reason": "笔记服务不可用"} for sid in source_ids]

        # 先把记录读出来并断开连接，避免删除文件/重建索引期间占着同一个库的读游标
        records: dict[str, dict[str, str]] = {}
        conn = self._get_central_conn()
        if not conn:
            return [], [{"source_id": sid, "reason": "数据库不可用"} for sid in source_ids]
        try:
            for source_id in source_ids:
                row = conn.execute(
                    "SELECT file_id, source_file_path FROM note_index_records WHERE source_id = ? LIMIT 1",
                    (source_id,),
                ).fetchone()
                if row:
                    records[source_id] = {
                        "file_id": str(row["file_id"] or "").strip(),
                        "source_file_path": str(row["source_file_path"] or ""),
                    }
        finally:
            conn.close()

        for source_id in source_ids:
            record = records.get(source_id)
            if not record:
                failed.append({"source_id": source_id, "reason": "索引中不存在该笔记"})
                continue
            try:
                rel_path = record["source_file_path"].replace("\\", "/").strip().lstrip("/")
                target = self._resolve_raw_path(rel_path)
                if target is None:
                    failed.append({"source_id": source_id, "reason": "路径越界"})
                    continue

                # 先删文件，再清派生索引：最坏情况只留下可被启动扫描兜底清理的孤儿索引
                if target.exists():
                    target.unlink()
                if not note_service.remove_file_data_by_file_id(record["file_id"]):
                    failed.append(
                        {
                            "source_id": source_id,
                            "reason": "文件已删除，但索引清理失败（重启后由启动扫描重建）",
                        }
                    )
                    continue
                deleted.append(source_id)
            except Exception as exc:
                logger.warning(f"删除笔记失败 source_id={source_id}: {exc}")
                failed.append({"source_id": source_id, "reason": str(exc)})
        return deleted, failed

    async def delete_notes(self):
        """删除一篇或多篇笔记：磁盘文件 + 元数据 + 切片 + 检索索引。"""
        data = await request.get_json(silent=True) or {}
        raw_ids = data.get("source_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "source_ids 不能为空"}), 400

        source_ids = [str(item or "").strip() for item in raw_ids if str(item or "").strip()]
        if not source_ids:
            return jsonify({"error": "source_ids 不能为空"}), 400
        if len(source_ids) > 200:
            return jsonify({"error": "单次最多删除 200 篇"}), 400

        deleted, failed = await asyncio.to_thread(self._delete_notes_sync, source_ids)
        if deleted:
            logger.info(f"[笔记删除] 删除完成 count={len(deleted)} 失败={len(failed)}")
        return jsonify({"deleted": deleted, "failed": failed})

    # ---------- 上传 ----------

    MAX_UPLOAD_BYTES = 2 * 1024 * 1024
    UPLOAD_SUBDIR = "upload"

    @staticmethod
    def _decode_bytes(raw: bytes) -> tuple[str | None, str]:
        for encoding in ("utf-8-sig", "gbk"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        return None, ""

    @staticmethod
    def _sanitize_title(title: str) -> str:
        cleaned = "".join(
            char for char in str(title or "") if char.isprintable() or char in " \t"
        )
        safe = re.sub(r'[<>:"/\\|?*]', "_", cleaned.replace("\t", " "))
        safe = re.sub(r"\s+", " ", safe).strip(". ")
        return safe[:80].strip(". ") or "untitled"

    def _allocate_upload_path(self, title: str, extension: str) -> Path:
        upload_dir = self._get_raw_dir() / self.UPLOAD_SUBDIR
        upload_dir.mkdir(parents=True, exist_ok=True)
        stem = self._sanitize_title(title)
        candidate = upload_dir / f"{stem}{extension}"
        if not candidate.exists():
            return candidate

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = upload_dir / f"{stem}_{stamp}{extension}"
        attempt = 1
        while candidate.exists():
            candidate = upload_dir / f"{stem}_{stamp}_{attempt}{extension}"
            attempt += 1
        return candidate

    async def upload_preview(self):
        """接收文件内容并解析标题，不写盘。"""
        data = await request.get_json(silent=True) or {}
        filename = Path(str(data.get("filename") or "").strip()).name
        encoded = str(data.get("base64") or "").strip()
        if not filename or not encoded:
            return jsonify({"error": "filename 与 base64 均为必填"}), 400

        extension = Path(filename).suffix.lower()
        if extension not in {".md", ".txt"}:
            return jsonify({"error": "只支持 .md 和 .txt 文件。"}), 400

        try:
            raw_bytes = base64.b64decode(encoded, validate=True)
        except Exception:
            return jsonify({"error": "文件内容解码失败"}), 400

        if len(raw_bytes) > self.MAX_UPLOAD_BYTES:
            limit_mb = self.MAX_UPLOAD_BYTES // (1024 * 1024)
            return jsonify({"error": f"文件超过 {limit_mb} MB 上限。"}), 400

        text, encoding = self._decode_bytes(raw_bytes)
        if text is None:
            return jsonify({"error": "无法识别文件编码，请另存为 UTF-8。"}), 400
        if not text.strip():
            return jsonify({"error": "文件内容为空。"}), 400

        heading = extract_h1(text)
        title = heading or Path(filename).stem
        return jsonify(
            {
                "preview": {
                    "filename": filename,
                    "extension": extension,
                    "size_bytes": len(raw_bytes),
                    "encoding": encoding,
                    "title": title,
                    "title_source": "h1" if heading else "filename",
                    "content": text,
                }
            }
        )

    async def upload_confirm(self):
        """把预览后确认的正文写入 raw/upload/ 并建立索引。"""
        data = await request.get_json(silent=True) or {}
        filename = Path(str(data.get("filename") or "").strip()).name
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or "")
        if not filename:
            return jsonify({"error": "filename 必填"}), 400
        if not title:
            title = Path(filename).stem

        extension = Path(filename).suffix.lower()
        if extension not in {".md", ".txt"}:
            return jsonify({"error": "只支持 .md 和 .txt 文件。"}), 400
        if not content.strip():
            return jsonify({"error": "文件内容为空。"}), 400
        if len(content.encode("utf-8")) > self.MAX_UPLOAD_BYTES:
            limit_mb = self.MAX_UPLOAD_BYTES // (1024 * 1024)
            return jsonify({"error": f"文件超过 {limit_mb} MB 上限。"}), 400

        note_service = self.plugin_context.get_component("note_service")
        if note_service is None:
            return jsonify({"error": "笔记服务不可用"}), 503

        # 标题没变就一个字节都不动，避免「确认即格式化」。
        original_title = str(data.get("original_title") or "").strip()
        if not original_title:
            return jsonify({"error": "original_title 必填"}), 400
        body = content if title == original_title else rewrite_title(content, title)

        target = self._allocate_upload_path(title, extension)
        raw_root = self._get_raw_dir().resolve()
        relative_path = target.resolve().relative_to(raw_root).as_posix()

        try:
            await asyncio.to_thread(atomic_write_text, target, body)
        except Exception as exc:
            logger.error(f"[笔记上传] 写入失败 path={relative_path}: {exc}", exc_info=True)
            return jsonify({"error": f"写入失败：{exc}"}), 500

        try:
            chunks, _timings = await asyncio.to_thread(
                note_service.parse_and_store_file_sync, str(target), relative_path
            )
        except Exception as exc:
            logger.error(f"[笔记上传] 索引更新失败 path={relative_path}: {exc}", exc_info=True)
            return (
                jsonify(
                    {
                        "error": f"文件已写入 {relative_path}，但索引更新失败：{exc}",
                        "saved_path": relative_path,
                    }
                ),
                500,
            )

        note_short_id = -1
        conn = self._get_central_conn()
        if conn:
            try:
                row = conn.execute(
                    "SELECT note_short_id FROM note_index_records WHERE source_file_path = ? LIMIT 1",
                    (relative_path,),
                ).fetchone()
                if row and row["note_short_id"] is not None:
                    note_short_id = int(row["note_short_id"])
            finally:
                conn.close()

        logger.info(
            f"[笔记上传] 完成 path={relative_path} encoding_chars={len(body)} 切片={chunks}"
        )
        return jsonify(
            {
                "saved": {
                    "relative_path": relative_path,
                    "note_short_id": note_short_id,
                    "title": extract_h1(body) or title,
                    "chunks": int(chunks),
                }
            }
        )

    async def list_note_files(self):
        """列出笔记文件"""
        raw_dir = self._get_raw_dir()
        if not raw_dir.exists():
            return jsonify({"files": [], "error": f"目录不存在: {raw_dir}"})

        files = []
        for root, _, filenames in os.walk(str(raw_dir)):
            for f in filenames:
                if f.endswith((".md", ".txt")):
                    abs_path = os.path.join(root, f)
                    rel_path = os.path.relpath(abs_path, str(raw_dir)).replace("\\", "/")
                    files.append(rel_path)
        files.sort()
        return jsonify({"files": files})

    async def get_file_content(self):
        """获取笔记文件内容"""
        rel_path = request.args.get("path", "").strip()
        if not rel_path:
            return jsonify({"error": "缺少 path 参数"}), 400

        raw_dir = self._get_raw_dir()
        target = (raw_dir / rel_path.replace("/", os.sep)).resolve()
        raw_root = raw_dir.resolve()

        if raw_root not in target.parents and target != raw_root:
            return jsonify({"error": "路径越界"}), 400

        if not target.exists():
            return jsonify({"error": f"文件不存在: {rel_path}"}), 404

        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
            return jsonify({"content": content, "path": rel_path})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    async def search_chunks(self):
        """切片搜索 API"""
        query = request.args.get("query", "").strip()
        limit = min(100, max(1, int(request.args.get("limit", 20))))

        if not query:
            return jsonify({"error": "缺少 query 参数"}), 400

        search_engine = self.plugin_context.get_component("note_chunk_search")
        if search_engine is None:
            return jsonify({"error": "切片搜索引擎不可用"}), 503

        try:
            results = await search_engine.search_async(query=query, limit=limit)
            items = []
            for r in results:
                items.append(
                    {
                        "source_file_path": r.get("source_file_path", ""),
                        "file_id": r.get("file_id", ""),
                        "chunk_index": r.get("chunk_index", 0),
                        "line_start": r.get("line_start", 0),
                        "line_end": r.get("line_end", 0),
                        "content": str(r.get("content") or ""),
                        "score": float(r.get("score", 0.0)),
                    }
                )
            return jsonify({"items": items, "total": len(items), "query": query})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    async def chunk_stats(self):
        """切片索引统计"""
        search_engine = self.plugin_context.get_component("note_chunk_search")
        chunk_store = self.plugin_context.get_component("note_chunk_store")

        stats = {
            "search_engine_available": search_engine is not None,
            "chunk_store_available": chunk_store is not None,
            "chunk_count": 0,
            "file_count": 0,
        }

        if chunk_store is not None:
            try:
                store_stats = chunk_store.get_stats()
                stats["chunk_count"] = store_stats.get("total_chunks", 0)
                stats["file_count"] = store_stats.get("total_files", 0)
            except Exception:
                pass

        return jsonify(stats)
