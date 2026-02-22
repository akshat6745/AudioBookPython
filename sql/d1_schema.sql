-- =========================================================
-- Cloudflare D1 Schema for AudioBookPython
-- Run via:  wrangler d1 execute audiobookpython-db --file=sql/d1_schema.sql
-- Or:       paste into Cloudflare D1 Console
-- =========================================================

-- ── NOVELS ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS novels (
    id              TEXT PRIMARY KEY,           -- slug e.g. "shadow_slave"
    title           TEXT NOT NULL,
    author          TEXT,
    description     TEXT,
    cover_url       TEXT,                       -- R2 path or CDN URL
    language        TEXT DEFAULT 'en',
    total_chapters  INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ── CHAPTERS (metadata only — content in R2) ──────────────
CREATE TABLE IF NOT EXISTS chapters (
    id              TEXT PRIMARY KEY,           -- e.g. "shadow_slave_ch_1"
    novel_id        TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_number  INTEGER NOT NULL,
    title           TEXT,
    r2_content_path TEXT NOT NULL,              -- "novels/shadow_slave/chapter_1.txt.gz"
    r2_audio_path   TEXT,                       -- "audio/shadow_slave/chapter_1.mp3"
    word_count      INTEGER,
    file_size_bytes INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(novel_id, chapter_number)
);

-- ── USERS ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    username    TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,                  -- bcrypt hashed
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- ── USER PROGRESS ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_progress (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    novel_id        TEXT NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_number  INTEGER NOT NULL DEFAULT 1,
    audio_position  REAL DEFAULT 0.0,           -- seconds into audio
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, novel_id)
);


-- ── INDEXES ───────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_chapters_novel_id ON chapters(novel_id);
CREATE INDEX IF NOT EXISTS idx_chapters_order    ON chapters(novel_id, chapter_number);
CREATE INDEX IF NOT EXISTS idx_progress_user     ON user_progress(user_id);
