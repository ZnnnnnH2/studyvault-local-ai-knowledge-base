"""Regression checks for the learning-workflow features merged on top of the recovery release."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from support import write_bundle


class MergedWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studyvault-merged-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        database_patch = patch.object(app, 'DB_PATH', directory / 'studyvault.db')
        database_patch.start()
        self.addCleanup(database_patch.stop)
        app.restore_bundled(*write_bundle(directory))
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        with app.connect() as db:
            self.cid, self.mid = db.execute('SELECT conversation_id,MIN(message_id) FROM message GROUP BY conversation_id ORDER BY conversation_id LIMIT 1').fetchone()
            self.point_id = db.execute('SELECT point_id FROM conversation_point WHERE conversation_id=?', (self.cid,)).fetchone()[0]

    def test_message_changes_keep_full_text_index_in_step(self):
        with app.connect() as db:
            db.execute("UPDATE message SET content='索引一致性检查' WHERE message_id=?", (self.mid,))
        self.assertEqual(self.client.get('/api/search', params={'q': '索引一致性检查'}).json()['total'], 1)
        with app.connect() as db:
            db.execute('DELETE FROM conversation WHERE conversation_id=?', (self.cid,))
            indexed, messages = db.execute('SELECT (SELECT COUNT(*) FROM message_fts_docsize),(SELECT COUNT(*) FROM message)').fetchone()
        self.assertEqual(indexed, messages)
        self.assertEqual(self.client.get('/api/search', params={'q': '索引一致性检查'}).json()['total'], 0)

    def test_conversation_list_accepts_learning_filters(self):
        self.assertEqual(self.client.get('/api/conversations', params={'annotation_status': 'open'}).json()['total'], 0)
        self.client.post('/api/annotations', json={'message_id': self.mid, 'annotation_type': 'QUESTION', 'content': '为什么？'})
        result = self.client.get('/api/conversations', params={'annotation_status': 'open'}).json()
        self.assertEqual([item['conversation_id'] for item in result['items']], [self.cid])
        self.assertEqual(result['items'][0]['unresolved_count'], 1)
        self.assertEqual(self.client.get('/api/conversations', params={'point_id': self.point_id}).json()['total'], 2)
        self.assertEqual(self.client.get('/api/conversations', params={'review_status': 'due'}).json()['total'], 0)
        self.assertEqual(self.client.get('/api/conversations', params={'review_status': 'bogus'}).status_code, 422)

    def test_point_decision_and_gap_priority_use_review_log(self):
        decided = self.client.post(f'/api/conversations/{self.cid}/points/{self.point_id}', json={'status': 'CONFIRMED'}).json()
        self.assertEqual(decided['status'], 'CONFIRMED')
        card = self.client.post('/api/reviews', json={'message_id': self.mid, 'question': '外键？', 'answer': '参照完整性'}).json()
        self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=wrong")
        gap = next(item for item in self.client.get('/api/stats').json()['gaps'] if item['point_id'] == self.point_id)
        self.assertEqual(gap['wrong_count'], 1)
        self.assertGreaterEqual(gap['priority'], 2)

    def test_notes_are_not_todos_but_questions_can_be_resolved(self):
        note = self.client.post('/api/annotations', json={'message_id': self.mid, 'annotation_type': 'NOTE', 'content': '笔记'}).json()
        question = self.client.post('/api/annotations', json={'message_id': self.mid, 'annotation_type': 'QUESTION', 'content': '疑问'}).json()
        self.assertEqual(self.client.patch(f"/api/annotations/{note['annotation_id']}", json={'is_resolved': True}).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/annotations/{note['annotation_id']}", json={'content': '改过的笔记'}).json()['content'], '改过的笔记')
        self.assertEqual(self.client.patch(f"/api/annotations/{question['annotation_id']}", json={'is_resolved': True}).json()['is_resolved'], 1)

    def test_curation_edits_title_and_course_change_keeps_point_status(self):
        self.client.post(f'/api/conversations/{self.cid}/points/{self.point_id}', json={'status': 'REJECTED'})
        other_course = next(c['course_id'] for c in self.client.get('/api/courses').json() if c['course_id'] != self.client.get(f'/api/conversations/{self.cid}').json()['conversation']['course_id'])
        self.assertEqual(self.client.patch(f'/api/conversations/{self.cid}', json={'title': '  '}).status_code, 400)
        result = self.client.patch(f'/api/conversations/{self.cid}', json={'title': '新标题', 'course_id': other_course}).json()
        self.assertEqual(result['title'], '新标题')
        detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.assertEqual(detail['conversation']['title'], '新标题')
        self.assertEqual([p['status'] for p in detail['points']], ['REJECTED'])
        self.assertIn('title', detail['curation']['fields'])

    def test_backup_from_before_learning_columns_is_accepted(self):
        backup = self.client.get('/api/backup').content
        path = Path(app.DB_PATH).parent / 'old-backup.sqlite3'
        path.write_bytes(backup)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=DELETE')  # keep edits in the uploaded file, not a -wal side file
            for trigger in ('message_fts_ai', 'message_fts_ad', 'message_fts_au'):
                db.execute(f'DROP TRIGGER {trigger}')
            db.execute('DROP TABLE message_fts')
            db.execute('ALTER TABLE conversation_point DROP COLUMN confirmed_at')
            db.commit()
        preview = self.client.post('/api/backup/preview', files={'file': ('old.sqlite3', path.read_bytes())})
        self.assertEqual(preview.status_code, 200, preview.text)
        restored = self.client.post('/api/backup/restore', json={'token': preview.json()['token'], 'confirm': True})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.client.get('/api/search', params={'q': '外键'}).json()['total'], 2)

    def test_backup_with_foreign_trigger_is_rejected(self):
        path = Path(app.DB_PATH).parent / 'evil.sqlite3'
        path.write_bytes(self.client.get('/api/backup').content)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('CREATE TRIGGER evil AFTER INSERT ON annotation BEGIN DELETE FROM message; END')
            db.commit()
        preview = self.client.post('/api/backup/preview', files={'file': ('evil.sqlite3', path.read_bytes())})
        self.assertEqual(preview.status_code, 400)


if __name__ == '__main__':
    unittest.main()
