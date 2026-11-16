"""Direct annotation-to-knowledge-point links and knowledge-point note aggregation."""
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

import app
from test_workflows import DatabaseTest


class AnnotationPointTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.restore()
        self.client = self.enterContext(TestClient(app.app))
        self.cid = self.client.get('/api/conversations').json()['items'][0]['conversation_id']
        self.detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.mid = self.detail['messages'][0]['message_id']
        self.course_id = self.detail['conversation']['course_id']
        with app.connect() as db:
            self.point_id = db.execute(
                "SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name='SQL 与查询'",
                (self.course_id,)).fetchone()[0]

    def create_annotation(self, content='笔记', point_ids=None, annotation_type='NOTE'):
        payload = {'message_id': self.mid, 'annotation_type': annotation_type, 'content': content}
        if point_ids is not None:
            payload['point_ids'] = point_ids
        return self.client.post('/api/annotations', json=payload)

    def create_point(self, name):
        return self.client.post('/api/knowledge-points', json={
            'course_id': self.course_id, 'point_name': name, 'allow_similar': True}).json()

    def test_annotation_can_link_multiple_points_and_detail_returns_them(self):
        second = self.create_point('范式设计')
        response = self.create_annotation('只讨论外键和范式', [self.point_id, second['point_id']])
        self.assertEqual(response.status_code, 200, response.text)
        annotation = response.json()
        self.assertEqual({row['point_name'] for row in annotation['points']}, {'SQL 与查询', '范式设计'})
        with app.connect() as db:
            rows = db.execute("SELECT point_id FROM annotation_point WHERE annotation_id=? ORDER BY point_id", (annotation['annotation_id'],)).fetchall()
        self.assertEqual([row[0] for row in rows], sorted([self.point_id, second['point_id']]))
        detail = self.client.get(f'/api/conversations/{self.cid}').json()
        linked = next(item for message in detail['messages'] for item in message['annotations'] if item['annotation_id'] == annotation['annotation_id'])
        self.assertEqual({row['point_name'] for row in linked['points']}, {'SQL 与查询', '范式设计'})
        listed = self.client.get('/api/annotations', params={'message_id': self.mid}).json()
        self.assertEqual({row['point_name'] for row in listed[0]['points']}, {'SQL 与查询', '范式设计'})

    def test_annotation_rejects_point_from_another_course(self):
        other_course = next(course['course_id'] for course in self.client.get('/api/courses').json() if course['course_id'] != self.course_id)
        other_point = self.client.post('/api/knowledge-points', json={
            'course_id': other_course, 'point_name': '其他课程知识点', 'allow_similar': True}).json()
        response = self.create_annotation('跨课程', [other_point['point_id']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('不属于该会话课程', response.json()['detail'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM annotation').fetchone()[0], 0)

    def test_annotation_point_links_can_be_replaced_and_cleared(self):
        second = self.create_point('范式设计')
        annotation = self.create_annotation('先关联外键', [self.point_id]).json()
        changed = self.client.patch(f"/api/annotations/{annotation['annotation_id']}", json={'point_ids': [second['point_id']]})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual([row['point_name'] for row in changed.json()['points']], ['范式设计'])
        cleared = self.client.patch(f"/api/annotations/{annotation['annotation_id']}", json={'point_ids': []})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()['points'], [])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM annotation_point').fetchone()[0], 0)

    def test_annotation_point_is_cascaded_on_annotation_delete(self):
        annotation = self.create_annotation('待删除', [self.point_id]).json()
        self.assertEqual(self.client.delete(f"/api/annotations/{annotation['annotation_id']}").status_code, 200)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM annotation_point WHERE annotation_id=?', (annotation['annotation_id'],)).fetchone()[0], 0)

    def test_point_notes_aggregate_message_notes_and_independent_notes(self):
        annotation = self.create_annotation('消息里的外键笔记', [self.point_id]).json()
        created = self.client.post(f'/api/knowledge-points/{self.point_id}/notes', json={'content': '独立的概念笔记'})
        self.assertEqual(created.status_code, 200, created.text)
        note = created.json()
        self.assertEqual(note['source'], 'point')
        notes = self.client.get(f'/api/knowledge-points/{self.point_id}/notes').json()
        self.assertEqual(notes['point']['point_name'], 'SQL 与查询')
        self.assertEqual(notes['total'], 2)
        by_source = {item['source']: item for item in notes['items']}
        self.assertEqual(by_source['message']['id'], annotation['annotation_id'])
        self.assertEqual(by_source['message']['conversation_id'], self.cid)
        self.assertEqual(by_source['message']['message_id'], self.mid)
        self.assertEqual(by_source['point']['content'], '独立的概念笔记')
        updated = self.client.patch(f"/api/point-notes/{note['point_note_id']}", json={'content': '改过的独立笔记'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['content'], '改过的独立笔记')
        self.assertEqual(self.client.delete(f"/api/point-notes/{note['point_note_id']}").status_code, 200)
        after = self.client.get(f'/api/knowledge-points/{self.point_id}/notes').json()
        self.assertEqual(after['total'], 1)
        self.assertEqual(after['items'][0]['source'], 'message')

    def test_point_notes_pagination_and_count(self):
        for index in range(5):
            self.client.post(f'/api/knowledge-points/{self.point_id}/notes', json={'content': f'笔记 {index}'})
        first = self.client.get(f'/api/knowledge-points/{self.point_id}/notes', params={'limit': 2, 'offset': 0}).json()
        second = self.client.get(f'/api/knowledge-points/{self.point_id}/notes', params={'limit': 2, 'offset': 2}).json()
        self.assertEqual((first['total'], len(first['items']), len(second['items'])), (5, 2, 2))
        suggestion = next(row for row in self.client.get('/api/knowledge-points/suggest', params={'course_id': self.course_id}).json()['items'] if row['point_id'] == self.point_id)
        self.assertEqual(suggestion['note_count'], 5)

    def test_merge_point_moves_annotation_links_and_independent_notes(self):
        source = self.create_point('SQL查询')
        annotation = self.create_annotation('关联到源知识点', [source['point_id']]).json()
        note = self.client.post(f"/api/knowledge-points/{source['point_id']}/notes", json={'content': '源知识点的独立笔记'}).json()
        merged = self.client.post(f"/api/knowledge-points/{self.point_id}/merge", json={'source_point_id': source['point_id']})
        self.assertEqual(merged.status_code, 200, merged.text)
        with app.connect() as db:
            link_points = [row[0] for row in db.execute('SELECT point_id FROM annotation_point WHERE annotation_id=?', (annotation['annotation_id'],))]
            note_point = db.execute('SELECT point_id FROM point_note WHERE point_note_id=?', (note['point_note_id'],)).fetchone()[0]
        self.assertEqual(link_points, [self.point_id])
        self.assertEqual(note_point, self.point_id)
        notes = self.client.get(f'/api/knowledge-points/{self.point_id}/notes').json()
        self.assertEqual(notes['total'], 2)

    def test_course_change_migrates_annotation_point_links(self):
        annotation = self.create_annotation('改课前', [self.point_id]).json()
        other_course = next(course['course_id'] for course in self.client.get('/api/courses').json() if course['course_id'] != self.course_id)
        result = self.client.patch(f'/api/conversations/{self.cid}', json={'course_id': other_course})
        self.assertEqual(result.status_code, 200, result.text)
        with app.connect() as db:
            rows = db.execute(
                'SELECT kp.course_id,kp.point_name FROM annotation_point ap '
                'JOIN annotation a ON a.annotation_id=ap.annotation_id '
                'JOIN message m ON m.message_id=a.message_id '
                'JOIN knowledge_point kp ON kp.point_id=ap.point_id '
                'WHERE a.annotation_id=?', (annotation['annotation_id'],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], other_course)
        self.assertEqual(rows[0][1], 'SQL 与查询')

    def test_backup_round_trip_keeps_annotation_links_and_point_notes(self):
        annotation = self.create_annotation('备份前笔记', [self.point_id]).json()
        note = self.client.post(f'/api/knowledge-points/{self.point_id}/notes', json={'content': '备份前独立笔记'}).json()
        path = self.directory / 'with-notes.sqlite3'
        path.write_bytes(self.client.get('/api/backup').content)
        preview = self.client.post('/api/backup/preview', files={'file': ('notes.sqlite3', path.read_bytes())})
        self.assertEqual(preview.status_code, 200, preview.text)
        restored = self.client.post('/api/backup/restore', json={'token': preview.json()['token']})
        self.assertEqual(restored.status_code, 200, restored.text)
        detail = self.client.get(f'/api/conversations/{self.cid}').json()
        linked = next(item for message in detail['messages'] for item in message['annotations'] if item['annotation_id'] == annotation['annotation_id'])
        self.assertEqual([row['point_id'] for row in linked['points']], [self.point_id])
        notes = self.client.get(f'/api/knowledge-points/{self.point_id}/notes').json()
        self.assertEqual(notes['total'], 2)
        self.assertIn('备份前独立笔记', [item['content'] for item in notes['items'] if item['source'] == 'point'])
        self.assertEqual(note['point_id'], self.point_id)

    def test_point_notes_are_scoped_to_author_and_backup_accepts_old_schema(self):
        note = self.client.post(f'/api/knowledge-points/{self.point_id}/notes', json={'content': '我的笔记'}).json()
        with app.connect() as db:
            db.execute("INSERT INTO app_user(user_id,role_id,username,display_name) VALUES(2,1,'other','Other')")
            db.execute("INSERT INTO point_note(point_id,author_id,content) VALUES(?,2,'别人的笔记')", (self.point_id,))
        notes = self.client.get(f'/api/knowledge-points/{self.point_id}/notes').json()
        self.assertEqual(notes['total'], 1)
        self.assertEqual(notes['items'][0]['content'], '我的笔记')
        self.assertEqual(self.client.delete(f"/api/point-notes/{note['point_note_id']}").status_code, 200)

        backup = self.client.get('/api/backup').content
        path = self.directory / 'before-note-tables.sqlite3'
        path.write_bytes(backup)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('DROP TABLE annotation_point')
            db.execute('DROP TABLE point_note')
            db.commit()
        preview = self.client.post('/api/backup/preview', files={'file': ('old.sqlite3', path.read_bytes())})
        self.assertEqual(preview.status_code, 200, preview.text)
        restored = self.client.post('/api/backup/restore', json={'token': preview.json()['token']})
        self.assertEqual(restored.status_code, 200, restored.text)
        with app.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'annotation_point', 'point_note'} <= tables)
