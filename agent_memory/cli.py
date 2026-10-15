"""CLI: agent-memory {serve|ingest|consolidate|index|stats|day|search|facts|proposals|refilter|init-token|doctor}."""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys

from . import config as config_mod
from .i18n import LANGUAGES


def _init_token(path: str) -> dict:
    """Creates the MCP Bearer token file (600) if missing. Never prints the token."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        os.chmod(path, 0o600)
        return {"token_file": path, "created": False}
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32) + "\n")
    return {"token_file": path, "created": True}


def _doctor(cfg) -> dict:
    """Read-only checks of the configuration (no secrets in the output)."""
    import ipaddress
    import shutil
    import stat
    out = {"config_ok": True, "db_path": cfg.db_path, "host": cfg.host, "port": cfg.port, "checks": {}}
    c = out["checks"]
    try:
        from .server import check_host
        check_host(cfg.host)
        c["bind_address"] = "ok (loopback)" if ipaddress.ip_address(cfg.host).is_loopback else "ok (private network)"
    except SystemExit as e:
        c["bind_address"] = f"FAIL: {e}"
    try:
        st = os.stat(cfg.mcp_token_file)
        c["token_file"] = "ok (600)" if not st.st_mode & (stat.S_IRWXG | stat.S_IRWXO) else "FAIL: mode is not 600"
    except OSError:
        c["token_file"] = "FAIL: missing (run `agent-memory init-token`)"
    c["claude_sources"] = [f"{s.path}: {'ok' if os.path.isdir(s.path) else 'missing'}" for s in cfg.claude_sources]
    c["openclaw_sources"] = [f"{s.path}: {'ok' if os.path.isdir(s.path) else 'missing'}" for s in cfg.openclaw_sources]
    if cfg.llm_command:
        c["llm"] = f"llm_command: {'ok' if shutil.which(cfg.llm_command[0]) else 'FAIL: not found'}"
    else:
        c["llm"] = f"claude CLI: {'ok' if shutil.which(cfg.claude_bin) else 'missing (consolidation will fail)'}"
    c["model_cached"] = os.path.isdir(cfg.models_dir) and bool(os.listdir(cfg.models_dir))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent-memory")
    p.add_argument("--config", help="path to config.toml (default: $AGENT_MEMORY_CONFIG or "
                                    "~/.config/agent-memory/config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="MCP server (streamable HTTP, Bearer token)")
    sub.add_parser("ingest", help="incremental ingestion of every source + embeddings")
    c = sub.add_parser("consolidate", help="session->day->week->month->year summaries, facts + INDEX.md")
    c.add_argument("--include-active", action="store_true", help="also summarize sessions that are still active")
    c.add_argument("--no-proposals", action="store_true", help="skip the self-learning step")
    c.add_argument("--no-facts", action="store_true", help="skip the facts extraction step")
    sub.add_parser("index", help="regenerate INDEX.md without calling the model")
    d = sub.add_parser("day", help="print the day(date) view as text")
    d.add_argument("date", nargs="?", default="yesterday", help="YYYY-MM-DD, today/hoy or yesterday/ayer")
    d.add_argument("--lang", choices=LANGUAGES, help="language of the headings (default: config `language`)")
    pr = sub.add_parser("proposals", help="approval queue: facts and self-learning proposals (JSON output)")
    pr.add_argument("action", choices=["pending", "show", "mark", "sweep", "secrets"],
                    help="mark ID applied|rejected|sent|failed; secrets: reads text on stdin and returns only the "
                         "number of redactions (never the text)")
    pr.add_argument("id", nargs="?", type=int)
    pr.add_argument("status", nargs="?")
    pr.add_argument("--result", default=None)
    sub.add_parser("stats", help="counts")
    b = sub.add_parser("search", help="hybrid search (like the MCP search tool), JSON output")
    b.add_argument("query")
    b.add_argument("--since")
    b.add_argument("--until")
    b.add_argument("--project")
    b.add_argument("--limit", type=int, default=10)
    r = sub.add_parser("refilter", help="run the secrets filter again over the whole database (dry run by default)")
    r.add_argument("--apply", action="store_true", help="write the changes (without it, only count)")
    r.add_argument("--no-embeddings", action="store_true",
                   help="with --apply, leave the vectors pending for the next ingestion")
    r.add_argument("--db", help="path of another database (e.g. a copy) instead of the configured one")
    f = sub.add_parser("facts", help="current facts (same text as the MCP facts tool)")
    f.add_argument("--query")
    f.add_argument("--category")
    f.add_argument("--history", action="store_true", help="include superseded values")
    sub.add_parser("init-token", help="create the MCP Bearer token file (600) if it does not exist")
    sub.add_parser("download-model", help="download the embedding model into models_dir (one-time)")
    sub.add_parser("doctor", help="check the configuration (JSON, no secrets)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config_mod.load(a.config)

    if a.cmd == "serve":
        from .server import main as serve
        serve(cfg)
        return 0
    if a.cmd == "init-token":
        print(json.dumps(_init_token(cfg.mcp_token_file)))
        return 0
    if a.cmd == "doctor":
        print(json.dumps(_doctor(cfg), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "download-model":
        from .embed import get_default
        dim = len(get_default(cfg).query("hello"))
        print(json.dumps({"model": cfg.embedding_model, "dim": dim, "models_dir": cfg.models_dir}))
        return 0

    from .db import connect
    from .secrets_filter import LiteralSecrets, SecretFilter
    if a.cmd == "refilter" and a.db:
        cfg.db_path = a.db
    conn = connect(cfg.db_path, cfg.embedding_dim)
    sf = SecretFilter(LiteralSecrets.from_file(cfg.literal_secrets_file))

    if a.cmd == "ingest":
        from .embed import get_default
        from .ingest import Ingestor
        out = Ingestor(conn, cfg, sf, get_default(cfg)).run_all()
    elif a.cmd == "search":
        from .embed import get_default
        from .search import search
        out = {"results": search(conn, get_default(cfg), a.query, a.since, a.until, a.project, a.limit)}
    elif a.cmd == "consolidate":
        from .consolidate import Consolidator, claude_cli_llm
        from .facts import system_prompt as facts_system
        from .proposals import system_prompt as proposals_system
        pllm = None
        if cfg.self_learning and not a.no_proposals:
            pllm = claude_cli_llm(cfg, proposals_system(cfg.language, cfg.user_name, cfg.assistant_name))
        fllm = None if a.no_facts else claude_cli_llm(cfg, facts_system(cfg.user_name, cfg.assistant_name))
        out = Consolidator(conn, cfg, claude_cli_llm(cfg), sf, proposals_llm=pllm,
                           facts_llm=fllm).run(include_active=a.include_active)
    elif a.cmd == "day":
        from .views import day
        print(day(conn, a.date, cfg.timezone, lang=a.lang or cfg.language))
        return 0
    elif a.cmd == "proposals":
        from . import proposals
        try:
            if a.action == "pending":
                out = {"proposals": proposals.pending(conn)}
            elif a.action == "secrets":
                out = {"redactions": proposals.count_secrets(sf, sys.stdin.read())}
            elif a.action == "sweep":
                out = {"new": proposals.sweep_notes(conn, sf, cfg.timezone)}
            elif a.id is None:
                raise ValueError("missing id")
            elif a.action == "show":
                prop = proposals.get(conn, a.id)
                if not prop:
                    raise ValueError(f"proposal {a.id} does not exist")
                out = {"proposal": prop}
            else:
                out = {"proposal": proposals.mark(conn, a.id, a.status or "", a.result)}
        except ValueError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            return 2
    elif a.cmd == "refilter":
        from .refilter import refilter
        ing = None
        if a.apply and not a.no_embeddings:
            from .embed import get_default
            from .ingest import Ingestor
            ing = Ingestor(conn, cfg, sf, get_default(cfg))
        out = refilter(conn, sf, apply=a.apply, ingestor=ing)
        if a.apply and not a.db:
            from .consolidate import write_index
            write_index(conn, cfg)  # INDEX.md includes headlines and notes
    elif a.cmd == "facts":
        from . import facts
        facts.ensure_schema(conn)
        print(facts.format_facts(conn, cfg.timezone, a.query, a.category, a.history))
        return 0
    elif a.cmd == "index":
        from .consolidate import write_index
        out = {"lines": write_index(conn, cfg).count("\n")}
    else:
        q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
        out = {"messages": q("SELECT COUNT(*) FROM raw_messages"), "sessions": q("SELECT COUNT(*) FROM sessions"),
               "vectors": q("SELECT COUNT(*) FROM vec_messages"), "summaries": q("SELECT COUNT(*) FROM summaries"),
               "notes": q("SELECT COUNT(*) FROM notes"),
               "redactions": q("SELECT COALESCE(SUM(redactions),0) FROM raw_messages")}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
