import json
import os
import subprocess
import sys

import pytest

from conftest import rec, write_jsonl
from agent_memory import proposals as P
from agent_memory.consolidate import Consolidator
from agent_memory.ingest import Ingestor
from agent_memory.secrets_filter import SecretFilter

A = "bbbbbbbb-0000-0000-0000-000000000001"
FAKE_GH = "gh" + "p_" + "a1B2c3D4e5" * 4


class JsonLLM:
    def __init__(self, items):
        self.items = items
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return "Here it is:\n" + json.dumps({"proposals": self.items}, ensure_ascii=False)


def _load_day(tmp_path, cfg, conn, embedder, day="2026-10-02"):
    write_jsonl(tmp_path / "projects" / "-home-alice-assistant" / f"{A}.jsonl", [
        rec("user", "b1", A, f"{day}T10:00:00Z", "No me resumas al final de cada respuesta, ya leo el diff"),
        rec("assistant", "b2", A, f"{day}T10:01:00Z", [{"type": "text", "text": "Entendido."}]),
        rec("user", "b3", A, f"{day}T11:00:00Z", "Prepárame otra vez el informe semanal de Jira"),
    ])
    Ingestor(conn, cfg, SecretFilter(), embedder).run_all()


def _toml(tmp_path, cfg):
    toml = tmp_path / "c.toml"
    toml.write_text(f'db_path = "{cfg.db_path}"\nindex_path = "{cfg.index_path}"\n'
                    f'literal_secrets_file = "{cfg.literal_secrets_file}"\n')
    return toml


def _cli(toml, *args, stdin=None):
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run([sys.executable, "-m", "agent_memory.cli", "--config", str(toml), *args],
                       input=stdin, capture_output=True, text=True, cwd=root)
    return r.returncode, r.stdout


def test_add_dedup_secret_and_transitions(conn):
    P.ensure_schema(conn)
    sf = SecretFilter()
    a = P.add(conn, sf, kind="preference", title="No resumir al final de cada respuesta", section="corrections")
    assert a
    # same idea with other punctuation/accents: repeated
    assert P.add(conn, sf, kind="preference", title="no resumir, al final de cada respuesta!") is None
    # with a secret: dropped entirely
    assert P.add(conn, sf, kind="preference", title=f"usa el token {FAKE_GH}") is None
    # skill with an invalid name
    assert P.add(conn, sf, kind="skill", title="x", skill_name="Bad Name", body="1. a") is None
    # unknown kinds are not accepted
    assert P.add(conn, sf, kind="preferencia", title="Otra cosa") is None
    s = P.add(conn, sf, kind="skill", title="Report", skill_name="weekly-report", skill_description="Report",
              body="1. Do it")
    assert s and P.get(conn, s)["title"].startswith("weekly-report:")
    assert [p["id"] for p in P.pending(conn)] == [a, s]
    P.mark(conn, a, "sent")
    P.mark(conn, a, "rejected")
    with pytest.raises(ValueError):
        P.mark(conn, a, "applied")
    # rejected: never re-proposed
    assert P.add(conn, sf, kind="preference", title="No resumir al final de cada respuesta") is None
    P.mark(conn, s, "failed", "push failed")
    assert P.mark(conn, s, "applied")["status"] == "applied"


def test_section_keys_and_labels(conn):
    P.ensure_schema(conn)
    sf = SecretFilter()
    a = P.add(conn, sf, kind="preference", title="Uno", section="Formato y estilo")
    b = P.add(conn, sf, kind="preference", title="Dos", section="approvals")
    c = P.add(conn, sf, kind="preference", title="Tres", section="Invented")
    assert [P.get(conn, i)["section"] for i in (a, b, c)] == ["style", "approvals", "corrections"]


def test_sweep_tagged_notes(cfg, conn, embedder):
    ing = Ingestor(conn, cfg, SecretFilter(), embedder)
    ing.add_note("Formato y estilo: 2026-10-03 · chat · Tablas solo si hay más de 3 columnas",
                 "preference-proposal")
    ing.add_note("---\nname: morning-briefing\ndescription: Briefing de la mañana\n---\n1. Calendario\n2. Tareas",
                 "skill-proposal")
    ing.add_note("--- a/PERSONALITY.md\n+++ b/PERSONALITY.md\n@@ -1 +1 @@\n-x\n+y\n", "personality:proposal")
    ing.add_note(f"clave {FAKE_GH}", "preference-proposal")
    ing.add_note("Approvals: respuestas cortas", "pref-proposal-x")  # unknown tag: ignored
    ing.add_note("nota normal", "demo-app")
    n = P.sweep_notes(conn, SecretFilter())
    assert n == 3
    ps = {p["kind"]: p for p in P.pending(conn)}
    assert ps["preference"]["section"] == "style" and ps["preference"]["origin"] == "chat"
    assert ps["preference"]["title"] == "Tablas solo si hay más de 3 columnas"
    assert ps["skill"]["skill_name"] == "morning-briefing" and "Calendario" in ps["skill"]["body"]
    assert P.is_unified_diff(ps["personality"]["body"])
    assert ps["personality"]["title"] == "PERSONALITY.md change (diff)"
    # idempotent
    assert P.sweep_notes(conn, SecretFilter()) == 0
    assert all(FAKE_GH not in json.dumps(p) for p in P.pending(conn))
    seen = conn.execute("SELECT source_ref, fingerprint, title FROM proposals WHERE status='rejected'").fetchall()
    assert len(seen) == 1 and seen[0]["fingerprint"].startswith("note-seen:") and seen[0]["title"] == P.DISCARDED_TITLE


def test_note_to_proposal_english_section_prefix():
    p = P.note_to_proposal("Corrections: 2026-10-03 · session · Always answer in Spanish", "preference", "2026-10-03")
    assert p["section"] == "corrections" and p["origin"] == "session" and p["title"] == "Always answer in Spanish"


