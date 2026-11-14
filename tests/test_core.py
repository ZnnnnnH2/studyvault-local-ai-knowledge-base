import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

import app


class ImporterTest(unittest.TestCase):
    def test_codex_jsonl_parser_keeps_user_and_assistant_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jsonl"
            path.write_text('\n'.join([
                '{"type":"session_meta","payload":{"session_id":"demo-1"}}',
                '{"type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"什么是外键？"}]}}',
                '{"type":"response_item","payload":{"role":"assistant","content":[{"type":"output_text","text":"外键维护参照完整性。"}]}}',
            ]), encoding="utf-8")
            parsed = app.parse_conversation(path, "Codex")
            self.assertEqual(parsed["source_id"], "demo-1")
            self.assertEqual([m["speaker"] for m in parsed["messages"]], ["user", "assistant"])

    def test_course_guess_is_deterministic(self):
        self.assertEqual(app.guess_course("SQL 外键和事务"), "数据库系统概论")
        self.assertEqual(app.guess_course("普通日常记录"), "通用学习")

    def test_parser_drops_internal_context_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jsonl"
            path.write_text("\n".join([
                '{"type":"session_meta","payload":{"session_id":"clean-1"}}',
                '{"type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"<environment_context>secret path</environment_context>真实问题"}]}}',
                '{"type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"Current runtime context."}]}}',
                '{"type":"response_item","payload":{"role":"assistant","content":[{"type":"output_text","text":"回答"}]}}',
            ]), encoding="utf-8")
            parsed = app.parse_conversation(path, "Codex")
            self.assertEqual([m["content"] for m in parsed["messages"]], ["真实问题", "回答"])

    def test_deepseek_parser_keeps_text_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("\n".join([
                '{"type":"session","id":"deep-1","createdAt":1700000000000}',
                '{"type":"session/title","data":{"title":"示例会话"}}',
                '{"type":"user/message","time":1700000001000,"data":{"content":[{"type":"text","text":"账号是 alice password: secret"}]}}',
                '{"type":"assistant/message","time":1700000002000,"data":{"message":{"role":"assistant","content":[{"type":"reasoning","text":"内部推理"},{"type":"text","text":"已处理"}]}}}',
            ]), encoding="utf-8")
            parsed = app.parse_conversation(path, "DeepSeek")
            self.assertEqual(parsed["source_id"], "deep-1")
            self.assertEqual([m["speaker"] for m in parsed["messages"]], ["user", "assistant"])
            self.assertIn("<已脱敏>", parsed["messages"][0]["content"])
            self.assertNotIn("secret", parsed["messages"][0]["content"])
            self.assertEqual(parsed["messages"][1]["content"], "已处理")
            self.assertEqual(app.redact_sensitive("AIIC_DASH_KEY=sk-abcdefghijklmnop"), "AIIC_DASH_KEY=<已脱敏>")
            self.assertEqual(app.redact_sensitive("Authorization: Bearer tp-abcdefghijklmnop"), "Authorization=<已脱敏>")

    def test_learning_loop_records_review_history_and_searches_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            original_db = app.DB_PATH
            app.DB_PATH = Path(directory) / "studyvault.db"
            try:
                app.init_db()
                with app.connect() as db:
                    source_id = db.execute("SELECT source_id FROM conversation_source WHERE platform_name='Codex'").fetchone()[0]
                    course_id = db.execute("SELECT course_id FROM course WHERE course_code='DB'").fetchone()[0]
                    job = db.execute("INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status) VALUES(1,?,?,?,'SUCCESS')", (source_id, "test.jsonl", "search-1")).lastrowid
                    conversation_id = db.execute("INSERT INTO conversation(owner_id,course_id,source_id,import_job_id,source_conversation_id,title) VALUES(1,?,?,?,?,?)", (course_id, source_id, job, "search-1", "外键复习")).lastrowid
                    message_id = db.execute("INSERT INTO message(conversation_id,seq_no,speaker,content) VALUES(?,?,?,?)", (conversation_id, 1, "assistant", "外键维护参照完整性")).lastrowid
                    db.execute("INSERT INTO review_card(message_id,owner_id,question,answer,due_at) VALUES(1,1,?,?,datetime('now','-1 day'))", ("什么是外键？", "维护参照完整性"))
                with TestClient(app.app) as client:
                    result = client.get("/api/search?q=外键")
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["items"][0]["conversation_id"], conversation_id)
                    result = client.post("/api/annotations", json={"message_id": message_id, "annotation_type": "QUESTION", "content": "为什么需要外键？"})
                    self.assertEqual(result.status_code, 200)
                    annotation_id = result.json()["annotation_id"]
                    result = client.post(f"/api/annotations/{annotation_id}/resolve")
                    self.assertTrue(result.json()["is_resolved"])
                    result = client.post("/api/reviews/1/complete?grade=wrong")
                    self.assertEqual(result.status_code, 200)
                with app.connect() as db:
                    event = db.execute("SELECT grade,interval_after FROM review_log WHERE review_card_id=1").fetchone()
                    self.assertEqual(tuple(event), ("wrong", 1))
            finally:
                app.DB_PATH = original_db


if __name__ == "__main__":
    unittest.main()
