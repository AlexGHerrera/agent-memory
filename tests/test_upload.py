import asyncio
import json
import os

import pytest

from agent_memory.server import BearerAuth
from agent_memory.upload import InboxRoutes, UploadError, safe_relpath, store

TOKEN = "t" * 40


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 299, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


def call(app, method, path, body=b"", headers=None, chunk=None):
    hdrs = [(b"authorization", f"Bearer {TOKEN}".encode())] + (headers or [])
    scope = {"type": "http", "method": method, "path": path, "headers": hdrs}
    parts = [body[i:i + chunk] for i in range(0, len(body), chunk)] if chunk and body else [body]
    msgs = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]
    out = []

    async def receive():
        return msgs.pop(0)

    async def send(m):
        out.append(m)

    asyncio.run(app(scope, receive, send))
    status = out[0]["status"]
    data = b"".join(m.get("body", b"") for m in out[1:])
    return status, data


@pytest.fixture
def app(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (tmp_path / "INDEX.md").write_text("# INDEX\nhello\n")
    return BearerAuth(InboxRoutes(_inner, str(inbox), str(tmp_path / "INDEX.md"), 1000), TOKEN), inbox


def test_put_new_idempotent_extend_and_never_overwrite(app):
    a, inbox = app
    st, data = call(a, "PUT", "/inbox/-Users-bob-x/abc.jsonl", b'{"a":1}\n', chunk=3)
    assert st == 201 and json.loads(data)["bytes"] == 8 and json.loads(data)["result"] == "new"
    f = inbox / "-Users-bob-x" / "abc.jsonl"
    assert f.read_bytes() == b'{"a":1}\n'
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    # same content: idempotent 200
    st, data = call(a, "PUT", "/inbox/-Users-bob-x/abc.jsonl", b'{"a":1}\n')
    assert st == 200 and json.loads(data)["result"] == "same"
    # the session went on (existing file is a prefix): extended
    st, data = call(a, "PUT", "/inbox/-Users-bob-x/abc.jsonl", b'{"a":1}\n{"b":2}\n')
    assert st == 201 and json.loads(data)["result"] == "extended" and f.read_bytes().count(b"\n") == 2
    # old retry (prefix of the existing file): 200, untouched
    st, data = call(a, "PUT", "/inbox/-Users-bob-x/abc.jsonl", b'{"a":1}\n')
    assert st == 200 and json.loads(data)["result"] == "contained"
    assert f.read_bytes() == b'{"a":1}\n{"b":2}\n'
    # different content: the original stays intact and the new one goes to a suffixed copy
    st, data = call(a, "PUT", "/inbox/-Users-bob-x/abc.jsonl", b'{"evil":1}\n')
    body = json.loads(data)
    assert st == 201 and body["result"] == "copy"
    assert f.read_bytes() == b'{"a":1}\n{"b":2}\n'
    copy = inbox / body["path"]
    assert copy.parent == f.parent and copy.name.startswith("abc.") and copy.name.endswith(".jsonl")
    assert copy.read_bytes() == b'{"evil":1}\n' and oct(copy.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in f.parent.iterdir() if p.name.endswith(".part")]


def test_extended_upload_ingests_without_duplicates(app, cfg, conn):
    from conftest import rec
    from agent_memory.config import ClaudeSource
    from agent_memory.ingest import Ingestor
    from agent_memory.secrets_filter import SecretFilter
    a, inbox = app
    cfg.claude_sources = [ClaudeSource(path=str(inbox), machine="remote")]
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    v1 = (rec("user", "m1", sid, "2026-10-03T09:00:00Z", "hello from the laptop")
          + rec("assistant", "m2", sid, "2026-10-03T09:00:01Z", "hello there")).encode()
    v2 = v1 + rec("user", "m3", sid, "2026-10-03T10:00:00Z", "continuing with --resume").encode()
    path = f"/inbox/-Users-bob-x/{sid}.jsonl"
    count = lambda: conn.execute("SELECT COUNT(*) FROM raw_messages WHERE session_id=?", (sid,)).fetchone()[0]
    assert call(a, "PUT", path, v1)[0] == 201
    Ingestor(conn, cfg, SecretFilter()).run_all()
    assert count() == 2
    assert call(a, "PUT", path, v2)[0] == 201
    Ingestor(conn, cfg, SecretFilter()).run_all()
    assert count() == 3
    # a diverging copy (same uuids + a new one) does not duplicate messages
    v3 = v1 + rec("user", "m4", sid, "2026-10-03T11:00:00Z", "different branch").encode()
    st, data = call(a, "PUT", path, v3)
    assert st == 201 and json.loads(data)["result"] == "copy"
    Ingestor(conn, cfg, SecretFilter()).run_all()
    assert count() == 4


def test_upload_limit_is_moderate():
    from agent_memory.config import Config
    assert 8 <= Config().upload_max_mb <= 128


def test_subagent_depth(app):
    a, inbox = app
    st, _ = call(a, "PUT", "/inbox/proj/sess/subagents/agent-1.jsonl", b"x\n")
    assert st == 201 and (inbox / "proj/sess/subagents/agent-1.jsonl").exists()


@pytest.mark.parametrize("path", [
    "/inbox/../x.jsonl", "/inbox/a/../../x.jsonl", "/inbox/a/x.txt", "/inbox/.hidden.jsonl",
    "/inbox/a/b/c/d/e.jsonl", "/inbox/a//x.jsonl", "/inbox/", "/inbox/a b.jsonl",
    "/inbox/a/.jsonl", "/inbox/a..b/x.jsonl"])
def test_bad_paths(app, path):
    a, inbox = app
    st, _ = call(a, "PUT", path, b"x\n")
    assert st == 400
    assert list(inbox.iterdir()) == []


def test_too_big(app):
    a, inbox = app
    assert call(a, "PUT", "/inbox/p/x.jsonl", b"x" * 1001, chunk=100)[0] == 413
    assert call(a, "PUT", "/inbox/p/x.jsonl", b"x", headers=[(b"content-length", b"5000")])[0] == 413
    assert not (inbox / "p" / "x.jsonl").exists()


def test_symlink_rejected(app, tmp_path):
    a, inbox = app
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, inbox / "evil")
    assert call(a, "PUT", "/inbox/evil/x.jsonl", b"x\n")[0] == 409
    os.symlink(outside / "f.jsonl", inbox / "f.jsonl")
    assert call(a, "PUT", "/inbox/f.jsonl", b"x\n")[0] == 409
    assert list(outside.iterdir()) == []


