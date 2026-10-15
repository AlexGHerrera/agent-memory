from conftest import rec, write_jsonl
from agent_memory.ingest import Ingestor
from agent_memory.search import search
from agent_memory.secrets_filter import SecretFilter
from agent_memory.views import day, resolve_date, session

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"
LONG = "Exact line with accents: año, señal, «quotes» and\n  indentation   with spaces.\n"


def _load(tmp_path, cfg, conn, embedder):
    write_jsonl(tmp_path / "projects" / "-home-alice-demo-app" / f"{A}.jsonl", [
        rec("user", "a1", A, "2026-10-02T10:00:00Z", "Set up the encrypted backup with borg to the NAS"),
        rec("assistant", "a2", A, "2026-10-02T10:01:00Z", [{"type": "text", "text": LONG}]),
    ])
    write_jsonl(tmp_path / "projects" / "-home-alice-other" / f"{B}.jsonl",
                [rec("user", "b1", B, "2026-10-03T11:00:00Z", "Lentil recipe for dinner",
                     cwd="/home/alice/other")])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()


def test_search_hybrid_and_filters(tmp_path, cfg, conn, embedder):
    _load(tmp_path, cfg, conn, embedder)
    r = search(conn, embedder, "backup borg")
    assert r and r[0]["session"] == A
    r = search(conn, embedder, "lentil")
    assert r[0]["session"] == B
    assert search(conn, embedder, "borg", since="2026-10-03") == [] or \
        all(x["date"] >= "2026-10-03" for x in search(conn, embedder, "borg", since="2026-10-03"))
    assert all(x["project"] == "other" for x in search(conn, embedder, "recipe backup", project="other"))
    # without an embedder (FTS only) it also works; diacritics do not matter
    r = search(conn, None, "senal")
    assert r and r[0]["session"] == A


def test_day_and_session_exact(tmp_path, cfg, conn, embedder):
    _load(tmp_path, cfg, conn, embedder)
    out = day(conn, "2026-10-02", cfg.timezone)
    assert A in out and B not in out and "No consolidated summary" in out
    conn.execute("INSERT INTO summaries(level, period, headline, text, session_ids, created_at) VALUES "
                 "('day','2026-10-02','Borg backup','Borg backup\n\n- Borg was configured', '[]', 'x')")
    out = day(conn, "2026-10-02", cfg.timezone)
    assert "Borg was configured" in out and out.startswith("# Day 2026-10-02")
    raw = session(conn, A, cfg.timezone)
    assert LONG in raw  # exact raw text, spaces and line breaks included
    assert "Set up the encrypted backup with borg to the NAS" in raw
    assert session(conn, A[:8], cfg.timezone) == raw  # unique prefix
    assert "No session" in session(conn, "nothing", cfg.timezone)
    assert "Invalid date" in day(conn, "not-a-date", cfg.timezone)


def test_day_view_is_localized(tmp_path, cfg, conn, embedder):
    _load(tmp_path, cfg, conn, embedder)
    es = day(conn, "2026-10-02", cfg.timezone, lang="es")
    assert es.startswith("# Día 2026-10-02") and "Aún no hay resumen" in es and "(hora local)" in es
    assert "Fecha no válida" in day(conn, "x", cfg.timezone, lang="es")


def test_resolve_date_accepts_english_and_spanish(cfg):
    assert resolve_date("today", cfg.timezone) == resolve_date("hoy", cfg.timezone)
    assert resolve_date("yesterday", cfg.timezone) == resolve_date("ayer", cfg.timezone)
    assert resolve_date("2026-10-02", cfg.timezone) == "2026-10-02"


def test_day_is_compact(tmp_path, cfg, conn, embedder):
    write_jsonl(tmp_path / "projects" / "p" / f"{A}.jsonl",
                [rec("user", f"a{i}", A, f"2026-10-02T10:{i:02d}:00Z", "huge dump " * 2000) for i in range(30)])
    write_jsonl(tmp_path / "projects" / "p" / A / "subagents" / "agent-x.jsonl",
                [rec("user", "s1", A, "2026-10-02T10:00:01Z", "task", isSidechain=True, agentId="x")])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    out = day(conn, "2026-10-02", cfg.timezone)
    assert len(out) < 8000
    assert "1 subagents" in out and f"`{A}/agent-x`" not in out
    assert f"{A}/agent-x" in session(conn, A, cfg.timezone)


def test_session_pagination(tmp_path, cfg, conn, embedder):
    big = "x" * 1000
    write_jsonl(tmp_path / "projects" / "p" / "big.jsonl",
                [rec("user", f"m{i}", "big", f"2026-10-03T10:{i // 60:02d}:{i % 60:02d}Z", f"{i} {big}")
                 for i in range(150)])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()
    p1 = session(conn, "big", cfg.timezone)
    assert 'session("big#2")' in p1
    p2 = session(conn, "big#2", cfg.timezone)
    assert "page 2 of 3" in p2
    allp = p1 + p2 + session(conn, "big#3", cfg.timezone)
    assert all(f"\n{i} x" in allp for i in range(150))
