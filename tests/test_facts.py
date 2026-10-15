"""Facts layer: supersede, secret filtering, approval gating, vault source, the MCP tool and the brief."""
import asyncio
import json
import os
import sqlite3

import pytest

from conftest import rec, write_jsonl
from agent_memory import facts as F
from agent_memory import proposals as P
from agent_memory.ingest import Ingestor
from agent_memory.secrets_filter import SecretFilter
from agent_memory.server import Memory, build_server

SF = SecretFilter()
A = "aaaaaaaa-0000-0000-0000-00000000000a"


@pytest.fixture
def fconn(conn):
    P.ensure_schema(conn)
    F.ensure_schema(conn)
    return conn


def cand(value, ts="2026-10-01T10:00:00.000Z", category="project", attribute="status", subject="Orchard",
         **kw):
    return {"subject": subject, "attribute": attribute, "value": value, "category": category, "valid_from": ts,
            "source_session": "s-1", "evidence": f"dice {value}", **kw}


def rows(conn, key=None):
    q = "SELECT * FROM facts" + (" WHERE key=?" if key else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(q, (key,) if key else ())]


# ------------------------------------------------------------------ supersede
def test_new_value_supersedes_old_one(fconn):
    a = F.store_candidate(fconn, SF, cand("en pausa", "2026-10-01T10:00:00.000Z"))
    b = F.store_candidate(fconn, SF, cand("activo", "2026-10-03T09:00:00.000Z"))
    old, new = F.get(fconn, a), F.get(fconn, b)
    assert old["status"] == "superseded" and old["valid_to"] == "2026-10-03T09:00:00.000Z"
    assert new["status"] == "current" and new["valid_to"] is None
    assert len(rows(fconn)) == 2  # nothing deleted


def test_same_value_is_deduplicated_and_key_is_normalized(fconn):
    assert F.store_candidate(fconn, SF, cand("activo"))
    assert F.store_candidate(fconn, SF, cand(" Activo ", attribute="Status", subject="orchard")) is None
    assert len(rows(fconn)) == 1


def test_older_value_becomes_history_not_current(fconn):
    b = F.store_candidate(fconn, SF, cand("activo", "2026-10-03T09:00:00.000Z"))
    a = F.store_candidate(fconn, SF, cand("en pausa", "2026-09-01T09:00:00.000Z"))  # backfilled later
    assert F.get(fconn, b)["status"] == "current"
    old = F.get(fconn, a)
    assert old["status"] == "superseded" and old["valid_to"] == "2026-10-03T09:00:00.000Z"


def test_only_one_current_per_key(fconn):
    F.store_candidate(fconn, SF, cand("a", "2026-10-01T00:00:00.000Z"))
    F.store_candidate(fconn, SF, cand("b", "2026-10-02T00:00:00.000Z"))
    F.store_candidate(fconn, SF, cand("c", "2026-10-03T00:00:00.000Z"))
    assert fconn.execute("SELECT COUNT(*) FROM facts WHERE status='current'").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        fconn.execute("UPDATE facts SET status='current'")


# ------------------------------------------------------------------ secrets
@pytest.mark.parametrize("c", [
    cand("hunter2-x", attribute="wifi_password"),
    cand("1234", attribute="pin del banco"),
    cand("ghp_" + "a1B2c3D4e5" * 4, attribute="github"),
    cand("4111 1111 1111 1111", attribute="visa"),
    cand("abcDEF123456ghiJKL7890xyz", attribute="api"),
    cand("ok", attribute="note", evidence="la contraseña es [REDACTED]"),
    cand("482913", attribute="codigo_sms"),
])
def test_secret_like_candidates_are_dropped(fconn, c):
    assert F.store_candidate(fconn, SF, c) is None
    assert rows(fconn) == []


