"""ChatGPT web export (conversations.json): tree flattening, filtering and multi-conversation upload."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


def node(node_id, parent, children, role=None, text=None, created=None, content_type='text', hidden=False):
    message = None
    if role:
        message = {'author': {'role': role}, 'create_time': created,
                   'content': {'content_type': content_type, 'parts': [text] if text is not None else []},
                   'metadata': {'is_visually_hidden_from_conversation': True} if hidden else {}}
    return {'id': node_id, 'parent': parent, 'children': children, 'message': message}


def export_fixture():
    # The user edited the first question, so "u1-old" is an abandoned branch; current_node picks "a2".
    first = {
        'title': '外键和级联删除', 'create_time': 1757000000.0, 'conversation_id': 'chatgpt-conv-1', 'current_node': 'a2',
        'mapping': {
            'root': node('root', None, ['sys']),
            'sys': node('sys', 'root', ['u1-old', 'u1'], 'system', '', 1757000000.0, hidden=True),
            'u1-old': node('u1-old', 'sys', ['a1-old'], 'user', '旧问题（已编辑掉）', 1757000001.0),
            'a1-old': node('a1-old', 'u1-old', [], 'assistant', '旧回答', 1757000002.0),
            'u1': node('u1', 'sys', ['tool'], 'user', '外键和 ON DELETE CASCADE 是什么关系？ password: hunter2', 1757000003.0),
            'tool': node('tool', 'u1', ['a1'], 'assistant', 'print(1)', 1757000004.0, content_type='code'),
            'a1': node('a1', 'tool', ['u2'], 'assistant', 'CASCADE 是外键的删除动作之一。', 1757000005.0),
            'u2': node('u2', 'a1', ['a2'], 'user', '那 RESTRICT 呢？', 1757000006.0),
            'a2': node('a2', 'u2', [], 'assistant', 'RESTRICT 在仍被引用时拒绝删除。', 1757000007.0),
        },
    }
    second = {
        'title': '', 'create_time': 1757100000.0, 'id': 'chatgpt-conv-2', 'current_node': 'b1',
        'mapping': {
            'r': node('r', None, ['q']),
            'q': node('q', 'r', ['b1'], 'user', '进程和线程的区别', 1757100001.0),
            'b1': node('b1', 'q', [], 'assistant', '线程共享进程的地址空间。', 1757100002.0),
        },
    }
    empty = {'title': '只有系统消息', 'create_time': 1757200000.0, 'id': 'chatgpt-conv-3', 'current_node': 's',
             'mapping': {'s': node('s', None, [], 'system', 'You are ChatGPT', 1757200000.0)}}
    return [first, second, empty]


class ChatGPTExportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studyvault-chatgpt-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'conversations.json'
        self.path.write_text(json.dumps(export_fixture(), ensure_ascii=False), encoding='utf-8')

    def test_follows_current_branch_and_drops_tool_hidden_and_system_messages(self):
        parsed = app.parse_chatgpt_export(self.path)
        self.assertEqual([p['source_id'] for p in parsed], ['chatgpt-conv-1', 'chatgpt-conv-2'])
        first = parsed[0]
        self.assertEqual(first['title'], '外键和级联删除')
        self.assertEqual([m['speaker'] for m in first['messages']], ['user', 'assistant', 'user', 'assistant'])
        self.assertNotIn('旧问题', json.dumps(first, ensure_ascii=False))
        self.assertNotIn('print(1)', json.dumps(first, ensure_ascii=False))
        self.assertNotIn('hunter2', first['messages'][0]['content'])
        self.assertTrue(first['messages'][0]['created_at'].startswith('2025-09-'))
        self.assertEqual(parsed[1]['title'], '进程和线程的区别')  # empty title falls back to the first question

    def test_rejects_agent_files_and_empty_exports(self):
        agent = self.directory / 'agent.json'
        agent.write_text(json.dumps({'messages': []}), encoding='utf-8')
        with self.assertRaises(ValueError):
            app.parse_chatgpt_export(agent)
        only_system = self.directory / 'empty.json'
        only_system.write_text(json.dumps(export_fixture()[2:]), encoding='utf-8')
        with self.assertRaises(ValueError):
            app.parse_chatgpt_export(only_system)

    def test_upload_preview_lists_each_conversation_and_imports_selected_ones(self):
        with patch.object(app, 'DB_PATH', self.directory / 'studyvault.db'), TestClient(app.app) as client:
            with self.path.open('rb') as stream:
                preview = client.post('/api/import/preview', files=[('files', ('conversations.json', stream, 'application/json'))], data={'platform': 'auto'}).json()
            self.assertEqual(preview['counts'], {'NEW': 2, 'DUPLICATE': 0, 'INVALID': 0})
            self.assertEqual({c['platform'] for c in preview['candidates']}, {'ChatGPT'})
            course = {c['title']: c for c in preview['candidates']}
            task = client.post('/api/import/tasks', json={'preview_token': preview['token'], 'selected': [course['外键和级联删除']['index']]}).json()
            for _ in range(100):
                status = client.get(f"/api/import/tasks/{task['task_id']}").json()
                if status['status'] == 'completed':
                    break
            self.assertEqual((status['imported'], status['results'][0]['title']), (1, '外键和级联删除'))
            with app.connect() as db:
                row = db.execute("SELECT c.title,co.course_name,s.platform_name,COUNT(m.message_id) FROM conversation c JOIN course co USING(course_id) JOIN conversation_source s USING(source_id) JOIN message m USING(conversation_id) GROUP BY c.conversation_id").fetchone()
            self.assertEqual(tuple(row), ('外键和级联删除', '数据库系统概论', 'ChatGPT', 4))
            with self.path.open('rb') as stream:
                again = client.post('/api/import/preview', files=[('files', ('conversations.json', stream, 'application/json'))]).json()
            self.assertEqual(again['counts'], {'NEW': 1, 'DUPLICATE': 1, 'INVALID': 0})

    def test_cli_import_file_imports_every_conversation(self):
        with patch.object(app, 'DB_PATH', self.directory / 'cli.db'):
            app.init_db()
            results = app.import_export_file(self.path, 'ChatGPT')
            self.assertEqual([r['status'] for r in results], ['SUCCESS', 'SUCCESS'])
            self.assertEqual([r['status'] for r in app.import_export_file(self.path, 'ChatGPT')], ['SKIPPED', 'SKIPPED'])


class SchedulerTest(unittest.TestCase):
    def test_wrong_answers_lower_ease_and_slow_later_growth(self):
        self.assertEqual(app.schedule_next(0, 0, 2.5, 'correct'), (3, 1, 2.5))
        self.assertEqual(app.schedule_next(3, 1, 2.5, 'correct'), (8, 2, 2.5))
        self.assertEqual(app.schedule_next(8, 2, 2.5, 'wrong'), (1, 0, 2.3))
        # Same streak, but a card that was missed grows more slowly than a fresh one.
        missed = app.schedule_next(3, 1, 2.3, 'correct')[0]
        fresh = app.schedule_next(3, 1, 2.5, 'correct')[0]
        self.assertLess(missed, fresh)

    def test_ease_has_a_floor_and_interval_a_ceiling(self):
        self.assertEqual(app.schedule_next(1, 0, 1.35, 'wrong')[2], 1.3)
        self.assertEqual(app.schedule_next(300, 9, 2.5, 'correct')[0], app.MAX_INTERVAL)
        self.assertEqual(app.schedule_next(3, 1, 1.3, 'correct')[0], 4)  # always at least one day longer


if __name__ == '__main__':
    unittest.main()
