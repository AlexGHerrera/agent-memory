"""Read views for the MCP: day and session."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import LEVEL_DAY, LEVEL_SESSION, ROLE_COMPACT_SUMMARY, ROLE_NOTE, SOURCE_NOTE, parse_ts
from .i18n import t

PAGE_CHARS = 60000
TOOL_PREVIEW = 300
ROLE_LABEL = {"user": "user", "assistant": "assistant", "tool_use": "tool", "tool_result": "result",
              ROLE_NOTE: "note", ROLE_COMPACT_SUMMARY: "compaction summary"}

TODAY = ("today", "hoy", "")
YESTERDAY = ("yesterday", "ayer")
DAY_BEFORE_YESTERDAY = ("anteayer",)


def resolve_date(value: str, tz: str) -> str:
    """'today'/'hoy', 'yesterday'/'ayer', 'anteayer' or YYYY-MM-DD -> YYYY-MM-DD (ValueError otherwise)."""
    f = (value or "").strip().lower()
    today = datetime.now(ZoneInfo(tz)).date()
    if f in TODAY:
        return today.isoformat()
    if f in YESTERDAY:
        return (today - timedelta(days=1)).isoformat()
    if f in DAY_BEFORE_YESTERDAY:
        return (today - timedelta(days=2)).isoformat()
    return date.fromisoformat(f).isoformat()


def _hm(ts: str, tz: str) -> str:
    dt = parse_ts(ts)
    return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M") if dt else "?"


def _local(ts: str | None, tz: str) -> str:
    dt = parse_ts(ts)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") if dt else "?"


def local_time(ts: str | None, tz: str) -> str:
    """'YYYY-MM-DD HH:MM' in the given timezone, labelled with it (search results)."""
    return f"{_local(ts, tz)} ({tz})"


def day(conn: sqlite3.Connection, value: str, tz: str, lang: str = "en") -> str:
    """Day view: summary, sessions with their ids, notes and old memories. `lang` localizes the headings."""
    try:
        d = resolve_date(value, tz)
    except ValueError:
        return t(lang, "invalid_date", date=value)
    out = [t(lang, "day_title", d=d)]
    s = conn.execute("SELECT headline, text, created_at FROM summaries WHERE level=? AND period=?",
                     (LEVEL_DAY, d)).fetchone()
    if s:
        out += ["", t(lang, "day_summary"), s["text"].strip(), "", t(lang, "summary_generated", at=s["created_at"])]
    else:
        out += ["", t(lang, "no_day_summary")]

    rows = conn.execute(
        "SELECT r.session_id sid, COUNT(*) n, MIN(r.ts) a, MAX(r.ts) b, s.project, s.machine, s.source,"
        " s.parent_id, s.title FROM raw_messages r LEFT JOIN sessions s ON s.id = r.session_id"
        " WHERE r.local_date = ? AND r.session_id IS NOT NULL GROUP BY r.session_id ORDER BY a", (d,)).fetchall()
    if not rows:
        out += ["", t(lang, "no_messages")]
        return "\n".join(out)

    if any(r["source"] != SOURCE_NOTE for r in rows):
        out += ["", t(lang, "sessions_heading")]
    subagents: dict[str, int] = {}
    for r in rows:
        if r["parent_id"]:
            subagents[r["parent_id"]] = subagents.get(r["parent_id"], 0) + 1
    for r in rows:
        if r["source"] == SOURCE_NOTE or r["parent_id"]:
            continue  # subagents are listed inside session(id) of their parent
        ss = conn.execute("SELECT headline, text FROM summaries WHERE level=? AND period=?",
                          (LEVEL_SESSION, r["sid"])).fetchone()
        sub = t(lang, "n_subagents", n=subagents[r["sid"]]) if r["sid"] in subagents else ""
        out.append(t(lang, "session_line", sid=r["sid"], project=r["project"] or "?", machine=r["machine"] or "?",
                     start=_hm(r["a"], tz), end=_hm(r["b"], tz), n=r["n"], sub=sub))
        if ss:
            out.append(f"  {ss['headline'] or _short(ss['text'], 200)}")
        elif r["title"]:
            out.append(t(lang, "session_start", title=r["title"]))
    orphans = [r for r in rows if r["parent_id"] and r["parent_id"] not in {x["sid"] for x in rows}]
    for r in orphans:
        out.append(t(lang, "orphan_line", sid=r["sid"], parent=r["parent_id"], start=_hm(r["a"], tz),
                     end=_hm(r["b"], tz)))

    notes = conn.execute("SELECT ts, project, text FROM notes WHERE raw_uid IN "
                         "(SELECT uid FROM raw_messages WHERE local_date=? AND role=?) ORDER BY ts",
                         (d, ROLE_NOTE)).fetchall()
    if notes:
        out += ["", t(lang, "notes_heading")]
        for n in notes:
            out.append(f"- {_hm(n['ts'], tz)} [{n['project'] or 'general'}] {n['text']}")
    return "\n".join(out)


def _short(text: str, n: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def session(conn: sqlite3.Connection, ident: str, tz: str) -> str:
    """Raw transcript of a session (exact text), paginated with 'id#2', 'id#3'..."""
    ident = (ident or "").strip()
    page = 1
    m = re.match(r"^(.*)#(\d+)$", ident)
    if m:
        ident, page = m.group(1), int(m.group(2))
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (ident,)).fetchone()
    if not s:
        cands = conn.execute("SELECT id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 5",
                             (ident + "%",)).fetchall()
        if len(cands) == 1:
            s = conn.execute("SELECT * FROM sessions WHERE id=?", (cands[0]["id"],)).fetchone()
        elif cands:
            return "Ambiguous id. Candidates: " + ", ".join(c["id"] for c in cands)
        else:
            return f"No session {ident!r}."
    sid = s["id"]
    head = [f"# Session {sid}",
            f"project: {s['project']} · machine: {s['machine']} · source: {s['source']} · "
            f"{_local(s['started_at'], tz)} → {_local(s['ended_at'], tz)} ({tz}) · {s['n_messages']} messages"]
    if s["parent_id"]:
        head.append(f"subagent of: {s['parent_id']}")
    subs = conn.execute("SELECT id FROM sessions WHERE parent_id=? ORDER BY started_at", (sid,)).fetchall()
    if subs:
        head.append("subagents: " + ", ".join(x["id"] for x in subs))
    summ = conn.execute("SELECT text FROM summaries WHERE level=? AND period=?", (LEVEL_SESSION, sid)).fetchone()
    if page == 1 and summ:
        head += ["", "## Summary", summ["text"].strip()]
    head += ["", "## Raw transcript (exact text of the user and the assistant; tools abbreviated)"]

    blocks = []
    for r in conn.execute("SELECT ts, role, text FROM raw_messages WHERE session_id=? ORDER BY ts, id", (sid,)):
        dt = parse_ts(r["ts"])
        stamp = dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S") if dt else r["ts"]
        text = r["text"]
        if r["role"] in ("tool_use", "tool_result"):
            flat = " ".join(text.split())
            text = flat[:TOOL_PREVIEW] + ("…" if len(flat) > TOOL_PREVIEW else "")
            blocks.append(f"  ↳ [{stamp}] {ROLE_LABEL[r['role']]}: {text}")
        else:
            blocks.append(f"[{stamp}] {ROLE_LABEL.get(r['role'], r['role'])}:\n{text}\n")

    pages, cur, size = [], [], 0
    for b in blocks:
        if cur and size + len(b) > PAGE_CHARS:
            pages.append(cur); cur, size = [], 0
        cur.append(b); size += len(b) + 1
    if cur:
        pages.append(cur)
    if not pages:
        return "\n".join(head + ["(no messages)"])
    if page > len(pages):
        return f"Session {sid} has {len(pages)} pages."
    body = pages[page - 1]
    foot = []
    if len(pages) > 1:
        foot = ["", f"[page {page} of {len(pages)}"
                + (f"; next: session(\"{sid}#{page + 1}\")]" if page < len(pages) else "]")]
    return "\n".join(head + body + foot)


def session_ids_json(ids) -> str:
    return json.dumps(sorted(ids))
