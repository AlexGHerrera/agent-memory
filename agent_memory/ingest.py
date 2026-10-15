"""Idempotent ingestion: Claude Code JSONL transcripts, OpenClaw transcripts, a read-only Markdown vault and notes.

Idempotency: every message has a unique `uid` (source uuid or hash) and is inserted with INSERT OR IGNORE.
JSONL files are read incrementally by offset (ingest_state table); if a file shrinks it is read again from the
start and the uid prevents duplicates.

Claude Code JSONL is also the generic input format: any agent can write records in that shape (see
docs/INGESTION.md) and list the folder in `claude_sources`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db import (ROLE_COMPACT_SUMMARY, ROLE_NOTE, ROLE_VAULT, SOURCE_NOTE, SOURCE_VAULT, VAULT_SESSION_PREFIX,
                 local_date, now_iso, parse_ts, utc_iso)
from .secrets_filter import SecretFilter

log = logging.getLogger("agent_memory.ingest")

EMBED_ROLES = {"user", "assistant", ROLE_NOTE, ROLE_COMPACT_SUMMARY, ROLE_VAULT}


@dataclass
class Stats:
    inserted: int = 0
    updated: int = 0
    skipped_excluded: int = 0
    redactions: int = 0
    files: int = 0
    sessions: set = field(default_factory=set)

    def as_dict(self):
        return {"inserted": self.inserted, "updated": self.updated, "excluded": self.skipped_excluded,
                "redactions": self.redactions, "files": self.files, "sessions_touched": len(self.sessions)}


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, cfg, sfilter: SecretFilter, embedder=None):
        self.conn = conn
        self.cfg = cfg
        self.sfilter = sfilter
        self.embedder = embedder
        self.stats = Stats()
        self.excluded = set(cfg.exclude_sessions)

    # ------------------------------------------------------------------ basics
    def is_excluded(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        base = session_id.split("/")[0]
        return base in self.excluded or session_id in self.excluded

    def _clean(self, text: str) -> tuple[str, int]:
        text, n = self.sfilter.redact(text)
        if len(text) > self.cfg.max_message_chars:
            text = text[: self.cfg.max_message_chars] + f"\n[... truncated: {len(text)} characters in total]"
        return text, n

    def add_message(self, uid: str, source: str, machine: str | None, project: str | None,
                    session_id: str | None, role: str, ts: datetime, text: str,
                    date_override: str | None = None, upsert: bool = False, prior_redactions: int = 0) -> bool:
        if self.is_excluded(session_id):
            self.stats.skipped_excluded += 1
            return False
        text, n = self._clean(text)
        n += prior_redactions  # redactions already made by the caller (text already filtered)
        if not text.strip():
            return False
        embedded = 0 if role in EMBED_ROLES and len(text.strip()) >= 8 else -1
        ld = date_override or local_date(ts, self.cfg.timezone)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO raw_messages(uid, source, machine, project, session_id, role, ts, local_date,"
            " text, redactions, embedded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, source, machine, project, session_id, role, utc_iso(ts), ld, text, n, embedded))
        if cur.rowcount:
            self.stats.inserted += 1
            self.stats.redactions += n
            if session_id:
                self.stats.sessions.add(session_id)
            return True
        if upsert:
            row = self.conn.execute("SELECT id, text FROM raw_messages WHERE uid=?", (uid,)).fetchone()
            if row and row["text"] != text:
                self.conn.execute("UPDATE raw_messages SET text=?, redactions=?, embedded=? WHERE id=?",
                                  (text, n, embedded, row["id"]))
                self.conn.execute("DELETE FROM vec_messages WHERE rowid=?", (row["id"],))
                self.stats.updated += 1
                if session_id:
                    self.stats.sessions.add(session_id)
        return False

    # ------------------------------------------------------------------ Claude Code
    def ingest_claude_root(self, root: str, machine: str) -> None:
        rootp = Path(os.path.expanduser(root))
        if not rootp.is_dir():
            log.info("source does not exist, skipping: %s", rootp)
            return
        for path in sorted(rootp.rglob("*.jsonl")):
            rel = path.relative_to(rootp)
            projdir = rel.parts[0] if len(rel.parts) > 1 else ""
            if projdir in self.cfg.exclude_projects:
                continue
            # Exclusion by file name or session folder (covers subagents)
            if any(p.removesuffix(".jsonl") in self.excluded for p in rel.parts):
                self.stats.skipped_excluded += 1
                continue
            try:
                self.ingest_claude_file(path, machine, projdir)
            except Exception:  # a corrupt file does not stop ingestion
                log.exception("error ingesting %s", path)

    def ingest_claude_file(self, path: Path, machine: str, projdir: str = "") -> None:
        st = path.stat()
        key = str(path)
        row = self.conn.execute("SELECT offset, size, mtime FROM ingest_state WHERE path=?", (key,)).fetchone()
        offset = 0
        if row:
            if row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                return
            offset = row["offset"] if st.st_size >= row["offset"] else 0
        project_default = _project_from_dir(projdir)
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        # Complete lines only: the last one may be half-written
        end = data.rfind(b"\n")
        if end < 0:
            return
        chunk = data[: end + 1]
        self.stats.files += 1
        for raw in chunk.splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                self._claude_record(rec, machine, project_default, path)
        self.conn.execute("INSERT OR REPLACE INTO ingest_state(path, offset, size, mtime) VALUES (?,?,?,?)",
                          (key, offset + end + 1, st.st_size, st.st_mtime))
        self.conn.commit()

    def _claude_record(self, rec: dict, machine: str, project_default: str, path: Path) -> None:
        typ = rec.get("type")
        if typ not in ("user", "assistant"):
            return
        msg = rec.get("message") or {}
        base_sid = rec.get("sessionId") or path.stem
        sid = base_sid
        parent = None
        if rec.get("isSidechain") and rec.get("agentId"):
            sid = f"{base_sid}/agent-{rec['agentId']}"
            parent = base_sid
        if self.is_excluded(sid):
            self.stats.skipped_excluded += 1
            return
        ts = parse_ts(rec.get("timestamp")) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        project = _project_from_cwd(rec.get("cwd")) or project_default
        uuid = rec.get("uuid") or hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()
        content = msg.get("content")
        parts: list[tuple[str, str]] = []
        if isinstance(content, str):
            if not rec.get("isMeta"):
                role = ROLE_COMPACT_SUMMARY if rec.get("isCompactSummary") else typ
                parts.append((role, content))
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if rec.get("isMeta"):
                        continue
                    role = ROLE_COMPACT_SUMMARY if rec.get("isCompactSummary") else typ
                    parts.append((role, b.get("text", "")))
                elif bt == "tool_use":
                    parts.append(("tool_use", f"{b.get('name')}: {json.dumps(b.get('input'), ensure_ascii=False)}"))
                elif bt == "tool_result":
                    parts.append(("tool_result", _tool_result_text(b.get("content"))))
                elif bt == "image":
                    parts.append((typ, "[image]"))
        for i, (role, text) in enumerate(parts):
            if self.add_message(f"cc:{uuid}:{i}", "claude", machine, project, sid, role, ts, text):
                self._touch_session(sid, "claude", machine, project, parent, str(path))

    # ------------------------------------------------------------------ Markdown vault (read-only)
    def ingest_vault(self, root: str) -> None:
        """Markdown notes of the vault. READ-ONLY: files are only opened for reading, nothing is ever written,
        moved or deleted there. A note whose size/mtime changed is read again; a new row is stored only when its
        content hash changed (uid vault:<path>:<sha>), so every version is kept and a touch adds nothing.
        Deleted notes keep their stored versions. Symlinks and anything outside the vault are skipped."""
        rootp = Path(root)
        if not rootp.is_dir():
            log.info("vault does not exist or is not readable, skipping: %s", root)
            return
        real_root = rootp.resolve()
        excluded = set(self.cfg.vault_exclude_dirs)
        max_bytes = self.cfg.vault_max_file_kb * 1024
        for path in sorted(rootp.rglob("*.md")):
            rel = path.relative_to(rootp)
            if any(p in excluded for p in rel.parts[:-1]) or path.is_symlink():
                continue
            try:
                if not path.resolve().is_relative_to(real_root) or not path.is_file():
                    continue
                st = path.stat()
                key = f"{VAULT_SESSION_PREFIX}{path}"
                row = self.conn.execute("SELECT size, mtime FROM ingest_state WHERE path=?", (key,)).fetchone()
                if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                    continue
                if st.st_size > max_bytes:
                    log.warning("vault note too big, skipping: %s (%d KB)", rel, st.st_size // 1024)
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                log.warning("cannot read vault note %s: %s", rel, e)
                continue
            self.stats.files += 1
            relp = rel.as_posix()
            sid = f"{VAULT_SESSION_PREFIX}{relp}"
            ts = datetime.fromtimestamp(st.st_mtime, timezone.utc)
            uid = f"{VAULT_SESSION_PREFIX}{relp}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
            project = rel.parts[0] if len(rel.parts) > 1 else "vault"
            if self.add_message(uid, SOURCE_VAULT, "vault", project, sid, ROLE_VAULT, ts, text):
                self._touch_session(sid, SOURCE_VAULT, "vault", project, None, str(path))
                self.conn.execute("UPDATE sessions SET title=? WHERE id=?", (relp, sid))
            self.conn.execute("INSERT OR REPLACE INTO ingest_state(path, offset, size, mtime) VALUES (?,?,?,?)",
                              (key, st.st_size, st.st_size, st.st_mtime))
            self.conn.commit()

    # ------------------------------------------------------------------ Notes
    def add_note(self, text: str, project: str | None = None) -> dict:
        ts = datetime.now(timezone.utc)
        clean, n = self._clean(text)
        day = local_date(ts, self.cfg.timezone)
        uid = f"note:{utc_iso(ts)}:{hashlib.sha256(clean.encode()).hexdigest()[:12]}"
        sid = f"notes-{day}"
        cur = self.conn.execute("INSERT INTO notes(ts, project, text, raw_uid) VALUES (?,?,?,?)",
                                (utc_iso(ts), project, clean, uid))
        self.add_message(uid, SOURCE_NOTE, "memory", project, sid, ROLE_NOTE, ts, clean)
        self._touch_session(sid, SOURCE_NOTE, "memory", project, None, None)
        self.refresh_sessions()
        self.conn.commit()
        return {"id": cur.lastrowid, "date": day, "redactions": n, "session": sid}

    # ------------------------------------------------------------------ Sessions and embeddings
    def _touch_session(self, sid, source, machine, project, parent, file_path):
        self.conn.execute(
            "INSERT INTO sessions(id, source, machine, project, parent_id, file_path, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,"
            " project=COALESCE(sessions.project, excluded.project)",
            (sid, source, machine, project, parent, file_path, now_iso()))

    def refresh_sessions(self) -> None:
        for sid in self.stats.sessions:
            agg = self.conn.execute(
                "SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM raw_messages WHERE session_id=?", (sid,)).fetchone()
            first = self.conn.execute(
                "SELECT text FROM raw_messages WHERE session_id=? AND role IN ('user',?)"
                " ORDER BY ts, id LIMIT 1", (sid, ROLE_NOTE)).fetchone()
            title = " ".join((first["text"] if first else "").split())[:120] or None
            self.conn.execute(
                "UPDATE sessions SET started_at=?, ended_at=?, n_messages=?, title=COALESCE(title, ?) WHERE id=?",
                (agg["a"], agg["b"], agg["n"], title, sid))
        self.conn.commit()

    def embed_pending(self, batch: int = 64) -> int:
        if self.embedder is None:
            return 0
        done = 0
        while True:
            rows = self.conn.execute(
                "SELECT id, text FROM raw_messages WHERE embedded=0 ORDER BY id LIMIT ?", (batch,)).fetchall()
            if not rows:
                break
            vecs = self.embedder.passages([r["text"][: self.cfg.embed_chars] for r in rows])
            for r, v in zip(rows, vecs):
                self.conn.execute("DELETE FROM vec_messages WHERE rowid=?", (r["id"],))
                self.conn.execute("INSERT INTO vec_messages(rowid, embedding) VALUES (?, ?)",
                                  (r["id"], v.astype("float32").tobytes()))
                self.conn.execute("UPDATE raw_messages SET embedded=1 WHERE id=?", (r["id"],))
            self.conn.commit()
            done += len(rows)
        return done

    def run_all(self) -> dict:
        for src in self.cfg.claude_sources:
            self.ingest_claude_root(src.path, src.machine)
        if self.cfg.openclaw_sources:
            from .openclaw import OpenClawIngestor
            oc = OpenClawIngestor(self)
            for src in self.cfg.openclaw_sources:
                oc.ingest_root(src)
        if self.cfg.vault_path:
            self.ingest_vault(self.cfg.vault_path)
        self.refresh_sessions()
        n = self.embed_pending()
        out = self.stats.as_dict()
        out["embedded"] = n
        return out


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
                elif c.get("type") == "image":
                    out.append("[image]")
        return "\n".join(out)
    return "" if content is None else str(content)


def _project_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    # Agent worktrees count as the parent project
    if "/.claude/worktrees/" in cwd:
        cwd = cwd.split("/.claude/worktrees/")[0]
    return os.path.basename(cwd.rstrip("/")) or cwd


def _project_from_dir(projdir: str) -> str:
    """Claude Code project folder -> project name (approximate): "-home-alice-work-api" -> "work-api"."""
    if not projdir:
        return "unknown"
    parts = projdir.strip("-").split("-")
    if parts[:1] in (["home"], ["Users"]) and len(parts) > 2:
        parts = parts[2:]
    elif parts[:1] == ["root"] and len(parts) > 1:
        parts = parts[1:]
    return "-".join(parts) or projdir