def test_methods_index_and_passthrough(app):
    a, _ = app
    assert call(a, "GET", "/inbox/p/x.jsonl")[0] == 405
    st, data = call(a, "GET", "/index")
    assert st == 200 and data.startswith(b"# INDEX")
    assert call(a, "POST", "/index")[0] == 405
    assert call(a, "POST", "/mcp") == (299, b"mcp")


def test_requires_bearer(app):
    a, inbox = app
    scope = {"type": "http", "method": "PUT", "path": "/inbox/p/x.jsonl", "headers": []}
    out = []

    async def receive():
        return {"type": "http.request", "body": b"x\n"}

    async def send(m):
        out.append(m)

    asyncio.run(a(scope, receive, send))
    assert out[0]["status"] == 401 and not (inbox / "p").exists()
    scope = {"type": "http", "method": "GET", "path": "/index", "headers": []}
    out.clear()
    asyncio.run(a(scope, receive, send))
    assert out[0]["status"] == 401


def test_safe_relpath_and_store(tmp_path):
    assert safe_relpath("a/b.jsonl") == ["a", "b.jsonl"]
    with pytest.raises(UploadError):
        safe_relpath("b.json")
    with pytest.raises(UploadError):
        store(str(tmp_path / "missing"), "a.jsonl", b"")