def test_extract_from_day_marks_and_limits(tmp_path, cfg, conn, embedder):
    _load_day(tmp_path, cfg, conn, embedder)
    dot = tmp_path / "dot"
    (dot / "skills" / "close-session").mkdir(parents=True)
    (dot / "skills" / "close-session" / "SKILL.md").write_text("---\nname: close-session\n---\n")
    (dot / "PREFERENCES.md").write_text("## Correcciones\n- 2026-10-01 · manual · Español siempre\n")
    llm = JsonLLM([
        {"type": "preference", "section": "corrections", "text": "No resumir al final", "evidence": "lo dijo"},
        {"type": "preference", "section": "Invented", "text": "Segunda", "evidence": ""},
        {"type": "preference", "text": "Tercera"}, {"type": "preference", "text": "Cuarta (sobra)"},
        {"type": "skill", "name": "jira-report", "description": "Informe semanal", "body": "1. a"},
        {"type": "skill", "name": "other", "description": "sobra", "body": "1. b"},
        {"type": "personality", "text": f"Usa {FAKE_GH}"},
        {"type": "weird", "text": "x"},
    ])
    r = P.extract(conn, cfg, llm, SecretFilter(), days_back=2, profile_dir=str(dot), today="2026-10-03")
    assert r["calls"] == 1  # 2026-10-01 has no messages: the model is not called
    kinds = [p["kind"] for p in P.pending(conn)]
    assert kinds.count("preference") == 3 and kinds.count("skill") == 1 and "personality" not in kinds
    assert {p["section"] for p in P.pending(conn) if p["kind"] == "preference"} == {"corrections"}
    prompt = llm.prompts[0]
    assert "close-session" in prompt and "Español siempre" in prompt and "<<<DATA" in prompt
    assert "informe semanal de Jira" in prompt and '"proposals"' in prompt
    # processed day: not called again
    assert P.extract(conn, cfg, llm, SecretFilter(), days_back=2, profile_dir=str(dot), today="2026-10-03")["calls"] == 0


def test_system_prompt_language():
    assert "in Spanish" in P.system_prompt("es") and "in English" in P.system_prompt("en")


def test_extract_llm_failure_retries_next_time(tmp_path, cfg, conn, embedder):
    _load_day(tmp_path, cfg, conn, embedder)

    def bad(prompt):
        return "not json"
    r = P.extract(conn, cfg, bad, SecretFilter(), days_back=1, profile_dir=str(tmp_path), today="2026-10-03")
    assert r["errors"] == 1
    r = P.extract(conn, cfg, JsonLLM([]), SecretFilter(), days_back=1, profile_dir=str(tmp_path), today="2026-10-03")
    assert r["calls"] == 1 and r["proposals"] == 0


def test_consolidator_runs_proposals_only_with_llm(tmp_path, cfg, conn, embedder, monkeypatch):
    _load_day(tmp_path, cfg, conn, embedder)
    cfg.profile_dir = str(tmp_path)
    # extract() looks back from the real current date: reach the fixture day whatever today is.
    from datetime import date
    cfg.proposals_days_back = (date.today() - date(2026, 10, 2)).days + 2

    def summ(prompt):
        return "Headline\n\n- x"
    out = Consolidator(conn, cfg, summ).run()
    assert "proposals_new" not in out
    out = Consolidator(conn, cfg, summ, proposals_llm=JsonLLM([{"type": "preference", "text": "Respuestas cortas"}])
                       ).run()
    assert out["proposals_new"] == 1


def test_cli_proposals_and_day(tmp_path, cfg, conn):
    P.ensure_schema(conn)
    pid = P.add(conn, SecretFilter(), kind="preference", title="Respuestas cortas")
    toml = _toml(tmp_path, cfg)
    rc, out = _cli(toml, "proposals", "pending")
    assert rc == 0 and json.loads(out)["proposals"][0]["id"] == pid
    rc, out = _cli(toml, "proposals", "show", str(pid))
    assert json.loads(out)["proposal"]["kind"] == "preference"
    rc, out = _cli(toml, "proposals", "mark", str(pid), "sent")
    assert json.loads(out)["proposal"]["status"] == "sent"
    rc, out = _cli(toml, "proposals", "mark", str(pid), "applied", "--result", "ok")
    assert json.loads(out)["proposal"]["result"] == "ok"
    rc, out = _cli(toml, "proposals", "mark", str(pid), "rejected")
    assert rc == 2 and "already" in json.loads(out)["error"]
    rc, out = _cli(toml, "proposals", "sweep")
    assert rc == 0 and json.loads(out) == {"new": 0}
    # the `day` CLI view uses the configured language (default en) unless --lang is given
    rc, out = _cli(toml, "day", "2026-10-02")
    assert rc == 0 and out.startswith("# Day 2026-10-02")
    rc, out = _cli(toml, "day", "2026-10-02", "--lang", "es")
    assert rc == 0 and out.startswith("# Día 2026-10-02")


def test_cli_proposals_secrets(tmp_path, cfg):
    toml = _toml(tmp_path, cfg)
    token = "sk-ant-" + "a1B2c3D4" * 4
    rc, out = _cli(toml, "proposals", "secrets", stdin=f"1. Exporta ANTHROPIC_API_KEY={token}\n2. Llama a la API")
    assert rc == 0 and json.loads(out)["redactions"] >= 1
    assert token not in out
    rc, out = _cli(toml, "proposals", "secrets", stdin="1. Abre Jira\n2. Resume")
    assert rc == 0 and json.loads(out)["redactions"] == 0
    assert P.count_secrets(SecretFilter(), "ya tapado [REDACTED:github]") == 1
