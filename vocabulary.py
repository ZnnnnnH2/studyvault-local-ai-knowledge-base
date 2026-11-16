"""Tag and knowledge-point vocabulary.

The application keeps two managed vocabularies: free-form tags and
course-scoped knowledge points.  Both need the same behaviour:

* show existing terms before creating a new one;
* reuse an existing term for exactly the same spelling (including case and
  full-width variants);
* refuse an implicit near-duplicate such as ``外键`` / ``外键约束`` and return
  the existing candidates so the caller can choose one or explicitly confirm
  the new term;
* rename and merge terms without losing conversation links, keeping the old
  spelling as an alias so a later snapshot restore resolves to the surviving
  row instead of recreating the duplicate.

The module contains the small matching engine plus its FastAPI router.  The
import and restore paths in ``app.py`` only use ``ensure_tag`` / ``ensure_point``
so they never prompt or fail on a near-duplicate.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

MAX_TERM_LENGTH = 50
SIMILARITY_THRESHOLD = 0.82
# Normalisation is only used for comparison; the stored spelling keeps spaces
# and punctuation.  Full-width characters are folded, and obvious separators
# are ignored so "SQL 与查询" and "SQL与查询" are treated as the same term.
_COMPARE_SEPARATORS = re.compile(r"[\s\u3000\-_/·,，、.。:：;；()（）\[\]【】]+")
_RELATION_RANK = {"exact": 0, "similar": 1, "contains": 2, "all": 3}


def normalize_term(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lstrip("#").strip()
    text = _COMPARE_SEPARATORS.sub("", text)
    return text.casefold()


def clean_term(value: str, *, truncate: bool = False) -> str:
    """Return the display spelling, raising ``ValueError`` for invalid input."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.lstrip("#").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise ValueError("名称不能为空")
    if len(text) > MAX_TERM_LENGTH:
        if truncate:
            text = text[:MAX_TERM_LENGTH].rstrip()
        else:
            raise ValueError(f"名称不能超过 {MAX_TERM_LENGTH} 个字符")
    return text


def term_relation(query: str, candidate: str) -> tuple[str, float] | None:
    """Classify how ``query`` relates to ``candidate``.

    ``exact`` and ``similar`` are the two states that need different UI
    treatment.  ``contains`` covers a short query such as "外" while the user is
    still typing; it is a suggestion, not a duplicate warning.
    """
    left, right = normalize_term(query), normalize_term(candidate)
    if not left or not right:
        return None
    if left == right:
        return "exact", 1.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 2 and shorter in longer:
        return "similar", 0.95 if shorter == left else 0.9
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if ratio >= SIMILARITY_THRESHOLD:
        return "similar", ratio
    if left in right or right in left:
        return "contains", min(len(left), len(right)) / max(len(left), len(right))
    return None


def _term_name(kind: str, row: dict[str, Any]) -> str:
    return row["tag_name"] if kind == "tag" else row["point_name"]