@pytest.mark.parametrize("c", [
    cand("Claude Code + chat bot", attribute="main_tool"),
    cand("SSH solo con clave, nunca contraseña por defecto", attribute="ssh_policy", category="decision"),
    cand("≤ 4k tokens", attribute="injection_token_budget"),
    cand("punto clave del proyecto", attribute="focus"),
])
def test_ordinary_project_words_are_not_secrets(fconn, c):
    # "contraseña" inside a policy value is a credential mention: dropped; the rest are legitimate facts
    stored = F.store_candidate(fconn, SF, c)
    assert (stored is None) == ("contraseña" in c["value"])


def test_invalid_candidates_are_dropped(fconn):
    assert F.store_candidate(fconn, SF, cand("x", category="secrets")) is None
    assert F.store_candidate(fconn, SF, cand("")) is None
    assert F.store_candidate(fconn, SF, cand("x", source_kind="rumor")) is None


# ------------------------------------------------------------------ approval gating
def test_low_risk_is_current_and_sensitive_is_pending(fconn):
    p = F.store_candidate(fconn, SF, cand("respuestas cortas", category="preference", attribute="reply_style",
                                          subject="user"))
    h = F.store_candidate(fconn, SF, cand("73 kg", category="health", attribute="weight", subject="user"))
    assert F.get(fconn, p)["status"] == "current"
    assert F.get(fconn, h)["status"] == "pending_approval"
    assert "73 kg" not in F.format_facts(fconn, "UTC")  # pending facts are never shown as true
    for cat in ("work", "decision", "personal"):
        assert F.get(fconn, F.store_candidate(fconn, SF, cand(cat, category=cat, attribute=cat)))["status"] == \
            "pending_approval"


def test_digest_approval_flow(fconn):
    old = F.store_candidate(fconn, SF, cand("75 kg", "2026-09-01T08:00:00.000Z", "health", "weight", "user",
                                            source_kind="vault"))
    assert F.get(fconn, old)["status"] == "current"  # vault: never gated
    new = F.store_candidate(fconn, SF, cand("73 kg", "2026-10-04T08:00:00.000Z", "health", "weight", "user"))
    assert F.link_pending(fconn, SF) == 1
    assert F.link_pending(fconn, SF) == 0  # idempotent
    prop = [p for p in P.pending(fconn) if p["kind"] == "fact"][0]
    assert prop["source_ref"] == f"fact:{new}" and "73 kg" in prop["title"] and prop["section"] == "health"
    P.mark(fconn, prop["id"], "sent")
    res = P.mark(fconn, prop["id"], "applied", "approved by the user")
    assert F.get(fconn, new)["status"] == "current"
    assert F.get(fconn, old)["status"] == "superseded" and F.get(fconn, old)["valid_to"] == "2026-10-04T08:00:00.000Z"
    assert "is now current" in res["result"]


def test_digest_rejection(fconn):
    fid = F.store_candidate(fconn, SF, cand("Acme Corp", category="work", attribute="employer", subject="user"))
    F.link_pending(fconn, SF)
    pid = P.pending(fconn)[0]["id"]
    P.mark(fconn, pid, "rejected")
    assert F.get(fconn, fid)["status"] == "rejected"
    # a rejected value is not proposed again (cooldown)
    assert F.store_candidate(fconn, SF, cand("Acme Corp", category="work", attribute="employer",
                                             subject="user")) is None


def test_newer_pending_value_replaces_older_pending(fconn):
    a = F.store_candidate(fconn, SF, cand("74 kg", "2026-10-02T08:00:00.000Z", "health", "weight", "user"))
    F.link_pending(fconn, SF)
    b = F.store_candidate(fconn, SF, cand("73 kg", "2026-10-04T08:00:00.000Z", "health", "weight", "user"))
    F.link_pending(fconn, SF)
    assert F.get(fconn, a)["status"] == "rejected" and F.get(fconn, b)["status"] == "pending_approval"
    pend = [p for p in P.pending(fconn) if p["kind"] == "fact"]
    assert [p["source_ref"] for p in pend] == [f"fact:{b}"]


