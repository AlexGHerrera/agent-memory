import os

import pytest

from agent_memory import config

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_example_config_loads_with_defaults():
    cfg = config.load(os.path.join(ROOT, "config.example.toml"))
    default = config.Config().resolve()
    assert cfg.host == "127.0.0.1" and cfg.port == 8765 and cfg.language == "en"
    assert cfg.db_path == default.db_path and cfg.db_path.endswith(os.path.join("data", "memory.db"))
    assert cfg.mcp_token_file.endswith(os.path.join("config", "mcp-token"))
    assert cfg.claude_sources == [] and cfg.openclaw_sources == []


def test_sources_paths_and_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('base_dir = "~/am"\nlanguage = "es"\n[[claude_sources]]\npath = "~/.claude/projects"\n'
                 '[[openclaw_sources]]\npath = "~/.openclaw/agents"\nagents = ["main"]\n')
    cfg = config.load(str(p))
    home = os.path.expanduser("~")
    assert cfg.base_dir == os.path.join(home, "am") and cfg.index_path == os.path.join(home, "am", "INDEX.md")
    assert cfg.claude_sources[0].path == os.path.join(home, ".claude", "projects")
    assert cfg.claude_sources[0].machine == "local"
    assert cfg.openclaw_sources[0].agents == ["main"] and cfg.openclaw_sources[0].primary_agents == ["main"]
    p.write_text('nope = 1\n')
    with pytest.raises(ValueError):
        config.load(str(p))
    p.write_text('language = "fr"\n')
    with pytest.raises(ValueError):
        config.load(str(p))


def test_missing_file_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_CONFIG", str(tmp_path / "missing.toml"))
    cfg = config.load()
    assert cfg.host == "127.0.0.1" and cfg.base_dir == os.path.expanduser(config.DEFAULT_BASE)


def test_init_token_and_doctor(tmp_path):
    from agent_memory.cli import _doctor, _init_token
    tok = tmp_path / "config" / "mcp-token"
    r = _init_token(str(tok))
    assert r["created"] and oct(tok.stat().st_mode & 0o777) == "0o600" and len(tok.read_text().strip()) == 64
    first = tok.read_text()
    assert _init_token(str(tok)) == {"token_file": str(tok), "created": False} and tok.read_text() == first
    cfg = config.Config(base_dir=str(tmp_path)).resolve()
    out = _doctor(cfg)
    assert out["checks"]["token_file"] == "ok (600)" and out["checks"]["bind_address"].startswith("ok")
    assert first.strip() not in repr(out)
