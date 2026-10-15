"""Secrets filter: applied to EVERY text before it is stored in the memory.

Redacts payment cards (Luhn + issuer prefix), known token formats, private keys, credentials in URLs,
assignments like password=... and known literal secrets (compared by SHA-256 hash, never in clear text).
It prefers over-redacting to leaking a secret. Detection patterns match Spanish and English text.

Known limits (not covered):
- Bare passwords without context ("hunter2" alone) and secrets split across lines or messages.
- Secrets in unlisted formats (proprietary tokens without a recognizable prefix) or encoded (base64 of a JSON
  with credentials, URL-encoding).
- In prose it only detects "contraseña/password/clave/pin (de X) es|is|: VALUE" with a value of 4+ characters
  without spaces; passwords with spaces (passphrases) are only caught when quoted after '=' or ':'.
- Cards: only 13-19 digits with a valid issuer prefix and Luhn; no standalone CVVs or expiry dates.
- Literal secrets: only those registered in literal-secrets.sha256 (no spaces, exact length).
- `SOMETHING_KEY` environment variables only in UPPERCASE (so YAML or prose "key: value" is not redacted).
- URL parameters: only those listed in _URL_PARAM and with a value of 8+ characters.
That is why, besides the filter, there is per-session exclusion (exclude_sessions).
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter

TAG = "[REDACTED:{}]"
# Markers already present in stored text.
MARKERS = ("[REDACTED:",)
_MARK = r"\[REDACTED"

_TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.S)),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("telegram-bot", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b|\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9_\-\.=+/]{24,}")),
    ("basic-auth", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{8,}={0,2}")),
    ("tailscale", re.compile(r"\btskey-[A-Za-z0-9\-]{16,}")),
    ("hf", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
]

# Credentials in URLs: scheme://user:PASSWORD@host
_URL_CRED = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)([^\s@/]{3,})(@)")

# Assignments: password=..., "api_key": "...", contraseña: ... (Spanish and English key names)
_KEYS = (r"pass(?:word|wd|phrase)?|pwd|contrase(?:ñ|n)a|clave|secret(?:o)?|client_secret|api[_\-]?key|apikey|"
         r"access[_\-]?key|secret[_\-]?key|private[_\-]?key|auth[_\-]?token|access[_\-]?token|refresh[_\-]?token|"
         r"token|pin|cvv|cvc|cvv2")
# The key may be quoted or backquoted (Markdown: `DB_PASSWORD`: value, **`X_TOKEN`**: value)
# and in camelCase (dbPassword=...).
_ASSIGN = re.compile(
    r"(?i)(?P<key>[\"'`]?\b(?:[A-Za-z0-9]+[_\-]|(?-i:[a-z0-9]+(?=[A-Z])))*(?:" + _KEYS + r")\b[\"'`]?\*{0,2}"
    r"\s*(?:=|:|=>)\s*)"
    r"(?P<val>\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s,;\"'}\]]{3,})")
_VALUE_SAFE = re.compile(r"^(?:[\"']?(?:null|none|true|false|\*+|x+|\.\.\.|<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
                         r"\[REDACT(?:ED|ADO):[^\]]*\]|os\.environ.*|getenv.*|process\.env.*)[\"']?)$", re.I)

# UPPERCASE environment variables *_KEY / *_PASSWORD / *_TOKEN / *_SECRET (STRIPE_KEY=..., `X_KEY`: ...).
# _ASSIGN already covers PASSWORD/TOKEN/SECRET; this adds *_KEY, which in lowercase would cause false positives.
_ENV_KEY = re.compile(
    r"(?P<key>[\"'`]?\b(?:[A-Z0-9]+_)+(?:KEY|PASSWORD|PASS|PWD|TOKEN|SECRET)\b[\"'`]?\*{0,2}[ \t]*(?:=|:)[ \t]*)"
    r"(?P<val>\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s,;\"'}\]]{3,})")

# Sensitive URL parameters (OAuth code/state, tokens, signatures): ?code=...&state=...
_URL_PARAM = re.compile(
    r"([?&](?:code|state|access_token|id_token|refresh_token|token|key|api_key|secret|sig|signature)=)"
    r"(?!" + _MARK + r")[^&\s\"'<>)]{8,}", re.I)

# Leftovers glued to a redaction: the value had a comma or another separator ("Contraseña: abc,def#1" leaves
# "[REDACTED:secret],def#1"). Normal trailing punctuation is kept ("... es [REDACTED:secret]. Luego").
_TAIL_STOP = "\\s`'\"<>()\\[\\]{}«»“”‘’"
_TAIL = re.compile(r"(\[REDACTED:secret\])(?![.,;:!?]*(?:[" + _TAIL_STOP + r"]|$))"
                   r"[^" + _TAIL_STOP + r"]*[^" + _TAIL_STOP + r".,;:!?]")

# Netscape-format cookies (domain TRUE/FALSE path TRUE/FALSE expiry name value): the value is redacted.
COOKIE_LINE = re.compile(
    r"^(?P<pre>\S+[ \t]+(?:TRUE|FALSE)[ \t]+\S+[ \t]+(?:TRUE|FALSE)[ \t]+\d+[ \t]+\S+[ \t]+)(?!" + _MARK + r")(?P<val>\S+)",
    re.M)


def credentials_dump(text: str) -> bool:
    """Cookie dump (2+ Netscape lines): ingestion drops the whole message."""
    return len(COOKIE_LINE.findall(text)) >= 2


# Prose (Spanish and English): "la contraseña es X", "password is X", "clave: X"
_PROSE = re.compile(
    r"(?i)(?P<key>\b(?:contrase(?:ñ|n)a|password|passphrase|clave|pin)\b(?:\s+\w+){0,3}?\s*(?:\b(?:es|is|era|was)\b\s*:?|:)\s*)"
    r"(?P<q>[\"'«`]?)(?P<val>[^\s\"'»`]{4,})")
_PROSE_STOP = {"incorrecta", "incorrecto", "correcta", "correcto", "segura", "seguro", "válida", "valida", "inválida",
               "obligatoria", "necesaria", "required", "wrong", "correct", "invalid", "valid", "incorrect", "muy",
               "demasiado", "too", "the", "una", "nueva", "new", "vacía", "empty", "débil", "weak", "fuerte",
               "strong", "larga", "corta", "secreta", "distinta", "diferente", "igual", "misma", "esta", "this",
               "that", "como", "porque", "because", "needed", "[redacted"}


def _prose_sub(m: re.Match) -> str:
    val = m.group("val")
    if val.lower().rstrip(".,;:!?)") in _PROSE_STOP or val.startswith(MARKERS) or _VALUE_SAFE.match(val):
        return m.group(0)
    tail = re.search(r"[.,;:!?)]+$", val)
    return m.group("key") + m.group("q") + TAG.format("secret") + (tail.group(0) if tail else "")


# Cards: 13-19 consecutive digits, in groups of 4 (Visa/MC...) or 4-6-5 (Amex). Not glued to letters or
# hyphens (avoids UUID fragments and the like).
_CARD = re.compile(r"(?<![\w\-.])(?:\d{4}(?:[ .\-]\d{4}){2}[ .\-]\d{1,7}|\d{4}[ .\-]\d{6}[ .\-]\d{5}|\d{13,19})"
                   r"(?![\w\-]|\.\d)")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _issuer_ok(d: str) -> bool:
    n = len(d)
    if d[0] == "4":
        return n in (13, 16, 19)
    if d[:2] in ("51", "52", "53", "54", "55") or 2221 <= int(d[:4]) <= 2720:
        return n == 16
    if d[:2] in ("34", "37"):
        return n == 15
    if d[:4] == "6011" or d[:2] == "65" or 644 <= int(d[:3]) <= 649 or d[:2] == "62":
        return 16 <= n <= 19
    if d[:2] == "35":
        return 16 <= n <= 19
    if d[:2] in ("36", "38", "39") or 300 <= int(d[:3]) <= 305:
        return 14 <= n <= 19
    return False


def _card_sub(m: re.Match) -> str:
    raw = m.group(0)
    d = re.sub(r"\D", "", raw)
    if 13 <= len(d) <= 19 and _issuer_ok(d) and luhn_ok(d):
        return TAG.format("card")
    return raw


class LiteralSecrets:
    """Known literal secrets, stored as (length, sha256). Searched with a sliding window."""

    def __init__(self, entries: list[tuple[int, str]] | None = None):
        self.by_len: dict[int, set[str]] = {}
        for n, h in entries or []:
            self.by_len.setdefault(n, set()).add(h)

    @classmethod
    def from_file(cls, path: str) -> "LiteralSecrets":
        entries = []
        if path and os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                h, n = line.split()
                entries.append((int(n), h.lower()))
        return cls(entries)

    def __bool__(self):
        return bool(self.by_len)

    def redact(self, text: str) -> tuple[str, int]:
        if not self.by_len:
            return text, 0
        count = 0
        for n, hashes in self.by_len.items():
            if len(text) < n:
                continue
            # Only runs without spaces of length >= n (generated secrets have no spaces)
            out, pos = [], 0
            for m in re.finditer(r"\S{%d,}" % n, text):
                run = m.group(0)
                i, pieces, last = 0, [], 0
                while i + n <= len(run):
                    if hashlib.sha256(run[i:i + n].encode()).hexdigest() in hashes:
                        pieces.append(run[last:i] + TAG.format("secret"))
                        i += n
                        last = i
                        count += 1
                    else:
                        i += 1
                pieces.append(run[last:])
                out.append(text[pos:m.start()] + "".join(pieces))
                pos = m.end()
            out.append(text[pos:])
            text = "".join(out)
        return text, count


class SecretFilter:
    def __init__(self, literals: LiteralSecrets | None = None):
        self.literals = literals or LiteralSecrets()

    def redact(self, text: str) -> tuple[str, int]:
        text, counts = self.redact_detail(text)
        return text, sum(counts.values())

    def redact_detail(self, text: str) -> tuple[str, Counter]:
        """Like redact, but with the number of redactions per rule (never returns the redacted values)."""
        counts: Counter = Counter()
        if not text:
            return text, counts
        text, n = self.literals.redact(text)
        counts["literal"] += n

        def run(rule: str, pat: re.Pattern, repl) -> None:
            """Applies the rule counting only substitutions that change something (refiltering does not inflate counts)."""
            nonlocal text
            fn = repl if callable(repl) else (lambda m: m.expand(repl))

            def sub(m: re.Match) -> str:
                out = fn(m)
                if out != m.group(0):
                    counts[rule] += 1
                return out
            text = pat.sub(sub, text)

        def assign(m: re.Match) -> str:
            val = m.group("val")
            if _VALUE_SAFE.match(val) or val.startswith(MARKERS):
                return m.group(0)
            return m.group("key") + TAG.format("secret")

        for name, pat in _TOKEN_PATTERNS:
            run(name, pat, TAG.format(name))
        run("url-credential", _URL_CRED, lambda m: m.group(1) + TAG.format("password") + m.group(3))
        run("url-param", _URL_PARAM, lambda m: m.group(1) + TAG.format("url"))
        run("assignment", _ASSIGN, assign)
        run("env-var", _ENV_KEY, assign)
        run("prose", _PROSE, _prose_sub)
        run("glued-tail", _TAIL, r"\1")
        run("cookie", COOKIE_LINE, lambda m: m.group("pre") + TAG.format("cookie"))
        run("card", _CARD, _card_sub)
        return text, +counts


def redact(text: str) -> str:
    """Shortcut without literal secrets."""
    return SecretFilter().redact(text)[0]


def count_markers(text: str) -> int:
    """Redaction markers already present in `text`."""
    return sum((text or "").count(m) for m in MARKERS)