def test_conversation_cannot_silently_override_vault(fconn):
    v = F.store_candidate(fconn, SF, cand("activo", category="project", source_kind="vault"))
    c = F.store_candidate(fconn, SF, cand("cerrado", "2026-10-05T00:00:00.000Z", category="project"))
    assert F.get(fconn, v)["status"] == "current"
    assert F.get(fconn, c)["status"] == "pending_approval"  # low-risk category, but the vault wins until ✅


def test_vault_overrides_conversation(fconn):
    c = F.store_candidate(fconn, SF, cand("Acme Corp", "2026-09-01T00:00:00.000Z", "work", "employer", "user"))
    F.decide(fconn, c, True)
    v = F.store_candidate(fconn, SF, cand("Globex", "2026-09-10T00:00:00.000Z", "work", "employer", "user",
                                          source_kind="vault"))
    assert F.get(fconn, v)["status"] == "current" and F.get(fconn, c)["status"] == "superseded"


# ------------------------------------------------------------------ extraction
class JsonLLM:
    def __init__(self, items):
        self.items, self.prompts = items, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        items = self.items(prompt) if callable(self.items) else self.items
        return json.dumps({"facts": items})


def _day(tmp_path, cfg, conn, embedder):
    write_jsonl(tmp_path / "projects" / "-home-alice-assistant" / f"{A}.jsonl", [
        rec("user", "f1", A, "2026-10-03T10:00:00Z", "Today I weighed 73 kg"),
        rec("user", "f2", A, "2026-10-03T11:00:00Z", "creo que quizá cambie de móvil"),
    ])
    ing = Ingestor(conn, cfg, SF, embedder)
    ing.run_all()
    return ing


def test_extract_sources_confidence_and_verifiability(tmp_path, cfg, conn, embedder, monkeypatch):
    P.ensure_schema(conn)
    ing = _day(tmp_path, cfg, conn, embedder)
    import agent_memory.ingest as ingest_mod
    from datetime import datetime, timezone

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ingest_mod, "datetime", FakeDT)
    note = ing.add_note("Orchard: el proyecto pasa a modo mantenimiento", "demo-app")
    ing.add_note("prefiero respuestas cortas", "preference-proposal")  # proposals are not a facts source
    llm = JsonLLM([
        {"subject": "user", "attribute": "weight", "value": "73 kg", "category": "health", "source": A,
         "confidence": "high", "evidence": "Today I weighed 73 kg"},
        {"subject": "user", "attribute": "phone", "value": "nuevo", "category": "personal", "source": A,
         "confidence": "low", "evidence": "quizá"},                       # conversation needs high confidence
        {"subject": "Orchard", "attribute": "status", "value": "mantenimiento", "category": "project",
         "source": f"note:{note['id']}", "confidence": "medium", "evidence": "modo mantenimiento"},
        {"subject": "user", "attribute": "city", "value": "Madrid", "category": "personal", "source": "made-up",
         "confidence": "high"},                                           # unknown source: dropped
    ])
    r = F.extract(conn, cfg, llm, SF, days_back=1, today="2026-10-04")
    assert r == {"calls": 1, "facts": 2, "errors": 0}
    prompt = llm.prompts[0]
    assert f"SOURCE note:{note['id']} (NOTE)" in prompt and f"SOURCE {A} (CONVERSATION)" in prompt
    assert "respuestas cortas" not in prompt and "<<<DATA" in prompt
    fs = {f["attribute"]: f for f in rows(conn)}
    assert set(fs) == {"weight", "status"}
    assert fs["weight"]["status"] == "pending_approval" and fs["weight"]["source_session"] == A
    assert fs["weight"]["valid_from"].endswith("Z")  # stored in UTC
    assert fs["status"]["status"] == "current" and fs["status"]["source_note"] == note["id"]
    assert fs["status"]["source_kind"] == "note" and fs["status"]["source_session"] == note["session"]
    # processed day: no second call
    assert F.extract(conn, cfg, llm, SF, days_back=1, today="2026-10-04")["calls"] == 0


