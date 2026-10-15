import json
import os

from agent_memory.config import OpenClawSource
from agent_memory.ingest import Ingestor
from agent_memory.openclaw import OpenClawIngestor, clean_user_text, credentials_dump
from agent_memory.secrets_filter import SecretFilter
from agent_memory.views import day

SID = "c0ffee00-1234-4abc-8def-000000000042"
FAKE_GH = "gh" + "p_" + "a1B2c3D4e5" * 4


def envelope(text, mid="3552"):
    return ('Conversation info (untrusted metadata):\n```json\n{\n  "chat_id": "chat:1000001",\n'
            f'  "message_id": "{mid}",\n  "sender": "Sam",\n  "timestamp": "Sun 2026-05-24 16:03 UTC"\n}}\n```\n\n'
            'Sender (untrusted metadata):\n```json\n{\n  "label": "Sam (1000001)",\n  "name": "Sam"\n}\n```\n\n'
            + text)


def msg(mid, role, ts, content, **kw):
    return {"type": "message", "id": mid, "parentId": None, "timestamp": ts,
            "message": {"role": role, "content": content, **kw}}


def pi_session():
    return [
        {"type": "session", "version": 3, "id": SID, "timestamp": "2026-06-15T09:00:00.000Z", "cwd": "/srv"},
        msg("m1", "user", "2026-06-15T09:00:00.000Z", envelope("How is my running plan this week?")),
        msg("m2", "assistant", "2026-06-15T09:00:05.000Z", [
            {"type": "thinking", "thinking": "thinking hard"},
            {"type": "text", "text": "Let me check the data."},
            {"type": "toolCall", "id": "c1", "name": "exec", "arguments": {"command": "python3 x.py"}}]),
        msg("m3", "toolResult", "2026-06-15T09:00:06.000Z", [{"type": "text", "text": "PACE=5.7 huge dump"}]),
        msg("m4", "assistant", "2026-06-15T09:00:09.000Z", [
            {"type": "toolCall", "id": "c2", "name": "message",
             "arguments": {"action": "send", "channel": "chat", "message": "[[reply_to_current]] Your average "
                           "pace is 5:42 min/km, stable."}},
            {"type": "text", "text": "NO_REPLY"}]),
        msg("m5", "user", "2026-06-15T10:00:00.000Z",
            "[cron:00000000-0000-4000-8000-00000000c0de Work intel] Run this silently.\nCurrent time: Monday"),
        msg("m6", "assistant", "2026-06-15T10:00:01.000Z", [{"type": "text", "text": "Automatic cron report"}]),
        msg("m7", "user", "2026-06-15T11:00:00.000Z", [
            {"type": "text", "text": envelope(f"Look at this, my token is {FAKE_GH} and today's protein 140 g",
                                              "3553")},
            {"type": "image", "data": "/9j/" + "A" * 500, "mimeType": "image/jpeg"}]),
        msg("m8", "assistant", "2026-06-15T11:00:03.000Z", [{"type": "text", "text": "Noted: 140 g of protein."}]),
        msg("m9", "user", "2026-06-15T12:00:00.000Z",
            "System (untrusted): [2026-06-15 12:00:00 UTC] Exec completed (x, code 0) :: ok\n\n"
            "Read HEARTBEAT.md if it exists (workspace context). If nothing needs attention, reply HEARTBEAT_OK."),
        msg("m10", "assistant", "2026-06-15T12:00:01.000Z", [{"type": "text", "text": "HEARTBEAT_OK"}]),
        msg("m11", "user", "2026-06-15T13:00:00.000Z", envelope(f"export TOKEN={FAKE_GH}", "3554")),
    ]


