#!/usr/bin/env python3
"""agent-memory SessionStart hook: injects optional context files + current key facts + an optional task list +
the memory INDEX.md + the recent log (day today/yesterday), with a hard cap (default ~4k tokens) for everything.
Trim order when over the cap: the log first, then INDEX, then the facts, then the context files, then the tasks.

- Current key facts: GET /facts/brief?project=<cwd basename> (Bearer). The server builds three sections with their
  own budgets (core identity facts, recent changes, facts of the current project with decay) and never includes the
  excluded categories (health by default); this hook caps the block again at AGENT_MEMORY_FACTS_TOKENS. It is a
  plain HTTP read, not the `facts` tool, so injecting a fact does not count as using it (decay).
- INDEX.md: GET /index with a local cache in ~/.cache/agent-memory/INDEX.md (used when the server is down).
- Recent log: the `day` tool of the `memory` MCP over HTTP.
Fails open: whatever is missing is skipped; it never breaks the session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import common as mc  # noqa: E402

CHARS_PER_TOKEN = 3.0      # conservative for non-English text with emojis and markdown
CUT_NOTE = "\n…(trimmed by the context cap; the rest is in the memory: search/day)"
# trim order: first what is biggest and matters least
TRIM_ORDER = ["log", "index", "facts", "context", "tasks"]


def strip_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def has_entries(text: str) -> bool:
    """The task list is only injected when it holds at least one entry ('- ' lines)."""
    return any(line.lstrip().startswith("- ") for line in text.splitlines())


def get_index(timeout: float = 3.0) -> str:
    cache = os.path.join(mc.CACHE_DIR, "INDEX.md")
    try:
        st, _, body = mc.http("GET", "/index", timeout=timeout)
        if st == 200 and body:
            text = body.decode("utf-8", errors="replace")
            mc.write_private(cache, text)
            return text
    except mc.MemoryServiceError as e:
        mc.log(f"session_start: remote INDEX unavailable ({e}); using cache")
    text = mc.read_text(cache, 60_000)
    if text:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(cache)))
        return f"(local cache from {when}; the memory did not answer)\n" + text
    return ""


def get_log(timeout: float = 4.0) -> str:
    parts = []
    for date in ("today", "yesterday"):
        try:
            parts.append(mc.mcp_call(mc.DAY_TOOL, {"date": date}, timeout).strip())
        except (mc.MemoryServiceError, ValueError) as e:
            mc.log(f"session_start: day({date}) failed: {e}")
            break
    return "\n\n".join(p for p in parts if p)


def get_facts(project: str | None = None, timeout: float = 4.0) -> str:
    """Facts brief for this session, capped at AGENT_MEMORY_FACTS_TOKENS (whole lines). '' if unavailable."""
    path = mc.FACTS_BRIEF_PATH
    if project:
        path += "?project=" + urllib.parse.quote(project[:80])
    try:
        st, _, body = mc.http("GET", path, timeout=timeout)
    except mc.MemoryServiceError as e:
        mc.log(f"session_start: facts brief unavailable ({e})")
        return ""
    if st != 200 or not body:
        return ""
    text = body.decode("utf-8", errors="replace").strip()
    return trim_lines(text, int(mc.facts_tokens() * CHARS_PER_TOKEN))


def trim_lines(text: str, max_chars: int) -> str:
    """Trims by whole lines from the end; if nothing useful fits, returns ''."""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(CUT_NOTE)
    if budget < 150:
        return ""
    out, used = [], 0
    for line in text.splitlines():
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    if not out:  # a single huge line
        return text[:budget].rstrip() + CUT_NOTE
    return "\n".join(out).rstrip() + CUT_NOTE


def render(sections: dict, titles: dict) -> str:
    head = ("# Memory context (injected at startup; capped)\n"
            "More detail always in the `memory` MCP (search, day, session, facts).")
    body = [head]
    for key in ("context", "facts", "tasks", "index", "log"):
        if sections.get(key):
            body.append(f"## {titles[key]}\n{sections[key]}")
    return "\n\n".join(body) + "\n"


def build_context(context: str, tasks: str, index: str, log_text: str, max_tokens: int,
                  tasks_path: str = "tasks", facts: str = "") -> str:
    max_chars = int(max_tokens * CHARS_PER_TOKEN)
    t = strip_comments(tasks)
    sections = {
        "context": strip_comments(context),
        "tasks": t if has_entries(t) else "",
        "facts": facts.strip(),
        "index": index.strip(),
        "log": log_text.strip(),
    }
    titles = {"context": "Context files",
              "facts": "Current key facts (memory; sources and history: facts(query, include_history=true))",
              "tasks": f"Open tasks ({tasks_path})", "index": "Memory index (INDEX.md)",
              "log": "Recent log (day today/yesterday)"}
    text = render(sections, titles)
    for key in TRIM_ORDER:
        over = len(text) - max_chars
        if over <= 0:
            break
        if sections[key]:
            sections[key] = trim_lines(sections[key], len(sections[key]) - over)
            text = render(sections, titles)
    if len(text) > max_chars:  # hard cap, no matter what
        text = text[: max_chars - len(CUT_NOTE)] + CUT_NOTE
    return text


def read_context_files(paths: list[str]) -> str:
    parts = []
    for p in paths:
        text = strip_comments(mc.read_text(p, 50_000))
        if text:
            parts.append(f"### {os.path.basename(p)}\n{text}")
    return "\n\n".join(parts)


def in_background(fn, timeout: float):
    """Runs fn in a daemon thread and returns a function that waits at most `timeout` s ('' if it is late)."""
    out = {"v": ""}

    def run():
        try:
            out["v"] = fn() or ""
        except Exception as e:  # noqa: BLE001 — fail open
            mc.log(f"session_start: {getattr(fn, '__name__', 'task')} failed: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t0 = time.time()

    def wait():
        t.join(max(0.0, timeout - (time.time() - t0)))
        return out["v"]
    return wait


def maybe_flush_spool() -> None:
    try:
        if mc.has_pending():
            here = os.path.dirname(os.path.realpath(__file__))
            env = dict(os.environ, AGENT_MEMORY_HOOK_CHILD="1")
            subprocess.Popen([sys.executable, os.path.join(here, "session_end.py"), "--flush"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=env)
    except OSError:
        pass


def main() -> None:
    payload = mc.read_stdin_json()
    tpath = mc.tasks_path()
    project = mc.project_name(payload.get("cwd") or os.getcwd())
    index, log_text, facts = (in_background(get_index, 10.0), in_background(get_log, 15.0),
                              in_background(lambda: get_facts(project), 8.0))
    index, log_text, facts = index(), log_text(), facts()
    ctx = build_context(read_context_files(mc.context_files()), mc.read_text(tpath) if tpath else "",
                        index, log_text, mc.context_tokens(), tpath or "tasks", facts)
    mc.log(f"session_start: {payload.get('source', '?')} {len(ctx)} characters injected")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}},
                     ensure_ascii=False))
    maybe_flush_spool()


if __name__ == "__main__":
    mc.run_safely(main)
