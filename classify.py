"""AI 建议知识点：让大模型读一段对话，提出所属课程和知识点，结果以 PROPOSED 状态等人确认。

- 只在用户点按钮时运行，从不在导入时自动调用：它会把会话标题、摘要和前几条消息发给
  ``.env.local`` 里配置的大模型（与 Text2SQL 只发表结构不同），页面上会写明这一点。
- 没配 key 时退回关键词规则（``app.POINT_RULES``），来源记为 ``rule``。
- 已确认或已拒绝的关系不会被覆盖；模型认为课程不对时，只返回改课建议，不写入知识点，
  等用户改课后再建议一次，避免把别的课的知识点挂到当前课程下。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException

import text2sql

MAX_CONTEXT_CHARS = 3000
MAX_NEW_POINTS = 1

PROMPT = """你在帮一个大学生整理和 AI 的学习对话。根据下面这段对话，判断它属于哪门课程、涉及哪些知识点。

可选课程及其已有知识点：
{catalog}

规则：
- course 必须从上面的课程名里原样选一个；实在都不沾边就选"通用学习"。
- points 选 1–3 个，优先用该课程已有的知识点名（原样照抄）；只有确实没有合适的才新起一个，最多 1 个，10 字以内。
- reason 用一句话说明依据，引用对话里的具体内容。

只输出 JSON：{{"course": "...", "points": [{{"name": "...", "reason": "..."}}]}}

