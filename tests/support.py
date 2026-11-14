"""Small, non-sensitive fixtures shared by API and browser regression checks."""
import json
from pathlib import Path


def write_bundle(directory: Path, count: int = 2) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    manifest, annotations = directory / 'conversations.jsonl', directory / 'annotations.jsonl'
    rows, notes = [], []
    for index in range(count):
        rows.append({
            'platform': 'Codex', 'source_id': f'fixture-{index:02}',
            'title': f'SQL 外键学习 {index:02}', 'started_at': '2026-01-01T00:00:00Z',
            'summary': '学习关系数据库的参照完整性。',
            'messages': [
                {'speaker': 'user', 'content': f'什么是外键？样例 {index:02}', 'created_at': '2026-01-01T00:00:00Z'},
                {'speaker': 'assistant', 'content': '外键将子表关联到父表，维护参照完整性。', 'created_at': '2026-01-01T00:00:01Z'},
            ],
            'tags': ['数据库', '复习'], 'knowledge_points': ['SQL 与查询'],
            'message_count': 2, 'user_message_count': 1, 'assistant_message_count': 1,
        })
        notes.append({'platform': 'Codex', 'source_id': f'fixture-{index:02}',
                      'summary_manual': '复核外键概念。', 'intent': '学习', 'topics': ['外键'],
                      'key_points': ['参照完整性'], 'unresolved_questions': [], 'action_items': ['练习 JOIN'],
                      'quality': '有效', 'confidence': 'high'})
    dump_jsonl(manifest, rows)
    dump_jsonl(annotations, notes)
    return manifest, annotations


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
