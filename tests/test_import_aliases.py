"""Rule-based import must resolve renamed and merged knowledge points."""
import json

from fastapi.testclient import TestClient

import app
from test_workflows import DatabaseTest


class ImportPointAliasTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        self.work = Path(self.temporary.name)
        self.client = self.enterContext(TestClient(app.app))

    def codex_file(self, name, question='SQL 查询练习', answer='SELECT 语句用于查询。'):
        path = self.work / f'{name}.jsonl'
        rows = [
            {'type': 'session_meta', 'payload': {'id': name}},
            {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': question}]}},
            {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'type': 'output_text', 'text': answer}]}},
        ]
        path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n', encoding='utf-8')
        return path

    def db_point(self, name='SQL 与查询'):
        with app.connect() as db:
            return dict(db.execute(
                "SELECT kp.point_id,kp.course_id FROM knowledge_point kp JOIN course co USING(course_id) "
                "WHERE co.course_code='DB' AND kp.point_name=?", (name,)).fetchone())

    def imported_point_links(self, conversation_id):
        with app.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT kp.point_id,kp.point_name,cp.status FROM conversation_point cp '
                'JOIN knowledge_point kp USING(point_id) WHERE cp.conversation_id=?', (conversation_id,))]

    def test_import_after_rename_links_the_renamed_point(self):
        point = self.db_point()
        renamed = self.client.patch(f"/api/knowledge-points/{point['point_id']}", json={
            'point_name': 'SQL基础', 'allow_similar': True})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        path = self.codex_file('after-rename')
        imported = app.import_file(path, 'Codex')
        self.assertEqual(imported['status'], 'SUCCESS')
        links = self.imported_point_links(imported['conversation_id'])
        self.assertEqual([(row['point_id'], row['point_name']) for row in links], [(point['point_id'], 'SQL基础')])
        self.assertEqual(links[0]['status'], 'PROPOSED')
        duplicate = app.import_file(path, 'Codex')
        self.assertEqual(duplicate['result_code'], 'SKIPPED_DUPLICATE')

    def test_import_after_merge_links_the_target_point(self):
        source = self.db_point()
        target = self.client.post('/api/knowledge-points', json={
            'course_id': source['course_id'], 'point_name': 'SQL查询', 'allow_similar': True}).json()
        merged = self.client.post(f"/api/knowledge-points/{target['point_id']}/merge", json={
            'source_point_id': source['point_id']})
        self.assertEqual(merged.status_code, 200, merged.text)
        path = self.codex_file('after-merge')
        imported = app.import_file(path, 'Codex')
        links = self.imported_point_links(imported['conversation_id'])
        self.assertEqual([(row['point_id'], row['point_name']) for row in links], [(target['point_id'], 'SQL查询')])
        self.assertEqual(links[0]['status'], 'PROPOSED')
