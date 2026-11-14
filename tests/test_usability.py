from contextlib import closing
import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from test_workflows import DatabaseTest


def codex_file(identity='uploaded-new', question='什么是 SQL？'):
    return '\n'.join(json.dumps(row, ensure_ascii=False) for row in [
        {'type': 'session_meta', 'payload': {'id': identity}},
        {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': question}]}},
        {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'SQL 是关系数据库查询语言。'}]}},
    ]).encode('utf-8')


class UsabilityTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.restore()
        self.client = self.enterContext(TestClient(app.app))
        self.cid = self.client.get('/api/conversations').json()['items'][0]['conversation_id']
        self.mid = self.client.get(f'/api/conversations/{self.cid}').json()['messages'][0]['message_id']

    def make_card(self):
        return self.client.post('/api/reviews', json={'message_id': self.mid, 'question': '外键的作用？', 'answer': '参照完整性'}).json()

    def wait_task(self, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.client.get(f'/api/import/tasks/{task_id}').json()
            if result['status'] in {'completed', 'cancelled'}:
                return result
            threading.Event().wait(.02)
        self.fail('Import task did not finish')

    def test_todo_list_filters_resolve_reopen_and_jump_ids(self):
        note_ids = {}
        for kind in ('NOTE', 'QUESTION', 'CORRECTION', 'IMPORTANT'):
            note_ids[kind] = self.client.post('/api/annotations', json={'message_id': self.mid, 'annotation_type': kind, 'content': kind}).json()['annotation_id']
        result = self.client.get('/api/todos').json()
        self.assertEqual(result['total'], 3)
        self.assertTrue(all(row['conversation_id'] == self.cid and row['message_id'] == self.mid for row in result['items']))
        self.assertEqual(self.client.get('/api/todos?annotation_type=QUESTION').json()['total'], 1)
        identifier = note_ids['QUESTION']
        self.assertEqual(self.client.patch(f'/api/annotations/{identifier}', json={'is_resolved': True}).status_code, 200)
        self.assertEqual(self.client.get('/api/todos').json()['total'], 2)
        self.assertEqual(self.client.get('/api/todos?status=resolved').json()['total'], 1)
        self.client.patch(f'/api/annotations/{identifier}', json={'is_resolved': False})
        self.assertEqual(self.client.get('/api/todos').json()['total'], 3)
        self.assertEqual(self.client.patch(f"/api/annotations/{note_ids['NOTE']}", json={'is_resolved': True}).status_code, 404)

    def test_card_management_preserves_history_and_blocks_suspended_review(self):
        card = self.make_card(); rid = card['review_card_id']
        self.client.post(f'/api/reviews/{rid}/complete?grade=correct')
        history = self.client.get('/api/reviews/history').json()['items']
        payload = {'question': '编辑后的问题', 'answer': '编辑后的答案', 'status': 'SUSPENDED', 'due_at': '2030-01-02T00:00:00Z'}
        edited = self.client.patch(f'/api/reviews/cards/{rid}', json=payload).json()
        self.assertEqual((edited['review_count'], edited['interval_days']), (1, 3))
        self.assertEqual(edited['due_at'], '2030-01-02 00:00:00')
        self.assertEqual(self.client.get('/api/reviews/due').json(), [])
        self.assertEqual(self.client.post(f'/api/reviews/{rid}/complete?grade=wrong').status_code, 400)
        self.assertEqual(self.client.get('/api/reviews/history').json()['items'][0]['review_log_id'], history[0]['review_log_id'])
        self.assertEqual(self.client.get('/api/reviews/cards?status=SUSPENDED').json()['total'], 1)
        self.client.patch(f'/api/reviews/cards/{rid}', json={'status': 'ACTIVE', 'due_at': '2020-01-01T00:00:00Z'})
        self.assertEqual(len(self.client.get('/api/reviews/due').json()), 1)
        for invalid in ({'question': ' '}, {'due_at': '2030-01-01T00:00:00'}, {}):
            self.assertEqual(self.client.patch(f'/api/reviews/cards/{rid}', json=invalid).status_code, 400)

    def test_favorites_progress_and_preferences_persist(self):
        self.client.post('/api/library/favorite', json={'conversation_id': self.cid, 'is_favorite': True})
        self.client.post('/api/library/progress', json={'conversation_id': self.cid, 'message_id': self.mid})
        settings = {'view': 'library', 'q': '外键', 'offset': 0, 'favorites_only': True, 'conversation_id': self.cid}
        self.client.post('/api/preferences', json=settings)
        app.startup()
        self.assertEqual(self.client.get('/api/conversations?favorites_only=true').json()['total'], 1)
        self.assertEqual(self.client.get('/api/library/recent').json()[0]['last_message_id'], self.mid)
        self.assertTrue(self.client.get('/api/preferences').json()['favorites_only'])
        self.assertEqual(self.client.get(f'/api/conversations/{self.cid}').json()['reading_state']['is_favorite'], 1)
        other_mid = self.client.get('/api/conversations/1').json()['messages'][0]['message_id']
        if self.cid != 1:
            self.assertEqual(self.client.post('/api/library/progress', json={'conversation_id': self.cid, 'message_id': other_mid}).status_code, 400)

    def test_complete_backup_round_trip_includes_learning_and_safety_backup(self):
        self.client.patch(f'/api/conversations/{self.cid}', json={'summary': '人工摘要', 'tags': ['保留']})
        self.client.post('/api/annotations', json={'message_id': self.mid, 'annotation_type': 'QUESTION', 'content': '需要确认'})
        rid = self.make_card()['review_card_id']
        self.client.post(f'/api/reviews/{rid}/complete?grade=correct')
        self.client.post('/api/library/favorite', json={'conversation_id': self.cid, 'is_favorite': True})
        self.client.post('/api/library/progress', json={'conversation_id': self.cid, 'message_id': self.mid})
        self.client.post('/api/preferences', json={'view': 'library', 'q': '外键', 'favorites_only': True})
        backup = self.client.get('/api/backup')
        self.assertEqual(backup.status_code, 200)
        before = {table: None for table in ('conversation', 'message', 'annotation', 'review_card', 'review_log', 'conversation_state', 'user_preference', 'audit_log')}
        with app.connect() as db:
            for table in before:
                before[table] = [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
            db.execute('DELETE FROM annotation')
            db.execute('DELETE FROM review_log')
            db.execute("UPDATE conversation SET summary='后来修改'")
            db.execute('DELETE FROM user_preference')
        preview = self.client.post('/api/backup/preview', files={'file': ('complete.sqlite3', backup.content)}).json()
        self.assertEqual(preview['counts']['review_log'], 1)
        self.assertEqual(self.client.get(f'/api/conversations/{self.cid}').json()['conversation']['summary'], '后来修改')
        restored = self.client.post('/api/backup/restore', json={'token': preview['token']})
        self.assertEqual(restored.status_code, 200)
        with app.connect() as db:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in db.execute(f'SELECT * FROM {table}')], rows, table)
        safety = self.client.get(restored.json()['safety_backup_url'])
        path = self.directory / 'safety.sqlite3'; path.write_bytes(safety.content)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_log').fetchone()[0], 0)
        self.assertEqual(self.client.post('/api/backup/restore', json={'token': preview['token']}).status_code, 404)

    def test_invalid_backup_does_not_change_current_database(self):
        original = self.client.get('/api/summary').json()
        self.assertEqual(self.client.post('/api/backup/preview', files={'file': ('broken.db', b'broken')}).status_code, 400)
        path = self.directory / 'unrelated.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE unrelated(x)')
        self.assertEqual(self.client.post('/api/backup/preview', files={'file': ('unrelated.db', path.read_bytes())}).status_code, 400)
        self.assertEqual(self.client.get('/api/summary').json(), original)

    def test_upload_preview_selection_results_and_no_early_writes(self):
        response = self.client.post('/api/import/preview', files=[('files', ('new.jsonl', codex_file())), ('files', ('duplicate.jsonl', codex_file('fixture-00'))), ('files', ('bad.jsonl', b'{bad'))])
        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertEqual(preview['counts'], {'NEW': 1, 'DUPLICATE': 1, 'INVALID': 1})
        self.assertEqual(self.client.get('/api/summary').json()['counts']['conversations'], 2)
        self.assertEqual(self.client.post('/api/import/tasks', json={'preview_token': preview['token'], 'selected': [2]}).status_code, 400)
        task = self.client.post('/api/import/tasks', json={'preview_token': preview['token'], 'selected': [0, 1]}).json()
        finished = self.wait_task(task['task_id'])
        self.assertEqual((finished['processed'], finished['imported'], finished['skipped'], finished['failed']), (2, 1, 1, 0))
        self.assertEqual(self.client.get('/api/summary').json()['counts']['conversations'], 3)
        self.assertEqual(self.client.post('/api/import/tasks', json={'preview_token': preview['token'], 'selected': [0]}).status_code, 409)

    def test_malformed_uploads_and_missing_tokens_fail_cleanly(self):
        content = b'{"type":"response_item","payload":[]}'
        preview = self.client.post('/api/import/preview', files={'files': ('malformed.jsonl', content)}).json()
        self.assertEqual(preview['counts']['INVALID'], 1)
        self.assertEqual(self.client.post('/api/import/tasks', json={'preview_token': 'missing', 'selected': [0]}).status_code, 404)
        self.assertEqual(self.client.get('/api/import/tasks/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/import/preview', data={'platform': 'unknown'}, files={'files': ('x.jsonl', codex_file())}).status_code, 400)

    def test_cancel_import_and_block_restore_while_task_running(self):
        backup = self.client.get('/api/backup').content
        backup_token = self.client.post('/api/backup/preview', files={'file': ('backup.db', backup)}).json()['token']
        preview = self.client.post('/api/import/preview', files=[('files', ('a.jsonl', codex_file('new-a'))), ('files', ('b.jsonl', codex_file('new-b')))]).json()
        entered, release = threading.Event(), threading.Event()
        original = app.import_parsed
        def slow(parsed, platform):
            entered.set(); release.wait(6)
            return original(parsed, platform)
        with patch.object(app, 'import_parsed', slow):
            task = self.client.post('/api/import/tasks', json={'preview_token': preview['token'], 'selected': [0, 1]}).json()['task_id']
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.client.post('/api/backup/restore', json={'token': backup_token}).status_code, 409)
                self.client.post(f'/api/import/tasks/{task}/cancel')
            finally:
                release.set()
            finished = self.wait_task(task)
        self.assertEqual((finished['status'], finished['processed'], finished['imported']), ('cancelled', 1, 1))

    def test_foreign_owner_mutations_and_backup_token_binding(self):
        backup = self.client.get('/api/backup').content
        token = self.client.post('/api/backup/preview', files={'file': ('backup.db', backup)}).json()['token']
        original = app.DB_PATH
        try:
            app.DB_PATH = self.directory / 'different.db'
            self.assertEqual(self.client.post('/api/backup/restore', json={'token': token}).status_code, 404)
        finally:
            app.DB_PATH = original
        with app.connect() as db:
            db.execute("INSERT INTO app_user(user_id,role_id,username,display_name) VALUES(2,1,'second','Second')")
            db.execute('UPDATE conversation SET owner_id=2 WHERE conversation_id=?', (self.cid,))
        self.assertEqual(self.client.post('/api/library/favorite', json={'conversation_id': self.cid, 'is_favorite': True}).status_code, 404)
        self.assertEqual(self.client.post('/api/library/progress', json={'conversation_id': self.cid, 'message_id': self.mid}).status_code, 404)

    def test_utf8_bom_agent_upload_and_platform_detection(self):
        files = [('files', ('bom.jsonl', b'\xef\xbb\xbf' + codex_file('bom-session'))),
                 ('files', ('gemini.json', json.dumps({'sessionId': 'gemini-new', 'messages': [{'type': 'user', 'content': '问题'}, {'type': 'gemini', 'content': '答案'}]}).encode()))]
        preview = self.client.post('/api/import/preview', files=files).json()
        self.assertEqual(preview['counts']['NEW'], 2)
        self.assertEqual([r['platform'] for r in preview['candidates']], ['Codex', 'Gemini CLI'])
