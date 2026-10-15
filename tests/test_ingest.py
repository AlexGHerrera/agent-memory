import json

from conftest import EXCLUDED, rec, write_jsonl
from agent_memory.ingest import Ingestor, _project_from_dir
from agent_memory.secrets_filter import SecretFilter

SID = "11111111-2222-3333-4444-555555555555"
FAKE_GH = "gh" + "p_" + "a1B2c3D4e5" * 4


def _setup(tmp_path):
    proj = tmp_path / "projects" / "-home-alice-projects-demo-app"
    write_jsonl(proj / f"{SID}.jsonl", [
        rec("user", "u1", SID, "2026-10-03T09:00:00.000Z", "How is the nightly backup doing?"),
        rec("assistant", "a1", SID, "2026-10-03T09:00:05.000Z",
            [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "The nightly backup is fine."}]),
        rec("assistant", "a2", SID, "2026-10-03T09:00:06.000Z",
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        rec("user", "u2", SID, "2026-10-03T09:00:07.000Z",
            [{"type": "tool_result", "tool_use_id": "t1", "content": f"token {FAKE_GH}"}]),
        json.dumps({"type": "queue-operation", "sessionId": SID}) + "\n",
    ])
    # subagent
    write_jsonl(proj / SID / "subagents" / "agent-abc.jsonl", [
        rec("user", "s1", SID, "2026-10-03T09:01:00.000Z", "Subagent task", isSidechain=True, agentId="abc"),
    ])
    # excluded session and its subagent
    write_jsonl(proj / f"{EXCLUDED}.jsonl", [rec("user", "x1", EXCLUDED, "2026-10-03T08:00:00Z", "HIDDEN-X")])
    write_jsonl(proj / EXCLUDED / "subagents" / "agent-zz.jsonl",
                [rec("user", "x2", EXCLUDED, "2026-10-03T08:00:00Z", "HIDDEN-Y", isSidechain=True, agentId="zz")])
    # excluded project
    write_jsonl(tmp_path / "projects" / "-home-alice-.local-share-agent-memory-work" / "w.jsonl",
                [rec("user", "w1", "w", "2026-10-03T08:00:00Z", "internal consolidation")])
    return proj


def count(conn, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM raw_messages WHERE {where}", args).fetchone()[0]


def test_claude_ingest_idempotent_and_exclusions(tmp_path, cfg, conn, embedder):
    proj = _setup(tmp_path)
    out = Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    n = count(conn)
    assert n == 5  # user, assistant text, tool_use, tool_result, subagent
    assert count(conn, "text LIKE '%HIDDEN%'") == 0
    assert count(conn, "text LIKE '%internal consolidation%'") == 0
    assert count(conn, "text LIKE ?", (f"%{FAKE_GH}%",)) == 0
    assert out["redactions"] >= 1
    # embeddings only for conversational text
    assert conn.execute("SELECT COUNT(*) FROM vec_messages").fetchone()[0] == 3
    # second pass: nothing new
    out2 = Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert out2["inserted"] == 0 and count(conn) == n
    # force a full reread (state deleted): the uid prevents duplicates
    conn.execute("DELETE FROM ingest_state"); conn.commit()
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert count(conn) == n
    # incremental: one line is appended
    write_jsonl(proj / f"{SID}.jsonl", [rec("user", "u3", SID, "2026-10-03T10:00:00Z", "new line")], mode="a")
    out3 = Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert out3["inserted"] == 1
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (SID,)).fetchone()
    assert s["n_messages"] == 5 and s["project"] == "demo-app" and s["started_at"].startswith("2026-10-03T09")
    sub = conn.execute("SELECT * FROM sessions WHERE id=?", (f"{SID}/agent-abc",)).fetchone()
    assert sub["parent_id"] == SID
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id LIKE ?", (EXCLUDED + "%",)).fetchone()[0] == 0


def test_minimal_generic_records(tmp_path, cfg, conn, embedder):
    """The documented minimal input format (docs/INGESTION.md) written by a third-party adapter."""
    lines = [
        {"type": "user", "uuid": "g-1", "sessionId": "chat-2026-10-03", "timestamp": "2026-10-03T08:00:00Z",
         "cwd": "/srv/agents/helpdesk", "message": {"role": "user", "content": "Remind me to renew the domain"}},
        {"type": "assistant", "uuid": "g-2", "sessionId": "chat-2026-10-03", "timestamp": "2026-10-03T08:00:04Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Noted: renew the domain."}]}},
    ]
    write_jsonl(tmp_path / "projects" / "my-agent" / "chat-2026-10-03.jsonl",
                [json.dumps(x) + "\n" for x in lines])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    rows = conn.execute("SELECT role, project, session_id FROM raw_messages ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("user", "helpdesk", "chat-2026-10-03"),
                                        ("assistant", "my-agent", "chat-2026-10-03")]


def test_partial_last_line_waits(tmp_path, cfg, conn, embedder):
    proj = tmp_path / "projects" / "p"
    f = proj / "s.jsonl"
    line = rec("user", "p1", "s", "2026-10-03T09:00:00Z", "complete")
    write_jsonl(f, [line, '{"type": "user", "uuid": "p2"'])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert count(conn) == 1
    with open(f, "a") as fh:
        fh.write(', "sessionId": "s", "timestamp": "2026-10-03T09:00:01Z", "message": {"content": "second"}}\n')
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert count(conn) == 2


def test_local_date_uses_timezone(tmp_path, cfg, conn, embedder):
    # 23:30 UTC on 3 October is already 4 October in Madrid (UTC+2 in summer)
    write_jsonl(tmp_path / "projects" / "p" / "s.jsonl",
                [rec("user", "z1", "s", "2026-10-03T23:30:00Z", "late night")])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert conn.execute("SELECT local_date FROM raw_messages").fetchone()[0] == "2026-10-04"


def test_missing_sources_are_ok(cfg, conn, embedder):
    out = Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    assert out["inserted"] == 0


def test_project_from_dir():
    assert _project_from_dir("-home-alice-projects-demo-app") == "projects-demo-app"
    assert _project_from_dir("-Users-bob-work-api") == "work-api"
    assert _project_from_dir("-root-tools") == "tools"
    assert _project_from_dir("my-agent") == "my-agent"
    assert _project_from_dir("") == "unknown"


def test_add_note(cfg, conn, embedder):
    r = Ingestor(conn, cfg, SecretFilter(), embedder).add_note("Buy batteries. password=abc12345", "home")
    assert r["redactions"] == 1
    n = conn.execute("SELECT text, project FROM notes").fetchone()
    assert "abc12345" not in n["text"] and n["project"] == "home"
    assert count(conn, "role='note'") == 1