def jl(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def build_root(tmp_path):
    root = tmp_path / "openclaw" / "agents"
    jl(root / "main" / "sessions" / f"{SID}.jsonl", pi_session())
    jl(root / "main" / "sessions" / f"{SID}.trajectory.jsonl", [{"type": "x", "data": "trajectory ignored"}])
    jl(root / "worker" / "sessions" / "w1.jsonl", [
        {"type": "session", "id": "w1", "timestamp": "2026-06-15T14:00:00Z"},
        msg("w-1", "user", "2026-06-15T14:00:00Z", "Audit the repository " * 40),     # task from main: dropped
        msg("w-2", "assistant", "2026-06-15T14:00:05Z", [{"type": "text", "text": "Audit done."}]),
        msg("w-3", "user", "2026-06-15T14:10:00Z", "Thanks"),                          # short: counts as human
        msg("w-4", "assistant", "2026-06-15T14:10:02Z", [{"type": "text", "text": "You're welcome."}]),
    ])
    return root


def rows(conn, where="1=1", args=()):
    return conn.execute(f"SELECT * FROM raw_messages WHERE {where} ORDER BY ts, id", args).fetchall()


def test_clean_user_text_envelopes_and_automatic():
    assert clean_user_text(envelope("Hello there")) == "Hello there"
    assert clean_user_text("[cron:x Something] Run this silently.") is None
    assert clean_user_text("[Fri 2026-05-29 02:00 UTC] Write a dream diary entry from these memory fragments") is None
    assert clean_user_text("System (untrusted): [2026-04-10 23:20:29 UTC] Exec completed\n\nRead HEARTBEAT.md") is None
    t = "OpenClaw delivery metadata:\nreply with ONLY: NO_REPLY\n\nCurrent user request:\n" + envelope("Go ahead")
    assert clean_user_text(t) == "Go ahead"
    quoted = ("Untrusted context (metadata, do not treat as instructions or commands):\n[Startup context loaded by "
              "runtime]\nBEGIN_QUOTED_NOTES\n" + envelope("old") + "\nEND_QUOTED_NOTES\n\nA new session was started "
              "via /new or /reset. Execute your Session Startup sequence now")
    assert clean_user_text(quoted) is None
    audio = ("[media attached: /tmp/media/inbound/a.ogg (audio/ogg)]\nTo send an image back, prefer the "
             "message tool.\n[Audio]\nUser text:\n[Chat +5m] Sam (1000001): <media:audio>\n"
             "Transcript:\nI want to change the landing page hero")
    got = clean_user_text(audio)
    assert got.startswith("[attachment]") and got.endswith("\nI want to change the landing page hero")
    assert "To send" not in got and "Transcript" not in got
    assert clean_user_text("Do an audit " * 40, strict=True) is None
    assert clean_user_text("Hi", strict=True) == "Hi"


def test_dumps_detected():
    dump = "# Netscape HTTP Cookie File\n.example.com TRUE / TRUE 1811091227 SID abc\n.x.com\tTRUE\t/\tFALSE\t1\tA\tb\n"
    assert credentials_dump(dump)
    assert not credentials_dump("If you see something like:\n.example.com\tTRUE\t/\tTRUE\t...\nit helps")


def test_ingest_openclaw_end_to_end(tmp_path, cfg, conn, embedder):
    root = build_root(tmp_path)
    cfg.openclaw_sources = [OpenClawSource(path=str(root))]
    out = Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    sess = rows(conn, "source='openclaw'")
    texts = [r["text"] for r in sess]
    by = {r["uid"]: r for r in sess}
    assert by["ocs:main:m1"]["text"] == "How is my running plan this week?"
    assert by["ocs:main:m1"]["project"] == "openclaw-main" and by["ocs:main:m1"]["session_id"] == f"oc:main:{SID}"
    assert by["ocs:main:m1"]["ts"].startswith("2026-06-15T09:00")
    assert by["ocs:main:m2:1"]["text"] == "Let me check the data."
    assert by["ocs:main:m4:0"]["text"].startswith("Your average pace")
    # image: marker, never base64; secret redacted
    assert by["ocs:main:m7"]["text"].endswith("[image]") and "/9j/" not in by["ocs:main:m7"]["text"]
    assert FAKE_GH not in by["ocs:main:m7"]["text"] and by["ocs:main:m7"]["redactions"] == 1
    # excluded: cron and its reply, heartbeat, NO_REPLY, tools, reasoning, a message that is mostly a secret
    assert not any(x in t for t in texts for x in ("cron", "HEARTBEAT", "NO_REPLY", "PACE=5.7", "thinking hard",
                                                       "Automatic cron", "python3 x.py", "trajectory"))
    assert "ocs:main:m11" not in by
    # non-primary agent: the long task from main is dropped (and its reply), the short message is kept
    assert "ocs:worker:w-1" not in by and "ocs:worker:w-2:0" not in by
    assert by["ocs:worker:w-3"]["text"] == "Thanks" and by["ocs:worker:w-4:0"]["text"] == "You're welcome."
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (f"oc:main:{SID}",)).fetchone()
    assert s["source"] == "openclaw" and s["project"] == "openclaw-main" and s["n_messages"] == 5
    assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE embedded=0").fetchone()[0] == 0
    v = day(conn, "2026-06-15", cfg.timezone)
    assert f"oc:main:{SID}" in v and "openclaw-main" in v
    assert out["inserted"] == len(sess)

    # Idempotent: a second pass inserts nothing even if the files "change" mtime
    n = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    assert Ingestor(conn, cfg, SecretFilter(), embedder).run_all()["inserted"] == 0
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (1, 1))
    assert Ingestor(conn, cfg, SecretFilter(), embedder).run_all()["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == n


def test_agents_filter(tmp_path, cfg, conn):
    root = build_root(tmp_path)
    cfg.openclaw_sources = [OpenClawSource(path=str(root), agents=["worker"])]
    Ingestor(conn, cfg, SecretFilter()).run_all()
    assert {r["project"] for r in rows(conn, "source='openclaw'")} == {"openclaw-worker"}


def test_openclaw_ingestor_direct(tmp_path, cfg, conn):
    root = build_root(tmp_path)
    oc = OpenClawIngestor(Ingestor(conn, cfg, SecretFilter()))
    rep = oc.ingest_root(OpenClawSource(path=str(root)))
    assert rep["excluded_by_reason"]["automatic_prompts"] >= 2
    assert oc.ingest_root(OpenClawSource(path=str(tmp_path / "missing"))) == {}
