"""Text2SQL：安全护栏、只读执行与离线兜底（不联网）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import text2sql
from support import write_bundle


class ValidateSqlTest(unittest.TestCase):
    def test_accepts_readonly_select_and_cte(self):
        self.assertEqual(text2sql.validate_sql('SELECT 1'), 'SELECT 1')
        self.assertEqual(text2sql.validate_sql('select * from course;'), 'select * from course')
        self.assertTrue(text2sql.validate_sql('WITH x AS (SELECT 1) SELECT * FROM x').startswith('WITH'))

    def test_strips_markdown_fence(self):
        self.assertEqual(text2sql.validate_sql('```sql\nSELECT 1\n```'), 'SELECT 1')

    def test_rejects_writes_and_multistatement(self):
        for bad in ['DELETE FROM course', 'UPDATE course SET course_name=1',
                    'INSERT INTO course VALUES(9)', 'DROP TABLE course',
                    'CREATE TABLE z(a)', 'PRAGMA table_info(course)',
                    'ATTACH DATABASE ":memory:" AS x', 'SELECT 1; DROP TABLE course',
                    '']:
            with self.assertRaises(ValueError, msg=bad):
                text2sql.validate_sql(bad)


class DatabaseText2SQLTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studyvault-t2s-')
        self.directory = Path(self.temporary.name)
        self.db_patch = patch.object(app, 'DB_PATH', self.directory / 'studyvault.db')
        self.db_patch.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.db_patch.stop)
        manifest, annotations = write_bundle(self.directory)
        app.restore_bundled(manifest, annotations)
        # 强制离线：测试绝不联网，走内置规则模板。
        self.cfg_patch = patch.object(
            text2sql, 'llm_config',
            return_value={'api_key': '', 'base_url': 'https://example.invalid', 'model': 'test', 'samples': 5})
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        self.client = TestClient(app.app)

    def test_readonly_connection_blocks_writes(self):
        # 即使绕过语句校验，只读连接也必须在数据库层拒绝写操作。
        for bad in ['DELETE FROM course', 'UPDATE course SET course_name=1', 'CREATE TABLE z(a)']:
            with self.assertRaises(Exception, msg=bad):
                text2sql.run_readonly(app.DB_PATH, bad)

    def test_readonly_query_returns_rows(self):
        result = text2sql.run_readonly(app.DB_PATH, 'SELECT course_name FROM course')
        self.assertEqual(result['columns'], ['course_name'])
        self.assertTrue(len(result['rows']) >= 1)

    def test_row_limit_marks_truncated(self):
        sql = ('WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n<100) '
               'SELECT n FROM t')
        result = text2sql.run_readonly(app.DB_PATH, sql, limit=5)
        self.assertEqual(len(result['rows']), 5)
        self.assertTrue(result['truncated'])

    def test_schema_endpoint_reports_offline(self):
        response = self.client.get('/api/text2sql/schema')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['llm_enabled'])
        self.assertIn('course', body['schema'])
        self.assertTrue(body['examples'])

    def test_fallback_template_executes_endpoint(self):
        response = self.client.post('/api/text2sql', json={'question': '每门课程各有多少会话和消息？'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['source'], 'fallback')
        self.assertIn('课程', body['columns'])
        self.assertTrue(len(body['rows']) >= 1)
        self.assertEqual(text2sql.validate_sql(body['sql']), body['sql'])

    def test_unmatched_offline_question_returns_422(self):
        response = self.client.post('/api/text2sql', json={'question': '今天北京天气怎么样'})
        self.assertEqual(response.status_code, 422)

    def test_focused_schema_only_includes_requested_tables(self):
        text = text2sql.build_schema_text(['course', 'conversation'])
        self.assertIn('course(', text)
        self.assertIn('conversation(', text)
        self.assertNotIn('review_card(', text)

    def test_schema_uses_real_column_names(self):
        self.assertIn('annotation_type', text2sql.SCHEMA_PROMPT)
        self.assertIn('review_count', text2sql.SCHEMA_PROMPT)
        self.assertNotIn('note_type', text2sql.SCHEMA_PROMPT)
        self.assertNotIn('streak', text2sql.SCHEMA_PROMPT)

    def test_result_signature_ignores_row_and_column_order(self):
        a = {'columns': ['x', 'y'], 'rows': [[1, 'a'], [2, 'b']]}
        b = {'columns': ['y', 'x'], 'rows': [['b', 2], ['a', 1]]}
        self.assertEqual(text2sql.result_signature(a), text2sql.result_signature(b))

    def test_vote_candidates_picks_majority_result(self):
        candidates = [
            'SELECT course_name FROM course ORDER BY course_id',
            'SELECT course_name FROM course ORDER BY course_name',
            'SELECT 1 WHERE 0',
        ]
        voted = text2sql.vote_candidates(candidates, app.DB_PATH)
        self.assertEqual(voted['votes'], 2)
        self.assertEqual(voted['valid'], 3)
        self.assertEqual(voted['tally'], [2, 1])

    def test_c3_endpoint_returns_diagnostics_with_fake_llm(self):
        # 用假的 _chat 走完整 C3 流水线：表召回 → 列召回 → 3 条采样（2 条同结果、1 条执行失败）。
        replies = iter([
            '["course"]',
            '{"course": ["course_name"]}',
            'SELECT course_name FROM course ORDER BY course_id',
            'SELECT course_name FROM course ORDER BY course_name',
            'SELECT nope FROM course',
        ])
        online = {'api_key': 'fake', 'base_url': 'https://example.invalid', 'model': 'test', 'samples': 3}
        with patch.object(text2sql, 'llm_config', return_value=online), \
                patch.object(text2sql, '_chat', side_effect=lambda *a, **k: next(replies)):
            response = self.client.post('/api/text2sql', json={'question': '有哪些课程？', 'method': 'c3'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['method'], 'c3')
        diag = body['c3']
        self.assertEqual(diag['linked_tables'], ['course'])
        self.assertEqual(diag['linked_columns'], {'course': ['course_name']})
        self.assertEqual((diag['samples'], diag['candidates'], diag['valid'], diag['votes']), (3, 3, 2, 2))
        self.assertEqual(diag['tally'], [2])

    def test_baseline_endpoint_has_no_c3_diagnostics(self):
        online = {'api_key': 'fake', 'base_url': 'https://example.invalid', 'model': 'test', 'samples': 3}
        with patch.object(text2sql, 'llm_config', return_value=online), \
                patch.object(text2sql, '_chat', return_value='SELECT course_name FROM course'):
            response = self.client.post('/api/text2sql', json={'question': '有哪些课程？', 'method': 'baseline'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['method'], 'baseline')
        self.assertNotIn('c3', response.json())

    def test_vote_candidates_raises_when_all_fail(self):
        with self.assertRaises(ValueError):
            text2sql.vote_candidates(['SELECT * FROM nonexistent_table'], app.DB_PATH)


if __name__ == '__main__':
    unittest.main()
