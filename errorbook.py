"""错题本：把散在对话里的"错"收拢到知识点下。

三类错题，全部来自已有表，不另存一份：
- 疑问（annotation.annotation_type='QUESTION'）：自己没弄懂的地方，``is_resolved=0`` 算没掌握；
- AI 答错（'CORRECTION'）：AI 说错、被自己指出的地方，``is_resolved=0`` 表示还没核对清楚；
- 常错卡（review_card.lapse_count>0）：复习时答错过的卡，连续答对 ``MASTERED_STREAK`` 次才算掌握。

每条错题按所在会话的主知识点归组（先取已确认的，再取待确认的），组内优先级：
疑问 ×3 + AI 答错 ×2 + 不稳的卡 ×2 + 已逾期 ×1。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

MASTERED_STREAK = 3
WEIGHTS = {"question": 3, "correction": 2, "card": 2, "overdue": 1}

# Primary knowledge point of a conversation: confirmed before proposed, then lowest id.
PRIMARY_POINT = """
    SELECT cp.conversation_id, kp.point_id, kp.point_name
    FROM conversation_point cp JOIN knowledge_point kp ON kp.point_id = cp.point_id
    WHERE cp.status IN ('CONFIRMED','PROPOSED')
      AND kp.point_id = (SELECT cp2.point_id FROM conversation_point cp2
                         WHERE cp2.conversation_id = cp.conversation_id AND cp2.status IN ('CONFIRMED','PROPOSED')
                         ORDER BY cp2.status = 'CONFIRMED' DESC, cp2.point_id LIMIT 1)
"""

ANNOTATION_ITEMS = f"""
    SELECT CASE a.annotation_type WHEN 'QUESTION' THEN 'question' ELSE 'correction' END AS kind,
           a.annotation_id AS id, a.content, a.is_resolved, a.created_at, a.updated_at,
           m.message_id, m.seq_no, m.speaker, c.conversation_id, c.title, co.course_id, co.course_name,
           pp.point_id, pp.point_name,
           EXISTS (SELECT 1 FROM review_card r WHERE r.message_id = m.message_id AND r.owner_id = c.owner_id) AS has_card,
           NULL AS lapse_count, NULL AS review_count, NULL AS due_at, NULL AS card_status, NULL AS overdue
    FROM annotation a
    JOIN message m ON m.message_id = a.message_id
    JOIN conversation c ON c.conversation_id = m.conversation_id
    JOIN course co ON co.course_id = c.course_id
    LEFT JOIN ({PRIMARY_POINT}) pp ON pp.conversation_id = c.conversation_id
    WHERE c.owner_id = ? AND a.annotation_type IN ('QUESTION','CORRECTION')
"""

CARD_ITEMS = f"""
    SELECT 'card' AS kind, r.review_card_id AS id, r.question AS content,
           (r.review_count >= {MASTERED_STREAK} OR r.status = 'COMPLETED') AS is_resolved,
           (SELECT MIN(l.reviewed_at) FROM review_log l WHERE l.review_card_id = r.review_card_id AND l.grade = 'wrong') AS created_at,
           r.last_reviewed_at AS updated_at,
           m.message_id, m.seq_no, m.speaker, c.conversation_id, c.title, co.course_id, co.course_name,
           pp.point_id, pp.point_name, 1 AS has_card,
           r.lapse_count, r.review_count, r.due_at, r.status AS card_status,
           (r.status = 'ACTIVE' AND r.due_at <= CURRENT_TIMESTAMP) AS overdue
    FROM review_card r
    JOIN message m ON m.message_id = r.message_id
    JOIN conversation c ON c.conversation_id = m.conversation_id
    JOIN course co ON co.course_id = c.course_id
    LEFT JOIN ({PRIMARY_POINT}) pp ON pp.conversation_id = c.conversation_id
    WHERE r.owner_id = ? AND c.owner_id = r.owner_id AND r.lapse_count > 0 AND r.status != 'SUSPENDED'