对话标题：{title}
摘要：{summary}
对话内容：
{dialogue}"""


def create_router(backend: Any) -> APIRouter:
    router = APIRouter()

    def catalog(db) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for row in db.execute("SELECT co.course_name, kp.point_name FROM course co LEFT JOIN knowledge_point kp ON kp.course_id=co.course_id ORDER BY co.course_id, kp.point_id"):
            result.setdefault(row[0], [])
            if row[1]:
                result[row[0]].append(row[1])
        return result

    def context(db, conversation_id: int) -> dict[str, Any]:
        row = db.execute("SELECT c.conversation_id,c.title,c.summary,c.course_id,co.course_name FROM conversation c JOIN course co USING(course_id) WHERE c.conversation_id=? AND c.owner_id=?",
                         (conversation_id, backend.DEMO_USER_ID)).fetchone()
        if not row:
            raise HTTPException(404, "会话不存在或无权访问")
        dialogue, used = [], 0
        for message in db.execute("SELECT speaker,content FROM message WHERE conversation_id=? ORDER BY seq_no", (conversation_id,)):
            piece = f"{'学生' if message['speaker'] == 'user' else 'AI'}：{message['content']}"[: MAX_CONTEXT_CHARS - used]
            dialogue.append(piece)
            used += len(piece)
            if used >= MAX_CONTEXT_CHARS:
                break
        return {**dict(row), "dialogue": "\n\n".join(dialogue)}

    def ask_llm(ctx: dict[str, Any], courses: dict[str, list[str]], config: dict[str, Any]) -> dict[str, Any]:
        listing = "\n".join(f"- {name}：{'、'.join(points) or '（暂无）'}" for name, points in courses.items())
        prompt = PROMPT.format(catalog=listing, title=ctx["title"], summary=ctx["summary"] or "（无）", dialogue=ctx["dialogue"])
        return text2sql._extract_json(text2sql._chat([{"role": "user", "content": prompt}], config, temperature=0.0))

    def rule_guess(ctx: dict[str, Any]) -> dict[str, Any]:
        text = (ctx["title"] + " " + ctx["dialogue"]).lower()
        points = [{"name": point, "reason": "命中关键词：" + "、".join(t for t in terms if t.lower() in text)}
                  for course, point, terms in backend.POINT_RULES if course == ctx["course_name"] and any(t.lower() in text for t in terms)]
        return {"course": ctx["course_name"], "points": points[:3]}

    def apply(db, ctx: dict[str, Any], proposal: dict[str, Any], source: str, courses: dict[str, list[str]]) -> dict[str, Any]:
        course = proposal.get("course") if proposal.get("course") in courses else ctx["course_name"]
        raw_points = [p for p in proposal.get("points") or [] if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip()]
        if course != ctx["course_name"]:
            target = db.execute("SELECT course_id FROM course WHERE course_name=?", (course,)).fetchone()[0]
            return {"conversation_id": ctx["conversation_id"], "source": source, "course_suggestion": {"course_id": target, "course_name": course},
                    "points": [{"point_name": p["name"].strip(), "evidence": str(p.get("reason") or "")} for p in raw_points[:3]], "added": 0}
        known, created, added, points = set(courses[course]), 0, 0, []
        for point in raw_points[:3]:
            name = point["name"].strip()[:20]
            if name not in known:
                if created >= MAX_NEW_POINTS:
                    continue
                created += 1
                db.execute("INSERT OR IGNORE INTO knowledge_point(course_id,point_name) VALUES(?,?)", (ctx["course_id"], name))
            point_id = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?", (ctx["course_id"], name)).fetchone()[0]
            evidence = str(point.get("reason") or "")[:300]
            # Existing CONFIRMED / REJECTED / PROPOSED rows win: the human decision is never overwritten.
            inserted = db.execute("INSERT OR IGNORE INTO conversation_point(conversation_id,point_id,confidence,source,status,evidence) VALUES(?,?,?,?, 'PROPOSED', ?)",
                                  (ctx["conversation_id"], point_id, 0.8 if source == "llm" else 0.5, source, evidence)).rowcount
            added += inserted
            status = db.execute("SELECT status FROM conversation_point WHERE conversation_id=? AND point_id=?", (ctx["conversation_id"], point_id)).fetchone()[0]
            points.append({"point_id": point_id, "point_name": name, "evidence": evidence, "status": status, "new_point": name not in known, "added": bool(inserted)})
        return {"conversation_id": ctx["conversation_id"], "source": source, "course_suggestion": None, "points": points, "added": added}

    def suggest(conversation_id: int, config: dict[str, Any]) -> dict[str, Any]:
        with backend.connect() as db:
            ctx, courses = context(db, conversation_id), catalog(db)
        if config["api_key"]:
            proposal, source = ask_llm(ctx, courses, config), "llm"  # network call outside the database lock
        else:
            proposal, source = rule_guess(ctx), "rule"
        with backend.connect() as db:
            return apply(db, ctx, proposal, source, courses)

    @router.post("/api/conversations/{conversation_id}/suggest-points")
    def suggest_points(conversation_id: int) -> dict[str, Any]:
        try:
            return suggest(conversation_id, text2sql.llm_config())
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - network / model output problems
            raise HTTPException(502, f"大模型建议失败：{exc}") from exc

    @router.post("/api/classify/unclassified")
    def classify_unclassified() -> dict[str, Any]:
        """Suggest points for every conversation that has no confirmed or proposed point yet."""
        with backend.connect() as db:
            ids = [r[0] for r in db.execute("SELECT c.conversation_id FROM conversation c WHERE c.owner_id=? AND NOT EXISTS (SELECT 1 FROM conversation_point cp WHERE cp.conversation_id=c.conversation_id AND cp.status IN ('CONFIRMED','PROPOSED')) ORDER BY c.conversation_id", (backend.DEMO_USER_ID,))]
        config = text2sql.llm_config()
        results, failed = [], 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            for future in [pool.submit(suggest, cid, config) for cid in ids]:
                try:
                    results.append(future.result())
                except Exception:  # noqa: BLE001 - one bad reply must not stop the batch
                    failed += 1
        return {"conversations": len(ids), "proposed": sum(r["added"] for r in results),
                "course_suggestions": sum(r["course_suggestion"] is not None for r in results), "failed": failed,
                "source": "llm" if config["api_key"] else "rule", "results": results}

    return router
