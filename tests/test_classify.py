"""AI 建议知识点：proposals stay PROPOSED, never override decisions, and respect the course."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import text2sql
from support import write_bundle

ONLINE = {'api_key': 'test', 'base_url': 'http://invalid', 'model': 'fake', 'samples': 1}
OFFLINE = {**ONLINE, 'api_key': ''}


class SuggestPointsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studyvault-classify-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        database = patch.object(app, 'DB_PATH', directory / 'studyvault.db')
        database.start()
        self.addCleanup(database.stop)
        app.restore_bundled(*write_bundle(directory, 2))
        with app.connect() as db:
            db.execute('DELETE FROM conversation_point')
            self.cid = db.execute('SELECT MIN(conversation_id) FROM conversation').fetchone()[0]
        self.client = self.enterContext(TestClient(app.app))

    def reply(self, payload):
        return patch.object(text2sql, '_chat', return_value=json.dumps(payload, ensure_ascii=False))

    def points(self):
        return {p['point_name']: p for p in self.client.get(f'/api/conversations/{self.cid}').json()['points']}

    def test_llm_proposals_are_pending_with_reason_and_capped_new_points(self):
        proposal = {'course': '数据库系统概论', 'points': [
            {'name': '数据库完整性', 'reason': '在讲外键维护参照完整性'},
            {'name': '外键设计模式', 'reason': '新知识点一'},
            {'name': '另一个新点', 'reason': '新知识点二，超出上限'}]}
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply(proposal):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual((result['source'], result['added'], result['course_suggestion']), ('llm', 2, None))
        points = self.points()
        self.assertEqual(set(points), {'数据库完整性', '外键设计模式'})
        self.assertTrue(all(p['status'] == 'PROPOSED' and p['source'] == 'llm' for p in points.values()))
        self.assertEqual(points['数据库完整性']['evidence'], '在讲外键维护参照完整性')

    def test_confirmed_and_rejected_decisions_are_not_overwritten(self):
        with app.connect() as db:
            point = db.execute("SELECT point_id FROM knowledge_point WHERE point_name='数据库完整性'").fetchone()[0]
            db.execute("INSERT INTO conversation_point(conversation_id,point_id,source,status) VALUES(?,?,'manual','REJECTED')", (self.cid, point))
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply({'course': '数据库系统概论', 'points': [{'name': '数据库完整性', 'reason': 'x'}]}):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual(result['added'], 0)
        self.assertEqual(self.points()['数据库完整性']['status'], 'REJECTED')

    def test_old_name_from_the_model_resolves_to_the_renamed_point(self):
        with app.connect() as db:
            point = db.execute("SELECT point_id FROM knowledge_point WHERE point_name='数据库完整性'").fetchone()[0]
            before = db.execute('SELECT COUNT(*) FROM knowledge_point').fetchone()[0]
        renamed = self.client.patch(f'/api/knowledge-points/{point}', json={'point_name': '完整性约束', 'allow_similar': True})
        self.assertEqual(renamed.status_code, 200)
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply({'course': '数据库系统概论', 'points': [{'name': '数据库完整性', 'reason': '旧名'}]}):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual([(p['point_id'], p['point_name'], p['new_point']) for p in result['points']], [(point, '完整性约束', False)])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_point').fetchone()[0], before)

    def test_other_course_only_returns_a_suggestion(self):
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply({'course': '操作系统', 'points': [{'name': '进程与线程', 'reason': '在讲 fork'}]}):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual(result['course_suggestion']['course_name'], '操作系统')
        self.assertEqual((result['added'], self.points()), (0, {}))
        with app.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM knowledge_point kp JOIN course co USING(course_id) WHERE co.course_name='数据库系统概论' AND kp.point_name='进程与线程'").fetchone())

    def test_unknown_course_falls_back_to_current_course(self):
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply({'course': '不存在的课', 'points': [{'name': 'SQL 与查询', 'reason': 'x'}]}):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual((result['course_suggestion'], result['added']), (None, 1))

    def test_offline_uses_keyword_rules(self):
        with patch.object(text2sql, 'llm_config', return_value=OFFLINE):
            result = self.client.post(f'/api/conversations/{self.cid}/suggest-points').json()
        self.assertEqual(result['source'], 'rule')
        self.assertIn('数据库完整性', self.points())  # fixture text mentions 外键 / 参照完整性
        self.assertEqual(self.points()['数据库完整性']['source'], 'rule')

    def test_bad_model_output_is_a_502_without_writes(self):
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), patch.object(text2sql, '_chat', return_value='not json'):
            self.assertEqual(self.client.post(f'/api/conversations/{self.cid}/suggest-points').status_code, 502)
        self.assertEqual(self.points(), {})
        self.assertEqual(self.client.post('/api/conversations/999999/suggest-points').status_code, 404)

    def test_batch_only_touches_unclassified_conversations(self):
        with app.connect() as db:
            other = db.execute('SELECT MAX(conversation_id) FROM conversation').fetchone()[0]
            point = db.execute("SELECT point_id FROM knowledge_point WHERE point_name='SQL 与查询'").fetchone()[0]
            db.execute("INSERT INTO conversation_point(conversation_id,point_id,source,status) VALUES(?,?,'manual','CONFIRMED')", (other, point))
        with patch.object(text2sql, 'llm_config', return_value=ONLINE), self.reply({'course': '数据库系统概论', 'points': [{'name': '数据库完整性', 'reason': 'x'}]}) as chat:
            result = self.client.post('/api/classify/unclassified').json()
        self.assertEqual((result['conversations'], result['proposed'], chat.call_count), (1, 1, 1))


if __name__ == '__main__':
    unittest.main()
