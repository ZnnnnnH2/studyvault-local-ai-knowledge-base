"""Compressed Agent sessions import without requiring a system zstd executable."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard

import app


class ZstdImportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studyvault-zstd-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database_patch = patch.object(app, 'DB_PATH', self.directory / 'studyvault.db')
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        app.init_db()

    def codex_bytes(self):
        rows = [
            {'type': 'session_meta', 'payload': {'id': 'zstd-1'}},
            {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': 'SQL 压缩导入'}]}},
            {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'type': 'output_text', 'text': '压缩记录可以读取。'}]}},
        ]
        return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n').encode('utf-8')

    def test_zstandard_dependency_reads_compressed_session(self):
        path = self.directory / 'compressed.jsonl.zstd'
        path.write_bytes(zstandard.ZstdCompressor().compress(self.codex_bytes()))
        parsed = app.parse_conversation(path, 'Codex')
        self.assertEqual(parsed['source_id'], 'zstd-1')
        self.assertEqual([message['speaker'] for message in parsed['messages']], ['user', 'assistant'])
        imported = app.import_file(path, 'Codex')
        self.assertEqual(imported['status'], 'SUCCESS')
        self.assertEqual(imported['messages'], 2)
        duplicate = app.import_file(path, 'Codex')
        self.assertEqual(duplicate['result_code'], 'SKIPPED_DUPLICATE')

    def test_corrupted_compressed_file_reports_controlled_error(self):
        path = self.directory / 'broken.jsonl.zstd'
        path.write_bytes(b'not a zstd frame')
        with self.assertRaisesRegex(RuntimeError, '解压会话失败'):
            app.parse_conversation(path, 'Codex')

    def large_codex_bytes(self, count=1000):
        rows = [{'type': 'session_meta', 'payload': {'id': 'zstd-truncated'}}]
        for index in range(count):
            rows.append({'type': 'response_item', 'payload': {
                'role': 'user' if index % 2 == 0 else 'assistant',
                'content': [{'type': 'input_text' if index % 2 == 0 else 'output_text',
                             'text': f'message-{index}'}]}})
        return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n').encode('utf-8')

    def test_truncated_frames_fail_without_partial_import_and_full_retry_succeeds(self):
        payload = self.large_codex_bytes()
        compressed = zstandard.ZstdCompressor(level=3, write_checksum=True).compress(payload)
        path = self.directory / 'truncated.jsonl.zstd'
        cases = {
            'tail': compressed[:-50],
            'half': compressed[:len(compressed) // 2],
            'second_frame': compressed + compressed[:100],
            'second_frame_half': compressed + compressed[:len(compressed) // 2],
        }
        for label, broken in cases.items():
            with self.subTest(label=label):
                path.write_bytes(broken)
                with self.assertRaisesRegex(RuntimeError, '压缩帧不完整'):
                    app.parse_conversation(path, 'Codex')
                with app.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM message').fetchone()[0], 0)
        path.write_bytes(compressed + compressed)
        self.assertEqual(len(app.parse_conversation(path, 'Codex')['messages']), 2000)
        path.write_bytes(compressed)
        with app.connect() as db:
            source = app.source_row(db, 'Codex')
            db.execute(
                "INSERT INTO import_job(owner_id,source_id,file_name,source_conversation_id,status,result_code,error_message) "
                "VALUES(?,?,?,'zstd-truncated','FAILED','FAILED_PARSE','truncated')",
                (app.DEMO_USER_ID, source["source_id"], str(path)))
        parsed = app.parse_conversation(path, 'Codex')
        self.assertEqual(len(parsed['messages']), 1000)
        imported = app.import_file(path, 'Codex')
        self.assertEqual(imported['status'], 'SUCCESS')
        self.assertEqual(imported['messages'], 1000)
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM message').fetchone()[0], 1000)
            job = db.execute(
                "SELECT status,result_code FROM import_job WHERE file_name=? AND source_conversation_id='zstd-truncated'",
                (str(path),)).fetchone()
        self.assertEqual(tuple(job), ('SUCCESS', 'SUCCESS'))
        duplicate = app.import_file(path, 'Codex')
        self.assertEqual(duplicate['result_code'], 'SKIPPED_DUPLICATE')


if __name__ == '__main__':
    unittest.main()