"""


class CardFromMistake(BaseModel):
    question: str | None = None
    answer: str | None = None


def create_router(backend: Any) -> APIRouter:
    router = APIRouter()

    def collect(db, course_id: int | None) -> list[dict[str, Any]]:
        args: list[Any] = [backend.DEMO_USER_ID]
        rows = [dict(r) for r in db.execute(ANNOTATION_ITEMS + (" AND c.course_id = ?" if course_id else ""), args + ([course_id] if course_id else []))]
        rows += [dict(r) for r in db.execute(CARD_ITEMS + (" AND c.course_id = ?" if course_id else ""), args + ([course_id] if course_id else []))]
        for row in rows:
            row["is_resolved"] = bool(row["is_resolved"])
            row["has_card"] = bool(row["has_card"])
            row["overdue"] = bool(row["overdue"]) if row["overdue"] is not None else False
        return rows

    @router.get("/api/mistakes")
    def mistakes(
        status: Literal["open", "mastered", "all"] = "open",
        kind: Literal["question", "correction", "card"] | None = None,
        course_id: int | None = None,
    ) -> dict[str, Any]:
        with backend.connect() as db:
            items = collect(db, course_id)
            week = {
                "new": db.execute(
                    "SELECT (SELECT COUNT(*) FROM annotation a JOIN message m USING(message_id) JOIN conversation c USING(conversation_id)"
                    "        WHERE c.owner_id=? AND a.annotation_type IN ('QUESTION','CORRECTION') AND a.created_at >= datetime('now','-7 days'))"
                    " + (SELECT COUNT(*) FROM review_card r WHERE r.owner_id=? AND (SELECT MIN(l.reviewed_at) FROM review_log l"
                    "        WHERE l.review_card_id=r.review_card_id AND l.grade='wrong') >= datetime('now','-7 days'))",
                    (backend.DEMO_USER_ID, backend.DEMO_USER_ID)).fetchone()[0],
                "mastered": db.execute(
                    "SELECT (SELECT COUNT(*) FROM annotation a JOIN message m USING(message_id) JOIN conversation c USING(conversation_id)"
                    "        WHERE c.owner_id=? AND a.annotation_type IN ('QUESTION','CORRECTION') AND a.is_resolved=1 AND a.updated_at >= datetime('now','-7 days'))"
                    " + (SELECT COUNT(DISTINCT l.review_card_id) FROM review_log l JOIN review_card r USING(review_card_id)"
                    "        WHERE r.owner_id=? AND r.lapse_count>0 AND l.streak_after=? AND l.reviewed_at >= datetime('now','-7 days'))",
                    (backend.DEMO_USER_ID, backend.DEMO_USER_ID, MASTERED_STREAK)).fetchone()[0],
            }
        open_items = [i for i in items if not i["is_resolved"]]
        summary = {
            "question": sum(i["kind"] == "question" for i in open_items),
            "correction": sum(i["kind"] == "correction" for i in open_items),
            "card": sum(i["kind"] == "card" for i in open_items),
            "overdue": sum(i["overdue"] for i in open_items),
            "points": len({(i["course_id"], i["point_id"]) for i in open_items}),
            "mastered": sum(i["is_resolved"] for i in items),
            "week": week,
        }
        # Priority always reflects what is still open, whatever the list below is filtered to.
        priority: dict[tuple, int] = {}
        for item in open_items:
            key = (item["course_id"], item["point_id"])
            priority[key] = priority.get(key, 0) + WEIGHTS[item["kind"]] + (WEIGHTS["overdue"] if item["overdue"] else 0)
        shown = [i for i in items if (status == "all" or i["is_resolved"] == (status == "mastered")) and (kind is None or i["kind"] == kind)]
        groups: dict[tuple, dict[str, Any]] = {}
        for item in shown:
            key = (item["course_id"], item["point_id"])
            group = groups.setdefault(key, {"course_id": item["course_id"], "course_name": item["course_name"], "point_id": item["point_id"],
                                            "point_name": item["point_name"] or "未归类", "priority": priority.get(key, 0),
                                            "counts": {"question": 0, "correction": 0, "card": 0}, "items": []})
            group["counts"][item["kind"]] += 1
            group["items"].append(item)
        order = {"question": 0, "correction": 1, "card": 2}
        for group in groups.values():
            # Newest first, then (stable) open before mastered, overdue first, doubts before AI errors before cards.
            group["items"].sort(key=lambda i: i["created_at"] or "", reverse=True)
            group["items"].sort(key=lambda i: (i["is_resolved"], not i["overdue"], order[i["kind"]], -(i["lapse_count"] or 0)))
        ranked = sorted(groups.values(), key=lambda g: (-g["priority"], g["point_id"] is None, g["course_name"], g["point_name"]))
        return {"summary": summary, "groups": ranked, "total": len(shown), "rule": {"weights": WEIGHTS, "mastered_streak": MASTERED_STREAK}}

    @router.get("/api/mistakes/weekly")
    def weekly(weeks: int = Query(12, ge=1, le=52)) -> list[dict[str, Any]]:
        """Per week (Monday start): mistakes written down, mistakes settled, reviews and how many were right."""
        sql = """
            WITH RECURSIVE week(start) AS (
                SELECT date('now', '-6 days', 'weekday 1', :back)
                UNION ALL SELECT date(start, '+7 days') FROM week WHERE start < date('now', '-6 days', 'weekday 1')
            ),
            noted AS (
                SELECT date(a.created_at, '-6 days', 'weekday 1') AS wk, COUNT(*) AS n FROM annotation a
                JOIN message m USING(message_id) JOIN conversation c USING(conversation_id)
                WHERE c.owner_id = :owner AND a.annotation_type IN ('QUESTION','CORRECTION') GROUP BY wk
            ),
            settled AS (
                SELECT date(a.updated_at, '-6 days', 'weekday 1') AS wk, COUNT(*) AS n FROM annotation a
                JOIN message m USING(message_id) JOIN conversation c USING(conversation_id)
                WHERE c.owner_id = :owner AND a.annotation_type IN ('QUESTION','CORRECTION') AND a.is_resolved = 1 GROUP BY wk
            ),
            reviews AS (
                SELECT date(l.reviewed_at, '-6 days', 'weekday 1') AS wk, COUNT(*) AS n, SUM(l.grade = 'correct') AS correct
                FROM review_log l JOIN review_card r USING(review_card_id) WHERE r.owner_id = :owner GROUP BY wk
            )
            SELECT week.start AS week_start, COALESCE(noted.n, 0) AS noted, COALESCE(settled.n, 0) AS settled,
                   COALESCE(reviews.n, 0) AS reviews, COALESCE(reviews.correct, 0) AS correct
            FROM week LEFT JOIN noted ON noted.wk = week.start LEFT JOIN settled ON settled.wk = week.start
            LEFT JOIN reviews ON reviews.wk = week.start ORDER BY week.start
        """
        # '-6 days' then 'weekday 1' (next Monday, or the same day if it is one) gives the Monday the week starts on.
        with backend.connect() as db:
            return [dict(r) for r in db.execute(sql, {"owner": backend.DEMO_USER_ID, "back": f"-{7 * (weeks - 1)} days"})]

    @router.post("/api/mistakes/{annotation_id}/card")
    def card_from_mistake(annotation_id: int, payload: CardFromMistake) -> dict[str, Any]:
        """Turn a doubt or an AI mistake into a review card on the same message (immediately due)."""
        with backend.connect() as db:
            row = db.execute(
                "SELECT a.annotation_type,a.content,m.message_id,m.content AS message FROM annotation a JOIN message m USING(message_id)"
                " JOIN conversation c USING(conversation_id) WHERE a.annotation_id=? AND c.owner_id=? AND a.annotation_type IN ('QUESTION','CORRECTION')",
                (annotation_id, backend.DEMO_USER_ID)).fetchone()
        if not row:
            raise HTTPException(404, "错题不存在或无权访问")
        question = (payload.question or "").strip() or (row["content"] if row["annotation_type"] == "QUESTION" else f"这里 AI 哪里说错了？正确的是什么？（{row['content'][:40]}…）")
        answer = (payload.answer or "").strip() or (row["content"] if row["annotation_type"] == "CORRECTION" else row["message"])
        return backend.create_review(backend.ReviewInput(message_id=row["message_id"], question=question, answer=answer))

    return router