def test_extract_failure_is_retried(tmp_path, cfg, conn, embedder):
    _day(tmp_path, cfg, conn, embedder)
    r = F.extract(conn, cfg, lambda p: "not json", SF, days_back=1, today="2026-10-04")
    assert r["errors"] == 1
    assert F.extract(conn, cfg, JsonLLM([]), SF, days_back=1, today="2026-10-04")["calls"] == 1


def test_consolidator_runs_facts_step(tmp_path, cfg, conn, embedder, monkeypatch):
    from agent_memory.consolidate import Consolidator
    from datetime import date
    _day(tmp_path, cfg, conn, embedder)
    cfg.facts_days_back = (date.today() - date(2026, 10, 3)).days + 2
    out = Consolidator(conn, cfg, lambda p: "Headline\n\n- x").run()
    assert "facts_new" not in out
    llm = JsonLLM([{"subject": "user", "attribute": "weight", "value": "73 kg", "category": "health", "source": A,
                    "confidence": "high", "evidence": "73 kg"}])
    out = Consolidator(conn, cfg, lambda p: "Headline\n\n- x", facts_llm=llm).run()
    assert out["facts_new"] == 1 and out["facts_to_approve"] == 1


# ------------------------------------------------------------------ vault
def _vault(tmp_path):
    v = tmp_path / "vault"
    (v / "00-Inbox").mkdir(parents=True)
    (v / "work").mkdir()
    (v / ".trash").mkdir()
    (v / ".obsidian").mkdir()
    (v / "00-Inbox" / "Profile.md").write_text("# Profile\n- Job: Globex (desde 2026-09-10)\n")
    (v / "00-Inbox" / "Log-Claude.md").write_text("- 2026-10-01 log de un agente\n")
    (v / "work" / "Company.md").write_text("---\nactualizado: 2026-09-10\nstatus: activo\n---\n# Company\n")
    (v / ".trash" / "Old.md").write_text("borrado")
    (v / ".obsidian" / "x.md").write_text("config")
    (v / "token.md").write_text("api: ghp_" + "a1B2c3D4e5" * 4 + "\n")
    (tmp_path / "outside.md").write_text("fuera del vault")
    os.symlink(tmp_path / "outside.md", v / "link.md")
    return v


