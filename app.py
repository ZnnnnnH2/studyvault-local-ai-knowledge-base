from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("STUDYVAULT_DB", BASE_DIR / "studyvault.db"))
DEMO_USER_ID = 1
DATA_LOCK = threading.RLock()

SCHEMA = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
# Columns added after the first release. schema.sql declares them for new databases;
# init_db() appends them to older databases, so they must stay last in each table.
MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "import_job": [("result_code", "TEXT NOT NULL DEFAULT 'SUCCESS' CHECK(result_code IN ('SUCCESS','SKIPPED_EMPTY','SKIPPED_DUPLICATE','FAILED_PARSE'))")],
    "conversation_annotation": [
        ("source", "TEXT NOT NULL DEFAULT 'AUTO' CHECK(source IN ('AUTO','MANUAL'))"),
        ("review_status", "TEXT NOT NULL DEFAULT 'PENDING' CHECK(review_status IN ('PENDING','CONFIRMED','REVISED'))"),
    ],
    "conversation_point": [
        ("status", "TEXT NOT NULL DEFAULT 'PROPOSED' CHECK(status IN ('PROPOSED','CONFIRMED','REJECTED'))"),
        ("evidence", "TEXT"),
        ("confirmed_by", "INTEGER REFERENCES app_user(user_id) ON DELETE RESTRICT"),
        ("confirmed_at", "TEXT"),
    ],
}

SOURCE_ROOTS = {
    "Codex": Path(os.getenv("CODEX_DATA_ROOT", Path.home() / ".codex")),
    "Claude Code": Path(os.getenv("CLAUDE_DATA_ROOT", Path.home() / ".claude")),
    "Pi": Path(os.getenv("PI_DATA_ROOT", Path.home() / ".pi" / "agent" / "sessions")),
    "DeepSeek": Path(os.getenv("DEEPSEEK_DATA_ROOT", Path.home() / ".dsh" / "sessions")),
    "Gemini CLI": Path(os.getenv("GEMINI_DATA_ROOT", Path.home() / ".gemini" / "tmp")),
}

COURSE_RULES = [
    ("数据库系统概论", ["数据库", "sql", "postgres", "sqlite", "关系模型", "事务", "外键", "索引"]),
    ("程序设计与工程实践", ["python", "typescript", "javascript", "api", "测试", "git", "代码", "编程"]),
    ("机器学习与数据分析", ["机器学习", "深度学习", "pytorch", "模型", "训练", "分类", "数据分析"]),
]
POINT_RULES = [
    ("数据库完整性", ["完整性", "外键", "参照完整性", "约束"]),
    ("SQL 与查询", ["sql", "select", "join", "查询"]),
    ("事务与并发", ["事务", "锁", "并发", "回滚"]),
    ("软件工程与测试", ["测试", "bug", "接口", "部署", "代码"]),
]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with DATA_LOCK:
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        legacy_fts = db.execute("SELECT sql FROM sqlite_master WHERE name='message_fts'").fetchone()
        if legacy_fts and "content='message'" not in legacy_fts[0]:
            # Earlier builds created a standalone FTS table whose delete triggers could not work.
            for trigger in ("message_fts_ai", "message_fts_ad", "message_fts_au"):
                db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            db.execute("DROP TABLE message_fts")
        # review_event was an unreleased duplicate of review_log; drop it when it never received rows.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_event'").fetchone() and not db.execute("SELECT 1 FROM review_event LIMIT 1").fetchone():
            db.execute("DROP TABLE review_event")
        added: set[tuple[str, str]] = set()
        for table, columns in MIGRATION_COLUMNS.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # fresh database: schema.sql creates the full table below
            for name, definition in columns:
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    added.add((table, name))
        db.executescript(SCHEMA)
        # One-time backfill when the columns first appear; later imports keep their PROPOSED/AUTO state.
        if ("conversation_point", "status") in added:
            db.execute("UPDATE conversation_point SET status='CONFIRMED' WHERE status='PROPOSED' AND source='rule'")
        if ("conversation_annotation", "source") in added:
            db.execute("UPDATE conversation_annotation SET source='MANUAL', review_status='CONFIRMED' WHERE source='AUTO' AND confidence IS NOT NULL")
        if db.execute("SELECT COUNT(*) FROM message_fts_docsize").fetchone()[0] != db.execute("SELECT COUNT(*) FROM message").fetchone()[0]:
            db.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
        db.executemany("INSERT OR IGNORE INTO app_role(role_id, role_code, role_name) VALUES(?,?,?)", [(1, "STUDENT", "学生"), (2, "TEACHER", "教师"), (3, "ADMIN", "管理员")])
        db.execute("INSERT OR IGNORE INTO app_user(user_id, role_id, username, display_name) VALUES(1,1,'demo','演示学生')")
        db.executemany("INSERT OR IGNORE INTO conversation_source(platform_name, source_type) VALUES(?,?)", [("Codex", "local_jsonl"), ("Claude Code", "local_jsonl"), ("Pi", "local_jsonl"), ("DeepSeek", "local_zstd"), ("Gemini CLI", "local_json")])
        legacy_deepseek = db.execute("SELECT source_id FROM conversation_source WHERE platform_name='DeepSeek' AND source_type='uploaded_file'").fetchone()
        if legacy_deepseek:
            db.execute("DELETE FROM conversation_source WHERE platform_name='DeepSeek' AND source_type='local_zstd' AND source_id NOT IN (SELECT DISTINCT source_id FROM conversation)")
            db.execute("UPDATE conversation_source SET source_type='local_zstd' WHERE source_id=?", (legacy_deepseek[0],))
        for code, name in [("DB", "数据库系统概论"), ("ENG", "程序设计与工程实践"), ("ML", "机器学习与数据分析"), ("GEN", "通用学习")]:
            db.execute("INSERT OR IGNORE INTO course(course_code, course_name) VALUES(?,?)", (code, name))
        courses = {r["course_name"]: r["course_id"] for r in db.execute("SELECT course_id,course_name FROM course")}
        for course_name, terms in [("数据库系统概论", ["数据库完整性", "SQL 与查询", "事务与并发"]), ("程序设计与工程实践", ["软件工程与测试"])] :
            for point in terms:
                db.execute("INSERT OR IGNORE INTO knowledge_point(course_id, point_name, level_no) VALUES(?,?,1)", (courses[course_name], point))


