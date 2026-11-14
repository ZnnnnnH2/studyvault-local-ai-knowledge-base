"""Local learning workflows, upload jobs, and complete SQLite backups."""
from __future__ import annotations

import copy
import itertools
import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


class CardPatch(BaseModel):
    question: str | None = None
    answer: str | None = None
    status: Literal['ACTIVE', 'SUSPENDED', 'COMPLETED'] | None = None
    due_at: datetime | None = None


class FavoriteInput(BaseModel):
    conversation_id: int
    is_favorite: bool


class ProgressInput(BaseModel):
    conversation_id: int
    message_id: int | None = None


class LibraryPreferences(BaseModel):
    view: Literal['overview', 'library', 'review', 'stats', 'todos', 'data'] = 'overview'
    q: str = Field('', max_length=2000)
    course_id: int | None = None
    source_id: int | None = None
    offset: int = Field(0, ge=0, le=1000000)
    favorites_only: bool = False
    conversation_id: int | None = None


class RestoreInput(BaseModel):
    token: str


class ImportInput(BaseModel):
    preview_token: str
    selected: list[int] = Field(min_length=1, max_length=20)


def create_router(backend: Any) -> APIRouter:
    router = APIRouter()
    temporary = tempfile.TemporaryDirectory(prefix='studyvault-transfer-')
    temp_root = Path(temporary.name)
    previews: dict[str, dict[str, Any]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    task_lock = threading.Lock()

    def database_key() -> str:
        return str(backend.DB_PATH.resolve())

    def require_conversation(db, cid):
        row = db.execute('SELECT * FROM conversation WHERE conversation_id=? AND owner_id=?', (cid, backend.DEMO_USER_ID)).fetchone()
        if not row:
            raise HTTPException(404, '会话不存在或无权访问')
        return row

    def session(token: str, kind: str):
        item = previews.get(token)
        if not item or item['kind'] != kind or item['database'] != database_key() or time.monotonic() - item['created'] > 3600:
            raise HTTPException(404, '预览已过期或不属于当前数据库，请重新上传')
        return item

    def new_session(kind: str):
        # The one-hour token lifetime bounds both memory and temporary upload storage.
        for key, item in list(previews.items()):
            if time.monotonic() - item['created'] > 3600 and not item.get('running'):
                for path in item['directory'].rglob('*'):
                    if path.is_file():
                        path.unlink(missing_ok=True)
                previews.pop(key, None)
        token = uuid.uuid4().hex
        directory = Path(temporary.name) / token
        directory.mkdir()
        item = {'kind': kind, 'database': database_key(), 'created': time.monotonic(), 'directory': directory, 'running': False}
        previews[token] = item
        return token, item

    async def save_upload(upload: UploadFile, path: Path, limit: int) -> int:
        size = 0
        try:
            with path.open('wb') as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, '上传超过大小限制')
                    output.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        return size

    @router.get('/api/todos')
    def todos(status: Literal['open', 'resolved', 'all'] = 'open', annotation_type: Literal['QUESTION', 'CORRECTION', 'IMPORTANT'] | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        args: list[Any] = [backend.DEMO_USER_ID]
        where = "c.owner_id=? AND a.annotation_type IN ('QUESTION','CORRECTION','IMPORTANT')"
        if status != 'all':
            where += ' AND a.is_resolved=?'
            args.append(int(status == 'resolved'))
        if annotation_type:
            where += ' AND a.annotation_type=?'
            args.append(annotation_type)
        joins = 'FROM annotation a JOIN message m USING(message_id) JOIN conversation c USING(conversation_id) JOIN course co USING(course_id)'
        with backend.connect() as db:
            total = db.execute(f'SELECT COUNT(*) {joins} WHERE {where}', args).fetchone()[0]
            items = [dict(row) for row in db.execute(f'SELECT a.*,m.seq_no,c.conversation_id,c.title,co.course_name {joins} WHERE {where} ORDER BY a.updated_at DESC,a.annotation_id DESC LIMIT ? OFFSET ?', (*args, limit, offset))]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @router.get('/api/reviews/cards')
    def cards(status: Literal['ACTIVE', 'SUSPENDED', 'COMPLETED'] | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        args: list[Any] = [backend.DEMO_USER_ID]
        condition = 'r.owner_id=?'
        if status:
            condition += ' AND r.status=?'
            args.append(status)
        joins = 'FROM review_card r JOIN message m USING(message_id) JOIN conversation c USING(conversation_id)'
        with backend.connect() as db:
            total = db.execute(f'SELECT COUNT(*) {joins} WHERE {condition}', args).fetchone()[0]
            items = [dict(row) for row in db.execute(f'SELECT r.*,c.conversation_id,c.title {joins} WHERE {condition} ORDER BY r.due_at,r.review_card_id LIMIT ? OFFSET ?', (*args, limit, offset))]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @router.patch('/api/reviews/cards/{card_id}')
    def edit_card(card_id: int, payload: CardPatch):
        values = payload.model_dump(exclude_none=True)
        if not values:
            raise HTTPException(400, '请提供要修改的卡片信息')
        for field in ('question', 'answer'):
            if field in values:
                values[field] = values[field].strip()
                if not values[field]:
                    raise HTTPException(400, '问题和答案不能为空')
        if payload.due_at:
            if payload.due_at.tzinfo is None:
                raise HTTPException(400, '到期时间必须包含时区')
            values['due_at'] = payload.due_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with backend.connect() as db:
            old = db.execute('SELECT * FROM review_card WHERE review_card_id=? AND owner_id=?', (card_id, backend.DEMO_USER_ID)).fetchone()
            if not old:
                raise HTTPException(404, '复习卡不存在或无权访问')
            assignments = ','.join(f'{name}=?' for name in values)
            db.execute(f'UPDATE review_card SET {assignments} WHERE review_card_id=?', (*values.values(), card_id))
            db.execute("INSERT INTO audit_log(actor_id,operation,table_name,record_id,before_data,after_data) VALUES(?,'EDIT_CARD','review_card',?,?,?)", (backend.DEMO_USER_ID, card_id, json.dumps(dict(old), ensure_ascii=False), json.dumps(values, ensure_ascii=False)))
            return dict(db.execute('SELECT * FROM review_card WHERE review_card_id=?', (card_id,)).fetchone())

    @router.post('/api/library/favorite')
    def favorite(payload: FavoriteInput):
        with backend.connect() as db:
            require_conversation(db, payload.conversation_id)
            db.execute('INSERT INTO conversation_state(owner_id,conversation_id,is_favorite) VALUES(?,?,?) ON CONFLICT(owner_id,conversation_id) DO UPDATE SET is_favorite=excluded.is_favorite', (backend.DEMO_USER_ID, payload.conversation_id, int(payload.is_favorite)))
        return payload.model_dump()

    @router.post('/api/library/progress')
    def progress(payload: ProgressInput):
        with backend.connect() as db:
            require_conversation(db, payload.conversation_id)
            if payload.message_id is not None and not db.execute('SELECT 1 FROM message WHERE message_id=? AND conversation_id=?', (payload.message_id, payload.conversation_id)).fetchone():
                raise HTTPException(400, '阅读消息不属于该会话')
            db.execute('INSERT INTO conversation_state(owner_id,conversation_id,last_message_id,last_opened_at) VALUES(?,?,?,?) ON CONFLICT(owner_id,conversation_id) DO UPDATE SET last_message_id=COALESCE(excluded.last_message_id,conversation_state.last_message_id),last_opened_at=excluded.last_opened_at', (backend.DEMO_USER_ID, payload.conversation_id, payload.message_id, backend.now()))
        return {'saved': True}

    @router.get('/api/library/recent')
    def recent_reading():
        with backend.connect() as db:
            return [dict(row) for row in db.execute('SELECT cs.*,c.title FROM conversation_state cs JOIN conversation c USING(conversation_id) WHERE cs.owner_id=? AND c.owner_id=? AND cs.last_opened_at IS NOT NULL ORDER BY cs.last_opened_at DESC LIMIT 8', (backend.DEMO_USER_ID, backend.DEMO_USER_ID))]

    @router.get('/api/preferences')
    def preferences():
        with backend.connect() as db:
            row = db.execute('SELECT library_state FROM user_preference WHERE user_id=?', (backend.DEMO_USER_ID,)).fetchone()
        return json.loads(row[0]) if row else {}

    @router.post('/api/preferences')
    def save_preferences(payload: LibraryPreferences):
        with backend.connect() as db:
            db.execute('INSERT INTO user_preference(user_id,library_state) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET library_state=excluded.library_state', (backend.DEMO_USER_ID, payload.model_dump_json()))
        return {'saved': True}

    def snapshot(destination: Path):
        with backend.connect() as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)

    def counts(db):
        return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('conversation', 'message', 'annotation', 'review_card', 'review_log', 'conversation_state', 'user_preference')}

    @router.get('/api/backup')
    def download_backup():
        path = temp_root / f'{uuid.uuid4().hex}.sqlite3'
        snapshot(path)
        return FileResponse(path, filename='studyvault-complete.sqlite3', media_type='application/vnd.sqlite3', background=BackgroundTask(path.unlink, missing_ok=True))

    def validate_backup(uploaded: Path, prepared: Path):
        with uploaded.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                raise ValueError('该文件不是 SQLite 完整备份')
        with closing(sqlite3.connect(':memory:')) as expected:
            expected.executescript(backend.SCHEMA)
            expected_tables = {r[0] for r in expected.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with closing(sqlite3.connect(uploaded.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
                actual_tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                fts_tables = {name for name in expected_tables if name == 'message_fts' or name.startswith('message_fts_')}
                required = expected_tables - {'review_log', 'conversation_state', 'user_preference'} - fts_tables
                if not required <= actual_tables or not actual_tables <= expected_tables:
                    raise ValueError('备份不是受支持的 StudyVault 数据库')
                expected_objects = set(expected.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('trigger','view')"))
                if not set(source.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('trigger','view')")) <= expected_objects:
                    raise ValueError('备份包含不支持的触发器或视图')
                for table in actual_tables:
                    actual = [r[1:3] for r in source.execute(f'PRAGMA table_info({table})')]
                    wanted = [r[1:3] for r in expected.execute(f'PRAGMA table_info({table})')]
                    later = {name for name, _ in backend.MIGRATION_COLUMNS.get(table, [])}
                    if actual != wanted[:len(actual)] or not {name for name, _ in wanted[len(actual):]} <= later:
                        raise ValueError(f'备份表结构不兼容：{table}')
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or source.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('备份完整性或外键检查失败')
                with closing(sqlite3.connect(prepared)) as target:
                    source.backup(target)
        with closing(sqlite3.connect(prepared)) as db:
            # Discard the uploaded full-text index; init_db() rebuilds it from message after restore.
            for trigger in ('message_fts_ai', 'message_fts_ad', 'message_fts_au'):
                db.execute(f'DROP TRIGGER IF EXISTS {trigger}')
            db.execute('DROP TABLE IF EXISTS message_fts')
            db.executescript(backend.SCHEMA)
            if not db.execute('SELECT 1 FROM app_user WHERE user_id=?', (backend.DEMO_USER_ID,)).fetchone():
                raise ValueError('备份缺少当前演示账户')
            return counts(db)

    @router.post('/api/backup/preview')
    async def preview_backup(file: UploadFile = File(...)):
        token, item = new_session('backup')
        path, prepared = item['directory'] / 'upload.sqlite3', item['directory'] / 'prepared.sqlite3'
        await save_upload(file, path, 200 * 1024 * 1024)
        try:
            item['counts'] = validate_backup(path, prepared)
        except (sqlite3.Error, ValueError) as exc:
            previews.pop(token, None)
            raise HTTPException(400, str(exc)) from exc
        item['prepared'] = prepared
        return {'token': token, 'counts': item['counts'], 'confirmation_required': True}

    @router.post('/api/backup/restore')
    def restore_backup(payload: RestoreInput):
        item = session(payload.token, 'backup')
        with task_lock:
            if any(task['database'] == database_key() and task['status'] in {'queued', 'running'} for task in tasks.values()):
                raise HTTPException(409, '请先等待导入完成或停止导入，再恢复备份')
        with backend.DATA_LOCK:
            item = session(payload.token, 'backup')
            with task_lock:
                if any(task['database'] == database_key() and task['status'] in {'queued', 'running'} for task in tasks.values()):
                    raise HTTPException(409, '请先等待导入完成或停止导入，再恢复备份')
            backup_id = uuid.uuid4().hex
            safety = backend.DB_PATH.parent / 'backups' / f'{backup_id}.sqlite3'
            safety.parent.mkdir(parents=True, exist_ok=True)
            snapshot(safety)
            try:
                with closing(sqlite3.connect(item['prepared'])) as source, closing(sqlite3.connect(backend.DB_PATH)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or target.execute('PRAGMA foreign_key_check').fetchone():
                        raise ValueError('恢复后的完整性检查失败')
                backend.init_db()
            except BaseException:
                with closing(sqlite3.connect(safety)) as source, closing(sqlite3.connect(backend.DB_PATH)) as target:
                    source.backup(target)
                raise
            previews.pop(payload.token, None)
            with backend.connect() as db:
                restored_counts = counts(db)
        return {'restored': True, 'counts': restored_counts, 'safety_backup_id': backup_id, 'safety_backup_url': f'/api/backup/safety/{backup_id}'}

    @router.get('/api/backup/safety/{backup_id}')
    def safety_backup(backup_id: str):
        if not re.fullmatch('[0-9a-f]{32}', backup_id):
            raise HTTPException(404, '恢复前备份不存在')
        path = backend.DB_PATH.parent / 'backups' / f'{backup_id}.sqlite3'
        if not path.is_file():
            raise HTTPException(404, '恢复前备份不存在')
        return FileResponse(path, filename=f'studyvault-before-restore-{backup_id}.sqlite3', media_type='application/vnd.sqlite3')

    def detect_platform(path: Path):
        for record in itertools.islice(backend.iter_records(path), 40):
            kind = record.get('type')
            if kind in {'session_meta', 'response_item'}:
                return 'Codex'
            if kind in {'user/message', 'assistant/message', 'session/title'}:
                return 'DeepSeek'
            if isinstance(record.get('messages'), list) and any(m.get('type') in {'gemini', 'user'} for m in record['messages'] if isinstance(m, dict)):
                return 'Gemini CLI'
            if kind in {'user', 'assistant'} and isinstance(record.get('message'), dict):
                return 'Claude Code'
            if kind == 'message' and isinstance(record.get('message'), dict):
                return 'Pi'
        raise ValueError('未识别出支持的 Agent 格式，请选择来源平台后重试')

    @router.post('/api/import/preview')
    async def preview_import(files: list[UploadFile] = File(...), platform: str = Form('auto')):
        if not 1 <= len(files) <= 20 or platform not in {'auto', *backend.SOURCE_ROOTS}:
            raise HTTPException(400, '每批请选择 1–20 个文件和有效来源平台')
        token, item = new_session('import')
        candidates, total_bytes = [], 0
        for index, upload in enumerate(files):
            name = Path((upload.filename or 'unnamed').replace('\\', '/')).name
            candidate: dict[str, Any] = {'index': index, 'file_name': name, 'status': 'INVALID', 'message_count': 0}
            candidates.append(candidate)
            if Path(name).suffix.lower() not in {'.json', '.jsonl'}:
                candidate['error'] = '支持未压缩的 .json / .jsonl Agent 会话文件'
                await upload.close()
                continue
            folder = item['directory'] / str(index)
            folder.mkdir()
            path = folder / (Path(name).stem + Path(name).suffix.lower())
            total_bytes += await save_upload(upload, path, 100 * 1024 * 1024 - total_bytes)
            try:
                source = detect_platform(path) if platform == 'auto' else platform
                parsed = backend.parse_conversation(path, source)
                parsed['path'] = f'upload/{name}'
                with backend.connect() as db:
                    source_id = backend.source_row(db, source)['source_id']
                    duplicate = bool(db.execute('SELECT 1 FROM conversation WHERE owner_id=? AND source_id=? AND source_conversation_id=?', (backend.DEMO_USER_ID, source_id, parsed['source_id'])).fetchone())
                candidate.update(platform=source, title=parsed['title'], message_count=len(parsed['messages']), status='DUPLICATE' if duplicate else 'NEW', parsed=parsed)
            except (ValueError, OSError, RuntimeError, TypeError, AttributeError, KeyError) as exc:
                candidate['error'] = str(exc).replace(str(item['directory']), '上传文件')
        item['candidates'] = candidates
        return {'token': token, 'candidates': [{k: v for k, v in row.items() if k != 'parsed'} for row in candidates], 'counts': {status: sum(row['status'] == status for row in candidates) for status in ('NEW', 'DUPLICATE', 'INVALID')}}

    def worker(task_id: str, item, selected, database: str):
        with task_lock:
            task = tasks[task_id]
            task['status'] = 'running'
        try:
            for index in selected:
                with task_lock:
                    if task['cancel_requested']:
                        task['status'] = 'cancelled'
                        break
                    task['current_file'] = item['candidates'][index]['file_name']
                candidate = item['candidates'][index]
                try:
                    with backend.DATA_LOCK:
                        if database_key() != database:
                            raise ValueError('当前数据库已切换，停止导入')
                        result = backend.import_parsed(candidate['parsed'], candidate['platform'])
                    outcome = {'file_name': candidate['file_name'], 'status': result['status'], 'messages': result['messages']}
                except Exception as exc:
                    outcome = {'file_name': candidate['file_name'], 'status': 'FAILED', 'error': str(exc)}
                with task_lock:
                    task['processed'] += 1
                    task[{'SUCCESS': 'imported', 'SKIPPED': 'skipped', 'FAILED': 'failed'}[outcome['status']]] += 1
                    task['results'].append(outcome)
            with task_lock:
                if task['status'] == 'running':
                    task['status'] = 'completed'
        finally:
            item['running'] = False

    @router.post('/api/import/tasks')
    def start_import(payload: ImportInput):
        item = session(payload.preview_token, 'import')
        selected = list(dict.fromkeys(payload.selected))
        if any(index < 0 or index >= len(item['candidates']) or item['candidates'][index]['status'] == 'INVALID' for index in selected):
            raise HTTPException(400, '选择中包含不可导入的文件')
        with backend.DATA_LOCK:
            with task_lock:
                if item.get('used'):
                    raise HTTPException(409, '该预览已启动过导入，请重新预览')
                item['used'] = item['running'] = True
                task_id = uuid.uuid4().hex
                tasks[task_id] = {'task_id': task_id, 'database': database_key(), 'status': 'queued', 'total': len(selected), 'processed': 0, 'imported': 0, 'skipped': 0, 'failed': 0, 'results': [], 'cancel_requested': False, 'current_file': ''}
            threading.Thread(target=worker, args=(task_id, item, selected, database_key()), daemon=True).start()
        return {'task_id': task_id}

    def get_task(task_id):
        task = tasks.get(task_id)
        if not task or task['database'] != database_key():
            raise HTTPException(404, '导入任务不存在或不属于当前数据库')
        return task

    @router.get('/api/import/tasks/{task_id}')
    def task_status(task_id: str):
        with task_lock:
            return {k: v for k, v in copy.deepcopy(get_task(task_id)).items() if k != 'database'}

    @router.post('/api/import/tasks/{task_id}/cancel')
    def cancel_task(task_id: str):
        with task_lock:
            get_task(task_id)['cancel_requested'] = True
        return {'cancel_requested': True}

    return router
