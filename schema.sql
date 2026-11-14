-- StudyVault SQLite schema. Used by app.init_db().
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS app_role (
    role_id INTEGER PRIMARY KEY,
    role_code TEXT NOT NULL UNIQUE,
    role_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id INTEGER PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES app_role(role_id) ON DELETE RESTRICT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS course (
    course_id INTEGER PRIMARY KEY,
    course_code TEXT NOT NULL UNIQUE,
    course_name TEXT NOT NULL,
    term TEXT,
    instructor_name TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_point (
    point_id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES course(course_id) ON DELETE RESTRICT,
    parent_id INTEGER REFERENCES knowledge_point(point_id) ON DELETE RESTRICT,
    point_name TEXT NOT NULL,
    description TEXT,
    level_no INTEGER NOT NULL DEFAULT 1 CHECK(level_no > 0),
    UNIQUE(course_id, point_name)
);

CREATE TABLE IF NOT EXISTS conversation_source (
    source_id INTEGER PRIMARY KEY,
    platform_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT,
    UNIQUE(platform_name, source_type)
);

CREATE TABLE IF NOT EXISTS import_job (
    import_job_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES app_user(user_id),
    source_id INTEGER NOT NULL REFERENCES conversation_source(source_id),
    file_name TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('SUCCESS','SKIPPED','FAILED')),
    total_messages INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result_code TEXT NOT NULL DEFAULT 'SUCCESS' CHECK(result_code IN ('SUCCESS','SKIPPED_EMPTY','SKIPPED_DUPLICATE','FAILED_PARSE')),
    UNIQUE(owner_id, source_id, file_name, source_conversation_id)
);

CREATE TABLE IF NOT EXISTS conversation (
    conversation_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES app_user(user_id),
    course_id INTEGER NOT NULL REFERENCES course(course_id) ON DELETE RESTRICT,
    source_id INTEGER NOT NULL REFERENCES conversation_source(source_id),
    import_job_id INTEGER NOT NULL REFERENCES import_job(import_job_id),
    source_conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    visibility TEXT NOT NULL DEFAULT 'PRIVATE' CHECK(visibility IN ('PRIVATE','SHARED')),
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, source_id, source_conversation_id)
);

CREATE TABLE IF NOT EXISTS conversation_annotation (
    annotation_id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    summary_manual TEXT NOT NULL,
    intent TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    key_points_json TEXT NOT NULL,
    unresolved_questions_json TEXT NOT NULL,
    action_items_json TEXT NOT NULL,
    quality TEXT NOT NULL CHECK(quality IN ('有效','混合','低价值')),
    confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source TEXT NOT NULL DEFAULT 'AUTO' CHECK(source IN ('AUTO','MANUAL')),
    review_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(review_status IN ('PENDING','CONFIRMED','REVISED'))
);

CREATE TABLE IF NOT EXISTS message (
    message_id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    seq_no INTEGER NOT NULL,
    speaker TEXT NOT NULL CHECK(speaker IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(conversation_id, seq_no)
);

CREATE TABLE IF NOT EXISTS tag (
    tag_id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES app_user(user_id),
    tag_name TEXT NOT NULL,
    UNIQUE(owner_id, tag_name)
);

CREATE TABLE IF NOT EXISTS conversation_tag (
    conversation_id INTEGER NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tag(tag_id) ON DELETE CASCADE,
    PRIMARY KEY(conversation_id, tag_id)
);

CREATE TABLE IF NOT EXISTS conversation_point (
    conversation_id INTEGER NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    point_id INTEGER NOT NULL REFERENCES knowledge_point(point_id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1 CHECK(confidence >= 0 AND confidence <= 1),
    source TEXT NOT NULL DEFAULT 'rule',
    status TEXT NOT NULL DEFAULT 'PROPOSED' CHECK(status IN ('PROPOSED','CONFIRMED','REJECTED')),
    evidence TEXT,
    confirmed_by INTEGER REFERENCES app_user(user_id) ON DELETE RESTRICT,
    confirmed_at TEXT,
    PRIMARY KEY(conversation_id, point_id)
);

CREATE TABLE IF NOT EXISTS annotation (
    annotation_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES message(message_id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES app_user(user_id),
    annotation_type TEXT NOT NULL CHECK(annotation_type IN ('NOTE','QUESTION','CORRECTION','IMPORTANT')),
    content TEXT NOT NULL,
    is_resolved INTEGER NOT NULL DEFAULT 0 CHECK(is_resolved IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_card (
    review_card_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES message(message_id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES app_user(user_id),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    due_at TEXT NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 1,
    review_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUSPENDED','COMPLETED')),
    last_reviewed_at TEXT,
    UNIQUE(owner_id, message_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES app_user(user_id),
    operation TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id INTEGER,
    before_data TEXT,
    after_data TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversation_course_updated ON conversation(course_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_conversation_seq ON message(conversation_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_review_due ON review_card(owner_id, status, due_at);
CREATE INDEX IF NOT EXISTS idx_annotation_message ON annotation(message_id, is_resolved);

-- Additive migration: existing databases gain review history on next startup.
CREATE TABLE IF NOT EXISTS review_log (
    review_log_id INTEGER PRIMARY KEY,
    review_card_id INTEGER NOT NULL REFERENCES review_card(review_card_id) ON DELETE CASCADE,
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    grade TEXT NOT NULL CHECK(grade IN ('correct','wrong')),
    interval_before INTEGER NOT NULL,
    interval_after INTEGER NOT NULL,
    streak_before INTEGER NOT NULL,
    streak_after INTEGER NOT NULL,
    due_before TEXT NOT NULL,
    due_after TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_log_card_time ON review_log(review_card_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_curation_record ON audit_log(table_name, record_id, operation, audit_id DESC);

CREATE TABLE IF NOT EXISTS conversation_state (
    owner_id INTEGER NOT NULL REFERENCES app_user(user_id),
    conversation_id INTEGER NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    is_favorite INTEGER NOT NULL DEFAULT 0 CHECK(is_favorite IN (0,1)),
    last_message_id INTEGER REFERENCES message(message_id) ON DELETE SET NULL,
    last_opened_at TEXT,
    PRIMARY KEY(owner_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS user_preference (
    user_id INTEGER PRIMARY KEY REFERENCES app_user(user_id),
    library_state TEXT NOT NULL DEFAULT '{}'
);

-- Full-text index over message.content. External-content FTS5: the text lives only in
-- message, and the triggers below keep the index in step with inserts, updates and deletes.
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    content, conversation_id UNINDEXED, message_id UNINDEXED,
    content='message', content_rowid='message_id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS message_fts_ai AFTER INSERT ON message BEGIN
    INSERT INTO message_fts(rowid, content, conversation_id, message_id) VALUES (new.message_id, new.content, new.conversation_id, new.message_id);
END;
CREATE TRIGGER IF NOT EXISTS message_fts_ad AFTER DELETE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, content, conversation_id, message_id) VALUES ('delete', old.message_id, old.content, old.conversation_id, old.message_id);
END;
CREATE TRIGGER IF NOT EXISTS message_fts_au AFTER UPDATE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, content, conversation_id, message_id) VALUES ('delete', old.message_id, old.content, old.conversation_id, old.message_id);
    INSERT INTO message_fts(rowid, content, conversation_id, message_id) VALUES (new.message_id, new.content, new.conversation_id, new.message_id);
END;