def text_from(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                pieces.append(str(item.get("text", "")))
        return "\n".join(p for p in pieces if p).strip()
    if isinstance(value, dict):
        return text_from(value.get("text") or value.get("content") or "")
    return ""


SENSITIVE_VALUE = re.compile(r"(?i)(password|passwd|密码|api[\s_-]*key|[a-z0-9_]+_key|token|authorization|账号|secret|credential)\s*(?:[:=：]|是|为)\s*(?:bearer\s+)?([^\s,，;；\n\"']+)")
BARE_SECRET = re.compile(r"(?i)\b(?:sk|tp)-[a-z0-9_-]{12,}\b")
INTERNAL_BLOCK = re.compile(r"(?is)<(?:recommended_plugins|environment_context|codex_internal_context|app-context|permissions instructions|send_user_message_question_reply|external_codex_apps_open_page|in-app-browser-context)(?:\s[^>]*)?>.*?</(?:recommended_plugins|environment_context|codex_internal_context|app-context|permissions instructions|send_user_message_question_reply|external_codex_apps_open_page|in-app-browser-context)>")
RUNTIME_NOISE = re.compile(r"(?i)^(?:current runtime context\.|current working directory:|the approval policy changed from|a new version of)")


def redact_sensitive(text: str) -> str:
    redacted = SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=<已脱敏>", text)
    return BARE_SECRET.sub("<已脱敏>", redacted)


def clean_import_text(text: str) -> str:
    return redact_sensitive(INTERNAL_BLOCK.sub("", text)).strip()


def iter_records(path: Path):
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item
            return
        if isinstance(payload, dict) and isinstance(payload.get("conversations"), list):
            for item in payload["conversations"]:
                if isinstance(item, dict):
                    yield item
            return
        if isinstance(payload, dict):
            yield payload
            return
        return
    if path.suffix in {".zst", ".zstd"}:
        executable = shutil.which("zstd") or str(Path.home() / "scoop" / "apps" / "miniconda3" / "current" / "Library" / "bin" / "zstd.exe")
        if not Path(executable).exists() and not shutil.which("zstd"):
            raise RuntimeError(f"读取压缩会话需要 zstd: {path}")
        process = subprocess.Popen([executable, "-dc", str(path)], stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for line in process.stdout:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError(f"解压会话失败: {path}")
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    return str(value)


def parse_conversation(path: Path, platform: str) -> dict[str, Any]:
    messages = []
    source_id = path.stem
    started = None
    title = path.stem
    for record in iter_records(path):
        timestamp = timestamp_text(record.get("timestamp") or record.get("time") or record.get("createdAt"))
        started = started or timestamp
        if platform == "Codex":
            if record.get("type") == "session_meta":
                source_id = record.get("payload", {}).get("session_id") or record.get("payload", {}).get("id") or source_id
            payload = record.get("payload", {})
            if record.get("type") != "response_item" or payload.get("role") not in {"user", "assistant"}:
                continue
            content = clean_import_text(text_from(payload.get("content")))
            speaker = payload.get("role")
        elif platform == "Claude Code":
            if record.get("type") not in {"user", "assistant"}:
                continue
            source_id = record.get("sessionId", source_id)
            message = record.get("message", record)
            content = clean_import_text(text_from(message.get("content")))
            speaker = message.get("role", record.get("type"))
        elif platform == "Pi":
            if record.get("type") == "session":
                source_id = record.get("id", source_id)
            if record.get("type") != "message":
                continue
            source_id = record.get("sessionId", source_id)
            message = record.get("message", {})
            content = clean_import_text(text_from(message.get("content")))
            speaker = message.get("role")
        elif platform == "Gemini CLI":
            if not isinstance(record.get("messages"), list):
                continue
            source_id = record.get("sessionId", source_id)
            for item in record["messages"]:
                speaker = {"user": "user", "gemini": "assistant"}.get(item.get("type"))
                content = clean_import_text(text_from(item.get("content")))
                if speaker and content:
                    messages.append({"speaker": speaker, "content": content, "created_at": timestamp_text(item.get("timestamp"))})
                    if title == path.stem and speaker == "user":
                        title = re.sub(r"\s+", " ", content).split("\n", 1)[0][:120]
            break
        elif platform == "DeepSeek":
            if record.get("type") == "session":
                source_id = record.get("id", source_id)
                continue
            if record.get("type") == "session/title":
                title = record.get("data", {}).get("title") or title
                continue
            if record.get("type") not in {"user/message", "assistant/message"}:
                continue
            data = record.get("data", {})
            message = data.get("message", {}) if isinstance(data.get("message"), dict) else {}
            speaker = message.get("role") or ("user" if record.get("type") == "user/message" else "assistant")
            content = clean_import_text(text_from(data.get("content") or message.get("content")))
        else:
            source_id = record.get("session_id") or record.get("sessionId") or record.get("id") or source_id
            message = record.get("message", record)
            content = clean_import_text(text_from(message.get("content") if isinstance(message, dict) else message))
            speaker = message.get("role") if isinstance(message, dict) else record.get("role")
        if speaker in {"user", "assistant"} and content and not RUNTIME_NOISE.search(content):
            messages.append({"speaker": speaker, "content": content, "created_at": timestamp})
            if title == path.stem and speaker == "user":
                title = re.sub(r"\s+", " ", content).split("\n", 1)[0][:120]
    if not messages:
        raise ValueError(f"{platform} 文件没有可导入的用户/助手消息: {path}")
    return {"source_id": source_id, "title": redact_sensitive(title) or "未命名会话", "started_at": started, "messages": messages, "path": str(path)}


def guess_course(text: str) -> str:
    lowered = text.lower()
    for course, terms in COURSE_RULES:
        if any(term.lower() in lowered for term in terms):
            return course
    return "通用学习"


def ensure_tag(db: sqlite3.Connection, name: str) -> int:
    db.execute("INSERT OR IGNORE INTO tag(owner_id, tag_name) VALUES(?,?)", (DEMO_USER_ID, name))
    return int(db.execute("SELECT tag_id FROM tag WHERE owner_id=? AND tag_name=?", (DEMO_USER_ID, name)).fetchone()[0])


def source_row(db: sqlite3.Connection, platform: str) -> sqlite3.Row:
    return db.execute("SELECT * FROM conversation_source WHERE platform_name=?", (platform,)).fetchone()


def import_file(path: Path, platform: str) -> dict[str, Any]:
    parsed = parse_conversation(path, platform)
    return import_parsed(parsed, platform)


def import_parsed(parsed: dict[str, Any], platform: str) -> dict[str, Any]:
    path = Path(parsed["path"])
    with connect() as db:
        source = source_row(db, platform)
        duplicate = db.execute("SELECT conversation_id FROM conversation WHERE owner_id=? AND source_id=? AND source_conversation_id=?", (DEMO_USER_ID, source["source_id"], parsed["source_id"])).fetchone()
        if duplicate:
            return {"status": "SKIPPED", "result_code": "SKIPPED_DUPLICATE", "path": str(path), "conversation_id": duplicate["conversation_id"], "messages": 0}
        course_name = guess_course(parsed["title"] + " " + " ".join(m["content"] for m in parsed["messages"][:10]))
        course = db.execute("SELECT course_id FROM course WHERE course_name=?", (course_name,)).fetchone()
        job = db.execute("INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status,result_code) VALUES(?,?,?,?,?,?)", (DEMO_USER_ID, source["source_id"], str(path), parsed["source_id"], "SUCCESS", "SUCCESS"))
        conversation = db.execute("INSERT INTO conversation(owner_id,course_id,source_id,import_job_id,source_conversation_id,title,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (DEMO_USER_ID, course[0], source["source_id"], job.lastrowid, parsed["source_id"], parsed["title"], parsed["started_at"], now()))
        conversation_id = conversation.lastrowid
        for seq, message in enumerate(parsed["messages"], 1):
            db.execute("INSERT INTO message(conversation_id,seq_no,speaker,content,created_at) VALUES(?,?,?,?,?)", (conversation_id, seq, message["speaker"], message["content"], message["created_at"]))
        whole = (parsed["title"] + " " + " ".join(m["content"] for m in parsed["messages"])).lower()
        for tag_name in re.findall(r"#[\w\u4e00-\u9fff-]+", whole):
            tag_id = ensure_tag(db, tag_name[1:][:50])
            db.execute("INSERT OR IGNORE INTO conversation_tag VALUES(?,?)", (conversation_id, tag_id))
        for point_name, terms in POINT_RULES:
            matched_terms = [term for term in terms if term.lower() in whole]
            if matched_terms:
                point = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?", (course[0], point_name)).fetchone()
                if point:
                    db.execute("INSERT OR IGNORE INTO conversation_point(conversation_id,point_id,source,evidence,status) VALUES(?,?,?,?,?)", (conversation_id, point[0], "rule", json.dumps(matched_terms, ensure_ascii=False), "PROPOSED"))
        db.execute("UPDATE import_job SET total_messages=? WHERE import_job_id=?", (len(parsed["messages"]), job.lastrowid))
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,after_data) VALUES(?,?,?,?,?)", (DEMO_USER_ID, "IMPORT", "conversation", conversation_id, json.dumps({"source": platform, "path": str(path)}, ensure_ascii=False)))
        return {"status": "SUCCESS", "path": str(path), "conversation_id": conversation_id, "messages": len(parsed["messages"])}


def source_files() -> list[tuple[Path, str]]:
    files = {}
    for platform, root in SOURCE_ROOTS.items():
        if not root.exists():
            continue
        patterns = ["*.jsonl", "*.jsonl.zst", "*.jsonl.zstd"]
        if platform in {"DeepSeek", "Gemini CLI"}:
            patterns.append("*.json")
        for pattern in patterns:
            for path in root.rglob(pattern):
                if platform == "Codex" and not any(part in {"sessions", "archived_sessions"} for part in path.parts):
                    continue
                if platform == "Claude Code" and ("projects" not in path.parts and "transcripts" not in path.parts):
                    continue
                if platform == "Gemini CLI" and "chats" not in path.parts:
                    continue
                files[(str(path).lower(), platform)] = path
    return sorted(((path, platform) for (_, platform), path in files.items()), key=lambda item: item[0].stat().st_mtime)


def scan_and_import() -> list[dict[str, Any]]:
    def record_failure(path: Path, platform: str, result_code: str, message: str) -> None:
        with connect() as db:
            source = source_row(db, platform)
            existing = db.execute("SELECT import_job_id FROM import_job WHERE owner_id=? AND source_id=? AND file_name=? AND source_conversation_id=?", (DEMO_USER_ID, source["source_id"], str(path), path.stem)).fetchone()
            if existing:
                db.execute("UPDATE import_job SET status='FAILED',result_code=?,error_message=?,created_at=CURRENT_TIMESTAMP WHERE import_job_id=?", (result_code, message, existing[0]))
            else:
                db.execute("INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status,result_code,error_message) VALUES(?,?,?,?,?,?,?)", (DEMO_USER_ID, source["source_id"], str(path), path.stem, "FAILED", result_code, message))
    results = []
    for path, platform in source_files():
        try:
            results.append(import_file(path, platform))
        except ValueError as exc:
            record_failure(path, platform, "SKIPPED_EMPTY", str(exc))
            results.append({"status": "SKIPPED", "path": str(path), "messages": 0, "result_code": "SKIPPED_EMPTY", "reason": str(exc)})
        except RuntimeError as exc:
            record_failure(path, platform, "FAILED_PARSE", str(exc))
            results.append({"status": "FAILED", "path": str(path), "messages": 0, "result_code": "FAILED_PARSE", "reason": str(exc)})
    return results


def read_bundle(manifest_path: Path, annotations_path: Path) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """Validate both immutable snapshots before opening or changing the database."""
    def records(path: Path) -> list[dict[str, Any]]:
        result = []
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name} 第 {line_no} 行 JSON 无效") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"{path.name} 第 {line_no} 行必须是对象")
                result.append(row)
        if not result:
            raise ValueError(f"{path.name} 不能为空")
        return result

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    def string_list(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value) and len(set(value)) == len(value)

    def key(row: dict[str, Any]) -> tuple[str, str]:
        platform, source_id = row.get("platform"), row.get("source_id")
        require(isinstance(platform, str) and platform in SOURCE_ROOTS, "快照包含不支持的来源平台")
        require(isinstance(source_id, str) and bool(source_id.strip()), "快照的 source_id 必须是非空字符串")
        return platform, source_id

    conversations = records(manifest_path)
    seen: set[tuple[str, str]] = set()
    for row in conversations:
        identity = key(row)
        require(identity not in seen, "会话快照包含重复的来源平台与会话 ID")
        seen.add(identity)
        for field in ("title", "summary"):
            require(isinstance(row.get(field), str) and bool(row[field].strip()), f"会话的 {field} 必须是非空字符串")
        require(row.get("started_at") is None or isinstance(row["started_at"], str), "started_at 必须是字符串或 null")
        messages = row.get("messages")
        require(isinstance(messages, list) and bool(messages), "会话必须包含消息列表")
        for message in messages:
            require(isinstance(message, dict), "消息必须是对象")
            require(isinstance(message.get("speaker"), str) and message["speaker"] in {"user", "assistant", "system"}, "消息发送者无效")
            require(isinstance(message.get("content"), str) and bool(message["content"].strip()), "消息正文不能为空")
            require(message.get("created_at") is None or isinstance(message["created_at"], str), "消息时间必须是字符串或 null")
        expected = {"message_count": len(messages), "user_message_count": sum(m["speaker"] == "user" for m in messages), "assistant_message_count": sum(m["speaker"] == "assistant" for m in messages)}
        for field, count in expected.items():
            require(type(row.get(field)) is int and row[field] == count, f"会话的 {field} 与消息列表不一致")
        for field in ("tags", "knowledge_points"):
            require(string_list(row.get(field)), f"{field} 必须是不含重复项的非空字符串列表")

    annotations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records(annotations_path):
        identity = key(row)
        require(identity not in annotations, "标注快照包含重复的来源平台与会话 ID")
        for field in ("summary_manual", "intent"):
            require(isinstance(row.get(field), str) and bool(row[field].strip()), f"标注的 {field} 不能为空")
        for field in ("topics", "key_points", "unresolved_questions", "action_items"):
            value = row.get(field)
            require(isinstance(value, list) and all(isinstance(x, str) for x in value), f"标注的 {field} 必须是字符串列表")
        require(isinstance(row.get("quality"), str) and row["quality"] in {"有效", "混合", "低价值"}, "标注 quality 无效")
        require(isinstance(row.get("confidence"), str) and row["confidence"] in {"high", "medium", "low"}, "标注 confidence 无效")
        annotations[identity] = row
    require(seen == set(annotations), "会话与标注必须按来源平台、会话 ID 一一对应")
    return conversations, annotations


def latest_curation(db: sqlite3.Connection, conversation_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT a.created_at,a.after_data,u.display_name actor_name FROM audit_log a JOIN app_user u ON u.user_id=a.actor_id WHERE a.table_name='conversation' AND a.record_id=? AND a.operation='CURATE' ORDER BY a.audit_id DESC LIMIT 1", (conversation_id,)).fetchone()


def restore_bundled(manifest_path: Path | None = None, annotations_path: Path | None = None) -> dict[str, Any]:
    """Restore missing data atomically, preserving existing user work and message IDs."""
    manifest_path = manifest_path or BASE_DIR / "staging" / "structured_manifest_final.jsonl"
    annotations_path = annotations_path or BASE_DIR / "staging" / "manual_annotations_all.jsonl"
    conversations, annotations = read_bundle(manifest_path, annotations_path)
    init_db()
    result = {"inserted": 0, "skipped": 0, "messages_added": 0, "summaries_added": 0, "annotations_added": 0, "tag_links_added": 0, "point_links_added": 0}
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in conversations:
            curated_fields: set[str] = set()
            source = source_row(db, row["platform"])
            existing = db.execute("SELECT * FROM conversation WHERE owner_id=? AND source_id=? AND source_conversation_id=?", (DEMO_USER_ID, source["source_id"], row["source_id"])).fetchone()
            if existing:
                conversation_id, course_id = existing["conversation_id"], existing["course_id"]
                curation = latest_curation(db, conversation_id)
                if curation:
                    curated_fields = set(json.loads(curation["after_data"])["curated_fields"])
                stored = [(m["seq_no"], m["speaker"], m["content"], m["created_at"]) for m in db.execute("SELECT seq_no,speaker,content,created_at FROM message WHERE conversation_id=? ORDER BY seq_no", (conversation_id,))]
                expected = [(i, m["speaker"], m["content"], m.get("created_at")) for i, m in enumerate(row["messages"], 1)]
                if stored != expected:
                    raise ValueError(f"恢复冲突：{row['platform']} 的已有会话 {row['source_id']} 消息内容、顺序或时间不同，整批恢复已回滚")
                if not existing["summary"]:
                    db.execute("UPDATE conversation SET summary=? WHERE conversation_id=?", (row["summary"], conversation_id))
                    result["summaries_added"] += 1
                result["skipped"] += 1
            else:
                course_name = guess_course(row["title"] + " " + " ".join(m["content"] for m in row["messages"][:10]))
                course_id = db.execute("SELECT course_id FROM course WHERE course_name=?", (course_name,)).fetchone()[0]
                job_id = db.execute("INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status,total_messages) VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,source_id,file_name,source_conversation_id) DO UPDATE SET status='SUCCESS',total_messages=excluded.total_messages RETURNING import_job_id", (DEMO_USER_ID, source["source_id"], str(manifest_path.resolve()), row["source_id"], "SUCCESS", len(row["messages"]))).fetchone()[0]
                updated = next((m.get("created_at") for m in reversed(row["messages"]) if m.get("created_at")), None) or row.get("started_at") or now()
                cursor = db.execute("INSERT INTO conversation(owner_id,course_id,source_id,import_job_id,source_conversation_id,title,summary,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (DEMO_USER_ID, course_id, source["source_id"], job_id, row["source_id"], row["title"], row["summary"], row.get("started_at"), updated))
                conversation_id = cursor.lastrowid
                db.executemany("INSERT INTO message(conversation_id,seq_no,speaker,content,created_at) VALUES(?,?,?,?,?)", [(conversation_id, seq, m["speaker"], m["content"], m.get("created_at")) for seq, m in enumerate(row["messages"], 1)])
                result["inserted"] += 1
                result["messages_added"] += len(row["messages"])
                result["summaries_added"] += 1
            for name in ([] if "tags" in curated_fields else row["tags"]):
                tag_id = ensure_tag(db, name)
                result["tag_links_added"] += db.execute("INSERT OR IGNORE INTO conversation_tag(conversation_id,tag_id) VALUES(?,?)", (conversation_id, tag_id)).rowcount
            for name in row["knowledge_points"]:
                db.execute("INSERT OR IGNORE INTO knowledge_point(course_id,point_name) VALUES(?,?)", (course_id, name))
                point_id = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?", (course_id, name)).fetchone()[0]
                result["point_links_added"] += db.execute("INSERT OR IGNORE INTO conversation_point(conversation_id,point_id,source) VALUES(?,?,?)", (conversation_id, point_id, "bundled")).rowcount
            note = annotations[(row["platform"], row["source_id"])]
            result["annotations_added"] += db.execute("INSERT OR IGNORE INTO conversation_annotation(conversation_id,summary_manual,intent,topics_json,key_points_json,unresolved_questions_json,action_items_json,quality,confidence,source,review_status) VALUES(?,?,?,?,?,?,?,?,?,'MANUAL','CONFIRMED')", (conversation_id, note["summary_manual"], note["intent"], *(json.dumps(note[field], ensure_ascii=False) for field in ("topics", "key_points", "unresolved_questions", "action_items")), note["quality"], note["confidence"])).rowcount
        result["integrity_check"] = db.execute("PRAGMA integrity_check").fetchone()[0]
        result["foreign_key_violations"] = len(db.execute("PRAGMA foreign_key_check").fetchall())
        if result["integrity_check"] != "ok" or result["foreign_key_violations"]:
            raise ValueError("恢复后的数据库完整性检查失败，整批恢复已回滚")
        result["counts"] = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("conversation", "message", "conversation_annotation", "conversation_tag", "conversation_point")}
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,after_data) VALUES(?,?,?,?)", (DEMO_USER_ID, "RESTORE_BUNDLED", "conversation", json.dumps(result, ensure_ascii=False)))
    return result


class AnnotationInput(BaseModel):
    message_id: int
    annotation_type: str
    content: str


class ReviewInput(BaseModel):
    message_id: int
    question: str
    answer: str


TODO_TYPES = {"QUESTION", "CORRECTION", "IMPORTANT"}


class AnnotationUpdate(BaseModel):
    content: str | None = None
    is_resolved: bool | None = None


class PointDecision(BaseModel):
    status: str


class PointAssignment(BaseModel):
    point_id: int
    evidence: str = ""


class TagInput(BaseModel):
    tag_name: str


class ClassificationReview(BaseModel):
    review_status: str


class CurationInput(BaseModel):
    course_id: int | None = None
    title: str | None = None
    summary: str | None = None
    tags: list[str] | None = None


app = FastAPI(title="StudyVault", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    with connect() as db:
        counts = {key: db.execute(query, (DEMO_USER_ID,)).fetchone()[0] for key, query in {"conversations": "SELECT COUNT(*) FROM conversation WHERE owner_id=?", "messages": "SELECT COUNT(*) FROM message m JOIN conversation c ON c.conversation_id=m.conversation_id WHERE c.owner_id=?", "annotations": "SELECT COUNT(*) FROM annotation WHERE author_id=? AND is_resolved=0", "due": "SELECT COUNT(*) FROM review_card WHERE owner_id=? AND status='ACTIVE' AND due_at<=CURRENT_TIMESTAMP"}.items()}
        recent = [dict(row) for row in db.execute("SELECT c.conversation_id,c.title,c.updated_at,s.platform_name,co.course_name,COUNT(m.message_id) message_count FROM conversation c JOIN conversation_source s ON s.source_id=c.source_id JOIN course co ON co.course_id=c.course_id LEFT JOIN message m ON m.conversation_id=c.conversation_id WHERE c.owner_id=? GROUP BY c.conversation_id ORDER BY c.updated_at DESC LIMIT 8", (DEMO_USER_ID,))]
        return {"counts": counts, "recent": recent}


@app.get("/api/courses")
def courses() -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT course_id,course_code,course_name FROM course ORDER BY course_name")]


@app.get("/api/knowledge-points")
def knowledge_points(course_id: int | None = None) -> list[dict[str, Any]]:
    with connect() as db:
        if course_id is None:
            rows = db.execute("SELECT point_id,course_id,point_name FROM knowledge_point ORDER BY point_name")
        else:
            rows = db.execute("SELECT point_id,course_id,point_name FROM knowledge_point WHERE course_id=? ORDER BY point_name", (course_id,))
        return [dict(row) for row in rows]


@app.get("/api/sources")
def sources() -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT source_id,platform_name FROM conversation_source ORDER BY platform_name")]


def fts_match(value: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", value, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def learning_filters(where: list[str], args: list[Any], point_id: int | None = None, tag_id: int | None = None, annotation_status: str | None = None, review_status: str | None = None, classification_status: str | None = None) -> None:
    """Append knowledge-point, tag, annotation, review and classification conditions shared by list and search endpoints."""
    if point_id is not None:
        where.append("EXISTS (SELECT 1 FROM conversation_point cp WHERE cp.conversation_id=c.conversation_id AND cp.point_id=?)"); args.append(point_id)
    if tag_id is not None:
        where.append("EXISTS (SELECT 1 FROM conversation_tag ct WHERE ct.conversation_id=c.conversation_id AND ct.tag_id=?)"); args.append(tag_id)
    if annotation_status:
        where.append("EXISTS (SELECT 1 FROM annotation a JOIN message am ON am.message_id=a.message_id WHERE am.conversation_id=c.conversation_id AND a.is_resolved=?)"); args.append(0 if annotation_status == "open" else 1)
    if review_status == "due":
        where.append("EXISTS (SELECT 1 FROM review_card r JOIN message rm ON rm.message_id=r.message_id WHERE rm.conversation_id=c.conversation_id AND r.owner_id=? AND r.status='ACTIVE' AND r.due_at<=CURRENT_TIMESTAMP)"); args.append(DEMO_USER_ID)
    elif review_status == "active":
        where.append("EXISTS (SELECT 1 FROM review_card r JOIN message rm ON rm.message_id=r.message_id WHERE rm.conversation_id=c.conversation_id AND r.owner_id=? AND r.status='ACTIVE')"); args.append(DEMO_USER_ID)
    elif review_status == "none":
        where.append("NOT EXISTS (SELECT 1 FROM review_card r JOIN message rm ON rm.message_id=r.message_id WHERE rm.conversation_id=c.conversation_id AND r.owner_id=?)"); args.append(DEMO_USER_ID)
    if classification_status:
        where.append("EXISTS (SELECT 1 FROM conversation_point cp WHERE cp.conversation_id=c.conversation_id AND cp.status=?)"); args.append(classification_status.upper())


@app.get("/api/search")
def search(
    q: str = "", course_id: int | None = None, point_id: int | None = None,
    tag_id: int | None = None, source_id: int | None = None,
    annotation_status: str | None = Query(None, pattern="^(open|resolved)$"),
    review_status: str | None = Query(None, pattern="^(due|active|none)$"),
    classification_status: str | None = Query(None, pattern="^(proposed|confirmed|rejected)$"),
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where = ["c.owner_id=?"]
    args: list[Any] = [DEMO_USER_ID]
    if course_id is not None:
        where.append("c.course_id=?"); args.append(course_id)
    if source_id is not None:
        where.append("c.source_id=?"); args.append(source_id)
    learning_filters(where, args, point_id, tag_id, annotation_status, review_status, classification_status)
    match = fts_match(q) if q else ""
    if match:
        where.append("(c.title LIKE ? OR EXISTS (SELECT 1 FROM message sm WHERE sm.conversation_id=c.conversation_id AND sm.content LIKE ?) OR EXISTS (SELECT 1 FROM message_fts WHERE message_fts.conversation_id=c.conversation_id AND message_fts MATCH ?))")
        args.extend([f"%{q}%", f"%{q}%", match])
    condition = " AND ".join(where)
    with connect() as db:
        rows = [dict(row) for row in db.execute(f"""
            SELECT c.conversation_id,c.title,c.updated_at,c.started_at,s.platform_name,co.course_name,
                   COUNT(DISTINCT m.message_id) message_count,
                   (SELECT COUNT(*) FROM annotation a JOIN message am ON am.message_id=a.message_id WHERE am.conversation_id=c.conversation_id AND a.is_resolved=0) unresolved_count,
                   (SELECT COUNT(*) FROM review_card r JOIN message rm ON rm.message_id=r.message_id WHERE rm.conversation_id=c.conversation_id AND r.owner_id=? AND r.status='ACTIVE' AND r.due_at<=CURRENT_TIMESTAMP) due_review_count
            FROM conversation c JOIN conversation_source s ON s.source_id=c.source_id JOIN course co ON co.course_id=c.course_id
            LEFT JOIN message m ON m.conversation_id=c.conversation_id
            WHERE {condition} GROUP BY c.conversation_id ORDER BY c.updated_at DESC LIMIT ? OFFSET ?
        """, [DEMO_USER_ID, *args, limit, offset])]
        for row in rows:
            matched = []
            if q:
                matched.append("消息内容或标题")
            if point_id:
                matched.append("知识点")
            if tag_id:
                matched.append("标签")
            if annotation_status:
                matched.append("批注状态")
            if review_status:
                matched.append("复习状态")
            if classification_status:
                matched.append("分类确认状态")
            row["matched_by"] = matched
        total = db.execute(f"SELECT COUNT(*) FROM conversation c WHERE {condition}", args).fetchone()[0]
        return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/conversations")
def conversations(
    q: str = "", course_id: int | None = None, source_id: int | None = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), favorites_only: bool = False,
    point_id: int | None = None, tag_id: int | None = None,
    annotation_status: str | None = Query(None, pattern="^(open|resolved)$"),
    review_status: str | None = Query(None, pattern="^(due|active|none)$"),
) -> dict[str, Any]:
    args: list[Any] = [DEMO_USER_ID]
    where = ["c.owner_id=?"]
    if favorites_only:
        where.append("EXISTS (SELECT 1 FROM conversation_state cs WHERE cs.conversation_id=c.conversation_id AND cs.owner_id=c.owner_id AND cs.is_favorite=1)")
    if q:
        pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where.append("(c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM message sm WHERE sm.conversation_id=c.conversation_id AND sm.content LIKE ? ESCAPE '\\'))")
        args.extend([pattern, pattern])
    if course_id:
        where.append("c.course_id=?"); args.append(course_id)
    if source_id:
        where.append("c.source_id=?"); args.append(source_id)
    learning_filters(where, args, point_id, tag_id, annotation_status, review_status)
    condition = " AND ".join(where)
    with connect() as db:
        rows = [dict(row) for row in db.execute(f"SELECT c.conversation_id,c.title,c.updated_at,c.started_at,s.platform_name,co.course_name,COUNT(m.message_id) message_count,(SELECT COUNT(*) FROM annotation a JOIN message am ON am.message_id=a.message_id WHERE am.conversation_id=c.conversation_id AND a.is_resolved=0) unresolved_count,COALESCE(cs.is_favorite,0) is_favorite,cs.last_opened_at,(SELECT seq_no FROM message WHERE message_id=cs.last_message_id) last_read_seq FROM conversation c JOIN conversation_source s ON s.source_id=c.source_id JOIN course co ON co.course_id=c.course_id LEFT JOIN message m ON m.conversation_id=c.conversation_id LEFT JOIN conversation_state cs ON cs.conversation_id=c.conversation_id AND cs.owner_id=c.owner_id WHERE {condition} GROUP BY c.conversation_id ORDER BY c.updated_at DESC,c.conversation_id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
        total = db.execute(f"SELECT COUNT(*) FROM conversation c WHERE {condition}", args).fetchone()[0]
        for row in rows:
            row.update(matched_message_id=None, match_seq_no=None, match_snippet=None)
            if q:
                match = db.execute("SELECT message_id,seq_no,content FROM message WHERE conversation_id=? AND content LIKE ? ESCAPE '\\' ORDER BY seq_no LIMIT 1", (row["conversation_id"], pattern)).fetchone()
                if match:
                    text = match["content"]
                    position = max(0, text.lower().find(q.lower()))
                    start, end = max(0, position - 65), min(len(text), position + min(len(q), 200) + 120)
                    row.update(matched_message_id=match["message_id"], match_seq_no=match["seq_no"], match_snippet=('…' if start else '') + text[start:end] + ('…' if end < len(text) else ''))
        return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/api/conversations/{conversation_id}")
def conversation_detail(conversation_id: int) -> dict[str, Any]:
    with connect() as db:
        conversation = db.execute("SELECT c.*,s.platform_name,co.course_name FROM conversation c JOIN conversation_source s ON s.source_id=c.source_id JOIN course co ON co.course_id=c.course_id WHERE c.conversation_id=? AND c.owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not conversation:
            raise HTTPException(404, "会话不存在或无权访问")
        messages = []
        for row in db.execute("SELECT * FROM message WHERE conversation_id=? ORDER BY seq_no", (conversation_id,)):
            item = dict(row)
            item["annotations"] = [dict(a) for a in db.execute("SELECT annotation_id,annotation_type,content,is_resolved,created_at FROM annotation WHERE message_id=? ORDER BY created_at", (row["message_id"],))]
            item["review_card"] = next((dict(r) for r in db.execute("SELECT review_card_id,question,answer,due_at,interval_days,review_count,status FROM review_card WHERE message_id=? AND owner_id=?", (row["message_id"], DEMO_USER_ID))), None)
            messages.append(item)
        points = [dict(p) for p in db.execute("SELECT kp.point_id,kp.point_name,cp.status,cp.source,cp.evidence,cp.confirmed_at FROM knowledge_point kp JOIN conversation_point cp ON cp.point_id=kp.point_id WHERE cp.conversation_id=?", (conversation_id,))]
        tags = [r[0] for r in db.execute("SELECT t.tag_name FROM tag t JOIN conversation_tag ct ON ct.tag_id=t.tag_id WHERE ct.conversation_id=? ORDER BY t.tag_name", (conversation_id,))]
        review = db.execute("SELECT summary_manual,intent,topics_json,key_points_json,unresolved_questions_json,action_items_json,quality,confidence FROM conversation_annotation WHERE conversation_id=?", (conversation_id,)).fetchone()
        curation = latest_curation(db, conversation_id)
        reading = db.execute("SELECT is_favorite,last_message_id,last_opened_at FROM conversation_state WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        return {"conversation": dict(conversation), "messages": messages, "points": points, "tags": tags, "manual_annotation": dict(review) if review else None, "reading_state": dict(reading) if reading else None, "curation": {"created_at": curation["created_at"], "actor_name": curation["actor_name"], "fields": json.loads(curation["after_data"])["curated_fields"]} if curation else None}


@app.patch("/api/conversations/{conversation_id}")
def curate_conversation(conversation_id: int, payload: CurationInput) -> dict[str, Any]:
    fields = {name for name in ("course_id", "title", "summary", "tags") if getattr(payload, name) is not None}
    if not fields:
        raise HTTPException(400, "请提供要修改的整理信息")
    if payload.title is not None and not payload.title.strip():
        raise HTTPException(400, "标题不能为空")
    if payload.summary is not None and not payload.summary.strip():
        raise HTTPException(400, "摘要不能为空")
    tags = list(dict.fromkeys(name.strip() for name in (payload.tags or [])))
    if payload.tags is not None and any(not name for name in tags):
        raise HTTPException(400, "标签名称不能为空；清空标签请提交空列表")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not current:
            raise HTTPException(404, "会话不存在或无权访问")
        if payload.course_id is not None and not db.execute("SELECT 1 FROM course WHERE course_id=?", (payload.course_id,)).fetchone():
            raise HTTPException(400, "课程不存在")
        before = {"course_id": current["course_id"], "title": current["title"], "summary": current["summary"], "tags": [r[0] for r in db.execute("SELECT t.tag_name FROM tag t JOIN conversation_tag ct USING(tag_id) WHERE ct.conversation_id=? ORDER BY t.tag_name", (conversation_id,))]}
        old_curation = latest_curation(db, conversation_id)
        curated_fields = fields | (set(json.loads(old_curation["after_data"])["curated_fields"]) if old_curation else set())
        course_id = payload.course_id if payload.course_id is not None else current["course_id"]
        title = payload.title.strip() if payload.title is not None else current["title"]
        summary_text = payload.summary.strip() if payload.summary is not None else current["summary"]
        if course_id != current["course_id"]:
            points = db.execute("SELECT kp.point_name,cp.confidence,cp.status,cp.evidence,cp.confirmed_by,cp.confirmed_at FROM conversation_point cp JOIN knowledge_point kp USING(point_id) WHERE cp.conversation_id=?", (conversation_id,)).fetchall()
            db.execute("DELETE FROM conversation_point WHERE conversation_id=?", (conversation_id,))
            for point in points:
                db.execute("INSERT OR IGNORE INTO knowledge_point(course_id,point_name) VALUES(?,?)", (course_id, point["point_name"]))
                point_id = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?", (course_id, point["point_name"])).fetchone()[0]
                db.execute("INSERT INTO conversation_point(conversation_id,point_id,confidence,source,status,evidence,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?,?)", (conversation_id, point_id, point["confidence"], "manual", point["status"], point["evidence"], point["confirmed_by"], point["confirmed_at"]))
        if payload.tags is not None:
            db.execute("DELETE FROM conversation_tag WHERE conversation_id=?", (conversation_id,))
            for name in tags:
                db.execute("INSERT INTO conversation_tag(conversation_id,tag_id) VALUES(?,?)", (conversation_id, ensure_tag(db, name)))
        db.execute("UPDATE conversation SET course_id=?,title=?,summary=?,updated_at=? WHERE conversation_id=?", (course_id, title, summary_text, now(), conversation_id))
        after = {"course_id": course_id, "title": title, "summary": summary_text, "tags": tags if payload.tags is not None else before["tags"], "curated_fields": sorted(curated_fields)}
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,before_data,after_data) VALUES(?,?,?,?,?,?)", (DEMO_USER_ID, "CURATE", "conversation", conversation_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
        return {"conversation_id": conversation_id, **after}


@app.post("/api/annotations")
def create_annotation(payload: AnnotationInput) -> dict[str, Any]:
    if payload.annotation_type not in {"NOTE", "QUESTION", "CORRECTION", "IMPORTANT"} or not payload.content.strip():
        raise HTTPException(400, "批注类型或内容无效")
    with connect() as db:
        allowed = db.execute("SELECT 1 FROM message m JOIN conversation c ON c.conversation_id=m.conversation_id WHERE m.message_id=? AND c.owner_id=?", (payload.message_id, DEMO_USER_ID)).fetchone()
        if not allowed:
            raise HTTPException(403, "无权批注此消息")
        row = db.execute("INSERT INTO annotation(message_id,author_id,annotation_type,content) VALUES(?,?,?,?) RETURNING annotation_id,annotation_type,content,is_resolved,created_at", (payload.message_id, DEMO_USER_ID, payload.annotation_type, payload.content.strip())).fetchone()
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,after_data) VALUES(?,?,?,?,?)", (DEMO_USER_ID, "CREATE", "annotation", row[0], json.dumps(payload.model_dump(), ensure_ascii=False)))
        return dict(row)


@app.get("/api/annotations")
def list_annotations(is_resolved: bool | None = None, message_id: int | None = None) -> list[dict[str, Any]]:
    where = ["c.owner_id=?"]
    args: list[Any] = [DEMO_USER_ID]
    if is_resolved is not None:
        where.append("a.is_resolved=?"); args.append(int(is_resolved))
    if message_id is not None:
        where.append("a.message_id=?"); args.append(message_id)
    with connect() as db:
        return [dict(row) for row in db.execute(f"SELECT a.*,m.conversation_id,c.title FROM annotation a JOIN message m ON m.message_id=a.message_id JOIN conversation c ON c.conversation_id=m.conversation_id WHERE {' AND '.join(where)} ORDER BY a.updated_at DESC", args)]


@app.patch("/api/annotations/{annotation_id}")
def update_annotation(annotation_id: int, payload: AnnotationUpdate) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT a.* FROM annotation a JOIN message m ON m.message_id=a.message_id JOIN conversation c ON c.conversation_id=m.conversation_id WHERE a.annotation_id=? AND c.owner_id=?", (annotation_id, DEMO_USER_ID)).fetchone()
        if not row:
            raise HTTPException(404, "批注不存在")
        if payload.is_resolved is not None and row["annotation_type"] not in TODO_TYPES:
            raise HTTPException(404, "待办不存在或无权访问")
        content = payload.content.strip() if payload.content is not None else row["content"]
        if not content:
            raise HTTPException(400, "批注内容不能为空")
        resolved = int(payload.is_resolved) if payload.is_resolved is not None else row["is_resolved"]
        updated = db.execute("UPDATE annotation SET content=?,is_resolved=?,updated_at=? WHERE annotation_id=? RETURNING *", (content, resolved, now(), annotation_id)).fetchone()
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,before_data,after_data) VALUES(?,?,?,?,?,?)", (DEMO_USER_ID, "UPDATE", "annotation", annotation_id, json.dumps(dict(row), ensure_ascii=False), json.dumps(dict(updated), ensure_ascii=False)))
        return dict(updated)


@app.post("/api/annotations/{annotation_id}/resolve")
def resolve_annotation(annotation_id: int) -> dict[str, Any]:
    return update_annotation(annotation_id, AnnotationUpdate(is_resolved=True))


@app.delete("/api/annotations/{annotation_id}")
def delete_annotation(annotation_id: int) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT a.* FROM annotation a JOIN message m ON m.message_id=a.message_id JOIN conversation c ON c.conversation_id=m.conversation_id WHERE a.annotation_id=? AND c.owner_id=?", (annotation_id, DEMO_USER_ID)).fetchone()
        if not row:
            raise HTTPException(404, "批注不存在")
        db.execute("DELETE FROM annotation WHERE annotation_id=?", (annotation_id,))
        db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,before_data) VALUES(?,?,?,?,?)", (DEMO_USER_ID, "DELETE", "annotation", annotation_id, json.dumps(dict(row), ensure_ascii=False)))
        return {"deleted": True, "annotation_id": annotation_id}


@app.post("/api/conversations/{conversation_id}/points/{point_id}")
def decide_point(conversation_id: int, point_id: int, payload: PointDecision) -> dict[str, Any]:
    if payload.status not in {"CONFIRMED", "REJECTED"}:
        raise HTTPException(400, "状态必须是 CONFIRMED 或 REJECTED")
    with connect() as db:
        allowed = db.execute("SELECT 1 FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not allowed:
            raise HTTPException(404, "会话不存在")
        point = db.execute("SELECT 1 FROM conversation_point WHERE conversation_id=? AND point_id=?", (conversation_id, point_id)).fetchone()
        if not point:
            raise HTTPException(404, "知识点关系不存在")
        row = db.execute("UPDATE conversation_point SET status=?,confirmed_by=?,confirmed_at=? WHERE conversation_id=? AND point_id=? RETURNING conversation_id,point_id,status,confirmed_by,confirmed_at", (payload.status, DEMO_USER_ID, now(), conversation_id, point_id)).fetchone()
        return dict(row)


@app.post("/api/conversations/{conversation_id}/points")
def add_point(conversation_id: int, payload: PointAssignment) -> dict[str, Any]:
    with connect() as db:
        conversation = db.execute("SELECT course_id FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        point = db.execute("SELECT point_id FROM knowledge_point WHERE point_id=? AND course_id=?", (payload.point_id, conversation[0] if conversation else -1)).fetchone()
        if not conversation or not point:
            raise HTTPException(404, "会话或课程知识点不存在")
        db.execute("INSERT OR IGNORE INTO conversation_point(conversation_id,point_id,source,evidence,status,confirmed_by,confirmed_at) VALUES(?,?, 'manual', ?, 'CONFIRMED', ?, ?)", (conversation_id, payload.point_id, payload.evidence.strip() or None, DEMO_USER_ID, now()))
        return dict(db.execute("SELECT conversation_id,point_id,status,source,evidence,confirmed_by,confirmed_at FROM conversation_point WHERE conversation_id=? AND point_id=?", (conversation_id, payload.point_id)).fetchone())


@app.post("/api/conversations/{conversation_id}/tags")
def add_tag(conversation_id: int, payload: TagInput) -> dict[str, Any]:
    name = payload.tag_name.strip().lstrip("#")
    if not name:
        raise HTTPException(400, "标签不能为空")
    with connect() as db:
        allowed = db.execute("SELECT 1 FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not allowed:
            raise HTTPException(404, "会话不存在")
        tag_id = ensure_tag(db, name[:50])
        db.execute("INSERT OR IGNORE INTO conversation_tag(conversation_id,tag_id) VALUES(?,?)", (conversation_id, tag_id))
        return {"conversation_id": conversation_id, "tag_id": tag_id, "tag_name": name[:50]}


@app.delete("/api/conversations/{conversation_id}/tags/{tag_id}")
def remove_tag(conversation_id: int, tag_id: int) -> dict[str, Any]:
    with connect() as db:
        allowed = db.execute("SELECT 1 FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not allowed:
            raise HTTPException(404, "会话不存在")
        db.execute("DELETE FROM conversation_tag WHERE conversation_id=? AND tag_id=?", (conversation_id, tag_id))
        return {"deleted": True, "conversation_id": conversation_id, "tag_id": tag_id}


@app.patch("/api/conversations/{conversation_id}/classification")
def review_classification(conversation_id: int, payload: ClassificationReview) -> dict[str, Any]:
    if payload.review_status not in {"PENDING", "CONFIRMED", "REVISED"}:
        raise HTTPException(400, "分类审核状态无效")
    with connect() as db:
        allowed = db.execute("SELECT 1 FROM conversation WHERE conversation_id=? AND owner_id=?", (conversation_id, DEMO_USER_ID)).fetchone()
        if not allowed:
            raise HTTPException(404, "会话不存在")
        row = db.execute("UPDATE conversation_annotation SET review_status=?,source=?,updated_at=? WHERE conversation_id=? RETURNING conversation_id,source,review_status,updated_at", (payload.review_status, "MANUAL" if payload.review_status != "PENDING" else "AUTO", now(), conversation_id)).fetchone()
        if not row:
            raise HTTPException(404, "结构化标注不存在")
        return dict(row)


@app.post("/api/reviews")
def create_review(payload: ReviewInput) -> dict[str, Any]:
    if not payload.question.strip() or not payload.answer.strip():
        raise HTTPException(400, "复习问题和答案不能为空")
    with connect() as db:
        message = db.execute("SELECT m.content FROM message m JOIN conversation c ON c.conversation_id=m.conversation_id WHERE m.message_id=? AND c.owner_id=?", (payload.message_id, DEMO_USER_ID)).fetchone()
        if not message:
            raise HTTPException(403, "无权创建此复习卡")
        db.execute("INSERT INTO review_card(message_id,owner_id,question,answer,due_at) VALUES(?,?,?,?,datetime('now')) ON CONFLICT(owner_id,message_id) DO NOTHING", (payload.message_id, DEMO_USER_ID, payload.question.strip(), payload.answer.strip()))
        row = db.execute("SELECT * FROM review_card WHERE owner_id=? AND message_id=?", (DEMO_USER_ID, payload.message_id)).fetchone()
        return dict(row)


@app.post("/api/reviews/{review_id}/complete")
def complete_review(review_id: int, grade: str = Query(..., pattern="^(correct|wrong)$")) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        card = db.execute("SELECT * FROM review_card WHERE review_card_id=? AND owner_id=?", (review_id, DEMO_USER_ID)).fetchone()
        if not card:
            raise HTTPException(404, "复习卡不存在")
        if card["status"] != "ACTIVE":
            raise HTTPException(400, "该卡片已暂停或完成，请先恢复卡片")
        intervals = [1, 3, 7, 14]
        previous_due = card["due_at"]
        previous_interval = card["interval_days"]
        next_count = card["review_count"] + 1 if grade == "correct" else 0
        interval = intervals[min(next_count, len(intervals) - 1)]
        db.execute("UPDATE review_card SET review_count=?,interval_days=?,due_at=datetime('now',?),last_reviewed_at=datetime('now'),status='ACTIVE' WHERE review_card_id=?", (next_count, interval, f"+{interval} day", review_id))
        updated = dict(db.execute("SELECT * FROM review_card WHERE review_card_id=?", (review_id,)).fetchone())
        db.execute("INSERT INTO review_log(review_card_id,grade,interval_before,interval_after,streak_before,streak_after,due_before,due_after) VALUES(?,?,?,?,?,?,?,?)", (review_id, grade, card["interval_days"], updated["interval_days"], card["review_count"], updated["review_count"], card["due_at"], updated["due_at"]))
        return updated


@app.get("/api/reviews/history")
def review_history(review_card_id: int | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    with connect() as db:
        args: list[Any] = [DEMO_USER_ID]
        where = "r.owner_id=?"
        if review_card_id is not None:
            if not db.execute("SELECT 1 FROM review_card WHERE review_card_id=? AND owner_id=?", (review_card_id, DEMO_USER_ID)).fetchone():
                raise HTTPException(404, "复习卡不存在或无权访问")
            where += " AND r.review_card_id=?"
            args.append(review_card_id)
        joins = "FROM review_log l JOIN review_card r ON r.review_card_id=l.review_card_id JOIN message m ON m.message_id=r.message_id JOIN conversation c ON c.conversation_id=m.conversation_id"
        total = db.execute(f"SELECT COUNT(*) {joins} WHERE {where}", args).fetchone()[0]
        items = [dict(row) for row in db.execute(f"SELECT l.*,r.question,c.conversation_id,c.title {joins} WHERE {where} ORDER BY l.reviewed_at DESC,l.review_log_id DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
        return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/reviews/due")
def due_reviews() -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT r.*,c.title,co.course_name FROM review_card r JOIN message m ON m.message_id=r.message_id JOIN conversation c ON c.conversation_id=m.conversation_id JOIN course co ON co.course_id=c.course_id WHERE r.owner_id=? AND r.status='ACTIVE' AND r.due_at<=CURRENT_TIMESTAMP ORDER BY r.due_at", (DEMO_USER_ID,))]


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    with connect() as db:
        course = [dict(row) for row in db.execute("SELECT co.course_name,COUNT(DISTINCT c.conversation_id) conversations,COUNT(m.message_id) messages FROM course co LEFT JOIN conversation c ON c.course_id=co.course_id AND c.owner_id=? LEFT JOIN message m ON m.conversation_id=c.conversation_id GROUP BY co.course_id ORDER BY conversations DESC", (DEMO_USER_ID,))]
        source = [dict(row) for row in db.execute("SELECT s.platform_name,COUNT(c.conversation_id) conversations FROM conversation_source s LEFT JOIN conversation c ON c.source_id=s.source_id AND c.owner_id=? GROUP BY s.source_id ORDER BY conversations DESC", (DEMO_USER_ID,))]
        points = [dict(row) for row in db.execute("SELECT kp.point_name,COUNT(DISTINCT c.conversation_id) conversations FROM knowledge_point kp LEFT JOIN conversation_point cp ON cp.point_id=kp.point_id LEFT JOIN conversation c ON c.conversation_id=cp.conversation_id AND c.owner_id=? GROUP BY kp.point_name ORDER BY conversations DESC,kp.point_name", (DEMO_USER_ID,))]
        gaps = [dict(row) for row in db.execute("""
            SELECT kp.point_id,kp.point_name,
                   COUNT(DISTINCT cp.conversation_id) conversations,
                   COUNT(DISTINCT CASE WHEN a.is_resolved=0 THEN a.annotation_id END) unresolved_count,
                   COUNT(DISTINCT CASE WHEN r.status='ACTIVE' AND r.due_at<=CURRENT_TIMESTAMP THEN r.review_card_id END) overdue_count,
                   COUNT(DISTINCT CASE WHEN rl.grade='wrong' THEN rl.review_log_id END) wrong_count,
                   (COUNT(DISTINCT CASE WHEN a.is_resolved=0 THEN a.annotation_id END) * 3
                    + COUNT(DISTINCT CASE WHEN r.status='ACTIVE' AND r.due_at<=CURRENT_TIMESTAMP THEN r.review_card_id END) * 2
                    + COUNT(DISTINCT CASE WHEN rl.grade='wrong' THEN rl.review_log_id END) * 2) priority
            FROM knowledge_point kp
            LEFT JOIN conversation_point cp ON cp.point_id=kp.point_id AND cp.status IN ('PROPOSED','CONFIRMED')
            LEFT JOIN conversation c ON c.conversation_id=cp.conversation_id AND c.owner_id=?
            LEFT JOIN message m ON m.conversation_id=c.conversation_id
            LEFT JOIN annotation a ON a.message_id=m.message_id
            LEFT JOIN review_card r ON r.message_id=m.message_id AND r.owner_id=?
            LEFT JOIN review_log rl ON rl.review_card_id=r.review_card_id
            GROUP BY kp.point_id ORDER BY priority DESC, kp.point_name
        """, (DEMO_USER_ID, DEMO_USER_ID))]
        reviews = dict(db.execute("SELECT COUNT(*) total,COALESCE(SUM(l.grade='correct'),0) correct,COALESCE(SUM(l.grade='wrong'),0) wrong FROM review_log l JOIN review_card r ON r.review_card_id=l.review_card_id WHERE r.owner_id=?", (DEMO_USER_ID,)).fetchone())
        return {"course": course, "source": source, "points": points, "reviews": reviews, "gaps": gaps}


@app.post("/api/import/scan")
def import_scan() -> dict[str, Any]:
    results = scan_and_import()
    return {"scanned": len(results), "imported": sum(r["status"] == "SUCCESS" for r in results), "skipped": sum(r["status"] == "SKIPPED" for r in results), "failed": sum(r["status"] == "FAILED" for r in results), "results": results}


@app.get("/api/export/conversations.json")
def export_json() -> Response:
    with connect() as db:
        rows = []
        for conversation in db.execute("SELECT c.*,s.platform_name,co.course_name FROM conversation c JOIN conversation_source s ON s.source_id=c.source_id JOIN course co ON co.course_id=c.course_id WHERE c.owner_id=? ORDER BY c.updated_at DESC", (DEMO_USER_ID,)):
            item = dict(conversation)
            item["messages"] = [dict(m) for m in db.execute("SELECT seq_no,speaker,content,created_at FROM message WHERE conversation_id=? ORDER BY seq_no", (conversation["conversation_id"],))]
            rows.append(item)
    return Response(json.dumps(rows, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": "attachment; filename=studyvault-export.json"})


@app.get("/api/export/structured.jsonl")
def export_structured() -> FileResponse:
    path = BASE_DIR / "staging" / "structured_manifest_final.jsonl"
    if not path.exists():
        raise HTTPException(404, "结构化导出尚未生成")
    return FileResponse(path, media_type="application/x-ndjson", filename="studyvault-structured.jsonl")


@app.get("/api/export/manual-annotations.jsonl")
def export_manual_annotations() -> FileResponse:
    path = BASE_DIR / "staging" / "manual_annotations_all.jsonl"
    if not path.exists():
        raise HTTPException(404, "人工标注尚未生成")
    return FileResponse(path, media_type="application/x-ndjson", filename="studyvault-manual-annotations.jsonl")


@app.get("/api/export/course/{course_id}.md")
def export_course(course_id: int) -> Response:
    with connect() as db:
        course = db.execute("SELECT course_name FROM course WHERE course_id=?", (course_id,)).fetchone()
        if not course:
            raise HTTPException(404, "课程不存在")
        lines = [f"# {course[0]}复习资料", ""]
        for c in db.execute("SELECT conversation_id,title,updated_at FROM conversation WHERE owner_id=? AND course_id=? ORDER BY updated_at DESC", (DEMO_USER_ID, course_id)):
            lines.append(f"## {c['title']}")
            for m in db.execute("SELECT speaker,content FROM message WHERE conversation_id=? ORDER BY seq_no", (c["conversation_id"],)):
                lines.extend([f"**{m['speaker']}**", m["content"], ""])
    return Response("\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=course-{course_id}.md"})


from usability import create_router
app.include_router(create_router(sys.modules[__name__]))

import text2sql
app.include_router(text2sql.create_router(sys.modules[__name__]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--init", action="store_true")
    actions.add_argument("--import-local", action="store_true")
    actions.add_argument("--restore-bundled", action="store_true", help="从附带的最终 JSONL 恢复全部会话和标注")
    args = parser.parse_args()
    try:
        if args.restore_bundled:
            output = restore_bundled()
        else:
            init_db()
            output = {"results": scan_and_import()} if args.import_local else {"database": str(DB_PATH), "initialized": True}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"失败：{exc}\n")
