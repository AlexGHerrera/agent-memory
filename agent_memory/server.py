"""Memory MCP server: EXACTLY 5 tools (search, day, session, note, facts).

Streamable HTTP transport at http://<host>:<port>/mcp. Default bind 127.0.0.1; a private-network address (RFC 1918,
CGNAT/Tailscale 100.64.0.0/10, IPv6 ULA) is allowed, 0.0.0.0/:: and public addresses are refused. Every HTTP request
needs `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import stat
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config as config_mod
from .db import connect
from .ingest import Ingestor
from .search import search as run_search
from .secrets_filter import LiteralSecrets, SecretFilter
from .views import day as view_day, local_time, resolve_date, session as view_session

log = logging.getLogger("agent_memory.server")

SERVER_NAME = "memory"
INSTRUCTIONS = (
    "Long-term memory of this agent: past sessions with the user, saved notes and current facts. For 'what did we "
    "do yesterday?' use day('yesterday' or YYYY-MM-DD); for the detail, session(id) with an id from that list; to "
    "look for something specific, search(query). note saves a note. facts() lists what is true today about the "
    "user (with history and source ids).")

MAX_NOTE_CHARS = 20000


class Memory:
    """Shared state of the tools (connection, filter and embedder)."""

    def __init__(self, cfg, embedder=None, conn=None):
        self.cfg = cfg
        self.conn = conn or connect(cfg.db_path, cfg.embedding_dim)
        self.sfilter = SecretFilter(LiteralSecrets.from_file(cfg.literal_secrets_file))
        self.embedder = embedder
        self.lock = threading.Lock()

    def search(self, query, since=None, until=None, project=None) -> str:
        try:
            since = resolve_date(since, self.cfg.timezone) if since else None
            until = resolve_date(until, self.cfg.timezone) if until else None
        except ValueError:
            return "Invalid dates: use YYYY-MM-DD."
        with self.lock:
            res = run_search(self.conn, self.embedder, query, since, until, project, limit=12)
        if not res:
            return "No results."
        lines = [f"{len(res)} results for {query!r} (times in {self.cfg.timezone}; use session(id) for the full "
                 f"raw transcript):"]
        for r in res:
            # Date and time both in the configured timezone: never a local date next to a UTC time.
            lines.append(f"- {local_time(r['ts'], self.cfg.timezone)} · session `{r['session']}` · {r['project']}"
                         f" · {r['source']}/{r['role']}\n  {r['snippet']}")
        return "\n".join(lines)

    def day(self, date) -> str:
        with self.lock:
            return view_day(self.conn, date, self.cfg.timezone, lang="en")

    def session(self, ident) -> str:
        with self.lock:
            return view_session(self.conn, ident, self.cfg.timezone)

    def facts(self, query=None, category=None, include_history=False) -> str:
        from . import facts as facts_mod
        with self.lock:
            facts_mod.ensure_schema(self.conn)
            return facts_mod.format_facts(self.conn, self.cfg.timezone, query, category, bool(include_history))

    def facts_brief(self, project=None) -> str:
        from . import facts as facts_mod
        with self.lock:
            return facts_mod.brief(self.conn, self.cfg, project)

    def note(self, text, project=None) -> str:
        if not (text or "").strip():
            return "Empty note: nothing saved."
        if len(text) > MAX_NOTE_CHARS:
            return f"Note too long ({len(text)} characters; maximum {MAX_NOTE_CHARS}). Not saved."
        if project:
            project = self.sfilter.redact(project)[0].strip()[:80] or None
        with self.lock:
            ing = Ingestor(self.conn, self.cfg, self.sfilter, self.embedder)
            r = ing.add_note(text, project)
            ing.embed_pending()
        extra = f" ({r['redactions']} secrets redacted)" if r["redactions"] else ""
        return f"Note {r['id']} saved on {r['date']}{extra}."


def build_server(mem: Memory, host: str, port: int, allowed_hosts: list[str] | None = None) -> FastMCP:
    hosts = list(allowed_hosts or []) + [f"{host}:{port}", host]
    if ipaddress.ip_address(host).is_loopback:
        hosts += [f"localhost:{port}", "localhost"]
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, host=host, port=port, streamable_http_path="/mcp",
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                               allowed_hosts=hosts, allowed_origins=[]))

    @mcp.tool()
    def search(query: str, since: str | None = None, until: str | None = None,
               project: str | None = None) -> str:
        """Hybrid search (exact text + semantic) across the user's whole memory.
        since/until: YYYY-MM-DD dates (inclusive). project: filter by project name (partial match)."""
        return mem.search(query, since, until, project)

    @mcp.tool()
    def day(date: str) -> str:
        """Summary of a day (YYYY-MM-DD, 'today'/'hoy' or 'yesterday'/'ayer') with its sessions and their ids."""
        return mem.day(date)

    @mcp.tool()
    def session(id: str) -> str:
        """Raw transcript of a session by id (exact text of the user and the assistant).
        Long sessions are paginated: ask for 'id#2', 'id#3'..."""
        return mem.session(id)

    @mcp.tool()
    def note(text: str, project: str | None = None) -> str:
        """Saves a note in the memory (secrets are filtered out)."""
        return mem.note(text, project)

    @mcp.tool()
    def facts(query: str | None = None, category: str | None = None, include_history: bool = False) -> str:
        """Current facts about the user (what is true today), each with its source id for session(id).
        query: words to match; category: health|work|decision|preference|personal|project;
        include_history: also show superseded values with their validity dates."""
        return mem.facts(query, category, include_history)

    return mcp


def check_host(host: str) -> None:
    """Loopback or a private-network address only (never 0.0.0.0/:: nor a public address)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise SystemExit(f"host must be an IP address, got {host!r}") from None
    if ip.is_unspecified:
        raise SystemExit("Listening on 0.0.0.0/:: is forbidden: bind to 127.0.0.1 or a private-network IP")
    if ip.is_global or ip.is_multicast:
        raise SystemExit(f"{host} is a public address: bind to 127.0.0.1 or a private-network IP")


class BearerAuth:
    """ASGI middleware: requires `Authorization: Bearer <token>` on every HTTP request (constant-time comparison)."""

    def __init__(self, app, token: str):
        if len(token) < 32:
            raise SystemExit("MCP token too short (minimum 32 characters)")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = b""
            for k, v in scope.get("headers", []):
                if k.lower() == b"authorization":
                    got = v
                    break
            if not hmac.compare_digest(got, self.expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def load_token(path: str) -> str:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{path} must have mode 600")
    token = open(path, encoding="utf-8").read().strip()
    if not token:
        raise SystemExit(f"{path} is empty")
    return token


def main(cfg=None) -> None:
    import uvicorn

    from .embed import get_default
    cfg = cfg or config_mod.load()
    check_host(cfg.host)
    token = load_token(cfg.mcp_token_file)
    mem = Memory(cfg, embedder=get_default(cfg))
    mem.embedder.query("warm up")  # load the model before accepting requests
    mcp = build_server(mem, cfg.host, cfg.port, cfg.allowed_hosts)
    from .upload import InboxRoutes
    os.makedirs(cfg.inbox_dir, mode=0o700, exist_ok=True)
    routes = InboxRoutes(mcp.streamable_http_app(), cfg.inbox_dir, cfg.index_path, cfg.upload_max_mb * 1024 * 1024,
                         facts_brief=mem.facts_brief)
    app = BearerAuth(routes, token)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
