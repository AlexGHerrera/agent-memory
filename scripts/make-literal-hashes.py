#!/usr/bin/env python3
"""Writes the list of literal secrets to redact as "sha256 length" lines (config `literal_secrets_file`).

Reads each given secret file (one secret per file, e.g. a backup passphrase file) and writes ONLY its SHA-256 and
length. It never prints or copies the secret. Secrets shorter than 12 characters are skipped (too many false
matches). Existing entries in OUTPUT are kept, so it can be run again with more files.

Usage: make-literal-hashes.py OUTPUT SECRET_FILE [SECRET_FILE ...] [--owner USER]
  --owner USER  chown OUTPUT to USER (when run as root for a service user)
"""
import hashlib
import os
import sys


def main(argv):
    owner = None
    if "--owner" in argv:
        i = argv.index("--owner")
        owner = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    out, sources = argv[0], argv[1:]
    entries = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            entries = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    for src in sources:
        if not os.path.exists(src):
            print(f"warning: {src} does not exist, skipped", file=sys.stderr)
            continue
        with open(src, encoding="utf-8") as f:
            secret = f.read().strip()
        if len(secret) < 12:
            print(f"warning: {src} is too short to search for safely, skipped", file=sys.stderr)
            continue
        line = f"{hashlib.sha256(secret.encode()).hexdigest()} {len(secret)}"
        del secret
        if line not in entries:
            entries.append(line)
    os.makedirs(os.path.dirname(os.path.abspath(out)), mode=0o700, exist_ok=True)
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# sha256 length — literal secrets redacted by agent-memory (generated, do not edit)\n")
        f.write("".join(e + "\n" for e in entries))
    if owner:
        import pwd
        pw = pwd.getpwnam(owner)
        os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.replace(tmp, out)
    print(f"{len(entries)} hashes in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
