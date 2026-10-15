"""SQLite schema: raw messages + FTS5 + sqlite-vec. Use the constants below instead of spelling stored values out."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import sqlite_vec

# Stored summary levels (CHECK constraint in `summaries`).
LEVEL_SESSION = "session"
LEVEL_DAY = "day"
LEVEL_WEEK = "week"
LEVEL_MONTH = "month"
LEVEL_YEAR = "year"
LEVEL_NAMES = {LEVEL_SESSION: "session", LEVEL_DAY: "day", LEVEL_WEEK: "week", LEVEL_MONTH: "month",
               LEVEL_YEAR: "year"}
# Stored roles/sources that are not plain user/assistant/tool_use/tool_result.
ROLE_NOTE = "note"                      # a note saved with the `note` tool
ROLE_COMPACT_SUMMARY = "compact_summary"   # Claude Code compaction summary
SOURCE_NOTE = "note"
SOURCE_OPENCLAW = "openclaw"
# Optional read-only Markdown vault source (config `vault_path`): one session per note, one row per version.
SOURCE_VAULT = "vault"
ROLE_VAULT = "vault"
VAULT_SESSION_PREFIX = "vault:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,          -- claude | openclaw | note | vault
    machine     TEXT,
    project     TEXT,
    parent_id   TEXT,                   -- parent session (subagents)
    title       TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    n_messages  INTEGER DEFAULT 0,
    file_path   TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,   -- source uuid or hash: idempotent ingestion
    source      TEXT NOT NULL,
    machine     TEXT,
    project     TEXT,
    session_id  TEXT,
    role        TEXT NOT NULL,          -- user | assistant | tool_use | tool_result | note | compact_summary |
                                        -- vault
    ts          TEXT NOT NULL,          -- ISO 8601 UTC
    local_date  TEXT NOT NULL,          -- YYYY-MM-DD in the user's timezone
    text        TEXT NOT NULL,
    redactions  INTEGER DEFAULT 0,
    embedded    INTEGER DEFAULT 0       -- 0 pending, 1 done, -1 never embedded (tools)
);
CREATE INDEX IF NOT EXISTS raw_session ON raw_messages(session_id, ts);
CREATE INDEX IF NOT EXISTS raw_date ON raw_messages(local_date);
CREATE INDEX IF NOT EXISTS raw_embedded ON raw_messages(embedded);

CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
    text, content='raw_messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS raw_ai AFTER INSERT ON raw_messages BEGIN
    INSERT INTO raw_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS raw_ad AFTER DELETE ON raw_messages BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS raw_au AFTER UPDATE OF text ON raw_messages BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO raw_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY,
    level       TEXT NOT NULL CHECK (level IN ('session','day','week','month','year')),
    period      TEXT NOT NULL,          -- session id | YYYY-MM-DD | YYYY-Www | YYYY-MM | YYYY
    headline    TEXT,
    text        TEXT NOT NULL,
    session_ids TEXT NOT NULL DEFAULT '[]',  -- JSON: link to the raw messages
    input_hash  TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(level, period)
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    project     TEXT,
    text        TEXT NOT NULL,
    raw_uid     TEXT
);

CREATE TABLE IF NOT EXISTS ingest_state (
    path        TEXT PRIMARY KEY,
    offset      INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL
);
"""


def connect(db_path: str, dim: int = 384) -> sqlite3.Connection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(embedding float[{dim}])")
    conn.commit()
    return conn


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def local_date(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def now_iso() -> str:
    return utc_iso(datetime.now(timezone.utc))
