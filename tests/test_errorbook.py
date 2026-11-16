"""错题本 API and the bundled semester of demo activity."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from support import write_bundle


class ErrorBookTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studyvault-errorbook-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        database = patch.object(app, 'DB_PATH', directory / 'studyvault.db')
        database.start()
        self.addCleanup(database.stop)
        app.restore_bundled(*write_bundle(directory, 2))
        self.client = self.enterContext(TestClient(app.app))
        with app.connect() as db:
            rows = db.execute("SELECT conversation_id, message_id, speaker FROM message ORDER BY message_id").fetchall()
        (self.c1, self.q1, _), (_, self.a1, _), (self.c2, self.q2, _), (_, self.a2, _) = rows

    def annotate(self, message_id, kind, text):
        return self.client.post('/api/annotations', json={'message_id': message_id, 'annotation_type': kind, 'content': text}).json()['annotation_id']

    def test_empty_book_and_only_three_kinds_count(self):
        self.annotate(self.a1, 'NOTE', '普通笔记')
        self.annotate(self.a1, 'IMPORTANT', '重点')
        data = self.client.get('/api/mistakes').json()
        self.assertEqual((data['total'], data['groups']), (0, []))
        self.assertEqual({k: data['summary'][k] for k in ('question', 'correction', 'card')}, {'question': 0, 'correction': 0, 'card': 0})

    def test_groups_by_point_with_priority_and_mastery(self):
        question = self.annotate(self.q1, 'QUESTION', '外键能不能引用唯一键？')
        self.annotate(self.a2, 'CORRECTION', 'AI 说外键只能引用主键，不对')
        card = self.client.post('/api/reviews', json={'message_id': self.a1, 'question': '外键作用？', 'answer': '参照完整性'}).json()
        # A card only enters the book after it was answered wrong at least once.
        self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=correct")
        self.assertEqual(self.client.get('/api/mistakes', params={'kind': 'card'}).json()['total'], 0)
        with app.connect() as db:
            db.execute("UPDATE review_card SET due_at=datetime('now','-1 day')")
        self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=wrong")
        with app.connect() as db:
            db.execute("UPDATE review_card SET due_at=datetime('now','-2 day')")
        data = self.client.get('/api/mistakes').json()
        self.assertEqual(data['total'], 3)
        [group] = data['groups']  # both fixture conversations share the point 'SQL 与查询'
        self.assertEqual((group['point_name'], group['course_name']), ('SQL 与查询', '数据库系统概论'))
        self.assertEqual(group['counts'], {'question': 1, 'correction': 1, 'card': 1})
        self.assertEqual(group['priority'], 3 + 2 + 2 + 1)  # the card is overdue
        self.assertEqual([i['kind'] for i in group['items']], ['card', 'question', 'correction'])  # overdue first
        self.assertEqual((data['summary']['overdue'], data['summary']['points']), (1, 1))
        card_item = group['items'][0]
        self.assertEqual((card_item['lapse_count'], card_item['review_count'], card_item['has_card']), (1, 0, True))
        # Three correct answers in a row master the card; resolving a doubt masters it too.
        for _ in range(3):
            self.client.post(f"/api/reviews/{card['review_card_id']}/complete?grade=correct")
        self.client.patch(f'/api/annotations/{question}', json={'is_resolved': True})
        open_now = self.client.get('/api/mistakes').json()
        self.assertEqual([i['kind'] for g in open_now['groups'] for i in g['items']], ['correction'])
        self.assertEqual(open_now['groups'][0]['priority'], 2)
        mastered = self.client.get('/api/mistakes', params={'status': 'mastered'}).json()
        self.assertEqual(sorted(i['kind'] for g in mastered['groups'] for i in g['items']), ['card', 'question'])
        self.assertEqual(mastered['summary']['week']['mastered'], 2)

    def test_unclassified_conversations_group_separately_and_course_filter(self):
        with app.connect() as db:
            db.execute('DELETE FROM conversation_point WHERE conversation_id=?', (self.c2,))
            ml = db.execute("SELECT course_id FROM course WHERE course_code='ML'").fetchone()[0]
        self.annotate(self.q1, 'QUESTION', 'q1')
        self.annotate(self.q2, 'QUESTION', 'q2')
        names = [g['point_name'] for g in self.client.get('/api/mistakes').json()['groups']]
        self.assertEqual(sorted(names), ['SQL 与查询', '未归类'])
        self.assertEqual(self.client.get('/api/mistakes', params={'course_id': ml}).json()['total'], 0)

    def test_rejected_point_is_not_used_for_grouping(self):
        with app.connect() as db:
            point = db.execute('SELECT point_id FROM conversation_point WHERE conversation_id=?', (self.c1,)).fetchone()[0]
        self.client.post(f'/api/conversations/{self.c1}/points/{point}', json={'status': 'REJECTED'})
        self.annotate(self.q1, 'QUESTION', 'q1')
        self.assertEqual(self.client.get('/api/mistakes').json()['groups'][0]['point_name'], '未归类')

    def test_weekly_counts_land_in_the_monday_week(self):
        from datetime import date, datetime, timedelta, timezone
        question = self.annotate(self.q1, 'QUESTION', 'q')
        self.annotate(self.a2, 'CORRECTION', 'c')
        with app.connect() as db:
            # A Monday and the Sunday before it belong to different weeks.
            db.execute("UPDATE annotation SET created_at='2026-11-09 08:00:00', updated_at='2026-11-09 08:00:00' WHERE annotation_id=?", (question,))
            db.execute("UPDATE annotation SET created_at='2026-11-08 23:00:00', updated_at='2026-11-08 23:00:00' WHERE annotation_id<>?", (question,))
            db.execute("UPDATE annotation SET is_resolved=1 WHERE annotation_id=?", (question,))
        weeks = {w['week_start']: w for w in self.client.get('/api/mistakes/weekly', params={'weeks': 52}).json()}
        self.assertTrue(all(date.fromisoformat(k).weekday() == 0 for k in weeks))
        self.assertEqual(len(weeks), 52)
        # Weeks are bucketed with SQLite date('now'), which is UTC, not the local date.
        today = datetime.now(timezone.utc).date()
        self.assertEqual(max(weeks), (today - timedelta(days=today.weekday())).isoformat())
        if '2026-11-09' in weeks:  # inside the 52-week window
            self.assertEqual((weeks['2026-11-09']['noted'], weeks['2026-11-09']['settled']), (1, 1))
            self.assertEqual(weeks['2026-11-02']['noted'], 1)

    def test_card_from_mistake_prefills_and_is_due_now(self):
        doubt = self.annotate(self.a1, 'QUESTION', '级联删除什么时候用？')
        correction = self.annotate(self.a2, 'CORRECTION', '应为 RESTRICT')
        card = self.client.post(f'/api/mistakes/{doubt}/card', json={}).json()
        self.assertEqual((card['question'], card['message_id']), ('级联删除什么时候用？', self.a1))
        self.assertIn('外键将子表', card['answer'])
        other = self.client.post(f'/api/mistakes/{correction}/card', json={'question': '自定义问题'}).json()
        self.assertEqual((other['question'], other['answer']), ('自定义问题', '应为 RESTRICT'))
        self.assertEqual(len(self.client.get('/api/reviews/due').json()), 2)
        items = {i['id']: i for g in self.client.get('/api/mistakes').json()['groups'] for i in g['items']}
        self.assertTrue(items[doubt]['has_card'])
        note = self.annotate(self.a1, 'NOTE', '笔记')
        self.assertEqual(self.client.post(f'/api/mistakes/{note}/card', json={}).status_code, 404)


class DemoActivityTest(unittest.TestCase):
    """The shipped demo corpus: restore + seed must be consistent with the real scheduler."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studyvault-demo-')
        self.addCleanup(temporary.cleanup)
        database = patch.object(app, 'DB_PATH', Path(temporary.name) / 'studyvault.db')
        database.start()
        self.addCleanup(database.stop)
        self.activity = [json.loads(line) for line in (app.BASE_DIR / 'staging' / 'demo_activity.jsonl').read_text(encoding='utf-8').splitlines() if line]

    def test_seed_matches_activity_file_and_is_idempotent(self):
        app.restore_bundled()
        result = app.seed_demo_activity()
        self.assertEqual(result['annotations'], sum(len(row['marks']) for row in self.activity))
        self.assertEqual(result['review_cards'], sum(len(row['cards']) for row in self.activity))
        self.assertEqual(result['review_logs'], sum(len(c['reviews']) for row in self.activity for c in row['cards']))
        self.assertTrue(app.seed_demo_activity()['skipped'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM annotation').fetchone()[0], result['annotations'])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            # Every card equals the replay of its own log.
            for card in db.execute('SELECT * FROM review_card'):
                logs = db.execute('SELECT * FROM review_log WHERE review_card_id=? ORDER BY reviewed_at,review_log_id', (card['review_card_id'],)).fetchall()
                self.assertEqual(card['lapse_count'], sum(l['grade'] == 'wrong' for l in logs))
                if logs:
                    self.assertEqual((card['review_count'], card['interval_days'], card['due_at'], card['ease_factor']),
                                     (logs[-1]['streak_after'], logs[-1]['interval_after'], logs[-1]['due_after'], logs[-1]['ease_after']))
                    for before, after in zip(logs, logs[1:]):
                        self.assertEqual(before['due_after'], after['due_before'])
                        self.assertLessEqual(before['reviewed_at'], after['reviewed_at'])

    def test_demo_book_is_rich_enough_to_show(self):
        app.restore_bundled()
        app.seed_demo_activity()
        with TestClient(app.app) as client:
            data = client.get('/api/mistakes').json()
        summary = data['summary']
        self.assertGreaterEqual(summary['question'], 8)
        self.assertGreaterEqual(summary['correction'], 4)
        self.assertGreaterEqual(summary['card'], 5)
        self.assertGreaterEqual(len(data['groups']), 6)
        self.assertGreaterEqual(summary['mastered'], 5)

    def test_seed_requires_restored_conversations(self):
        app.init_db()
        with self.assertRaises(ValueError):
            app.seed_demo_activity()
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM annotation').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
