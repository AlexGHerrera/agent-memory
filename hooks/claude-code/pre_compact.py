#!/usr/bin/env python3
"""agent-memory PreCompact hook: before compacting, dumps the work in progress to the memory (`note`, MCP over HTTP)
so that compaction loses nothing:

- open tasks: the optional task list file + the session task list (TodoWrite / TaskCreate+TaskUpdate);
- commitments: assistant sentences such as "I'll follow up", "pending:", "te aviso"…;
- context: the user's last messages and the assistant's last conclusions.

Deterministic and fast (no LLM). If the memory does not answer, the note goes to the pending queue.
Never blocks compaction: always exits with 0.
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import common as mc  # noqa: E402

# English and Spanish phrasings.
COMMITMENT = re.compile(
    r"\b(I'll|I will|will follow up|follow-up|pending:|to do:|tomorrow|tonight|as soon as|when it finishes|"
    r"te aviso|te avisar[ée]|te recuerdo|me encargo|lo har[ée]|queda(n)? pendiente|pendiente(s)?:|en cuanto|"
    r"mañana|esta noche|cuando termine|seguimiento)\b", re.I)
NOTE_MAX = 12_000


def commitments(conclusions: list[str], last_n: int = 30, max_items: int = 12) -> list[str]:
    out: list[str] = []
    for text in conclusions[-last_n:]:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            s = sentence.strip(" -*•\t")
            if 12 <= len(s) and COMMITMENT.search(s) and s not in out:
                out.append(mc.clip(s, 240))
    return out[-max_items:]


def build_note(payload: dict, digest: dict, tasks_text: str, tasks_path: str) -> str:
    project = mc.project_name(digest["cwd"] or payload.get("cwd", ""))
    lines = [f"PreCompact dump ({payload.get('trigger', '?')}) · session {payload.get('session_id', '?')} · "
             f"{mc.machine()} · {project} · {time.strftime('%Y-%m-%d %H:%M')}", ""]
    if digest["open_items"]:
        lines += ["## Open session tasks"] + [f"- {mc.clip(x, 240)}" for x in digest["open_items"][:30]] + [""]
    t = tasks_text.strip()
    if t:
        lines += [f"## {tasks_path}", mc.clip(t, 3000), ""]
    c = commitments(digest["conclusions"])
    if c:
        lines += ["## Detected commitments (heuristic)"] + [f"- {x}" for x in c] + [""]
    if digest["human"]:
        lines += ["## Last user messages"] + [f"- {mc.clip(h, 400)}" for h in digest["human"][-8:]] + [""]
    if digest["conclusions"]:
        lines += ["## Last assistant conclusions"] + [f"- {mc.clip(x, 700)}" for x in digest["conclusions"][-3:]]
    return mc.clip("\n".join(lines), NOTE_MAX)


def main() -> None:
    payload = mc.read_stdin_json()
    transcript = str(payload.get("transcript_path") or "")
    digest = mc.transcript_digest(transcript) if transcript else mc.transcript_digest("/nonexistent")
    tpath = mc.tasks_path()
    note = build_note(payload, digest, mc.read_text(tpath, 20_000) if tpath else "", tpath or "tasks")
    mc.note(note, mc.project_name(digest["cwd"] or payload.get("cwd", "")), timeout=8.0)


if __name__ == "__main__":
    mc.run_safely(main)
