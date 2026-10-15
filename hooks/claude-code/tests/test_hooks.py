"""Tests for the Claude Code hooks (stdlib only: `python3 -m unittest discover -s hooks/claude-code/tests` or pytest).

They start a fake memory server (minimal streamable-HTTP MCP + /index + /facts/brief + PUT /inbox) on 127.0.0.1
and run the hooks as subprocesses with a temporary HOME: they never touch ~/.claude nor a real memory server.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOOKS)
import common as mc  # noqa: E402
import pre_compact  # noqa: E402
import session_end  # noqa: E402
import session_start  # noqa: E402

TOKEN = "x" * 48
ENV_KEYS = ("AGENT_MEMORY_URL", "AGENT_MEMORY_TOKEN_FILE", "AGENT_MEMORY_MACHINE", "AGENT_MEMORY_UPLOAD",
            "AGENT_MEMORY_TASKS", "AGENT_MEMORY_CONTEXT_FILES", "AGENT_MEMORY_SUMMARY")
FACTS_BRIEF = ("### Core\n- [work] user · employer: Globex\n"
               "### Recent (last 7 days)\n- [work] user · office: Main Street (was remote)\n"
               "### Project demo app\n- [project] Orchard · status: active")


class FakeMemory(BaseHTTPRequestHandler):
    calls: list = []
    uploads: dict = {}
    gets: list = []
    index = "# INDEX\n## Recent days\n- 2026-10-03: something\n"

    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _reply(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/index":
            return self._reply(200, self.index.encode(), "text/markdown")
        if self.path.startswith("/facts/brief"):
            FakeMemory.gets.append(self.path)
            return self._reply(200, FACTS_BRIEF.encode(), "text/markdown")
        self._reply(404)

    def do_DELETE(self):
        self._reply(200)

    def do_PUT(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length", 0))
        FakeMemory.uploads[self.path] = self.rfile.read(n)
        self._reply(201, b'{"ok":true}')

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length", 0))
        msg = json.loads(self.rfile.read(n))
        if msg.get("method") == "initialize":
            return self._reply(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),
                               extra={"mcp-session-id": "s1"})
        if msg.get("method") == "notifications/initialized":
            return self._reply(202)
        if msg.get("method") == "tools/call":
            assert self.headers.get("Mcp-Session-Id") == "s1"
            FakeMemory.calls.append(msg["params"])
            name = msg["params"]["name"]
            text = {"day": "Day X: session abc · work on the hooks", "note": "Note 7 stored"}.get(name, "?")
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}]}})
            return self._reply(200, f"event: message\ndata: {body}\n\n".encode(), "text/event-stream")
        self._reply(400)


def rec(typ, content, **kw):
    d = {"type": typ, "cwd": "/home/alice/project-x", "message": {"role": typ, "content": content}}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def make_transcript(path, turns=12, summary_note=False):
    lines = []
    for i in range(turns):
        lines.append(rec("user", f"user message {i}"))
        lines.append(rec("assistant", [{"type": "text", "text": f"Step {i} done. I'll follow up when it finishes."}]))
        lines.append(rec("user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]))
    lines.append(rec("user", "<command-name>/clear</command-name>"))
    lines.append(rec("user", "meta", isMeta=True))
    lines.append(rec("assistant", [{"type": "tool_use", "id": "a", "name": "TodoWrite", "input": {"todos": [
        {"content": "ship the hooks", "status": "in_progress"}, {"content": "done", "status": "completed"}]}}]))
    lines.append(rec("assistant", [{"type": "tool_use", "id": "c1", "name": "TaskCreate",
                                    "input": {"subject": "test on the laptop"}}]))
    lines.append(rec("user", [{"type": "tool_result", "tool_use_id": "c1", "content": "Task #4 created"}]))
    lines.append(rec("assistant", [{"type": "tool_use", "id": "c2", "name": "TaskCreate", "input": {"subject": "other"}}]))
    lines.append(rec("user", [{"type": "tool_result", "tool_use_id": "c2", "content": "Task #5 created"}]))
    lines.append(rec("assistant", [{"type": "tool_use", "id": "u", "name": "TaskUpdate",
                                    "input": {"taskId": "5", "status": "completed"}}]))
    if summary_note:
        lines.append(rec("assistant", [{"type": "tool_use", "id": "m", "name": "mcp__memory__note",
                                        "input": {"text": "Session summary: ..."}}]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMemory)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeMemory.calls.clear()
        FakeMemory.uploads.clear()
        FakeMemory.gets.clear()
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".config", "agent-memory"))
        tok = os.path.join(self.home, ".config", "agent-memory", "mcp-token")
        with open(tok, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(tok, 0o600)
        self.tok = tok
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
                    "AGENT_MEMORY_URL": self.url, "AGENT_MEMORY_UPLOAD": "1"}
        # in-process: isolate state
        self._saved = (mc.STATE_DIR, mc.CACHE_DIR, mc.CONFIG_FILE)
        mc.STATE_DIR = os.path.join(self.home, ".local", "state", "agent-memory-hooks")
        mc.CACHE_DIR = os.path.join(self.home, ".cache", "agent-memory")
        mc.CONFIG_FILE = os.path.join(self.home, ".config", "agent-memory", "hooks.json")
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["AGENT_MEMORY_URL"] = self.url
        os.environ["AGENT_MEMORY_TOKEN_FILE"] = tok
        os.environ["AGENT_MEMORY_UPLOAD"] = "1"

    def tearDown(self):
        mc.STATE_DIR, mc.CACHE_DIR, mc.CONFIG_FILE = self._saved
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def run_hook(self, script, payload, args=(), env_extra=None, stdin=None):
        env = dict(self.env, **(env_extra or {}))
        data = stdin if stdin is not None else json.dumps(payload)
        return subprocess.run([sys.executable, os.path.join(HOOKS, script), *args], input=data,
                              capture_output=True, text=True, env=env, timeout=60)

    def hook_log(self):
        return mc.read_text(os.path.join(self.home, ".local", "state", "agent-memory-hooks", "hooks.log"))


class TestFacts(unittest.TestCase):
    def test_facts_block_inside_budget_and_trimmed_after_log_and_index(self):
        idx = "\n".join(f"- index {i} " + "x" * 80 for i in range(200))
        log = "\n".join(f"log {i} " + "y" * 80 for i in range(400))
        out = session_start.build_context("P" * 2000, "- task", idx, log, 4000, facts=FACTS_BRIEF)
        self.assertLessEqual(len(out), 4000 * session_start.CHARS_PER_TOKEN)
        self.assertIn(FACTS_BRIEF, out)
        self.assertNotIn("log 0", out)
        self.assertLess(out.index("## Current key facts"), out.index("## Open tasks"))

    def test_total_never_exceeds_cap(self):
        big = "\n".join(f"- [work] user · a{i}: " + "v" * 70 for i in range(300))
        for tokens in (500, 1000, 4000):
            for parts in (("P" * 9000, "- t" * 900, big, big, big), ("", "", "", "", big)):
                out = session_start.build_context(*parts[:4], tokens, facts=parts[4])
                self.assertLessEqual(len(out), tokens * session_start.CHARS_PER_TOKEN)

    def test_no_facts_no_section(self):
        self.assertNotIn("key facts", session_start.build_context("P", "", "", "", 4000))


class TestBudget(unittest.TestCase):
    def test_under_budget_keeps_everything(self):
        out = session_start.build_context("I am the assistant", "- task 1", "# INDEX", "log", 4000)
        for s in ("I am the assistant", "task 1", "# INDEX", "log"):
            self.assertIn(s, out)

    def test_trims_log_then_index_and_hard_cap(self):
        idx = "\n".join(f"- index {i} " + "x" * 80 for i in range(200))
        log = "\n".join(f"log {i} " + "y" * 80 for i in range(400))
        out = session_start.build_context("P" * 2000, "- open task", idx, log, 4000)
        self.assertLessEqual(len(out), 4000 * session_start.CHARS_PER_TOKEN)
        self.assertIn("P" * 2000, out)
        self.assertIn("open task", out)
        self.assertIn("index 0", out)          # the INDEX keeps its head
        self.assertNotIn("log 0", out)          # the log is trimmed first (entirely, here)

    def test_log_trimmed_before_index(self):
        idx = "- index\n" * 50
        log = "\n".join(f"log {i} " + "y" * 80 for i in range(400))
        out = session_start.build_context("P", "", idx, log, 4000)
        self.assertIn("log 0", out)
        self.assertEqual(out.count("- index"), 50)
        self.assertIn("trimmed", out)

    def test_hard_cap_even_with_huge_context(self):
        out = session_start.build_context("Z" * 100_000, "", "", "", 1000)
        self.assertLessEqual(len(out), 1000 * session_start.CHARS_PER_TOKEN)

    def test_empty_tasks_not_injected_and_comments_stripped(self):
        out = session_start.build_context("<!-- hidden -->Visible", "# TASKS\n<!-- - not an entry -->\n", "", "",
                                          4000)
        self.assertNotIn("Open tasks", out)
        self.assertNotIn("hidden", out)
        self.assertIn("Visible", out)


class TestDigest(unittest.TestCase):
    def test_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            make_transcript(p, turns=3)
            with open(p, "a") as f:
                f.write("no json\n")
            dg = mc.transcript_digest(p)
        self.assertEqual(len(dg["human"]), 3)
        self.assertEqual(dg["cwd"], "/home/alice/project-x")
        self.assertTrue(any("ship the hooks" in x for x in dg["open_items"]))
        self.assertTrue(any("test on the laptop" in x for x in dg["open_items"]))
        self.assertFalse(any("other" in x or "done [" in x for x in dg["open_items"]))
        self.assertEqual(len(dg["conclusions"]), 3)

    def test_notes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            make_transcript(p, turns=1, summary_note=True)
            self.assertEqual(mc.transcript_digest(p)["notes"], ["Session summary: ..."])

    def test_missing_file(self):
        dg = mc.transcript_digest("/nonexistent/x.jsonl")
        self.assertEqual(dg["human"], [])

    def test_commitments(self):
        c = pre_compact.commitments(["Ready. I'll follow up when the ingestion ends. Nothing else.",
                                     "Queda pendiente: rotar la clave"])
        self.assertTrue(any("follow up" in x for x in c))
        self.assertTrue(any("Queda pendiente" in x for x in c))
        self.assertFalse(any("Nothing else" in x for x in c))


class TestSettings(Base):
    def test_env_then_file_then_default(self):
        os.environ.pop("AGENT_MEMORY_URL", None)
        self.assertEqual(mc.memory_url(), mc.DEFAULT_URL)
        mc.write_private(mc.CONFIG_FILE, json.dumps({"url": "http://file.example:1/", "context_files": ["~/a.md"],
                                                     "upload": False}))
        self.assertEqual(mc.memory_url(), "http://file.example:1")
        self.assertEqual(mc.context_files(), [os.path.join(os.path.expanduser("~"), "a.md")])
        os.environ["AGENT_MEMORY_URL"] = "http://env.example:2"
        self.assertEqual(mc.memory_url(), "http://env.example:2")
        self.assertTrue(mc.upload_enabled())     # env AGENT_MEMORY_UPLOAD=1 wins over the file
        os.environ.pop("AGENT_MEMORY_UPLOAD")
        self.assertFalse(mc.upload_enabled())

    def test_tasks_path_default_empty(self):
        self.assertEqual(mc.tasks_path(), "")
        os.environ["AGENT_MEMORY_TASKS"] = "/tmp/x/TASKS.md"
        self.assertEqual(mc.tasks_path(), "/tmp/x/TASKS.md")


class TestSessionStart(Base):
    def test_injects_index_log_context_and_facts(self):
        tasks = os.path.join(self.home, "TASKS.md")
        with open(tasks, "w") as f:
            f.write("# TASKS\n- [ ] call the landlord\n")
        persona = os.path.join(self.home, "PERSONA.md")
        with open(persona, "w") as f:
            f.write("You are Helper, terse and precise.\n")
        r = self.run_hook("session_start.py", {"source": "startup", "session_id": "s", "cwd": "/x/demo app"},
                          env_extra={"AGENT_MEMORY_TASKS": tasks, "AGENT_MEMORY_CONTEXT_FILES": persona})
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# INDEX", ctx)
        self.assertIn("Day X", ctx)
        self.assertIn("call the landlord", ctx)
        self.assertIn("You are Helper", ctx)
        self.assertLessEqual(len(ctx), 4000 * session_start.CHARS_PER_TOKEN)
        self.assertEqual([c["name"] for c in FakeMemory.calls], ["day", "day"])  # the brief is not a tool call
        self.assertEqual([c["arguments"] for c in FakeMemory.calls], [{"date": "today"}, {"date": "yesterday"}])
        self.assertIn("## Current key facts", ctx)
        self.assertIn("office: Main Street (was remote)", ctx)
        self.assertEqual(FakeMemory.gets[-1], "/facts/brief?project=demo%20app")
        self.assertTrue(os.path.exists(os.path.join(self.home, ".cache", "agent-memory", "INDEX.md")))
        self.assertNotIn(TOKEN, self.hook_log())

    def test_fail_open_without_server_uses_cache(self):
        os.makedirs(os.path.join(self.home, ".cache", "agent-memory"))
        with open(os.path.join(self.home, ".cache", "agent-memory", "INDEX.md"), "w") as f:
            f.write("# INDEX cached\n")
        t0 = time.time()
        r = self.run_hook("session_start.py", {"source": "startup"},
                          env_extra={"AGENT_MEMORY_URL": "http://127.0.0.1:9"})
        self.assertEqual(r.returncode, 0)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("INDEX cached", ctx)
        self.assertIn("local cache", ctx)
        self.assertLess(time.time() - t0, 20)

    def test_garbage_stdin_and_disabled(self):
        r = self.run_hook("session_start.py", None, stdin="{{{no json")
        self.assertEqual(r.returncode, 0)
        r = self.run_hook("session_start.py", {}, env_extra={"AGENT_MEMORY_HOOK_CHILD": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class TestPreCompact(Base):
    def test_dumps_state_to_memory(self):
        tr = os.path.join(self.tmp, "p", "s1.jsonl")
        make_transcript(tr, turns=4)
        r = self.run_hook("pre_compact.py", {"session_id": "s1", "transcript_path": tr, "trigger": "auto"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeMemory.calls), 1)
        self.assertEqual(FakeMemory.calls[0]["name"], "note")
        args = FakeMemory.calls[0]["arguments"]
        self.assertEqual(args["project"], "project-x")
        for s in ("PreCompact dump", "ship the hooks", "test on the laptop", "follow up", "user message 3"):
            self.assertIn(s, args["text"])

    def test_offline_spools_and_flush_later(self):
        tr = os.path.join(self.tmp, "p", "s1.jsonl")
        make_transcript(tr, turns=2)
        r = self.run_hook("pre_compact.py", {"session_id": "s1", "transcript_path": tr},
                          env_extra={"AGENT_MEMORY_URL": "http://127.0.0.1:9"})
        self.assertEqual(r.returncode, 0)
        spool = os.path.join(self.home, ".local", "state", "agent-memory-hooks", "pending")
        self.assertEqual(len(os.listdir(spool)), 1)
        self.assertEqual(oct(os.stat(os.path.join(spool, os.listdir(spool)[0])).st_mode & 0o777), "0o600")
        r = self.run_hook("session_end.py", {}, args=("--flush",))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(os.listdir(spool), [])
        self.assertEqual(FakeMemory.calls[0]["name"], "note")

    def test_crash_is_fail_open(self):
        r = self.run_hook("pre_compact.py", None, stdin="[1,2]")
        self.assertEqual(r.returncode, 0)


class TestSessionEnd(Base):
    def test_upload_targets_and_upload(self):
        proj = os.path.join(self.tmp, "projects", "-Users-alice-project-x")
        tr = os.path.join(proj, "abc.jsonl")
        make_transcript(tr, turns=2)
        os.makedirs(os.path.join(proj, "abc", "subagents"))
        with open(os.path.join(proj, "abc", "subagents", "agent-1.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(session_end.upload_session(tr), 2)
        self.assertIn("/inbox/-Users-alice-project-x/abc.jsonl", FakeMemory.uploads)
        self.assertIn("/inbox/-Users-alice-project-x/abc/subagents/agent-1.jsonl", FakeMemory.uploads)

    def test_upload_offline_spools(self):
        tr = os.path.join(self.tmp, "projects", "p", "abc.jsonl")
        make_transcript(tr, turns=1)
        os.environ["AGENT_MEMORY_URL"] = "http://127.0.0.1:9"
        self.assertEqual(session_end.upload_session(tr), 0)
        os.environ["AGENT_MEMORY_URL"] = self.url
        self.assertEqual(mc.flush_spool(), 1)
        self.assertIn("/inbox/p/abc.jsonl", FakeMemory.uploads)

    def test_summary_rules(self):
        tr = os.path.join(self.tmp, "p", "s.jsonl")
        make_transcript(tr, turns=10)
        fake = lambda material: "- fake summary"  # noqa: E731
        self.assertFalse(session_end.summarize_session("s", tr, llm=fake))      # 10 turns: no
        make_transcript(tr, turns=11, summary_note=True)
        self.assertFalse(session_end.summarize_session("s", tr, llm=fake))      # already left a summary
        make_transcript(tr, turns=11)
        seen = {}

        def fake2(material):
            seen["m"] = material
            return "- fake summary"
        self.assertTrue(session_end.summarize_session("s", tr, llm=fake2))
        self.assertIn("user message 10", seen["m"])
        self.assertIn("Step 3 done", seen["m"])
        text = FakeMemory.calls[-1]["arguments"]["text"]
        self.assertIn("Automatic summary of session s", text)
        self.assertFalse(session_end.summarize_session("s", tr, llm=fake2))     # already summarized
        make_transcript(tr, turns=25)
        self.assertTrue(session_end.summarize_session("s", tr, llm=fake2))      # grew by >10 turns

    def test_material_cap(self):
        dg = {"human": ["h" * 600] * 200, "conclusions": ["c" * 800] * 200}
        m = session_end.build_material(dg, 10_000)
        self.assertLessEqual(len(m), 10_000)
        self.assertIn("## User messages", m)

    def test_hook_returns_fast_and_worker_uploads(self):
        proj = os.path.join(self.tmp, "projects", "proj")
        tr = os.path.join(proj, "sess.jsonl")
        make_transcript(tr, turns=2)
        t0 = time.time()
        r = self.run_hook("session_end.py", {"session_id": "sess", "transcript_path": tr, "reason": "exit"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 5)
        for _ in range(100):
            if FakeMemory.uploads:
                break
            time.sleep(0.1)
        self.assertIn("/inbox/proj/sess.jsonl", FakeMemory.uploads)

    def test_no_upload_by_default(self):
        tr = os.path.join(self.tmp, "projects", "proj", "sess.jsonl")
        make_transcript(tr, turns=2)
        pf = os.path.join(self.tmp, "payload.json")
        with open(pf, "w") as f:
            json.dump({"session_id": "sess", "transcript_path": tr}, f)
        r = self.run_hook("session_end.py", {}, args=("--worker", pf), env_extra={"AGENT_MEMORY_UPLOAD": "0"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(FakeMemory.uploads, {})
        self.assertFalse(os.path.exists(pf))


class TestHeadersHelper(Base):
    def test_prints_header_or_empty(self):
        r = self.run_hook("headers_helper.py", None, stdin="", env_extra={"AGENT_MEMORY_TOKEN_FILE": self.tok})
        self.assertEqual(json.loads(r.stdout), {"Authorization": f"Bearer {TOKEN}"})
        r = self.run_hook("headers_helper.py", None, stdin="")        # default location in ~/.config
        self.assertEqual(json.loads(r.stdout), {"Authorization": f"Bearer {TOKEN}"})
        other = os.path.join(self.tmp, "other-token")
        os.rename(self.tok, other)
        r = self.run_hook("headers_helper.py", None, stdin="")
        self.assertEqual(json.loads(r.stdout), {})
        r = self.run_hook("headers_helper.py", None, args=("--token-file", other), stdin="")
        self.assertEqual(json.loads(r.stdout), {"Authorization": f"Bearer {TOKEN}"})


class TestMcpClient(Base):
    def test_wrong_token_raises(self):
        with self.assertRaises(mc.MemoryServiceError):
            mc.http("GET", "/index", token="wrong" * 10)

    def test_mcp_call(self):
        self.assertEqual(mc.mcp_call("day", {"date": "today"}), "Day X: session abc · work on the hooks")


if __name__ == "__main__":
    unittest.main()