def _snapshot(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return out


def test_vault_ingest_is_read_only_versioned_and_filtered(tmp_path, cfg, conn, embedder):
    v = _vault(tmp_path)
    before = _snapshot(v)
    cfg.vault_path = str(v)
    Ingestor(conn, cfg, SF, embedder).run_all()
    assert _snapshot(v) == before  # nothing written, moved or chmod-ed in the vault
    got = {r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM raw_messages WHERE source='vault'")}
    assert set(got) == {"vault:00-Inbox/Profile.md", "vault:00-Inbox/Log-Claude.md", "vault:work/Company.md",
                        "vault:token.md"}  # no .trash, .obsidian or symlink escaping the vault
    assert got["vault:work/Company.md"]["text"].startswith("---\nactualizado: 2026-09-10")  # frontmatter kept
    assert "ghp_" not in got["vault:token.md"]["text"]  # secrets filter applies
    assert got["vault:work/Company.md"]["project"] == "work"
    assert conn.execute("SELECT title FROM sessions WHERE id='vault:00-Inbox/Profile.md'").fetchone()[0] == \
        "00-Inbox/Profile.md"
    # unchanged: nothing new; touched with the same content: nothing new; edited: a new version
    Ingestor(conn, cfg, SF, embedder).run_all()
    os.utime(v / "00-Inbox" / "Profile.md", (1, 1_900_000_000))
    Ingestor(conn, cfg, SF, embedder).run_all()
    assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE source='vault'").fetchone()[0] == 4
    (v / "00-Inbox" / "Profile.md").write_text("# Profile\n- Job: Initech\n")
    Ingestor(conn, cfg, SF, embedder).run_all()
    assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE session_id='vault:00-Inbox/Profile.md'"
                        ).fetchone()[0] == 2


def test_vault_disabled_by_default(tmp_path, cfg, conn, embedder):
    _vault(tmp_path)
    Ingestor(conn, cfg, SF, embedder).run_all()
    assert conn.execute("SELECT COUNT(*) FROM raw_messages WHERE source='vault'").fetchone()[0] == 0


def test_vault_facts_are_canonical(tmp_path, cfg, conn, embedder):
    P.ensure_schema(conn)
    F.ensure_schema(conn)
    old = F.store_candidate(conn, SF, cand("Acme Corp", "2026-01-10T00:00:00.000Z", "work", "employer", "user"))
    F.decide(conn, old, True)
    cfg.vault_path = str(_vault(tmp_path))
    cfg.vault_noncanonical = ["00-Inbox/Log-*"]
    Ingestor(conn, cfg, SF, embedder).run_all()

    def answer(prompt):
        assert "VAULT NOTES" in prompt and "Log-Claude.md (NOTE)" in prompt
        assert "SOURCE vault:00-Inbox/Profile.md (VAULT)" in prompt
        return [{"subject": "user", "attribute": "employer", "value": "Globex", "category": "work",
                 "source": "vault:00-Inbox/Profile.md", "confidence": "medium", "evidence": "Globex"},
                {"subject": "user", "attribute": "agent_log", "value": "x", "category": "work",
                 "source": "vault:00-Inbox/Log-Claude.md", "confidence": "high", "evidence": "log"}]
    llm = JsonLLM(answer)
    r = F.extract_vault(conn, cfg, llm, SF)
    assert r == {"calls": 1, "facts": 2, "errors": 0}
    emp = [f for f in rows(conn) if f["attribute"] == "employer"]
    assert [(f["value"], f["status"]) for f in emp] == [("Acme Corp", "superseded"), ("Globex", "current")]
    assert emp[1]["source_session"] == "vault:00-Inbox/Profile.md" and emp[1]["source_kind"] == "vault"
    log = [f for f in rows(conn) if f["attribute"] == "agent_log"][0]
    assert log["source_kind"] == "note" and log["status"] == "pending_approval"  # non-canonical vault note
    assert F.extract_vault(conn, cfg, llm, SF)["calls"] == 0  # each version is analyzed once
    # the source can be verified with session(id)
    from agent_memory.views import session
    assert "Globex" in session(conn, "vault:00-Inbox/Profile.md", cfg.timezone)


# ------------------------------------------------------------------ MCP tool
def test_facts_tool(cfg, conn, embedder):
    P.ensure_schema(conn)
    F.ensure_schema(conn)
    F.store_candidate(conn, SF, cand("en pausa", "2026-09-01T10:00:00.000Z"))
    F.store_candidate(conn, SF, cand("activo", "2026-10-05T23:25:00.000Z"))
    F.store_candidate(conn, SF, cand("respuestas cortas", category="preference", attribute="reply_style",
                                     subject="user"))
    F.store_candidate(conn, SF, cand("73 kg", category="health", attribute="weight", subject="user"))
    srv = build_server(Memory(cfg, embedder=embedder, conn=conn), "127.0.0.1", 18765)

    def call(args):
        res = asyncio.run(srv.call_tool("facts", args))
        content = res[0] if isinstance(res, tuple) else res
        return "\n".join(c.text for c in content)

    out = call({})
    assert "2 current facts" in out and "Orchard · status: activo" in out and "src session `s-1`" in out
    assert "since 2026-10-06" in out  # 23:25 UTC on 10-05 is 10-06 in Madrid: stored UTC, shown local
    assert "en pausa" not in out and "73 kg" not in out and "1 facts pending" in out
    hist = call({"include_history": True, "query": "orchard"})
    assert "was: en pausa (2026-09-01 → 2026-10-06)" in hist and "reply_style" not in hist
    assert "reply_style" in call({"category": "preference"}) and "Orchard" not in call({"category": "preference"})
    assert "Unknown category" in call({"category": "secrets"})
    assert call({"query": "nada que ver"}).startswith("No facts match.")


# ------------------------------------------------------------------ search date label (bug fix)
def test_search_label_uses_local_date_and_time(tmp_path, cfg, conn, embedder):
    write_jsonl(tmp_path / "projects" / "p" / "s.jsonl",
                [rec("user", "z1", "s", "2026-10-05T23:25:00Z", "late night reminder")])
    Ingestor(conn, cfg, SF, embedder).run_all()
    out = Memory(cfg, embedder=embedder, conn=conn).search("reminder")
    assert "2026-10-06 01:25 (Europe/Madrid)" in out
    assert "23:25 UTC" not in out


# ------------------------------------------------------------------ SessionStart brief
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def _brief_fixture(conn):
    P.ensure_schema(conn)
    F.ensure_schema(conn)
    F.store_candidate(conn, SF, cand("Globex", "2026-09-10T00:00:00.000Z", "work", "employer", "user",
                                     source_kind="vault", core=True))
    F.store_candidate(conn, SF, cand("70 kg", "2026-01-20T00:00:00.000Z", "health", "weight_goal", "user",
                                     source_kind="vault", core=True))
    F.store_candidate(conn, SF, cand("75 kg", "2026-09-01T08:00:00.000Z", "health", "weight", "user",
                                     source_kind="vault"))
    F.store_candidate(conn, SF, cand("73 kg", "2026-10-04T08:00:00.000Z", "health", "weight", "user",
                                     source_kind="vault"))
    F.store_candidate(conn, SF, cand("remoto", "2026-09-01T08:00:00.000Z", "work", "office", "user",
                                     source_kind="vault"))
    F.store_candidate(conn, SF, cand("Main Street office", "2026-10-04T08:00:00.000Z", "work", "office", "user",
                                     source_kind="vault"))
    F.store_candidate(conn, SF, cand("en producción", "2026-08-01T00:00:00.000Z", "project", "status", "Orchard",
                                     project="Orchard"))
    F.store_candidate(conn, SF, cand("Next.js", "2026-06-01T00:00:00.000Z", "project", "stack", "Orchard",
                                     project="Orchard"))
    F.store_candidate(conn, SF, cand("Jira", "2026-06-01T00:00:00.000Z", "project", "tracker", "Otro",
                                     project="Otro"))


def test_brief_sections(cfg, conn):
    _brief_fixture(conn)
    conn.execute("UPDATE facts SET last_used=? WHERE attribute='status'", ("2026-10-01T00:00:00.000Z",))
    out = F.brief(conn, cfg, "orchard", now=NOW)
    core, rest = out.split("### Recent")
    assert "### Core" in core and "employer: Globex" in core
    recent, ctx = rest.split("### Project orchard")
    assert "office: Main Street office (was remoto)" in recent and "employer" not in recent  # no duplicates across sections
    assert "status: en producción" in ctx          # used 5 days ago: kept
    assert "stack" not in ctx                      # untouched for months: decayed out of the injection
    assert "Jira" not in out                   # another project's fact: only in its own context
    assert "src" not in out
    assert "Jira" in F.brief(conn, cfg, "Otro", now=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert "### Project" not in F.brief(conn, cfg, "home", now=NOW)  # a home folder is not a project


def test_brief_excludes_health_but_tool_keeps_it(cfg, conn):
    _brief_fixture(conn)
    out = F.brief(conn, cfg, "orchard", now=NOW)
    assert "[health]" not in out and "70 kg" not in out and "73 kg" not in out   # core and recent: not injected
    assert "employer: Globex" in out
    tool = F.format_facts(conn, cfg.timezone, category="health")                 # still stored, on demand
    assert "weight_goal: 70 kg" in tool and "weight: 73 kg" in tool


def test_brief_does_not_count_as_use_but_the_tool_does(cfg, conn):
    _brief_fixture(conn)
    F.brief(conn, cfg, "orchard", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE last_used IS NOT NULL").fetchone()[0] == 0
    F.format_facts(conn, cfg.timezone, "orchard")
    used = {r[0] for r in conn.execute("SELECT attribute FROM facts WHERE last_used IS NOT NULL")}
    assert used == {"status", "stack"}
    assert "stack: Next.js" in F.brief(conn, cfg, "orchard")  # used again: back in its context


def test_core_needs_vault_or_approval(fconn):
    conv = F.store_candidate(fconn, SF, cand("fase 5", attribute="phase", core=True))          # project, chat
    vault = F.store_candidate(fconn, SF, cand("70 kg", category="health", attribute="goal", subject="user",
                                              core=True, source_kind="vault"))
    gated = F.store_candidate(fconn, SF, cand("Globex", category="work", attribute="employer", subject="user",
                                              core=True))
    assert (F.get(fconn, conv)["core"], F.get(fconn, vault)["core"], F.get(fconn, gated)["core"]) == (0, 1, 1)
    assert F.get(fconn, gated)["status"] == "pending_approval"   # core only shows once the user approves it


def test_reextraction_confirms(cfg, conn):
    _brief_fixture(conn)
    assert F.store_candidate(conn, SF, cand("Next.js", "2026-10-05T00:00:00.000Z", "project", "stack",
                                            "Orchard")) is None
    assert conn.execute("SELECT last_confirmed FROM facts WHERE attribute='stack'").fetchone()[0]


def test_brief_section_budgets_and_total(cfg, conn):
    P.ensure_schema(conn)
    F.ensure_schema(conn)
    for i in range(60):
        F.store_candidate(conn, SF, cand("valor largo " * 4 + str(i), f"2026-10-0{1 + i % 5}T00:00:00.000Z",
                                         "preference",
                                         f"attr_{i}", "user", core=i % 2 == 0, project="Orchard",
                                         source_kind="vault"))
    out = F.brief(conn, cfg, "orchard", now=NOW)
    parts = {p.split("\n", 1)[0]: p.split("\n", 1)[1] for p in ("\n" + out).split("\n### ")[1:]}
    assert len(parts["Core"]) <= cfg.facts_core_chars
    assert len(parts["Recent (last 7 days)"]) <= cfg.facts_recent_chars
    assert len(parts["Project orchard"]) <= cfg.facts_context_chars
    total = cfg.facts_core_chars + cfg.facts_recent_chars + cfg.facts_context_chars + 100  # + headings
    assert len(out) <= total
    cfg.facts_core_chars, cfg.facts_recent_chars, cfg.facts_context_chars = 100, 0, 0  # configurable
    small = F.brief(conn, cfg, "orchard", now=NOW)
    assert small.startswith("### Core") and "### Recent" not in small and len(small) <= 100 + 10


def test_brief_endpoint(tmp_path, cfg, conn):
    from test_upload import TOKEN, _inner, call
    from agent_memory.server import BearerAuth
    from agent_memory.upload import InboxRoutes
    _brief_fixture(conn)
    mem = Memory(cfg, embedder=None, conn=conn)
    app = BearerAuth(InboxRoutes(_inner, str(tmp_path), str(tmp_path / "INDEX.md"), 1000,
                                 facts_brief=mem.facts_brief), TOKEN)
    st, body = call(app, "GET", "/facts/brief")
    assert st == 200 and "### Core" in body.decode()
    assert call(app, "POST", "/facts/brief")[0] == 405
