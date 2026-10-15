#!/usr/bin/env python3
"""Renders config.toml from config.example.toml (used by install.sh). Never overwrites an existing file.

Usage: render_config.py TEMPLATE OUTPUT [key=value ...] [--claude-source PATH:MACHINE ...] [--openclaw-source PATH]
Values are written as TOML: integers and true/false as is, lists as JSON-style arrays, anything else quoted.
"""
from __future__ import annotations

import json
import os
import re
import sys


def toml_value(v: str) -> str:
    if re.fullmatch(r"-?\d+", v) or v in ("true", "false") or (v.startswith("[") and v.endswith("]")):
        return v
    return json.dumps(v)


def render(template: str, values: dict[str, str], claude: list[tuple[str, str]], openclaw: list[str]) -> str:
    lines = template.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([a-z_]+) = ", line)
        if m and m.group(1) in values and m.group(1) not in seen:
            lines[i] = f"{m.group(1)} = {toml_value(values[m.group(1)])}"
            seen.add(m.group(1))
    missing = set(values) - seen
    if missing:
        raise SystemExit(f"unknown config keys: {', '.join(sorted(missing))}")
    out = "\n".join(lines) + "\n"
    for path, machine in claude:
        out += f'\n[[claude_sources]]\npath = {json.dumps(path)}\nmachine = {json.dumps(machine)}\n'
    for path in openclaw:
        out += f'\n[[openclaw_sources]]\npath = {json.dumps(path)}\nmachine = "openclaw"\n'
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    template, output, rest = argv[0], argv[1], argv[2:]
    if os.path.exists(output):
        print(f"config exists, kept: {output}")
        return 0
    values, claude, openclaw = {}, [], []
    it = iter(rest)
    for a in it:
        if a == "--claude-source":
            val = next(it)
            path, sep, machine = val.rpartition(":")
            claude.append((path, machine or "local") if sep else (val, "local"))
        elif a == "--openclaw-source":
            openclaw.append(next(it))
        else:
            k, _, v = a.partition("=")
            values[k] = v
    with open(template, encoding="utf-8") as f:
        text = render(f.read(), values, claude, openclaw)
    os.makedirs(os.path.dirname(os.path.abspath(output)), mode=0o700, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"config written: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
