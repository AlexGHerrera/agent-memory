from agent_memory.ingest import Ingestor
from agent_memory.refilter import refilter
from agent_memory.secrets_filter import SecretFilter

SECRET = "Zq81" + "xyw" + "Lp9"   # built in parts


def _seed(conn):
    """Rows stored with an older filter (written directly, without the current filter)."""
    ins = ("INSERT INTO raw_messages(uid, source, role, ts, local_date, text, redactions, embedded)"
           " VALUES (?,?,?,?,?,?,?,?)")
    conn.execute(ins, ("u1", "claude", "user", "2026-10-01T10:00:00Z", "2026-10-01",
                       f"- `MCP_PASSWORD`: {SECRET}\nthen we review the code", 0, 1))
    conn.execute(ins, ("u2", "claude", "assistant", "2026-10-01T10:01:00Z", "2026-10-01",
                       "nothing secret here, just the code of the function", 0, 1))
    conn.execute(ins, ("u3", "claude", "tool_result", "2026-10-01T10:02:00Z", "2026-10-01",
                       f"Contraseña: {SECRET},rest", 0, -1))
    conn.execute("INSERT INTO notes(ts, project, text) VALUES (?,?,?)",
                 ("2026-10-01T10:03:00Z", None, f"callback ?state=abcdefghij&code={SECRET}"))
    conn.execute("INSERT INTO summaries(level, period, headline, text, created_at) VALUES (?,?,?,?,?)",
                 ("day", "2026-10-01", "Normal day", f"STRIPE_KEY={SECRET} was configured", "2026-10-02T00:00:00Z"))
    conn.execute("INSERT INTO sessions(id, source, title) VALUES (?,?,?)", ("s1", "claude", "Session without secrets"))
    conn.commit()


def _fts(conn, term):
    return conn.execute("SELECT COUNT(*) FROM raw_fts WHERE raw_fts MATCH ?", (f'"{term}"',)).fetchone()[0]


def test_dry_run_counts_without_writing(conn):
    _seed(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM raw_messages ORDER BY id")]
    rep = refilter(conn, SecretFilter())
    assert rep["applied"] is False
    assert rep["rows"] == {"raw_messages": 2, "notes": 1, "summaries": 1, "sessions": 0}
    assert rep["redactions"]["raw_messages"] == {"assignment": 2, "glued-tail": 1}
    assert rep["redactions"]["notes"] == {"url-param": 2}
    assert rep["redactions"]["summaries"] == {"env-var": 1}
    assert SECRET not in repr(rep)
    assert [tuple(r) for r in conn.execute("SELECT * FROM raw_messages ORDER BY id")] == before


def test_apply_updates_fts_and_embeddings(conn, cfg, embedder):
    _seed(conn)
    ing = Ingestor(conn, cfg, SecretFilter(), embedder)
    # initial (filler) vectors for the embedded rows
    for rid in (1, 2):
        conn.execute("INSERT OR REPLACE INTO vec_messages(rowid, embedding) VALUES (?, ?)",
                     (rid, embedder.passages(["x"])[0].tobytes()))
    conn.commit()
    assert _fts(conn, SECRET) == 2

    rep = refilter(conn, SecretFilter(), apply=True, ingestor=ing)
    assert rep["pending_embedding"] == 1 and rep["reembedded"] == 1
    assert _fts(conn, SECRET) == 0 and _fts(conn, "REDACTED") == 2
    for table, col in (("raw_messages", "text"), ("notes", "text"), ("summaries", "text")):
        assert not conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{SECRET}%",)).fetchone()[0]
    r1 = conn.execute("SELECT text, redactions, embedded FROM raw_messages WHERE id=1").fetchone()
    assert r1["embedded"] == 1 and r1["redactions"] == 1 and "then we review the code" in r1["text"]
    new_vec = conn.execute("SELECT embedding FROM vec_messages WHERE rowid=1").fetchone()[0]
    assert new_vec == embedder.passages([r1["text"]])[0].tobytes()
    r3 = conn.execute("SELECT text, embedded FROM raw_messages WHERE id=3").fetchone()
    assert r3["text"] == "Contraseña: [REDACTED:secret]" and r3["embedded"] == -1
    assert conn.execute("SELECT text FROM raw_messages WHERE id=2").fetchone()[0].endswith("code of the function")

    again = refilter(conn, SecretFilter())
    assert sum(again["rows"].values()) == 0


def test_markers_are_not_redacted_again(conn):
    ins = ("INSERT INTO raw_messages(uid, source, role, ts, local_date, text, redactions, embedded)"
           " VALUES (?,?,?,?,?,?,?,?)")
    conn.execute(ins, ("u1", "claude", "user", "2026-10-01T10:00:00Z", "2026-10-01",
                       "Contraseña: [REDACTED:secret], password=[REDACTED:secret] ?code=[REDACTED:url]", 2, 1))
    conn.commit()
    assert sum(refilter(conn, SecretFilter())["rows"].values()) == 0
