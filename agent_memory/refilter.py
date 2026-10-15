"""Refilters the database with the current secrets filter (agent-memory refilter).

Walks every text column (raw messages, notes, summaries, session titles and proposals) and runs SecretFilter
over it again. By default it is a dry run: it only counts rows and redactions per rule and never returns or
logs the values. With apply=True it writes: the raw_messages FTS reindexes itself (trigger raw_au) and embedded
rows that change lose their vector and become pending (embedded=0) so it is recomputed.
"""
from __future__ import annotations

from collections import Counter

from .secrets_filter import SecretFilter

# table -> text columns to refilter
TARGETS: list[tuple[str, tuple[str, ...]]] = [
    ("raw_messages", ("text",)),
    ("notes", ("text",)),
    ("summaries", ("headline", "text")),
    ("sessions", ("title",)),
    ("proposals", ("title", "body", "evidence", "skill_description", "result")),
]


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def scan(conn, sfilter: SecretFilter) -> tuple[dict, dict[str, list[tuple[int, dict[str, str], int]]]]:
    """Returns (report, changes). changes: table -> [(rowid, {column: new_text}, number of redactions)]."""
    report = {"rows": {}, "redactions": {}, "rows_by_rule": {}}
    changes: dict[str, list] = {}
    for table, wanted in TARGETS:
        have = _columns(conn, table)
        cols = [c for c in wanted if c in have]
        if not cols:
            continue
        rows_changed, by_rule, rows_by_rule = [], Counter(), Counter()
        for row in conn.execute(f"SELECT rowid, {', '.join(cols)} FROM {table}"):
            new, n_row, rules_row = {}, 0, set()
            for i, c in enumerate(cols, start=1):
                val = row[i]
                if not val:
                    continue
                red, counts = sfilter.redact_detail(val)
                if red != val:
                    new[c] = red
                    n_row += sum(counts.values())
                    by_rule.update(counts)
                    rules_row.update(counts)
            if new:
                rows_changed.append((row[0], new, n_row))
                rows_by_rule.update(rules_row)
        changes[table] = rows_changed
        report["rows"][table] = len(rows_changed)
        report["redactions"][table] = dict(by_rule.most_common())
        report["rows_by_rule"][table] = dict(rows_by_rule.most_common())
    return report, changes


def apply_changes(conn, changes: dict[str, list]) -> int:
    """Writes the changes in one transaction. Returns how many raw_messages rows need re-embedding."""
    reembed = 0
    with conn:
        for table, rows in changes.items():
            for rowid, new, n in rows:
                sets = ", ".join(f"{c}=?" for c in new)
                conn.execute(f"UPDATE {table} SET {sets} WHERE rowid=?", (*new.values(), rowid))
                if table == "raw_messages":
                    cur = conn.execute("UPDATE raw_messages SET redactions=COALESCE(redactions,0)+?,"
                                       " embedded=CASE WHEN embedded=1 THEN 0 ELSE embedded END"
                                       " WHERE rowid=? AND embedded>=0", (n, rowid))
                    if cur.rowcount:
                        conn.execute("DELETE FROM vec_messages WHERE rowid=?", (rowid,))
                        reembed += 1
                    else:
                        conn.execute("UPDATE raw_messages SET redactions=COALESCE(redactions,0)+? WHERE rowid=?",
                                     (n, rowid))
    return reembed


def refilter(conn, sfilter: SecretFilter, apply: bool = False, ingestor=None) -> dict:
    """Dry run (apply=False) or apply. With an ingestor (with an embedder), recomputes the vectors at the end."""
    report, changes = scan(conn, sfilter)
    report["applied"] = apply
    if apply:
        report["pending_embedding"] = apply_changes(conn, changes)
        if ingestor is not None and ingestor.embedder is not None:
            report["reembedded"] = ingestor.embed_pending()
    return report