def _conflict(kind: str, term: str, suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    noun = "标签" if kind == "tag" else "知识点"
    names = "、".join(f"「{_term_name(kind, row)}」" for row in suggestions[:3])
    return {
        "code": "SIMILAR_EXISTS",
        "kind": kind,
        "term": term,
        "message": f"已有相近{noun}{names}。请先选择已有项；如确需新建，请确认后再创建。",
        "suggestions": suggestions[:5],
    }


def _name_conflict(kind: str, term: str, existing: dict[str, Any]) -> dict[str, Any]:
    noun = "标签" if kind == "tag" else "知识点"
    return {
        "code": "NAME_EXISTS",
        "kind": kind,
        "term": term,
        "message": f"已有同名{noun}「{_term_name(kind, existing)}」，请直接选择已有项。",
        "suggestions": [existing],
    }


def _tag_rows(db: sqlite3.Connection, owner_id: int) -> list[dict[str, Any]]:
    rows = [dict(row) for row in db.execute(
        """SELECT t.tag_id,t.tag_name,COUNT(DISTINCT c.conversation_id) conversation_count
           FROM tag t
           LEFT JOIN conversation_tag ct ON ct.tag_id=t.tag_id
           LEFT JOIN conversation c ON c.conversation_id=ct.conversation_id AND c.owner_id=?
           WHERE t.owner_id=?
           GROUP BY t.tag_id
           ORDER BY t.tag_name""", (owner_id, owner_id))]
    aliases: dict[int, list[str]] = {}
    for row in db.execute("SELECT tag_id,alias_name FROM tag_alias WHERE owner_id=? ORDER BY alias_name", (owner_id,)):
        aliases.setdefault(int(row["tag_id"]), []).append(row["alias_name"])
    for row in rows:
        row["aliases"] = aliases.get(int(row["tag_id"]), [])
    return rows


def _point_rows(db: sqlite3.Connection, owner_id: int, course_id: int | None = None) -> list[dict[str, Any]]:
    condition, args = "", [owner_id, owner_id, owner_id]
    if course_id is not None:
        condition, args = "AND kp.course_id=?", [owner_id, owner_id, owner_id, course_id]
    rows = [dict(row) for row in db.execute(
        f"""SELECT kp.point_id,kp.course_id,kp.point_name,kp.parent_id,co.course_name,
                   COUNT(DISTINCT c.conversation_id) conversation_count,
                   (SELECT COUNT(*) FROM annotation_point ap2
                    JOIN annotation a2 ON a2.annotation_id=ap2.annotation_id
                    JOIN message m2 ON m2.message_id=a2.message_id
                    JOIN conversation c2 ON c2.conversation_id=m2.conversation_id
                    WHERE ap2.point_id=kp.point_id AND c2.owner_id=?) annotation_count,
                   (SELECT COUNT(*) FROM point_note pn2
                    WHERE pn2.point_id=kp.point_id AND pn2.author_id=?) point_note_count
            FROM knowledge_point kp
            JOIN course co ON co.course_id=kp.course_id
            LEFT JOIN conversation_point cp ON cp.point_id=kp.point_id
            LEFT JOIN conversation c ON c.conversation_id=cp.conversation_id AND c.owner_id=?
            WHERE 1=1 {condition}
            GROUP BY kp.point_id
            ORDER BY co.course_name,kp.point_name""", args)]
    aliases: dict[int, list[str]] = {}
    alias_sql = "SELECT point_id,alias_name FROM knowledge_point_alias WHERE 1=1"
    alias_args: list[Any] = []
    if course_id is not None:
        alias_sql += " AND course_id=?"
        alias_args.append(course_id)
    for row in db.execute(alias_sql + " ORDER BY alias_name", alias_args):
        aliases.setdefault(int(row["point_id"]), []).append(row["alias_name"])
    for row in rows:
        row["aliases"] = aliases.get(int(row["point_id"]), [])
        row["note_count"] = int(row["annotation_count"]) + int(row["point_note_count"])
    return rows


def _best_relation(query: str, row: dict[str, Any], kind: str, aliases: list[str]) -> tuple[dict[str, Any] | None, float]:
    if not normalize_term(query):
        return {"relation": "all", "matched_alias": None}, 0.0
    best: tuple[str, float] | None = term_relation(query, _term_name(kind, row))
    matched_alias = None
    for alias in aliases:
        relation = term_relation(query, alias)
        if relation and (best is None or _RELATION_RANK[relation[0]] < _RELATION_RANK[best[0]]
                         or (_RELATION_RANK[relation[0]] == _RELATION_RANK[best[0]] and relation[1] > best[1])):
            best, matched_alias = relation, alias
    if best is None:
        return None, 0.0
    return {"relation": best[0], "matched_alias": matched_alias}, best[1]


def suggest_tags(db: sqlite3.Connection, owner_id: int, query: str = "", limit: int = 20) -> dict[str, Any]:
    rows = _tag_rows(db, owner_id)
    matches: list[tuple[int, float, dict[str, Any]]] = []
    for row in rows:
        relation = _best_relation(query, row, "tag", row["aliases"])
        if relation[0] is None:
            continue
        item = dict(row)
        item.update(relation[0])
        matches.append((_RELATION_RANK[item["relation"]], -relation[1], item))
    matches.sort(key=lambda item: (item[0], item[1], -item[2]["conversation_count"], item[2]["tag_name"]))
    items = [item[2] for item in matches]
    return {"items": items[:limit], "total": len(items), "query": query}


def suggest_points(db: sqlite3.Connection, owner_id: int, query: str = "", course_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    rows = _point_rows(db, owner_id, course_id)
    matches: list[tuple[int, float, dict[str, Any]]] = []
    for row in rows:
        relation = _best_relation(query, row, "point", row["aliases"])
        if relation[0] is None:
            continue
        item = dict(row)
        item.update(relation[0])
        matches.append((_RELATION_RANK[item["relation"]], -relation[1], item))
    matches.sort(key=lambda item: (item[0], item[1], -item[2]["conversation_count"], item[2]["point_name"]))
    items = [item[2] for item in matches]
    return {"items": items[:limit], "total": len(items), "query": query}


def find_tag(db: sqlite3.Connection, owner_id: int, name: str) -> dict[str, Any] | None:
    """Resolve an exact tag name or alias, then a normalised spelling variant."""
    cleaned = str(name or "").strip().lstrip("#").strip()
    if not cleaned:
        return None
    exact = db.execute("SELECT tag_id,tag_name FROM tag WHERE owner_id=? AND tag_name=?", (owner_id, cleaned)).fetchone()
    if exact:
        return {"tag_id": int(exact["tag_id"]), "tag_name": exact["tag_name"], "matched": "exact", "matched_alias": None}
    alias = db.execute(
        "SELECT t.tag_id,t.tag_name,a.alias_name FROM tag_alias a JOIN tag t ON t.tag_id=a.tag_id "
        "WHERE a.owner_id=? AND a.alias_name=?", (owner_id, cleaned)).fetchone()
    if alias:
        return {"tag_id": int(alias["tag_id"]), "tag_name": alias["tag_name"], "matched": "alias", "matched_alias": alias["alias_name"]}
    key = normalize_term(cleaned)
    if not key:
        return None
    for row in db.execute("SELECT tag_id,tag_name FROM tag WHERE owner_id=?", (owner_id,)):
        if normalize_term(row["tag_name"]) == key:
            return {"tag_id": int(row["tag_id"]), "tag_name": row["tag_name"], "matched": "normalized", "matched_alias": None}
    for row in db.execute(
            "SELECT t.tag_id,t.tag_name,a.alias_name FROM tag_alias a JOIN tag t ON t.tag_id=a.tag_id WHERE a.owner_id=?", (owner_id,)):
        if normalize_term(row["alias_name"]) == key:
            return {"tag_id": int(row["tag_id"]), "tag_name": row["tag_name"], "matched": "alias", "matched_alias": row["alias_name"]}
    return None


def find_point(db: sqlite3.Connection, course_id: int, name: str) -> dict[str, Any] | None:
    cleaned = str(name or "").strip().lstrip("#").strip()
    if not cleaned:
        return None
    exact = db.execute("SELECT point_id,point_name FROM knowledge_point WHERE course_id=? AND point_name=?", (course_id, cleaned)).fetchone()
    if exact:
        return {"point_id": int(exact["point_id"]), "point_name": exact["point_name"], "course_id": course_id,
                "matched": "exact", "matched_alias": None}
    alias = db.execute(
        "SELECT kp.point_id,kp.point_name,a.alias_name FROM knowledge_point_alias a "
        "JOIN knowledge_point kp ON kp.point_id=a.point_id WHERE a.course_id=? AND a.alias_name=?",
        (course_id, cleaned)).fetchone()
    if alias:
        return {"point_id": int(alias["point_id"]), "point_name": alias["point_name"], "course_id": course_id,
                "matched": "alias", "matched_alias": alias["alias_name"]}
    key = normalize_term(cleaned)
    if not key:
        return None
    for row in db.execute("SELECT point_id,point_name FROM knowledge_point WHERE course_id=?", (course_id,)):
        if normalize_term(row["point_name"]) == key:
            return {"point_id": int(row["point_id"]), "point_name": row["point_name"], "course_id": course_id,
                    "matched": "normalized", "matched_alias": None}
    for row in db.execute(
            "SELECT kp.point_id,kp.point_name,a.alias_name FROM knowledge_point_alias a "
            "JOIN knowledge_point kp ON kp.point_id=a.point_id WHERE a.course_id=?", (course_id,)):
        if normalize_term(row["alias_name"]) == key:
            return {"point_id": int(row["point_id"]), "point_name": row["point_name"], "course_id": course_id,
                    "matched": "alias", "matched_alias": row["alias_name"]}
    return None


def ensure_tag(db: sqlite3.Connection, name: str, owner_id: int = 1) -> int:
    """Import/restore helper: reuse aliases and spelling variants, never prompt."""
    found = find_tag(db, owner_id, name)
    if found:
        return int(found["tag_id"])
    cleaned = clean_term(name, truncate=True)
    return int(db.execute("INSERT INTO tag(owner_id,tag_name) VALUES(?,?)", (owner_id, cleaned)).lastrowid)


def ensure_point(db: sqlite3.Connection, course_id: int, name: str) -> int:
    found = find_point(db, course_id, name)
    if found:
        return int(found["point_id"])
    cleaned = clean_term(name, truncate=True)
    return int(db.execute("INSERT INTO knowledge_point(course_id,point_name) VALUES(?,?)", (course_id, cleaned)).lastrowid)


def create_or_get_tag(db: sqlite3.Connection, owner_id: int, name: str, allow_similar: bool = False) -> dict[str, Any]:
    cleaned = clean_term(name)
    found = find_tag(db, owner_id, cleaned)
    if found:
        row = next((item for item in _tag_rows(db, owner_id) if int(item["tag_id"]) == int(found["tag_id"])), None)
        return {**(row or {"tag_id": found["tag_id"], "tag_name": found["tag_name"], "conversation_count": 0, "aliases": []}),
                "created": False, "matched": found["matched"], "matched_alias": found["matched_alias"]}
    suggestions = suggest_tags(db, owner_id, cleaned, limit=5)["items"]
    if suggestions and not allow_similar:
        raise HTTPException(409, _conflict("tag", cleaned, suggestions))
    tag_id = int(db.execute("INSERT INTO tag(owner_id,tag_name) VALUES(?,?)", (owner_id, cleaned)).lastrowid)
    return {"tag_id": tag_id, "tag_name": cleaned, "conversation_count": 0, "aliases": [],
            "created": True, "matched": "new", "matched_alias": None}


def create_or_get_point(db: sqlite3.Connection, owner_id: int, course_id: int, name: str, allow_similar: bool = False) -> dict[str, Any]:
    cleaned = clean_term(name)
    found = find_point(db, course_id, cleaned)
    if found:
        row = next((item for item in _point_rows(db, owner_id, course_id) if int(item["point_id"]) == int(found["point_id"])), None)
        return {**(row or {"point_id": found["point_id"], "point_name": found["point_name"], "course_id": course_id,
                           "conversation_count": 0, "aliases": []}),
                "created": False, "matched": found["matched"], "matched_alias": found["matched_alias"]}
    suggestions = suggest_points(db, owner_id, cleaned, course_id, limit=5)["items"]
    if suggestions and not allow_similar:
        raise HTTPException(409, _conflict("point", cleaned, suggestions))
    point_id = int(db.execute("INSERT INTO knowledge_point(course_id,point_name) VALUES(?,?)", (course_id, cleaned)).lastrowid)
    return {"point_id": point_id, "point_name": cleaned, "course_id": course_id, "conversation_count": 0, "aliases": [],
            "created": True, "matched": "new", "matched_alias": None}


def rename_tag(db: sqlite3.Connection, owner_id: int, tag_id: int, name: str, allow_similar: bool = False) -> dict[str, Any]:
    current = db.execute("SELECT tag_id,tag_name FROM tag WHERE tag_id=? AND owner_id=?", (tag_id, owner_id)).fetchone()
    if not current:
        raise HTTPException(404, "标签不存在")
    cleaned = clean_term(name)
    if normalize_term(cleaned) == normalize_term(current["tag_name"]):
        return {"tag_id": tag_id, "tag_name": current["tag_name"], "conversation_count": 0, "aliases": [], "renamed": False}
    found = find_tag(db, owner_id, cleaned)
    if found and int(found["tag_id"]) != int(tag_id):
        raise HTTPException(409, _name_conflict("tag", cleaned, {"tag_id": found["tag_id"], "tag_name": found["tag_name"], "conversation_count": 0}))
    suggestions = [item for item in suggest_tags(db, owner_id, cleaned, limit=6)["items"] if int(item["tag_id"]) != int(tag_id)]
    if suggestions and not allow_similar:
        raise HTTPException(409, _conflict("tag", cleaned, suggestions))
    if found and found["matched"] == "alias":
        db.execute("DELETE FROM tag_alias WHERE owner_id=? AND alias_name=?", (owner_id, found["matched_alias"]))
    old_name = current["tag_name"]
    db.execute("INSERT OR IGNORE INTO tag_alias(owner_id,alias_name,tag_id) VALUES(?,?,?)", (owner_id, old_name, tag_id))
    db.execute("UPDATE tag SET tag_name=? WHERE tag_id=?", (cleaned, tag_id))
    row = next((item for item in _tag_rows(db, owner_id) if int(item["tag_id"]) == int(tag_id)), None)
    return {**(row or {"tag_id": tag_id, "tag_name": cleaned, "conversation_count": 0, "aliases": []}), "renamed": True}


def rename_point(db: sqlite3.Connection, owner_id: int, point_id: int, name: str, allow_similar: bool = False) -> dict[str, Any]:
    current = db.execute("SELECT point_id,course_id,point_name FROM knowledge_point WHERE point_id=?", (point_id,)).fetchone()
    if not current:
        raise HTTPException(404, "知识点不存在")
    cleaned = clean_term(name)
    if normalize_term(cleaned) == normalize_term(current["point_name"]):
        return {"point_id": point_id, "course_id": current["course_id"], "point_name": current["point_name"],
                "conversation_count": 0, "aliases": [], "renamed": False}
    found = find_point(db, int(current["course_id"]), cleaned)
    if found and int(found["point_id"]) != int(point_id):
        raise HTTPException(409, _name_conflict("point", cleaned, {"point_id": found["point_id"], "point_name": found["point_name"], "conversation_count": 0}))
    suggestions = [item for item in suggest_points(db, owner_id, cleaned, int(current["course_id"]), limit=6)["items"] if int(item["point_id"]) != int(point_id)]
    if suggestions and not allow_similar:
        raise HTTPException(409, _conflict("point", cleaned, suggestions))
    if found and found["matched"] == "alias":
        db.execute("DELETE FROM knowledge_point_alias WHERE course_id=? AND alias_name=?", (current["course_id"], found["matched_alias"]))
    old_name = current["point_name"]
    db.execute("INSERT OR IGNORE INTO knowledge_point_alias(course_id,alias_name,point_id) VALUES(?,?,?)",
               (current["course_id"], old_name, point_id))
    db.execute("UPDATE knowledge_point SET point_name=? WHERE point_id=?", (cleaned, point_id))
    row = next((item for item in _point_rows(db, owner_id, int(current["course_id"])) if int(item["point_id"]) == int(point_id)), None)
    return {**(row or {"point_id": point_id, "course_id": current["course_id"], "point_name": cleaned,
                       "conversation_count": 0, "aliases": []}), "renamed": True}


def merge_tags(db: sqlite3.Connection, owner_id: int, target_id: int, source_id: int) -> dict[str, Any]:
    if target_id == source_id:
        raise HTTPException(400, "不能把标签合并到自身")
    target = db.execute("SELECT tag_id,tag_name FROM tag WHERE tag_id=? AND owner_id=?", (target_id, owner_id)).fetchone()
    source = db.execute("SELECT tag_id,tag_name FROM tag WHERE tag_id=? AND owner_id=?", (source_id, owner_id)).fetchone()
    if not target or not source:
        raise HTTPException(404, "标签不存在")
    moved = db.execute(
        "INSERT OR IGNORE INTO conversation_tag(conversation_id,tag_id) SELECT conversation_id,? FROM conversation_tag WHERE tag_id=?",
        (target_id, source_id)).rowcount
    db.execute("DELETE FROM conversation_tag WHERE tag_id=?", (source_id,))
    db.execute("UPDATE tag_alias SET tag_id=? WHERE tag_id=?", (target_id, source_id))
    db.execute("INSERT INTO tag_alias(owner_id,alias_name,tag_id) VALUES(?,?,?) "
               "ON CONFLICT(owner_id,alias_name) DO UPDATE SET tag_id=excluded.tag_id",
               (owner_id, source["tag_name"], target_id))
    db.execute("DELETE FROM tag WHERE tag_id=?", (source_id,))
    row = next((item for item in _tag_rows(db, owner_id) if int(item["tag_id"]) == int(target_id)), None)
    return {"merged": True, "target": row, "source_tag_id": source_id, "source_tag_name": source["tag_name"], "moved_links": moved}


def merge_points(db: sqlite3.Connection, owner_id: int, target_id: int, source_id: int) -> dict[str, Any]:
    if target_id == source_id:
        raise HTTPException(400, "不能把知识点合并到自身")
    target = db.execute("SELECT point_id,course_id,point_name,parent_id FROM knowledge_point WHERE point_id=?", (target_id,)).fetchone()
    source = db.execute("SELECT point_id,course_id,point_name,parent_id FROM knowledge_point WHERE point_id=?", (source_id,)).fetchone()
    if not target or not source:
        raise HTTPException(404, "知识点不存在")
    if int(target["course_id"]) != int(source["course_id"]):
        raise HTTPException(400, "只能合并同一课程内的知识点")
    if target["parent_id"] == source_id:
        db.execute("UPDATE knowledge_point SET parent_id=? WHERE point_id=?", (source["parent_id"], target_id))
    db.execute("UPDATE knowledge_point SET parent_id=? WHERE parent_id=? AND point_id<>?", (target_id, source_id, target_id))
    target_links = {int(row["conversation_id"]): dict(row) for row in db.execute(
        "SELECT * FROM conversation_point WHERE point_id=?", (target_id,))}
    source_links = [dict(row) for row in db.execute("SELECT * FROM conversation_point WHERE point_id=?", (source_id,))]
    rank = {"REJECTED": 0, "PROPOSED": 1, "CONFIRMED": 2}
    moved = 0
    for link in source_links:
        existing = target_links.get(int(link["conversation_id"]))
        if existing is None:
            db.execute(
                "INSERT INTO conversation_point(conversation_id,point_id,confidence,source,status,evidence,confirmed_by,confirmed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (link["conversation_id"], target_id, link["confidence"], link["source"], link["status"],
                 link["evidence"], link["confirmed_by"], link["confirmed_at"]))
            moved += 1
            continue
        status = existing["status"]
        confirmed_by, confirmed_at = existing["confirmed_by"], existing["confirmed_at"]
        if rank.get(link["status"], 0) > rank.get(status, 0):
            status = link["status"]
            confirmed_by, confirmed_at = link["confirmed_by"], link["confirmed_at"]
        source_kind, evidence = existing["source"], existing["evidence"]
        if link["source"] == "manual" and source_kind != "manual":
            source_kind, evidence = link["source"], link["evidence"]
        if (status, confirmed_by, confirmed_at, source_kind, evidence) != (
                existing["status"], existing["confirmed_by"], existing["confirmed_at"], existing["source"], existing["evidence"]):
            db.execute(
                "UPDATE conversation_point SET status=?,confirmed_by=?,confirmed_at=?,source=?,evidence=? "
                "WHERE conversation_id=? AND point_id=?",
                (status, confirmed_by, confirmed_at, source_kind, evidence, link["conversation_id"], target_id))
    db.execute("DELETE FROM conversation_point WHERE point_id=?", (source_id,))
    # Notes keep their direct point links; move both message-note links and
    # independent point notes to the surviving point before the source row is
    # deleted (the FKs would otherwise cascade-delete them).
    db.execute("INSERT OR IGNORE INTO annotation_point(annotation_id,point_id,created_at) "
               "SELECT annotation_id,?,created_at FROM annotation_point WHERE point_id=?",
               (target_id, source_id))
    db.execute("DELETE FROM annotation_point WHERE point_id=?", (source_id,))
    moved_notes = db.execute("UPDATE point_note SET point_id=? WHERE point_id=?", (target_id, source_id)).rowcount
    db.execute("UPDATE knowledge_point_alias SET point_id=? WHERE point_id=?", (target_id, source_id))
    db.execute("INSERT INTO knowledge_point_alias(course_id,alias_name,point_id) VALUES(?,?,?) "
               "ON CONFLICT(course_id,alias_name) DO UPDATE SET point_id=excluded.point_id",
               (target["course_id"], source["point_name"], target_id))
    db.execute("DELETE FROM knowledge_point WHERE point_id=?", (source_id,))
    row = next((item for item in _point_rows(db, owner_id, int(target["course_id"])) if int(item["point_id"]) == int(target_id)), None)
    return {"merged": True, "target": row, "source_point_id": source_id, "source_point_name": source["point_name"],
            "moved_links": moved, "moved_notes": moved_notes}


class TagCreate(BaseModel):
    tag_name: str
    allow_similar: bool = False


class TagRename(BaseModel):
    tag_name: str
    allow_similar: bool = False


class TagMergeSource(BaseModel):
    source_tag_id: int


class TagMerge(TagMergeSource):
    target_tag_id: int


class PointCreate(BaseModel):
    course_id: int
    point_name: str
    allow_similar: bool = False


class PointRename(BaseModel):
    point_name: str
    allow_similar: bool = False


class PointMergeSource(BaseModel):
    source_point_id: int


class PointMerge(PointMergeSource):
    target_point_id: int


def create_router(backend: Any) -> APIRouter:
    router = APIRouter()

    def audit(db: sqlite3.Connection, operation: str, table: str, record_id: int | None, after: dict[str, Any], before: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO audit_log(actor_id,operation,table_name,record_id,before_data,after_data) VALUES(?,?,?,?,?,?)",
            (backend.DEMO_USER_ID, operation, table, record_id,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False)))

    @router.get("/api/tags")
    def list_tags(q: str = "", query: str | None = None, limit: int = Query(20, ge=1, le=200)):
        with backend.connect() as db:
            return suggest_tags(db, backend.DEMO_USER_ID, query if query else q, limit)

    @router.get("/api/tags/suggest")
    def list_tag_suggestions(q: str = "", query: str | None = None, limit: int = Query(20, ge=1, le=200)):
        with backend.connect() as db:
            return suggest_tags(db, backend.DEMO_USER_ID, query if query else q, limit)

    @router.post("/api/tags")
    def create_tag(payload: TagCreate):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = create_or_get_tag(db, backend.DEMO_USER_ID, payload.tag_name, payload.allow_similar)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if result["created"]:
                audit(db, "CREATE", "tag", result["tag_id"], {"tag_name": result["tag_name"]})
            return result

    @router.patch("/api/tags/{tag_id}")
    def edit_tag(tag_id: int, payload: TagRename):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = rename_tag(db, backend.DEMO_USER_ID, tag_id, payload.tag_name, payload.allow_similar)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if result.get("renamed"):
                audit(db, "RENAME", "tag", tag_id, {"tag_name": result["tag_name"]})
            return result

    @router.post("/api/tags/{tag_id}/merge")
    def merge_tag(tag_id: int, payload: TagMergeSource):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = merge_tags(db, backend.DEMO_USER_ID, tag_id, payload.source_tag_id)
            audit(db, "MERGE", "tag", tag_id, {"source_tag_id": payload.source_tag_id, "source_tag_name": result["source_tag_name"]})
            return result

    @router.post("/api/tags/merge")
    def merge_tag_generic(payload: TagMerge):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = merge_tags(db, backend.DEMO_USER_ID, payload.target_tag_id, payload.source_tag_id)
            audit(db, "MERGE", "tag", payload.target_tag_id, {"source_tag_id": payload.source_tag_id, "source_tag_name": result["source_tag_name"]})
            return result

    @router.get("/api/knowledge-points/suggest")
    def list_point_suggestions(q: str = "", query: str | None = None, course_id: int | None = None,
                               limit: int = Query(20, ge=1, le=200)):
        with backend.connect() as db:
            if course_id is not None and not db.execute("SELECT 1 FROM course WHERE course_id=?", (course_id,)).fetchone():
                raise HTTPException(404, "课程不存在")
            return suggest_points(db, backend.DEMO_USER_ID, query if query else q, course_id, limit)

    @router.post("/api/knowledge-points")
    def create_point(payload: PointCreate):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM course WHERE course_id=?", (payload.course_id,)).fetchone():
                raise HTTPException(404, "课程不存在")
            try:
                result = create_or_get_point(db, backend.DEMO_USER_ID, payload.course_id, payload.point_name, payload.allow_similar)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if result["created"]:
                audit(db, "CREATE", "knowledge_point", result["point_id"],
                      {"point_name": result["point_name"], "course_id": payload.course_id})
            return result

    @router.patch("/api/knowledge-points/{point_id}")
    def edit_point(point_id: int, payload: PointRename):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = rename_point(db, backend.DEMO_USER_ID, point_id, payload.point_name, payload.allow_similar)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if result.get("renamed"):
                audit(db, "RENAME", "knowledge_point", point_id, {"point_name": result["point_name"]})
            return result

    @router.post("/api/knowledge-points/{point_id}/merge")
    def merge_point(point_id: int, payload: PointMergeSource):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = merge_points(db, backend.DEMO_USER_ID, point_id, payload.source_point_id)
            audit(db, "MERGE", "knowledge_point", point_id,
                  {"source_point_id": payload.source_point_id, "source_point_name": result["source_point_name"]})
            return result

    @router.post("/api/knowledge-points/merge")
    def merge_point_generic(payload: PointMerge):
        with backend.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = merge_points(db, backend.DEMO_USER_ID, payload.target_point_id, payload.source_point_id)
            audit(db, "MERGE", "knowledge_point", payload.target_point_id,
                  {"source_point_id": payload.source_point_id, "source_point_name": result["source_point_name"]})
            return result

    return router
