"""Shared helpers for the agent-memory Claude Code hooks (stdlib only; Python >= 3.9; Linux and macOS).

Principle: FAIL OPEN. No hook may break or block the session: every error is logged to
~/.local/state/agent-memory-hooks/hooks.log and the hook exits with 0.

Configuration (by priority): environment variables > ~/.config/agent-memory/hooks.json > defaults.
  AGENT_MEMORY_URL            base URL of the memory server (default http://127.0.0.1:8765)
  AGENT_MEMORY_TOKEN          Bearer token (better not: use the file or the macOS keychain)
  AGENT_MEMORY_TOKEN_FILE     file holding the token (600). Default: the first readable of
                              ~/.config/agent-memory/mcp-token and ~/.local/share/agent-memory/config/mcp-token
  AGENT_MEMORY_MACHINE        label used in notes (default: local)
  AGENT_MEMORY_UPLOAD         1 = upload transcripts to the server at SessionEnd (client on another machine);
                              0 (default) = the server reads ~/.claude/projects itself
  AGENT_MEMORY_CONTEXT_FILES  extra Markdown files injected at SessionStart, separated by ':' (e.g. a persona or
                              a preferences file); default none
  AGENT_MEMORY_TASKS          a task list file injected at SessionStart (default none)
  AGENT_MEMORY_CONTEXT_TOKENS SessionStart injection cap (default 4000)
  AGENT_MEMORY_FACTS_TOKENS   cap of the "current key facts" block inside that budget (default 450)
  AGENT_MEMORY_SUMMARY        1 (default) = summarize long sessions at SessionEnd with `claude -p`; 0 = off
  AGENT_MEMORY_SUMMARY_MODEL  model for that summary (default haiku)
  AGENT_MEMORY_HOOK_CHILD=1   set on processes spawned by the hooks (claude -p): the hooks do nothing
  AGENT_MEMORY_NO_HOOKS=1     disables the hooks for that session
The hooks.json keys are the same names in lowercase without the prefix (url, token_file, machine, upload,
context_files [list], tasks, context_tokens, facts_tokens, summary, summary_model).
The token is never written to logs or output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CONFIG_FILE = os.environ.get("AGENT_MEMORY_HOOKS_CONFIG") or os.path.join(HOME, ".config", "agent-memory",
                                                                          "hooks.json")
STATE_DIR = os.environ.get("AGENT_MEMORY_STATE_DIR") or os.path.join(HOME, ".local", "state", "agent-memory-hooks")
CACHE_DIR = os.environ.get("AGENT_MEMORY_CACHE_DIR") or os.path.join(HOME, ".cache", "agent-memory")
DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TOKEN_FILES = (os.path.join(HOME, ".config", "agent-memory", "mcp-token"),
                       os.path.join(HOME, ".local", "share", "agent-memory", "config", "mcp-token"))
KEYCHAIN_SERVICE = "agent-memory"
LOG_MAX = 1_000_000

# Memory MCP tool names (server `memory`).
NOTE_TOOL = "note"
DAY_TOOL = "day"
FACTS_BRIEF_PATH = "/facts/brief"
NOTE_TOOL_SUFFIXES = ("memory__note",)


def _file_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def setting(name: str, default=None):
    """AGENT_MEMORY_<NAME> env var, then the `name` key of hooks.json, then `default`."""
    v = os.environ.get("AGENT_MEMORY_" + name.upper())
    if v:
        return v
    cfg = _file_config()
    if cfg.get(name) not in (None, ""):
        return cfg[name]
    return default


def _flag(name: str, default: bool) -> bool:
    v = setting(name, None)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(setting(name, default))
    except (TypeError, ValueError):
        return default


def machine() -> str:
    return str(setting("machine", "local"))


def memory_url() -> str:
    return str(setting("url", DEFAULT_URL)).rstrip("/")


def upload_enabled() -> bool:
    return _flag("upload", False)


def summary_enabled() -> bool:
    return _flag("summary", True)


def summary_model() -> str:
    return str(setting("summary_model", "haiku"))


def tasks_path() -> str:
    p = setting("tasks", "")
    return os.path.expanduser(str(p)) if p else ""


def context_files() -> list[str]:
    v = setting("context_files", [])
    items = v if isinstance(v, list) else [x for x in str(v).split(os.pathsep) if x]
    return [os.path.expanduser(str(x)) for x in items]


def context_tokens() -> int:
    return _int("context_tokens", 4000)


def facts_tokens() -> int:
    return _int("facts_tokens", 450)


def disabled() -> bool:
    return os.environ.get("AGENT_MEMORY_HOOK_CHILD") == "1" or os.environ.get("AGENT_MEMORY_NO_HOOKS") == "1"


# ---------------------------------------------------------------- log and state

def log(msg: str) -> None:
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        p = os.path.join(STATE_DIR, "hooks.log")
        if os.path.exists(p) and os.path.getsize(p) > LOG_MAX:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def run_safely(fn) -> None:
    """Runs a hook body and ALWAYS exits with 0 (fail open)."""
    try:
        if not disabled():
            fn()
    except BaseException as e:  # noqa: BLE001 — SystemExit/KeyboardInterrupt included: never break the session
        if not isinstance(e, SystemExit) or e.code not in (0, None):
            log(f"ERROR {os.path.basename(sys.argv[0])}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
    sys.exit(0)


def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        d = json.loads(data) if data.strip() else {}
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def read_text(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- token

def _token_from_file(path: str) -> str:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def load_token() -> str:
    t = os.environ.get("AGENT_MEMORY_TOKEN", "").strip()
    if t:
        return t
    configured = setting("token_file", "")
    for f in ([str(configured)] if configured else []) + list(DEFAULT_TOKEN_FILES):
        t = _token_from_file(f)
        if t:
            return t
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


# ---------------------------------------------------------------- HTTP / MCP

class MemoryServiceError(Exception):
    """The memory service could not be reached or answered with an error."""


def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None,
         timeout: float = 5.0, token: str | None = None):
    token = token if token is not None else load_token()
    if not token:
        raise MemoryServiceError("no MCP token (see INSTALL.md)")
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    req = urllib.request.Request(memory_url() + path, data=body, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — URL from our own config
    except urllib.error.HTTPError as e:
        raise MemoryServiceError(f"HTTP {e.code} on {method} {path}") from None
    except (urllib.error.URLError, OSError) as e:
        raise MemoryServiceError(f"cannot reach the memory service ({type(e).__name__})") from None
    with resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def _parse_rpc(ctype: str, raw: bytes, want_id: int) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in (ctype or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
        raise MemoryServiceError("SSE response without a result")
    msg = json.loads(text)
    if isinstance(msg, list):
        msg = next((m for m in msg if m.get("id") == want_id), {})
    return msg


def mcp_call(tool: str, arguments: dict, timeout: float = 8.0) -> str:
    """Calls a tool of the `memory` MCP over streamable HTTP (initialize → initialized → tools/call)."""
    token = load_token()
    base_h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload: dict, sid: str | None):
        h = dict(base_h)
        if sid:
            h["Mcp-Session-Id"] = sid
        return http("POST", "/mcp", json.dumps(payload).encode(), h, timeout, token)

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "agent-memory-hooks", "version": "1"}}}
    _, hdrs, _ = post(init, None)
    sid = next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, hdrs, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments}}, sid)
    ctype = next((v for k, v in hdrs.items() if k.lower() == "content-type"), "")
    msg = _parse_rpc(ctype, raw, 2)
    if "error" in msg:
        raise MemoryServiceError(f"MCP error: {msg['error'].get('message', '')[:200]}")
    res = msg.get("result") or {}
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
    if res.get("isError"):
        raise MemoryServiceError(f"tool {tool} returned an error: {text[:200]}")
    if sid:
        try:
            http("DELETE", "/mcp", None, {"Mcp-Session-Id": sid}, 2.0, token)
        except MemoryServiceError:
            pass
    return text


# ---------------------------------------------------------------- pending queue (never lose anything)

def spool_dir() -> str:
    return os.path.join(STATE_DIR, "pending")


def spool(kind: str, payload: dict) -> None:
    """Stores a failed operation (note or upload) so the next hook retries it."""
    name = f"{int(time.time() * 1000)}-{os.getpid()}-{kind}.json"
    write_private(os.path.join(spool_dir(), name), json.dumps({"kind": kind, **payload}, ensure_ascii=False))
    log(f"pending item stored: {name}")


def note(text: str, project: str | None = None, timeout: float = 8.0) -> bool:
    args = {"text": text[:20000]}
    if project:
        args["project"] = project[:80]
    try:
        r = mcp_call(NOTE_TOOL, args, timeout)
        log(f"note OK: {r[:120]}")
        return True
    except (MemoryServiceError, ValueError) as e:
        log(f"note failed ({e}); stored as pending")
        spool("note", {"args": args})
        return False


def upload_file(local: str, rel: str, timeout: float = 30.0) -> None:
    with open(local, "rb") as f:
        data = f.read()
    quoted = "/".join(urllib.request.quote(p, safe="") for p in rel.split("/"))
    st, _, _ = http("PUT", "/inbox/" + quoted, data, {"Content-Type": "application/x-ndjson"}, timeout)
    if st not in (200, 201):
        raise MemoryServiceError(f"upload returned {st}")


def has_pending() -> bool:
    try:
        return any(n.endswith(".json") for n in os.listdir(spool_dir()))
    except OSError:
        return False


def flush_spool(max_items: int = 20, budget_s: float = 20.0) -> int:
    """Retries pending items; those failing again stay. Returns how many were sent."""
    try:
        paths = [os.path.join(spool_dir(), n) for n in os.listdir(spool_dir()) if n.endswith(".json")]
    except OSError:
        return 0
    paths = sorted(paths, key=os.path.basename)[:max_items]
    sent, t0 = 0, time.time()
    for p in paths:
        if time.time() - t0 > budget_s:
            break
        n = os.path.basename(p)
        try:
            with open(p, encoding="utf-8") as f:
                item = json.load(f)
            kind = item.get("kind")
            if kind == "note":
                mcp_call(NOTE_TOOL, item["args"], 8.0)
            elif kind == "upload":
                if os.path.exists(item["local"]):
                    upload_file(item["local"], item["rel"])
            os.unlink(p)
            sent += 1
        except (MemoryServiceError, OSError, ValueError, KeyError) as e:
            log(f"pending {n} still failing: {e}")
            if isinstance(e, MemoryServiceError):
                break  # offline: do not insist with the rest
    return sent


# ---------------------------------------------------------------- transcripts

def iter_jsonl(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
    return out


def is_human_turn(rec: dict) -> bool:
    """A message typed by the user (not tool results, metadata, local commands or summaries)."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = "\n".join(_texts(content)).strip()
    if not text:
        return False
    return not text.startswith(("<command-name>", "<local-command", "<command-message>", "<system-reminder>",
                                "Caveat: The messages below"))


