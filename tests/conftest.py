import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_memory.config import ClaudeSource, Config  # noqa: E402
from agent_memory.db import connect  # noqa: E402
from agent_memory.embed import HashEmbedder  # noqa: E402

EXCLUDED = "0f0f0f0f-1111-4222-8333-444444444444"
CWD = "/home/alice/projects/demo-app"


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.base_dir = str(tmp_path)
    c.db_path = str(tmp_path / "data" / "memory.db")
    c.index_path = str(tmp_path / "INDEX.md")
    c.work_dir = str(tmp_path / "work")
    c.inbox_dir = str(tmp_path / "inbox")
    c.literal_secrets_file = str(tmp_path / "literal.sha256")
    c.timezone = "Europe/Madrid"
    c.exclude_sessions = [EXCLUDED]
    c.exclude_projects = ["-home-alice-.local-share-agent-memory-work"]
    c.claude_sources = [ClaudeSource(path=str(tmp_path / "projects"), machine="laptop")]
    return c


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_path, cfg.embedding_dim)
    yield c
    c.close()


@pytest.fixture
def embedder():
    return HashEmbedder(384)


def rec(typ, uuid, sid, ts, content, **kw):
    d = {"type": typ, "uuid": uuid, "sessionId": sid, "timestamp": ts, "cwd": CWD,
         "message": {"role": typ, "content": content}}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False) + "\n"


def write_jsonl(path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.writelines(lines)
