"""Hybrid search: FTS5 (BM25) + vectors (sqlite-vec), fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import re
import sqlite3

RRF_K = 60
TOOL_ROLES = ("tool_use", "tool_result")


def fts_query(q: str) -> str | None:
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 1]
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words[:24])


def _filters(since, until, project, alias="r"):
    where, args = [], []
    if since:
        where.append(f"{alias}.local_date >= ?"); args.append(since)
    if until:
        where.append(f"{alias}.local_date <= ?"); args.append(until)
    if project:
        where.append(f"{alias}.project LIKE ?"); args.append(f"%{project}%")
    return where, args


def search(conn: sqlite3.Connection, embedder, query: str, since: str | None = None,
           until: str | None = None, project: str | None = None, limit: int = 10) -> list[dict]:
    where, args = _filters(since, until, project)
    extra = (" AND " + " AND ".join(where)) if where else ""
    ranks: dict[int, float] = {}

    q = fts_query(query)
    if q:
        try:
            rows = conn.execute(
                "SELECT r.id FROM raw_fts f JOIN raw_messages r ON r.id = f.rowid "
                f"WHERE raw_fts MATCH ?{extra} ORDER BY bm25(raw_fts) LIMIT 100", [q, *args]).fetchall()
            for i, r in enumerate(rows):
                ranks[r["id"]] = ranks.get(r["id"], 0) + 1 / (RRF_K + i + 1)
        except sqlite3.OperationalError:
            pass

    if embedder is not None:
        qv = embedder.query(query).astype("float32").tobytes()
        if where:
            rows = conn.execute(
                "SELECT r.id, vec_distance_cosine(v.embedding, ?) d FROM raw_messages r "
                "JOIN vec_messages v ON v.rowid = r.id WHERE r.embedded = 1 AND " + " AND ".join(where) +
                " ORDER BY d LIMIT 100", [qv, *args]).fetchall()
        else:
            rows = conn.execute(
                "SELECT rowid AS id, distance d FROM vec_messages WHERE embedding MATCH ? AND k = 100 ORDER BY d",
                [qv]).fetchall()
        for i, r in enumerate(rows):
            ranks[r["id"]] = ranks.get(r["id"], 0) + 1 / (RRF_K + i + 1)

    if not ranks:
        return []
    ids = list(ranks)
    rows = conn.execute(
        f"SELECT id, session_id, ts, local_date, project, role, source, machine, text FROM raw_messages "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out = []
    for r in rows:
        score = ranks[r["id"]] * (0.5 if r["role"] in TOOL_ROLES else 1.0)
        out.append({"id": r["id"], "session": r["session_id"], "ts": r["ts"], "date": r["local_date"],
                    "project": r["project"], "role": r["role"], "source": r["source"], "machine": r["machine"],
                    "score": round(score, 5), "snippet": snippet(r["text"], query)})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def snippet(text: str, query: str, width: int = 400) -> str:
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    low = flat.lower()
    pos = -1
    for w in sorted(re.findall(r"\w+", query.lower()), key=len, reverse=True):
        if len(w) > 2:
            pos = low.find(w)
            if pos >= 0:
                break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    return ("…" if start else "") + flat[start:start + width] + "…"
