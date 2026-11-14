import json
import sqlite3

from fastapi.testclient import TestClient

import app
from test_workflows import DatabaseTest


class AdditionalFeatureTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.restore()
        self.client = self.enterContext(TestClient(app.app))
        self.cid = self.client.get('/api/conversations').json()['items'][0]['conversation_id']
        self.detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.mid = self.detail['messages'][1]['message_id']

    def test_search_returns_matching_message_and_literal_special_characters(self):
        result = self.client.get('/api/conversations', params={'q': '维护参照完整性'}).json()
        self.assertEqual(result['total'], 2)
        for item in result['items']:
            self.assertEqual(item['match_seq_no'], 2)
            self.assertIn('维护参照完整性', item['match_snippet'])
            messages = self.client.get(f"/api/conversations/{item['conversation_id']}").json()['messages']
            self.assertEqual(item['matched_message_id'], messages[1]['message_id'])
        with app.connect() as db:
            db.execute("UPDATE message SET content=content||' 100% _literal' WHERE message_id=?", (self.mid,))
        for query in ('%', '_literal'):
            self.assertEqual(self.client.get('/api/conversations', params={'q': query}).json()['total'], 1)
        self.assertIsNone(self.client.get('/api/conversations').json()['items'][0]['matched_message_id'])

    def test_curation_preserves_original_messages_and_survives_restore(self):
        course_id = next(row['course_id'] for row in self.client.get('/api/courses').json() if row['course_code'] == 'ENG')
        response = self.client.patch(f'/api/conversations/{self.cid}', json={'course_id': course_id, 'summary': '人工校正的摘要', 'tags': ['已确认', '已确认', ' 待练习 ']})
        self.assertEqual(response.status_code, 200)
        self.restore()
        detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.assertEqual(detail['conversation']['course_id'], course_id)
        self.assertEqual(detail['conversation']['summary'], '人工校正的摘要')
        self.assertEqual(sorted(detail['tags']), ['已确认', '待练习'])
        self.assertEqual(detail['messages'], self.detail['messages'])
        self.assertEqual(detail['curation']['fields'], ['course_id', 'summary', 'tags'])
        with app.connect() as db:
            point_courses = [r[0] for r in db.execute('SELECT kp.course_id FROM conversation_point cp JOIN knowledge_point kp USING(point_id) WHERE cp.conversation_id=?', (self.cid,))]
            self.assertTrue(point_courses and all(value == course_id for value in point_courses))
            audit = db.execute("SELECT before_data,after_data FROM audit_log WHERE operation='CURATE'").fetchone()
            self.assertEqual(json.loads(audit['before_data'])['summary'], self.detail['conversation']['summary'])
            self.assertEqual(json.loads(audit['after_data'])['summary'], '人工校正的摘要')

    def test_partial_curation_keeps_manual_tag_deletion_across_restore(self):
        self.assertEqual(self.client.patch(f'/api/conversations/{self.cid}', json={'tags': []}).status_code, 200)
        self.assertEqual(self.client.patch(f'/api/conversations/{self.cid}', json={'summary': '只更新摘要'}).status_code, 200)
        self.restore()
        detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.assertEqual(detail['tags'], [])
        self.assertEqual(detail['curation']['fields'], ['summary', 'tags'])

    def test_curation_rejects_invalid_input_without_partial_writes(self):
        for payload in ({}, {'course_id': 99999, 'tags': []}, {'summary': ' '}, {'tags': [' ']}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(f'/api/conversations/{self.cid}', json=payload).status_code, 400)
                detail = self.client.get(f'/api/conversations/{self.cid}').json()
                self.assertEqual(detail, self.detail)
        self.assertEqual(self.client.patch('/api/conversations/999999', json={'summary': 'x'}).status_code, 404)

    def create_card(self):
        return self.client.post('/api/reviews', json={'message_id': self.mid, 'question': '外键的作用？', 'answer': '维护参照完整性'}).json()

    def test_review_logs_preserve_every_outcome_and_duplicate_creation_adds_no_log(self):
        card = self.create_card()
        rid = card['review_card_id']
        correct = self.client.post(f'/api/reviews/{rid}/complete?grade=correct').json()
        wrong = self.client.post(f'/api/reviews/{rid}/complete?grade=wrong').json()
        self.create_card()
        history = self.client.get('/api/reviews/history', params={'review_card_id': rid}).json()
        self.assertEqual(history['total'], 2)
        latest, earlier = history['items']
        self.assertEqual((latest['grade'], latest['interval_before'], latest['interval_after'], latest['streak_before'], latest['streak_after']), ('wrong', 3, 1, 1, 0))
        self.assertEqual((earlier['due_before'], earlier['due_after']), (card['due_at'], correct['due_at']))
        self.assertEqual(latest['due_after'], wrong['due_at'])
        self.assertEqual(self.client.get('/api/stats').json()['reviews'], {'total': 2, 'correct': 1, 'wrong': 1})
        app.startup()
        self.assertEqual(self.client.get('/api/reviews/history').json()['total'], 2)

    def test_review_update_and_log_write_are_one_transaction(self):
        card = self.create_card()
        with app.connect() as db:
            db.execute("CREATE TRIGGER fail_review_log BEFORE INSERT ON review_log BEGIN SELECT RAISE(ABORT,'controlled log failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=correct")
        with app.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM review_card').fetchone()), card)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_log').fetchone()[0], 0)

    def test_history_pagination_and_other_user_isolation(self):
        rid = self.create_card()['review_card_id']
        for _ in range(25):
            self.client.post(f'/api/reviews/{rid}/complete?grade=correct')
        first = self.client.get('/api/reviews/history').json()
        second = self.client.get('/api/reviews/history?offset=20').json()
        self.assertEqual((first['total'], len(first['items']), len(second['items'])), (25, 20, 5))
        ids = [row['review_log_id'] for row in first['items'] + second['items']]
        self.assertEqual(len(set(ids)), 25)
        with app.connect() as db:
            db.execute("INSERT INTO app_user(user_id,role_id,username,display_name) VALUES(2,1,'other','Other')")
            other_card = db.execute("INSERT INTO review_card(owner_id,message_id,question,answer,due_at) VALUES(2,?,'other','other',datetime('now')) RETURNING review_card_id", (self.mid,)).fetchone()[0]
            db.execute("INSERT INTO review_log(review_card_id,grade,interval_before,interval_after,streak_before,streak_after,due_before,due_after) VALUES(?,'wrong',1,1,0,0,'x','y')", (other_card,))
        self.assertEqual(self.client.get(f'/api/reviews/history?review_card_id={other_card}').status_code, 404)
        self.assertEqual(self.client.get('/api/reviews/history').json()['total'], 25)
        self.assertEqual(self.client.get('/api/stats').json()['reviews']['total'], 25)

    def test_existing_database_gets_empty_log_table_without_fabricating_history(self):
        card = self.create_card()
        with app.connect() as db:
            db.execute('DROP TABLE review_log')
            db.execute('UPDATE review_card SET review_count=3 WHERE review_card_id=?', (card['review_card_id'],))
        app.startup()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT review_count FROM review_card').fetchone()[0], 3)
        self.assertEqual(self.client.get('/api/reviews/history').json()['total'], 0)
