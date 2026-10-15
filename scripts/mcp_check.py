#!/usr/bin/env python3
"""End-to-end check of a running agent-memory server over streamable HTTP (stdlib only). Used by smoke.sh.

Usage: mcp_check.py URL TOKEN_FILE [--expect-session ID --expect-text TEXT]
Prints one PASS/FAIL line per check (never the token) and exits 1 if any check failed.
Side effect: saves one note ("agent-memory smoke test <nonce>") in the memory.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import urllib.error
import urllib.request

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok or not detail else f": {detail}"), flush=True)
    return ok


class Client:
    def __init__(self, url: str, token: str):
        self.url, self.token, self.sid, self.next_id = url.rstrip("/"), token, None, 1

    def raw(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None, auth=True):
        h = dict(headers or {})
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items()), e.read()

    def rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self.next_id
            self.next_id += 1
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        st, hdrs, body = self.raw("POST", "/mcp", json.dumps(msg).encode(), h)
        self.sid = self.sid or next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
        if notify:
            return {"status": st}
        text = body.decode("utf-8", "replace")
        ctype = next((v for k, v in hdrs.items() if k.lower() == "content-type"), "")
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    if data.get("id") == msg["id"]:
                        return data
            return {"error": {"message": f"no SSE result (HTTP {st})"}}
        return json.loads(text) if text.strip() else {"error": {"message": f"empty reply (HTTP {st})"}}

    def tool(self, name: str, args: dict) -> str:
        res = self.rpc("tools/call", {"name": name, "arguments": args})
        if "error" in res:
            raise RuntimeError(res["error"].get("message", "error"))
        r = res.get("result") or {}
        text = "\n".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))
        if r.get("isError"):
            raise RuntimeError(text[:200])
        return text


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    url, token_file = argv[0], argv[1]
    expect_session = argv[argv.index("--expect-session") + 1] if "--expect-session" in argv else None
    expect_text = argv[argv.index("--expect-text") + 1] if "--expect-text" in argv else None
    token = open(token_file, encoding="utf-8").read().strip()
    c = Client(url, token)

    st, _, _ = c.raw("POST", "/mcp", b"{}", {"Content-Type": "application/json"}, auth=False)
    check("auth: request without token is rejected (401)", st == 401, f"HTTP {st}")
    bad = Client(url, "x" * 64)
    st, _, _ = bad.raw("GET", "/index")
    check("auth: wrong token is rejected (401)", st == 401, f"HTTP {st}")

    init = c.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "agent-memory-smoke", "version": "1"}})
    name = ((init.get("result") or {}).get("serverInfo") or {}).get("name")
    if not check("initialize: server answers as 'memory'", name == "memory", json.dumps(init)[:200]):
        return 1
    c.rpc("notifications/initialized", notify=True)

    tools = sorted(t["name"] for t in (c.rpc("tools/list").get("result") or {}).get("tools", []))
    check("tools/list: exactly search, day, session, note, facts", tools == ["day", "facts", "note", "search",
                                                                          "session"], str(tools))

    nonce = "smoke" + secrets.token_hex(4)
    day = None
    try:
        out = c.tool("note", {"text": f"agent-memory smoke test {nonce}", "project": "smoke-test"})
        m = re.search(r"saved on (\d{4}-\d{2}-\d{2})", out)
        day = m.group(1) if m else None
        check("note: saves a note", bool(m), out[:200])
    except Exception as e:  # noqa: BLE001
        check("note: saves a note", False, str(e))
    for label, fn in (
            ("search: finds the note (exact text)", lambda: nonce in c.tool("search", {"query": nonce})),
            ("day: today's view lists the note", lambda: nonce in c.tool("day", {"date": "today"})),
            ("session: raw view of today's notes session",
             lambda: day is not None and nonce in c.tool("session", {"id": f"notes-{day}"})),
            ("facts: answers", lambda: bool(re.search(r"facts|No facts", c.tool("facts", {})))),
            ("facts: rejects an unknown category", lambda: "Unknown category" in c.tool("facts",
                                                                                      {"category": "nope"}))):
        try:
            check(label, fn())
        except Exception as e:  # noqa: BLE001
            check(label, False, str(e))
    if expect_session:
        try:
            check(f"session: ingested transcript {expect_session} is readable",
                  (expect_text or "") in c.tool("session", {"id": expect_session}))
            check("search: finds the ingested transcript (semantic + text)",
                  expect_session in c.tool("search", {"query": expect_text or expect_session}))
        except Exception as e:  # noqa: BLE001
            check("ingested transcript", False, str(e))

    st, _, body = c.raw("GET", "/index")
    check("GET /index: INDEX.md is served", st == 200 and body.startswith(b"# INDEX"), f"HTTP {st}")
    st, _, _ = c.raw("GET", "/facts/brief?project=smoke-test")
    check("GET /facts/brief: answers", st == 200, f"HTTP {st}")
    if c.sid:
        c.raw("DELETE", "/mcp", None, {"Mcp-Session-Id": c.sid})

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
