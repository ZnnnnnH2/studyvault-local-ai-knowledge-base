import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from support import dump_jsonl, write_bundle


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studyvault-tests-')
        self.directory = Path(self.temporary.name)
        self.database_patch = patch.object(app, 'DB_PATH', self.directory / 'studyvault.db')
        self.database_patch.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.database_patch.stop)
        self.manifest, self.annotations = write_bundle(self.directory)

    def restore(self):
        return app.restore_bundled(self.manifest, self.annotations)

    def table_counts(self):
        with app.connect() as db:
            return {r[0]: db.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
                    for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class RestoreTest(DatabaseTest):
    def test_full_bundled_restore_and_idempotence(self):
        # Expected counts are derived from the shipped (synthetic) demo bundle, not hardcoded,
        # so this stays correct if the demo set is regenerated.
        conversations, _ = app.read_bundle(app.BASE_DIR / 'staging' / 'structured_manifest_final.jsonl',
                                           app.BASE_DIR / 'staging' / 'manual_annotations_all.jsonl')
        expected_conversations = len(conversations)
        expected_messages = sum(c['message_count'] for c in conversations)
        result = app.restore_bundled()
        self.assertEqual(result['counts']['conversation'], expected_conversations)
        self.assertEqual(result['counts']['message'], expected_messages)
        self.assertEqual(result['counts']['conversation_annotation'], expected_conversations)
        self.assertEqual(result['integrity_check'], 'ok')
        self.assertEqual(result['foreign_key_violations'], 0)
        again = app.restore_bundled()
        self.assertEqual(again['counts'], result['counts'])
        self.assertEqual(again['inserted'], 0)
        self.assertEqual(again['skipped'], expected_conversations)
        self.assertTrue(all(again[key] == 0 for key in ('messages_added', 'summaries_added', 'annotations_added', 'tag_links_added', 'point_links_added')))

    def test_restore_keeps_existing_notes_cards_and_nonempty_metadata(self):
        self.restore()
        with app.connect() as db:
            mid = db.execute('SELECT MIN(message_id) FROM message').fetchone()[0]
            cid = db.execute('SELECT conversation_id FROM message WHERE message_id=?', (mid,)).fetchone()[0]
            db.execute("UPDATE conversation SET summary='自己的摘要' WHERE conversation_id=?", (cid,))
            db.execute("UPDATE conversation_annotation SET intent='自己的标注' WHERE conversation_id=?", (cid,))
            db.execute("INSERT INTO annotation(message_id,author_id,annotation_type,content) VALUES(?,1,'NOTE','我的笔记')", (mid,))
            db.execute("INSERT INTO review_card(message_id,owner_id,question,answer,due_at,review_count,interval_days) VALUES(?,1,'Q','A','2030-01-01',3,14)", (mid,))
            original = dict(db.execute('SELECT * FROM review_card').fetchone())
        self.restore()
        with app.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM review_card').fetchone()), original)
            self.assertEqual(db.execute('SELECT content FROM annotation').fetchone()[0], '我的笔记')
            self.assertEqual(db.execute('SELECT summary FROM conversation WHERE conversation_id=?', (cid,)).fetchone()[0], '自己的摘要')
            self.assertEqual(db.execute('SELECT intent FROM conversation_annotation WHERE conversation_id=?', (cid,)).fetchone()[0], '自己的标注')

    def test_restore_fills_only_missing_summary_annotations_and_links(self):
        self.restore()
        with app.connect() as db:
            cid = db.execute('SELECT MIN(conversation_id) FROM conversation').fetchone()[0]
            db.execute('UPDATE conversation SET summary=NULL WHERE conversation_id=?', (cid,))
            for table in ('conversation_annotation', 'conversation_tag', 'conversation_point'):
                db.execute(f'DELETE FROM {table} WHERE conversation_id=?', (cid,))
        result = self.restore()
        self.assertEqual([result[k] for k in ('inserted', 'summaries_added', 'annotations_added', 'tag_links_added', 'point_links_added')], [0, 1, 1, 2, 1])

    def test_restore_reuses_import_job_when_conversation_was_deleted(self):
        self.restore()
        with app.connect() as db:
            db.execute('DELETE FROM conversation WHERE conversation_id=(SELECT MIN(conversation_id) FROM conversation)')
        result = self.restore()
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['counts']['message'], 4)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM import_job').fetchone()[0], 2)

    def test_conflict_rolls_back_all_previous_rows_in_batch(self):
        self.restore()
        rows = [json.loads(line) for line in self.manifest.read_text(encoding='utf-8').splitlines()]
        notes = [json.loads(line) for line in self.annotations.read_text(encoding='utf-8').splitlines()]
        new, note = copy.deepcopy(rows[0]), copy.deepcopy(notes[0])
        new['source_id'] = note['source_id'] = 'new-before-conflict'
        rows[1]['messages'][0]['content'] = 'different content'
        dump_jsonl(self.manifest, [new, *rows])
        dump_jsonl(self.annotations, [note, *notes])
        before = self.table_counts()
        with self.assertRaisesRegex(ValueError, '恢复冲突'):
            self.restore()
        self.assertEqual(self.table_counts(), before)

    def test_invalid_bundles_are_rejected_before_database_is_created(self):
        base_rows = [json.loads(line) for line in self.manifest.read_text(encoding='utf-8').splitlines()]
        base_notes = [json.loads(line) for line in self.annotations.read_text(encoding='utf-8').splitlines()]
        cases = ('missing_file', 'bad_json', 'duplicate_session', 'duplicate_annotation',
                 'missing_annotation', 'unknown_annotation', 'wrong_count', 'bad_speaker', 'bad_quality', 'bad_tags')
        for case in cases:
            with self.subTest(case=case):
                rows, notes = copy.deepcopy(base_rows), copy.deepcopy(base_notes)
                if case == 'duplicate_session': rows.append(rows[0])
                if case == 'duplicate_annotation': notes.append(notes[0])
                if case == 'missing_annotation': notes.pop()
                if case == 'unknown_annotation': notes[0]['source_id'] = 'unknown'
                if case == 'wrong_count': rows[0]['message_count'] += 1
                if case == 'bad_speaker': rows[0]['messages'][0]['speaker'] = []
                if case == 'bad_quality': notes[0]['quality'] = []
                if case == 'bad_tags': rows[0]['tags'] = [123]
                dump_jsonl(self.manifest, rows); dump_jsonl(self.annotations, notes)
                if case == 'bad_json': self.manifest.write_text('{invalid\n', encoding='utf-8')
                if case == 'missing_file': self.annotations.unlink()
                with self.assertRaises((ValueError, OSError)):
                    self.restore()
                self.assertFalse(app.DB_PATH.exists())


class ApiWorkflowTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.manifest, self.annotations = write_bundle(self.directory, 25)
        self.restore()
        self.client = self.enterContext(TestClient(app.app))

    def first_message(self):
        item = self.client.get('/api/conversations').json()['items'][0]
        detail = self.client.get(f"/api/conversations/{item['conversation_id']}").json()
        return detail['messages'][0]['message_id'], item['conversation_id']

    def test_pagination_search_filters_and_stable_ties(self):
        first = self.client.get('/api/conversations').json()
        second = self.client.get('/api/conversations?offset=20').json()
        self.assertEqual((first['total'], len(first['items']), len(second['items'])), (25, 20, 5))
        ids = [x['conversation_id'] for x in first['items'] + second['items']]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(set(ids)), 25)
        self.assertEqual(self.client.get('/api/conversations?q=参照完整性').json()['total'], 25)
        self.assertEqual(self.client.get('/api/conversations?q=not-a-match').json()['total'], 0)
        source = next(x['source_id'] for x in self.client.get('/api/sources').json() if x['platform_name'] == 'Pi')
        self.assertEqual(self.client.get(f'/api/conversations?source_id={source}').json()['total'], 0)
        self.assertEqual(self.client.get('/api/conversations?offset=-1').status_code, 422)

    def test_review_is_immediately_due_and_duplicate_preserves_progress(self):
        mid, _ = self.first_message()
        payload = {'message_id': mid, 'question': '外键的作用？', 'answer': '维护参照完整性。'}
        response = self.client.post('/api/reviews', json=payload)
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertEqual(len(self.client.get('/api/reviews/due').json()), 1)
        completed = self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=correct").json()
        self.assertEqual((completed['review_count'], completed['interval_days']), (1, 3))
        self.assertEqual(self.client.get('/api/reviews/due').json(), [])
        repeated = self.client.post('/api/reviews', json={**payload, 'question': '不同的问题'}).json()
        self.assertEqual(repeated, completed)
        wrong = self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=wrong").json()
        self.assertEqual((wrong['review_count'], wrong['interval_days']), (0, 1))

    def test_review_and_annotation_validation_and_ownership(self):
        mid, cid = self.first_message()
        for payload, expected in [({'message_id': mid, 'question': ' ', 'answer': 'a'}, 400),
                                  ({'message_id': -1, 'question': 'q', 'answer': 'a'}, 403)]:
            self.assertEqual(self.client.post('/api/reviews', json=payload).status_code, expected)
        self.assertEqual(self.client.post('/api/annotations', json={'message_id': mid, 'annotation_type': 'INVALID', 'content': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/annotations', json={'message_id': mid, 'annotation_type': 'CORRECTION', 'content': '学习批注'}).status_code, 200)
        detail = self.client.get(f'/api/conversations/{cid}').json()
        self.assertEqual(len(detail['messages'][0]['annotations']), 1)
        self.assertEqual(self.client.get('/api/conversations/999999').status_code, 404)

    def test_statistics_and_exports_match_current_database(self):
        stats = self.client.get('/api/stats').json()
        self.assertEqual(sum(row['conversations'] for row in stats['course']), 25)
        self.assertEqual(sum(row['messages'] for row in stats['course']), 50)
        exported = self.client.get('/api/export/conversations.json').json()
        self.assertEqual(len(exported), 25)
        self.assertEqual(sum(len(row['messages']) for row in exported), 50)
        course = next(row['course_id'] for row in self.client.get('/api/courses').json() if row['course_code'] == 'DB')
        markdown = self.client.get(f'/api/export/course/{course}.md')
        self.assertEqual(markdown.status_code, 200)
        self.assertEqual(markdown.text.count('## SQL 外键学习'), 25)
        self.assertEqual(Path(app.export_structured().path).name, 'structured_manifest_final.jsonl')
        self.assertEqual(Path(app.export_manual_annotations().path).name, 'manual_annotations_all.jsonl')

    def test_other_owner_not_counted_in_knowledge_point_stats(self):
        with app.connect() as db:
            db.execute("INSERT INTO app_user(user_id,role_id,username,display_name) VALUES(2,1,'other','other')")
            original = db.execute('SELECT * FROM conversation LIMIT 1').fetchone()
            job = db.execute("INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status) VALUES(2,?,'other','other','SUCCESS')", (original['source_id'],)).lastrowid
            cid = db.execute("INSERT INTO conversation(owner_id,course_id,source_id,import_job_id,source_conversation_id,title) VALUES(2,?,?,?,'other','other')", (original['course_id'], original['source_id'], job)).lastrowid
            point = db.execute("SELECT point_id FROM knowledge_point WHERE point_name='SQL 与查询'").fetchone()[0]
            db.execute('INSERT INTO conversation_point(conversation_id,point_id) VALUES(?,?)', (cid, point))
        points = self.client.get('/api/stats').json()['points']
        self.assertEqual(next(row['conversations'] for row in points if row['point_name'] == 'SQL 与查询'), 25)

    def test_data_survives_startup_and_foreign_keys_are_enforced(self):
        mid, _ = self.first_message()
        self.client.post('/api/annotations', json={'message_id': mid, 'annotation_type': 'NOTE', 'content': 'persist'})
        app.startup()
        self.assertEqual(self.client.get('/api/summary').json()['counts']['annotations'], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with app.connect() as db:
                db.execute("INSERT INTO annotation(message_id,author_id,annotation_type,content) VALUES(-1,1,'NOTE','invalid')")


if __name__ == '__main__':
    unittest.main()
