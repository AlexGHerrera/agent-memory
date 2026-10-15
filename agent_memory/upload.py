"""Minimal HTTP routes next to the MCP, behind the same BearerAuth:

- `PUT /inbox/<folder>/<...>/<session>.jsonl` — a remote machine uploads Claude Code transcripts to the inbox
  (`cfg.inbox_dir`); the periodic ingestion picks them up when the inbox is listed in `claude_sources`. Atomic
  write (tmp + rename) that never overwrites different content: see `store`.
- `GET /index` — returns INDEX.md (the SessionStart hook uses and caches it).
- `GET /facts/brief?project=<slug>` — the facts block injected by the SessionStart hook.

Everything else goes to the MCP app. No dependencies: plain ASGI.
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import urllib.parse
from datetime import datetime, timezone

SEGMENT = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9._-]{0,200}$")   # no leading dot; "-" allowed (project folders)
MAX_DEPTH = 4
PREFIX = "/inbox/"


class UploadError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def safe_relpath(rel: str) -> list[str]:
    """Validates the relative path: 1-4 safe segments, no '..', the last one ends in .jsonl."""
    parts = rel.split("/")
    if not 1 <= len(parts) <= MAX_DEPTH:
        raise UploadError(400, "invalid depth")
    for p in parts:
        if not SEGMENT.match(p) or p in (".", "..") or ".." in p:
            raise UploadError(400, "invalid segment")
    if not parts[-1].endswith(".jsonl") or len(parts[-1]) <= len(".jsonl"):
        raise UploadError(400, "only .jsonl files")
    return parts


def _ensure_dir(base: str, parts: list[str]) -> str:
    """Creates (700) the intermediate directories, rejecting symlinks anywhere."""
    try:
        st = os.lstat(base)
    except FileNotFoundError:
        raise UploadError(500, "inbox does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UploadError(500, "invalid inbox")
    cur = base
    for p in parts:
        cur = os.path.join(cur, p)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o700)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UploadError(409, "path taken")
    return cur


def _read_existing(path: str) -> bytes | None:
    """Current content of the target (None if missing). Rejects symlinks and anything that is not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:  # it is a symlink
            raise UploadError(409, "path taken") from None
        raise UploadError(500, "could not read the target") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise UploadError(409, "path taken")
        return f.read()


def _write_new(d: str, name: str, dest: str, data: bytes) -> None:
    # the temp file does not end in .jsonl: ingestion (rglob *.jsonl) never sees it half-written
    tmp = os.path.join(d, f".{name}.{secrets.token_hex(4)}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def store(base: str, rel: str, data: bytes) -> tuple[str, str]:
    """Stores the upload without ever losing what is already in the inbox. Returns (path, result):

    - "new": it did not exist.
    - "same": it already existed with the same content (idempotent, untouched).
    - "contained": the upload is a prefix of the existing file (old retry from the client queue): untouched.
    - "extended": the existing file is a prefix of the upload (the session went on: --resume or a retry after it
      grew): replaced; ingestion continues from its offset without duplicates.
    - "copy": it differs in some other way: stored alongside as `<session>.<UTC>-<rand>.jsonl` and the original
      stays intact (ingestion deduplicates by uuid). No 409, because the client hook would retry forever from its
      queue and block later uploads.
    """
    parts = safe_relpath(rel)
    d = _ensure_dir(base, parts[:-1])
    name = parts[-1]
    dest = os.path.join(d, name)
    existing = _read_existing(dest)
    if existing is None:
        result = "new"
    elif existing == data:
        return dest, "same"
    elif existing.startswith(data):
        return dest, "contained"
    elif data.startswith(existing):
        result = "extended"
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{name[:-len('.jsonl')]}.{stamp}-{secrets.token_hex(3)}.jsonl"
        dest = os.path.join(d, name)
        result = "copy"
    _write_new(d, name, dest, data)
    return dest, result


async def _send(send, status: int, payload, ctype: bytes = b"application/json") -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class InboxRoutes:
    """ASGI middleware: serves /inbox/*, /index and /facts/brief; everything else goes to `app` (the MCP)."""

    def __init__(self, app, inbox_dir: str, index_path: str, max_bytes: int, facts_brief=None):
        self.app = app
        self.facts_brief = facts_brief   # callable(project | None) -> str (SessionStart injection)
        self.inbox_dir = inbox_dir
        self.index_path = index_path
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/index":
            if scope["method"] != "GET":
                return await _send(send, 405, {"error": "method"})
            try:
                with open(self.index_path, "rb") as f:
                    return await _send(send, 200, f.read(), b"text/markdown; charset=utf-8")
            except OSError:
                return await _send(send, 404, {"error": "no INDEX"})
        if path == "/facts/brief" and self.facts_brief is not None:
            if scope["method"] != "GET":
                return await _send(send, 405, {"error": "method"})
            qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            project = (qs.get("project") or [""])[0][:80] or None
            try:
                text = self.facts_brief(project)
            except Exception:  # noqa: BLE001 — the hook fails open
                return await _send(send, 500, {"error": "facts unavailable"})
            return await _send(send, 200, text.encode(), b"text/markdown; charset=utf-8")
        if path.startswith(PREFIX):
            if scope["method"] != "PUT":
                return await _send(send, 405, {"error": "method"})
            return await self._put(scope, receive, send, path[len(PREFIX):])
        return await self.app(scope, receive, send)

    async def _put(self, scope, receive, send, rel):
        try:
            safe_relpath(rel)
            for k, v in scope.get("headers", []):
                if k.lower() == b"content-length" and int(v) > self.max_bytes:
                    raise UploadError(413, "too large")
            chunks, size = [], 0
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                chunk = msg.get("body", b"")
                size += len(chunk)
                if size > self.max_bytes:
                    raise UploadError(413, "too large")
                chunks.append(chunk)
                if not msg.get("more_body"):
                    break
            dest, result = store(self.inbox_dir, rel, b"".join(chunks))
        except UploadError as e:
            return await _send(send, e.status, {"error": e.msg})
        except ValueError:
            return await _send(send, 400, {"error": "invalid headers"})
        status = 200 if result in ("same", "contained") else 201
        return await _send(send, status, {"ok": True, "bytes": size, "result": result,
                                          "path": os.path.relpath(dest, self.inbox_dir)})