def transcript_digest(path: str) -> dict:
    """Extracts from a JSONL: user turns, conclusions (last assistant text before each user turn),
    memory notes written and the state of the task list (TodoWrite / TaskCreate+TaskUpdate)."""
    human, conclusions, notes = [], [], []
    last_assistant_text = ""
    todos: list[dict] = []
    tasks: dict[str, dict] = {}
    pending_create: dict[str, str] = {}
    cwd = ""
    for rec in iter_jsonl(path):
        cwd = rec.get("cwd") or cwd
        if rec.get("isSidechain"):
            continue
        typ = rec.get("type")
        content = (rec.get("message") or {}).get("content")
        if typ == "assistant":
            t = "\n".join(_texts(content)).strip()
            if t:
                last_assistant_text = t
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name", ""), b.get("input") or {}
                    if name.endswith(NOTE_TOOL_SUFFIXES):
                        notes.append(str(inp.get("text") or ""))
                    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        todos = [x for x in inp["todos"] if isinstance(x, dict)]
                    elif name == "TaskCreate":
                        pending_create[b.get("id", "")] = str(inp.get("subject") or inp.get("description") or "")[:200]
                    elif name == "TaskUpdate" and inp.get("taskId") is not None:
                        tid = str(inp["taskId"])
                        tasks.setdefault(tid, {"subject": f"task {tid}", "status": "pending"})
                        if inp.get("status"):
                            tasks[tid]["status"] = inp["status"]
                        if inp.get("subject"):
                            tasks[tid]["subject"] = str(inp["subject"])[:200]
        elif typ == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending_create:
                        subj = pending_create.pop(b["tool_use_id"])
                        rtext = b.get("content")
                        rtext = rtext if isinstance(rtext, str) else " ".join(_texts(rtext))
                        m = re.search(r"#?(\d+)", rtext or "")
                        tid = m.group(1) if m else f"n{len(tasks) + 1}"
                        tasks.setdefault(tid, {"subject": subj, "status": "pending"})
                        tasks[tid]["subject"] = subj or tasks[tid]["subject"]
            if is_human_turn(rec):
                if last_assistant_text:
                    conclusions.append(last_assistant_text)
                    last_assistant_text = ""
                human.append("\n".join(_texts(content)).strip())
    if last_assistant_text:
        conclusions.append(last_assistant_text)
    open_items = [f"{x.get('content', '')} [{x.get('status', '')}]" for x in todos
                  if x.get("status") in ("pending", "in_progress")]
    open_items += [f"{v['subject']} [{v['status']}]" for v in tasks.values()
                   if v.get("status") in ("pending", "in_progress")]
    return {"human": human, "conclusions": conclusions, "notes": notes, "open_items": open_items, "cwd": cwd}


def project_name(cwd: str) -> str:
    return os.path.basename((cwd or "").rstrip("/")) or "no-project"


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def find_claude() -> str | None:
    for c in (shutil.which("claude"), os.path.join(HOME, ".local", "bin", "claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.access(c, os.X_OK):
            return c
    return None
