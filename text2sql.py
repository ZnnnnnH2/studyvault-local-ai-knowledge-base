"""Text2SQL：自然语言 → SQL → 只读执行 → 展示结果。

两种生成方式：
- baseline：零样本，把整库 schema 塞进 prompt，让模型直接吐一条 SELECT。
- c3：复现 C3-SQL(《C3: Zero-shot Text-to-SQL with ChatGPT》, 2023)的三件套——
    1. Clear Prompting：先做 schema linking（表召回 → 列召回），只把相关表/列喂给生成；
    2. Calibration with Hints：prepend 一组固定“纠偏提示”，压模型的常见毛病；
    3. Consistency：高温采样多条候选，各自执行，按结果集投票取多数。

安全边界（多层防御，任何一层都能阻止写操作）：语句校验（仅单条 SELECT/WITH）、
``mode=ro`` 只读连接、SQLite 授权器只放行读、进度处理器限步、自动 LIMIT。
没有配置大模型 API Key 时退回内置规则模板，保证离线演示与测试无需联网。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent

ROW_LIMIT = 200
PROGRESS_STEP_LIMIT = 500_000

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|"
    r"attach|detach|pragma|vacuum|reindex|analyze|trigger|grant|revoke|"
    r"commit|rollback|savepoint|begin)\b",
    re.IGNORECASE,
)
_READONLY_AUTH = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


# ---------------------------------------------------------------------------
# 结构化 schema：唯一真相源。baseline 的 prompt 与 C3 的聚焦 schema 都由它生成，
# 列名严格对齐 schema.sql，避免模型引用不存在的列。
# ---------------------------------------------------------------------------
STRUCTURED_SCHEMA: dict[str, tuple[list[str], str]] = {
    "course": (["course_id", "course_name"], "课程"),
    "knowledge_point": (["point_id", "point_name"], "知识点"),
    "conversation_source": (["source_id", "platform_name"], "来源平台，如 Codex / Claude Code / DeepSeek"),
    "conversation": (
        ["conversation_id", "owner_id", "course_id", "source_id", "source_conversation_id", "title", "summary", "started_at", "updated_at"],
        "会话；演示用户 owner_id=1；收藏与阅读状态在 conversation_state，不在这里",
    ),
    "conversation_state": (
        ["owner_id", "conversation_id", "is_favorite", "last_message_id", "last_opened_at"],
        "每个会话的收藏(is_favorite 0/1)与阅读进度",
    ),
    "message": (
        ["message_id", "conversation_id", "seq_no", "speaker", "content", "created_at"],
        "消息；speaker 取值 'user'/'assistant'/'system'；无 owner 列，统计归属需 JOIN conversation",
    ),
    "tag": (["tag_id", "owner_id", "tag_name"], "标签"),
    "conversation_tag": (["conversation_id", "tag_id"], "会话-标签 多对多"),
    "conversation_point": (
        ["conversation_id", "point_id", "status", "evidence"],
        "会话-知识点；status 取值 'PROPOSED'/'CONFIRMED'/'REJECTED'",
    ),
    "annotation": (
        ["annotation_id", "message_id", "annotation_type", "content", "is_resolved"],
        "消息级批注；annotation_type 取值 'NOTE'/'QUESTION'/'CORRECTION'/'IMPORTANT'；is_resolved 0/1",
    ),
    "conversation_annotation": (
        ["annotation_id", "conversation_id", "summary_manual", "intent", "quality", "confidence"],
        "会话级深读标注；quality 取值 '有效'/'混合'/'低价值'",
    ),
    "review_card": (
        ["review_card_id", "message_id", "owner_id", "question", "answer", "due_at", "interval_days", "review_count", "status"],
        "复习卡；owner_id=1；status 取值 'ACTIVE'/'SUSPENDED'/'COMPLETED'；review_count 为连续答对次数",
    ),
    "review_log": (
        ["review_log_id", "review_card_id", "grade", "reviewed_at"],
        "每次复习记录；grade 取值 'correct'/'wrong'",
    ),
}

FOREIGN_KEYS = [
    ("conversation.course_id", "course.course_id"),
    ("conversation.source_id", "conversation_source.source_id"),
    ("conversation_state.conversation_id", "conversation.conversation_id"),
    ("message.conversation_id", "conversation.conversation_id"),
    ("conversation_tag.conversation_id", "conversation.conversation_id"),
    ("conversation_tag.tag_id", "tag.tag_id"),
    ("conversation_point.conversation_id", "conversation.conversation_id"),
    ("conversation_point.point_id", "knowledge_point.point_id"),
    ("annotation.message_id", "message.message_id"),
    ("conversation_annotation.conversation_id", "conversation.conversation_id"),
    ("review_card.message_id", "message.message_id"),
    ("review_log.review_card_id", "review_card.review_card_id"),
]

CONVENTIONS = (
    "约定：会话归属用 conversation.owner_id = 1；复习卡用 review_card.owner_id = 1。"
    "统计消息条数用 message 表（需要归属时 JOIN conversation）；统计会话数用 conversation 表。"
    "时间列为 ISO8601 文本，可用字符串比较或 date()/datetime()。"
)


def build_schema_text(tables: list[str] | None = None, columns: dict[str, list[str]] | None = None) -> str:
    """生成喂给模型的 schema 文本。tables=None 给全库；否则只给聚焦子集。"""
    names = tables if tables is not None else list(STRUCTURED_SCHEMA)
    lines = []
    for name in names:
        if name not in STRUCTURED_SCHEMA:
            continue
        all_cols, note = STRUCTURED_SCHEMA[name]
        cols = columns.get(name) if columns else None
        shown = [c for c in all_cols if c in cols] if cols else all_cols
        if not shown:
            shown = all_cols
        lines.append(f"{name}({', '.join(shown)}) — {note}")
    fks = [f"{a} → {b}" for a, b in FOREIGN_KEYS if tables is None or (a.split('.')[0] in names and b.split('.')[0] in names)]
    text = "表：\n" + "\n".join(lines)
    if fks:
        text += "\n\n外键：\n" + "\n".join(fks)
    text += "\n\n" + CONVENTIONS
    return text


SCHEMA_PROMPT = build_schema_text()

CALIBRATION_HINTS = (
    "生成 SQL 时遵守以下规则（它们用于纠正常见错误）：\n"
    "1. 只选择问题明确要求的列；问“多少/数量/几个”时用 COUNT(...)，不要把被计数的列也 SELECT 出来。\n"
    "2. 需要跨表时优先用 LEFT JOIN，并尽量把过滤条件放进 JOIN 的 ON 子句，避免因 NULL 丢行。\n"
    "3. 不要无必要地使用 DISTINCT、GROUP BY 或子查询。\n"
    "4. 会话相关统计加 conversation.owner_id = 1；复习相关加 review_card.owner_id = 1。\n"
    "5. 列名必须来自给定 schema，不要臆造。\n"
    "6. 只输出一条 SELECT 查询，不要解释、不要分号、不要 Markdown 代码块。"
)

EXAMPLES = [
    "每门课程各有多少会话和消息？",
    "各来源平台的会话数量排名",
    "未解决的疑问批注有多少条？",
    "复习答对和答错各多少次？",
    "标题里包含“外键”的会话有哪些？",
    "最近更新的 10 个会话",
]


def _load_env_local() -> None:
    for name in (".env.local", ".env"):
        path = BASE_DIR / name
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        except OSError:
            continue


_load_env_local()


def llm_config() -> dict[str, Any]:
    api_key = os.getenv("STUDYVAULT_LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    return {
        "api_key": api_key,
        "base_url": os.getenv("STUDYVAULT_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "model": os.getenv("STUDYVAULT_LLM_MODEL", "deepseek-chat"),
        "samples": int(os.getenv("STUDYVAULT_T2S_SAMPLES", "5")),
    }


# ---------------------------------------------------------------------------
# 离线规则兜底
# ---------------------------------------------------------------------------
_FALLBACK_TEMPLATES: list[tuple[list[str], str]] = [
    (["课程", "每门", "分课程"],
     "SELECT co.course_name AS 课程, COUNT(DISTINCT c.conversation_id) AS 会话数, COUNT(m.message_id) AS 消息数 "
     "FROM course co LEFT JOIN conversation c ON c.course_id = co.course_id AND c.owner_id = 1 "
     "LEFT JOIN message m ON m.conversation_id = c.conversation_id GROUP BY co.course_id ORDER BY 会话数 DESC"),
    (["来源", "平台"],
     "SELECT s.platform_name AS 来源, COUNT(c.conversation_id) AS 会话数 FROM conversation_source s "
     "LEFT JOIN conversation c ON c.source_id = s.source_id AND c.owner_id = 1 GROUP BY s.source_id ORDER BY 会话数 DESC"),
    (["疑问", "未解决", "待办", "批注"],
     "SELECT a.annotation_type AS 类型, COUNT(*) AS 数量 FROM annotation a WHERE a.is_resolved = 0 "
     "GROUP BY a.annotation_type ORDER BY 数量 DESC"),
    (["复习", "答对", "答错", "正确率"],
     "SELECT l.grade AS 评分, COUNT(*) AS 次数 FROM review_log l JOIN review_card r ON r.review_card_id = l.review_card_id "
     "WHERE r.owner_id = 1 GROUP BY l.grade"),
    (["知识点"],
     "SELECT kp.point_name AS 知识点, COUNT(DISTINCT cp.conversation_id) AS 会话数 FROM knowledge_point kp "
     "LEFT JOIN conversation_point cp ON cp.point_id = kp.point_id AND cp.status IN ('PROPOSED','CONFIRMED') "
     "GROUP BY kp.point_id ORDER BY 会话数 DESC"),
    (["最近", "最新"],
     "SELECT title AS 标题, updated_at AS 更新时间 FROM conversation WHERE owner_id = 1 ORDER BY updated_at DESC LIMIT 10"),
    (["收藏"],
     "SELECT c.title AS 标题 FROM conversation c JOIN conversation_state cs ON cs.conversation_id = c.conversation_id "
     "WHERE cs.owner_id = 1 AND cs.is_favorite = 1"),
]


def fallback_sql(question: str) -> str | None:
    for keywords, sql in _FALLBACK_TEMPLATES:
        if any(word in question for word in keywords):
            return sql
    return None


# ---------------------------------------------------------------------------
# 安全校验 + 只读执行
# ---------------------------------------------------------------------------
def validate_sql(sql: str) -> str:
    cleaned = sql.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    cleaned = cleaned.rstrip(";").strip()
    if not cleaned:
        raise ValueError("生成的 SQL 为空")
    if ";" in cleaned:
        raise ValueError("只允许单条查询语句")
    head = cleaned.lstrip("(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("只允许 SELECT / WITH 查询")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("查询中包含不被允许的写操作关键字")
    return cleaned


def _authorizer(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READONLY_AUTH else sqlite3.SQLITE_DENY


def run_readonly(db_path: Path, sql: str, limit: int = ROW_LIMIT) -> dict[str, Any]:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        steps = {"n": 0}

        def _guard() -> int:
            steps["n"] += 1
            return 1 if steps["n"] > PROGRESS_STEP_LIMIT // 1000 else 0

        connection.set_progress_handler(_guard, 1000)
        connection.set_authorizer(_authorizer)
        try:
            cursor = connection.execute(sql)
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower() or "abort" in str(exc).lower():
                raise ValueError("查询过于复杂或耗时过长，已中止") from exc
            raise ValueError(f"SQL 执行失败：{exc}") from exc
        columns = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(limit + 1)
        truncated = len(fetched) > limit
        rows = [list(r) for r in fetched[:limit]]
        return {"columns": columns, "rows": rows, "truncated": truncated}
    finally:
        connection.set_authorizer(None)
        connection.close()


def result_signature(result: dict[str, Any]) -> str:
    """把执行结果归一化成可比较的指纹：忽略列名、行序、列序，比较值的多重集合。
    用于自一致性投票，也用于评测时与标准答案比对（execution accuracy）。"""
    rows = result.get("rows", [])
    bag = Counter(tuple(sorted("" if v is None else str(v) for v in row)) for row in rows)
    return json.dumps(sorted(bag.items()), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 大模型调用
# ---------------------------------------------------------------------------
def _chat(messages: list[dict[str, str]], config: dict[str, Any], temperature: float = 0.0) -> str:
    payload = {"model": config["model"], "messages": messages, "temperature": temperature, "stream": False}
    request = urllib.request.Request(
        f"{config['base_url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    return json.loads(match.group(1)) if match else json.loads(text)


def generate_baseline(question: str, config: dict[str, Any]) -> str:
    system = (
        "你是一个只读 SQL 生成助手，面向 SQLite 数据库。"
        "只输出一条 SELECT 查询，不要解释、不要 Markdown、不要分号。\n\n" + SCHEMA_PROMPT
    )
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": question}], config)


# ---------------------------------------------------------------------------
# C3-SQL
# ---------------------------------------------------------------------------
def recall_tables(question: str, config: dict[str, Any]) -> list[str]:
    """C3 的表召回：让模型从全部表里挑出与问题相关的表。"""
    catalog = "\n".join(f"- {name}: {note}" for name, (_cols, note) in STRUCTURED_SCHEMA.items())
    system = "你是数据库 schema 分析助手。给定问题和表清单，挑出回答该问题所需的表。"
    user = (f"表清单：\n{catalog}\n\n问题：{question}\n\n"
            '只返回一个 JSON 数组，元素是相关表名（最多 6 个，按相关度排序），例如 ["conversation","course"]。')
    try:
        data = _extract_json(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], config))
        tables = [t for t in data if t in STRUCTURED_SCHEMA]
        return tables or list(STRUCTURED_SCHEMA)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError, TypeError):
        return list(STRUCTURED_SCHEMA)


def recall_columns(question: str, tables: list[str], config: dict[str, Any]) -> dict[str, list[str]]:
    """C3 的列召回：在已召回的表里挑出相关列。"""
    detail = "\n".join(f"- {name}({', '.join(STRUCTURED_SCHEMA[name][0])})" for name in tables if name in STRUCTURED_SCHEMA)
    system = "你是数据库 schema 分析助手。给定问题和若干表的列，挑出回答该问题所需的列。"
    user = (f"表与列：\n{detail}\n\n问题：{question}\n\n"
            '只返回一个 JSON 对象，key 是表名，value 是该表相关列名数组，例如 {"conversation":["course_id"]}。'
            "外键列请一并保留。")
    try:
        data = _extract_json(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], config))
        columns = {t: [c for c in cols if c in STRUCTURED_SCHEMA.get(t, ([],))[0]] for t, cols in data.items() if t in tables}
        return {t: cols for t, cols in columns.items() if cols} or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def generate_candidates(question: str, schema_text: str, config: dict[str, Any], samples: int, temperature: float = 0.7) -> list[str]:
    """C3 的一致性：高温采样多条候选 SQL（只保留通过安全校验的）。"""
    system = ("你是一个只读 SQL 生成助手，面向 SQLite 数据库。\n\n" + CALIBRATION_HINTS + "\n\n相关表结构：\n" + schema_text)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]

    def one(_: int) -> str | None:
        try:
            return validate_sql(_chat(messages, config, temperature=temperature))
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            return None

    # N 条采样互相独立，并发发出；map 保持提交顺序，投票平局时的取舍仍是确定的。
    n = max(1, samples)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return [sql for sql in pool.map(one, range(n)) if sql]


def vote_candidates(candidates: list[str], db_path: Path) -> dict[str, Any]:
    """执行每条候选，按结果集指纹投票，取得票最多的那条。"""
    buckets: dict[str, dict[str, Any]] = {}
    tally: Counter[str] = Counter()
    for sql in candidates:
        try:
            result = run_readonly(db_path, sql)
        except ValueError:
            continue
        sig = result_signature(result)
        tally[sig] += 1
        buckets.setdefault(sig, {"sql": sql, "result": result})
    if not tally:
        raise ValueError("所有候选 SQL 都执行失败")
    best_sig, votes = tally.most_common(1)[0]
    chosen = buckets[best_sig]
    return {"sql": chosen["sql"], "result": chosen["result"], "votes": votes, "valid": sum(tally.values()),
            "tally": [count for _, count in tally.most_common()]}


def generate_c3(question: str, config: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """完整 C3 流水线：schema linking → 校偏生成（采样）→ 执行投票。"""
    tables = recall_tables(question, config)
    columns = recall_columns(question, tables, config)
    schema_text = build_schema_text(tables, columns)
    candidates = generate_candidates(question, schema_text, config, config["samples"])
    if not candidates:
        raise ValueError("模型未能生成有效 SQL")
    voted = vote_candidates(candidates, db_path)
    result = voted["result"]
    result.update({
        "sql": voted["sql"],
        "c3": {
            "linked_tables": tables,
            "linked_columns": columns or {},
            "candidates": len(candidates),
            "valid": voted["valid"],
            "votes": voted["votes"],
            "tally": voted["tally"],
            "samples": config["samples"],
        },
    })
    return result


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class Text2SQLRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    method: str = Field(default="c3", pattern="^(baseline|c3)$")


def create_router(backend: Any) -> APIRouter:
    router = APIRouter(prefix="/api/text2sql")

    @router.get("/schema")
    def schema() -> dict[str, Any]:
        config = llm_config()
        return {
            "schema": SCHEMA_PROMPT,
            "examples": EXAMPLES,
            "llm_enabled": bool(config["api_key"]),
            "model": config["model"] if config["api_key"] else None,
            "row_limit": ROW_LIMIT,
            "samples": config["samples"],
            "methods": ["baseline", "c3"],
        }

    @router.post("")
    def query(body: Text2SQLRequest) -> dict[str, Any]:
        question = body.question.strip()
        config = llm_config()
        if not config["api_key"]:
            raw_sql = fallback_sql(question)
            if raw_sql is None:
                raise HTTPException(422, "未配置大模型且该问题无匹配的离线模板。请换一种问法，或设置 DEEPSEEK_API_KEY 后重试。")
            sql = validate_sql(raw_sql)
            result = run_readonly(backend.DB_PATH, sql)
            result.update({"sql": sql, "source": "fallback", "method": "fallback", "question": question,
                           "note": "未配置大模型 API Key，使用内置规则模板。可设置 DEEPSEEK_API_KEY 启用真实 Text2SQL。"})
            return result
        try:
            if body.method == "c3":
                result = generate_c3(question, config, backend.DB_PATH)
            else:
                sql = validate_sql(generate_baseline(question, config))
                result = run_readonly(backend.DB_PATH, sql)
                result["sql"] = sql
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(502, f"大模型调用失败：{exc}") from exc
        result.update({"source": "llm", "method": body.method, "question": question, "note": None})
        return result

    return router
