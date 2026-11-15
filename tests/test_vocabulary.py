"""Vocabulary selection, near-duplicate guards, rename/merge and alias restore."""
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

import app
from test_workflows import DatabaseTest


class VocabularyTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.restore()
        self.client = self.enterContext(TestClient(app.app))
        self.cid = self.client.get('/api/conversations').json()['items'][0]['conversation_id']
        self.detail = self.client.get(f'/api/conversations/{self.cid}').json()
        self.course_id = self.detail['conversation']['course_id']

    def create_tag(self, name, allow_similar=False):
        return self.client.post('/api/tags', json={'tag_name': name, 'allow_similar': allow_similar})

    def create_point(self, name, allow_similar=False, course_id=None):
        return self.client.post('/api/knowledge-points', json={
            'course_id': course_id or self.course_id, 'point_name': name, 'allow_similar': allow_similar})

    def tag_count(self):
        with app.connect() as db:
            return db.execute('SELECT COUNT(*) FROM tag').fetchone()[0]

    def test_existing_vocabulary_is_listed_with_usage_and_suggestions(self):
        data = self.client.get('/api/tags').json()
        names = {row['tag_name']: row for row in data['items']}
        self.assertEqual(names['数据库']['conversation_count'], 2)
        self.assertEqual(names['复习']['conversation_count'], 2)
        exact = self.client.get('/api/tags', params={'q': '数据库'}).json()
        self.assertEqual(exact['items'][0]['relation'], 'exact')
        self.assertEqual(exact['items'][0]['tag_name'], '数据库')
        self.assertEqual(self.client.get('/api/tags/suggest', params={'q': '数据库'}).json()['items'][0]['tag_name'], '数据库')
        self.assertEqual(self.client.get('/api/knowledge-points', params={'course_id': self.course_id, 'q': 'SQL'}).json()[0]['point_name'], 'SQL 与查询')
        similar = self.client.get('/api/tags', params={'q': '数据'}).json()
        self.assertTrue(similar['items'])
        self.assertIn(similar['items'][0]['relation'], {'similar', 'contains'})
        self.assertEqual(self.client.get('/api/tags', params={'q': '没有这个标签'}).json()['items'], [])
        points = self.client.get('/api/knowledge-points/suggest', params={'course_id': self.course_id}).json()
        self.assertTrue(any(row['point_name'] == 'SQL 与查询' for row in points['items']))
        self.assertTrue(all(row['course_id'] == self.course_id for row in points['items']))

    def test_exact_and_spelling_variants_reuse_the_same_tag(self):
        before = self.tag_count()
        exact = self.create_tag('数据库').json()
        self.assertFalse(exact['created'])
        self.assertEqual(exact['tag_name'], '数据库')
        created = self.create_tag('Python').json()
        self.assertTrue(created['created'])
        variant = self.create_tag('python').json()
        self.assertFalse(variant['created'])
        self.assertEqual(variant['tag_id'], created['tag_id'])
        fullwidth = self.create_tag('ＳＱＬ').json()
        reused = self.create_tag('SQL').json()
        self.assertEqual(fullwidth['tag_id'], reused['tag_id'])
        self.assertEqual(self.tag_count(), before + 2)

    def test_similar_tag_is_rejected_until_explicitly_confirmed(self):
        self.assertTrue(self.create_tag('外键').json()['created'])
        before = self.tag_count()
        conflict = self.create_tag('外键约束')
        self.assertEqual(conflict.status_code, 409)
        detail = conflict.json()['detail']
        self.assertEqual(detail['code'], 'SIMILAR_EXISTS')
        self.assertEqual(detail['suggestions'][0]['tag_name'], '外键')
        self.assertEqual(self.tag_count(), before)
        confirmed = self.create_tag('外键约束', allow_similar=True)
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.json()['created'])
        self.assertEqual(self.tag_count(), before + 1)

    def test_conversation_tag_endpoint_honours_the_same_guard(self):
        self.assertEqual(self.client.post(f'/api/conversations/{self.cid}/tags', json={'tag_name': '外键'}).json()['created'], True)
        before = len(self.client.get(f'/api/conversations/{self.cid}').json()['tag_items'])
        conflict = self.client.post(f'/api/conversations/{self.cid}/tags', json={'tag_name': '外键约束'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.client.get(f'/api/conversations/{self.cid}').json()['tag_items']), before)
        created = self.client.post(f'/api/conversations/{self.cid}/tags', json={'tag_name': '外键约束', 'allow_similar': True}).json()
        self.assertTrue(created['created'])
        linked = self.client.get(f'/api/conversations/{self.cid}').json()['tag_items']
        self.assertIn(created['tag_id'], [row['tag_id'] for row in linked])
        self.assertEqual(self.client.delete(f"/api/conversations/{self.cid}/tags/{created['tag_id']}").status_code, 200)

    def test_tag_rename_keeps_old_name_as_alias_and_restore_uses_it(self):
        client = self.client
        tags = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        renamed = client.patch(f"/api/tags/{tags['数据库']['tag_id']}", json={'tag_name': '数据库基础'})
        self.assertEqual(renamed.status_code, 200)
        self.assertTrue(renamed.json()['renamed'])
        current = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        self.assertIn('数据库基础', current)
        self.assertNotIn('数据库', current)
        self.assertIn('数据库', current['数据库基础']['aliases'])
        self.restore()
        after = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        self.assertIn('数据库基础', after)
        self.assertNotIn('数据库', after)
        self.assertEqual(client.get(f'/api/conversations/{self.cid}').json()['tags'].count('数据库基础'), 1)
        duplicate = client.patch(f"/api/tags/{after['数据库基础']['tag_id']}", json={'tag_name': '复习'})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()['detail']['code'], 'NAME_EXISTS')

    def test_tag_merge_moves_links_and_keeps_alias(self):
        client = self.client
        source = self.create_tag('错题').json()
        client.post(f'/api/conversations/{self.cid}/tags', json={'tag_id': source['tag_id']})
        tags = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        target_id = tags['数据库']['tag_id']
        merged = client.post(f'/api/tags/{target_id}/merge', json={'source_tag_id': source['tag_id']})
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertTrue(merged.json()['merged'])
        current = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        self.assertNotIn('错题', current)
        self.assertIn('错题', current['数据库']['aliases'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tag WHERE tag_id=?', (source['tag_id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_tag WHERE conversation_id=? AND tag_id=?', (self.cid, target_id)).fetchone()[0], 1)
        reused = self.create_tag('错题').json()
        self.assertFalse(reused['created'])
        self.assertEqual(reused['tag_id'], target_id)
        self.assertEqual(reused['matched'], 'alias')

    def test_point_create_similar_and_merge_workflow(self):
        client = self.client
        exact = self.create_point('SQL 与查询').json()
        self.assertFalse(exact['created'])
        conflict = self.create_point('SQL查询')
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['detail']['suggestions'][0]['point_name'], 'SQL 与查询')
        created = self.create_point('SQL查询', allow_similar=True).json()
        self.assertTrue(created['created'])
        linked = client.post(f'/api/conversations/{self.cid}/points', json={'point_name': '范式设计'})
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertTrue(linked.json()['created'])
        self.assertEqual(linked.json()['status'], 'CONFIRMED')
        with app.connect() as db:
            target = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name='SQL 与查询'", (self.course_id,)).fetchone()[0]
        merged = client.post(f'/api/knowledge-points/{target}/merge', json={'source_point_id': created['point_id']})
        self.assertEqual(merged.status_code, 200, merged.text)
        current = {row['point_name']: row for row in client.get('/api/knowledge-points/suggest', params={'course_id': self.course_id}).json()['items']}
        self.assertNotIn('SQL查询', current)
        self.assertIn('SQL查询', current['SQL 与查询']['aliases'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_point WHERE point_id=?', (created['point_id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_point WHERE conversation_id=? AND point_id=?', (self.cid, target)).fetchone()[0], 1)

    def test_merge_point_keeps_a_confirmed_link_over_a_proposed_one(self):
        client = self.client
        with app.connect() as db:
            target = db.execute("SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name='SQL 与查询'", (self.course_id,)).fetchone()[0]
            self.assertEqual(db.execute("SELECT status FROM conversation_point WHERE conversation_id=? AND point_id=?", (self.cid, target)).fetchone()[0], 'PROPOSED')
        created = client.post(f'/api/conversations/{self.cid}/points', json={'point_name': '范式设计'}).json()
        self.assertEqual(created['status'], 'CONFIRMED')
        merged = client.post(f'/api/knowledge-points/{target}/merge', json={'source_point_id': created['point_id']})
        self.assertEqual(merged.status_code, 200, merged.text)
        with app.connect() as db:
            row = db.execute("SELECT status,source FROM conversation_point WHERE conversation_id=? AND point_id=?", (self.cid, target)).fetchone()
        self.assertEqual(tuple(row), ('CONFIRMED', 'manual'))

    def test_point_rename_alias_survives_snapshot_restore(self):
        client = self.client
        with app.connect() as db:
            point = dict(db.execute("SELECT point_id,point_name FROM knowledge_point WHERE course_id=? AND point_name='SQL 与查询'", (self.course_id,)).fetchone())
        renamed = client.patch(f"/api/knowledge-points/{point['point_id']}", json={'point_name': 'SQL 查询基础'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.restore()
        with app.connect() as db:
            names = [row[0] for row in db.execute('SELECT point_name FROM knowledge_point WHERE course_id=?', (self.course_id,))]
            aliases = [row[0] for row in db.execute('SELECT alias_name FROM knowledge_point_alias WHERE course_id=?', (self.course_id,))]
            links = db.execute('SELECT COUNT(*) FROM conversation_point cp JOIN knowledge_point kp USING(point_id) WHERE cp.conversation_id=? AND kp.point_name=?', (self.cid, 'SQL 查询基础')).fetchone()[0]
        self.assertIn('SQL 查询基础', names)
        self.assertNotIn('SQL 与查询', names)
        self.assertIn('SQL 与查询', aliases)
        self.assertEqual(links, 1)

    def test_curation_bulk_tags_prompt_on_near_duplicate(self):
        client = self.client
        self.create_tag('外键')
        rejected = client.patch(f'/api/conversations/{self.cid}', json={'tags': ['外键约束']})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()['detail']['code'], 'SIMILAR_EXISTS')
        self.assertNotIn('外键约束', client.get(f'/api/conversations/{self.cid}').json()['tags'])
        accepted = client.patch(f'/api/conversations/{self.cid}', json={'tags': ['外键约束'], 'tags_allow_similar': True})
        self.assertEqual(accepted.status_code, 200)
        self.assertIn('外键约束', accepted.json()['tags'])

    def test_backup_round_trip_keeps_aliases_and_old_backups_still_restore(self):
        client = self.client
        source = self.create_tag('错题').json()
        tags = {row['tag_name']: row for row in client.get('/api/tags').json()['items']}
        client.post(f"/api/tags/{tags['数据库']['tag_id']}/merge", json={'source_tag_id': source['tag_id']})
        backup = client.get('/api/backup').content
        root = self.directory
        path = root / 'with-aliases.sqlite3'
        path.write_bytes(backup)
        preview = client.post('/api/backup/preview', files={'file': ('backup.sqlite3', path.read_bytes())})
        self.assertEqual(preview.status_code, 200, preview.text)
        restored = client.post('/api/backup/restore', json={'token': preview.json()['token']})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIn('错题', {alias for row in client.get('/api/tags').json()['items'] for alias in row['aliases']})

        older = root / 'without-alias-tables.sqlite3'
        older.write_bytes(backup)
        with closing(sqlite3.connect(older)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('DROP TABLE tag_alias')
            db.execute('DROP TABLE knowledge_point_alias')
            db.commit()
        preview = client.post('/api/backup/preview', files={'file': ('older.sqlite3', older.read_bytes())})
        self.assertEqual(preview.status_code, 200, preview.text)
        restored = client.post('/api/backup/restore', json={'token': preview.json()['token']})
        self.assertEqual(restored.status_code, 200, restored.text)
        with app.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'tag_alias', 'knowledge_point_alias'} <= tables)
