"""Configuration (TOML, no secrets).

Lookup order: the `--config` CLI flag, then $AGENT_MEMORY_CONFIG, then ~/.config/agent-memory/config.toml.
Every option is documented in config.example.toml. Relative paths and `~` are expanded; paths left empty are
derived from `base_dir`.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import LANGUAGES

DEFAULT_PATH = "~/.config/agent-memory/config.toml"
DEFAULT_BASE = "~/.local/share/agent-memory"


@dataclass
class ClaudeSource:
    path: str          # root like ~/.claude/projects (Claude Code JSONL transcripts, any depth)
    machine: str = "local"   # free label stored with every message (e.g. laptop, server)


@dataclass
class OpenClawSource:
    path: str                  # OpenClaw agents root, e.g. ~/.openclaw/agents (reads <agent>/sessions/*.jsonl)
    machine: str = "openclaw"
    agents: list[str] = field(default_factory=list)          # empty = every agent folder
    primary_agents: list[str] = field(default_factory=lambda: ["main"])   # agents a human talks to directly


@dataclass
class Config:
    base_dir: str = DEFAULT_BASE
    db_path: str = ""          # default <base_dir>/data/memory.db
    index_path: str = ""       # default <base_dir>/INDEX.md
    models_dir: str = ""       # default <base_dir>/models
    work_dir: str = ""         # default <base_dir>/work (cwd of the LLM CLI)
    inbox_dir: str = ""        # default <base_dir>/inbox (PUT /inbox/... uploads)
    timezone: str = "UTC"
    # Language of what the user reads: summaries, proposals, INDEX.md headings and the `day` CLI view (en | es).
    # Prompts to the model and MCP tool output are always English.
    language: str = "en"
    # How the prompts refer to the human and to the agent.
    user_name: str = "the user"
    assistant_name: str = "the assistant"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384
    claude_sources: list[ClaudeSource] = field(default_factory=list)
    openclaw_sources: list[OpenClawSource] = field(default_factory=list)
    # Optional Markdown notes folder (e.g. an Obsidian vault), read-only (empty = disabled). Notes are ingested by
    # mtime + content hash, frontmatter included; they are the canonical source for the facts layer, except the
    # notes matching `vault_noncanonical` (globs relative to the vault), which count like ordinary notes.
    vault_path: str = ""
    vault_exclude_dirs: list[str] = field(default_factory=lambda: [".git", ".obsidian", ".trash", ".stfolder"])
    vault_noncanonical: list[str] = field(default_factory=list)
    vault_max_file_kb: int = 512
    # Exclusions: whole sessions (including their subagents) and projects (folder name under ~/.claude/projects)
    exclude_sessions: list[str] = field(default_factory=list)
    exclude_projects: list[str] = field(default_factory=list)
    # SHA-256 hashes of literal secrets redacted from any text (never the secret itself). Default
    # <base_dir>/config/literal-secrets.sha256 (optional file).
    literal_secrets_file: str = ""
    # MCP server
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: list[str] = field(default_factory=list)
    mcp_token_file: str = ""   # default <base_dir>/config/mcp-token (600)
    upload_max_mb: int = 64
    # Facts brief injected at session start (GET /facts/brief): character budget per section, recency window and
    # decay of contextual facts. The hook caps the whole block again.
    facts_core_chars: int = 500
    facts_recent_chars: int = 450
    facts_context_chars: int = 350
    facts_recent_days: int = 7
    facts_decay_days: int = 21
    # Categories never injected at session start (still available through facts() and search()).
    facts_brief_excluded: list[str] = field(default_factory=lambda: ["health"])
    # Consolidation (nightly). Default backend: Claude Code CLI (`claude -p --model <llm_model>`). If
    # `llm_command` is set, that command is run instead: it receives the system prompt + prompt on stdin and must
    # print the reply on stdout.
    claude_bin: str = "claude"
    llm_model: str = "haiku"
    llm_command: list[str] = field(default_factory=list)
    llm_timeout: int = 600
    max_llm_calls: int = 80
    session_idle_minutes: int = 30
    consolidate_sources: list[str] = field(default_factory=lambda: ["claude", "openclaw"])
    # Facts extraction budgets (LLM calls per nightly run) and window of complete days.
    facts_max_calls: int = 3
    facts_vault_max_calls: int = 2
    facts_days_back: int = 2
    # Self-learning (optional): nightly proposals of preferences, skills and personality tweaks. `profile_dir`
    # holds PREFERENCES.md, PERSONALITY.md and skills/<name>/SKILL.md (read-only; used as context).
    self_learning: bool = False
    profile_dir: str = ""
    proposals_max_calls: int = 3
    proposals_days_back: int = 2
    # Sizes
    max_message_chars: int = 50000
    embed_chars: int = 2000

    def resolve(self) -> "Config":
        """Expands `~` and fills derived paths from base_dir."""
        x = os.path.expanduser
        self.base_dir = x(self.base_dir)
        b = self.base_dir
        self.db_path = x(self.db_path) if self.db_path else os.path.join(b, "data", "memory.db")
        self.index_path = x(self.index_path) if self.index_path else os.path.join(b, "INDEX.md")
        self.models_dir = x(self.models_dir) if self.models_dir else os.path.join(b, "models")
        self.work_dir = x(self.work_dir) if self.work_dir else os.path.join(b, "work")
        self.inbox_dir = x(self.inbox_dir) if self.inbox_dir else os.path.join(b, "inbox")
        self.literal_secrets_file = (x(self.literal_secrets_file) if self.literal_secrets_file
                                     else os.path.join(b, "config", "literal-secrets.sha256"))
        self.mcp_token_file = x(self.mcp_token_file) if self.mcp_token_file else os.path.join(b, "config", "mcp-token")
        self.vault_path = x(self.vault_path) if self.vault_path else ""
        self.profile_dir = x(self.profile_dir) if self.profile_dir else ""
        for s in self.claude_sources:
            s.path = x(s.path)
        for s in self.openclaw_sources:
            s.path = x(s.path)
        return self


def load(path: str | None = None) -> Config:
    path = os.path.expanduser(path or os.environ.get("AGENT_MEMORY_CONFIG") or DEFAULT_PATH)
    cfg = Config()
    if not Path(path).exists():
        return cfg.resolve()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for k, v in data.items():
        if k == "claude_sources":
            cfg.claude_sources = [ClaudeSource(**s) for s in v]
        elif k == "openclaw_sources":
            cfg.openclaw_sources = [OpenClawSource(**s) for s in v]
        elif hasattr(cfg, k) and not k.startswith("_") and k != "resolve":
            setattr(cfg, k, v)
        else:
            raise ValueError(f"unknown key in {path}: {k}")
    if cfg.language not in LANGUAGES:
        raise ValueError(f"unsupported language in {path}: {cfg.language!r} (use one of {', '.join(LANGUAGES)})")
    return cfg.resolve()
