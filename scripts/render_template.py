#!/usr/bin/env python3
"""Renders a systemd template (used by install.sh): replaces @KEY@ with the given values.

Usage: render_template.py TEMPLATE OUTPUT KEY=VALUE [KEY=VALUE ...]
Values are inserted literally (no shell or sed interpretation); `%` is escaped as `%%` for systemd. Values with
whitespace, quotes, backslashes or control characters are refused, because systemd would split or reinterpret them.
Fails if a placeholder is left unrendered.
"""
import os
import re
import sys

BAD = re.compile(r"[\s\"'\\\x00-\x1f]")


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    template, output, pairs = argv[0], argv[1], argv[2:]
    values = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_]+", k):
            print(f"invalid argument: {p!r}", file=sys.stderr)
            return 2
        if BAD.search(v):
            print(f"unsupported character in {k} (whitespace, quotes or backslashes): {v!r}", file=sys.stderr)
            return 2
        values[k] = v.replace("%", "%%")
    with open(template, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"@([A-Z_]+)@", lambda m: values.get(m.group(1), m.group(0)), text)
    left = sorted(set(re.findall(r"@([A-Z_]+)@", text)))
    if left:
        print(f"unrendered placeholders in {template}: {', '.join(left)}", file=sys.stderr)
        return 1
    tmp = output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
