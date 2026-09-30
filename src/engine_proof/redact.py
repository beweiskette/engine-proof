"""Remove secret-like values from text before it leaves engine-proof.

Engine logs and engine output can quote URLs with API keys, command lines with
tokens, connection strings with passwords and paths that contain the user name.
Every message and evidence string is passed through `redact` before it is
rendered, and hook output is capped in size.

The rules are pattern based. They catch common formats, not every possible secret.

`unreal_probe.py` runs inside the Unreal editor without this package, so it keeps
a copy of RULES. tests/test_redaction.py checks that both copies stay identical.
"""

from __future__ import annotations

import re

MARK = "[REDACTED]"

# (pattern, replacement). Applied in order; replacements only use group references
# so the list can be compared with the copy in unreal_probe.py.
RULES: list[tuple[str, str]] = [
    # PEM private key blocks.
    (r"(?s)-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
     r"-----BEGIN \1PRIVATE KEY----- [REDACTED] -----END \1PRIVATE KEY-----"),
    # Credentials in URLs: scheme://user:password@host
    (r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@", r"\1\2:[REDACTED]@"),
    # Authorization headers, with or without a scheme word.
    (r"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)((?:bearer|basic|token|digest|negotiate)\s+)?"
     r"[^\s\"',;]+", r"\1\2[REDACTED]"),
    (r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{8,}=*", r"\1[REDACTED]"),
    # Well-known token formats.
    (r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}", "[REDACTED]"),
    (r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", "[REDACTED]"),
    (r"\bglpat-[A-Za-z0-9_\-]{20,}", "[REDACTED]"),
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[REDACTED]"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "[REDACTED]"),
    (r"\bxox[abposr]-[A-Za-z0-9\-]{10,}", "[REDACTED]"),
    (r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}", "[REDACTED]"),
    (r"\bhf_[A-Za-z0-9]{30,}", "[REDACTED]"),
    (r"\bnpm_[A-Za-z0-9]{36}\b", "[REDACTED]"),
    (r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", "[REDACTED]"),
    # key=value and key: value where the key names a secret (query strings,
    # command lines, connection strings, JSON, environment variables).
    (r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9_.\-]*(?:passw(?:or)?d|pwd|passphrase|secret|token|api[_\-]?key|apikey"
     r"|access[_\-]?key|private[_\-]?key|credentials?|signature|session[_\-]?id|cookie)[\"']?\s*[=:]\s*)"
     r"(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s&;,\"'<>]+)", r"\1[REDACTED]"),
    (r"(?i)([?&](?:key|sig|code|auth)=)[^&\s#\"'<>]+", r"\1[REDACTED]"),
    # User names in home directory paths.
    (r"(?i)\b([a-z]:[\\/]+(?:users|documents and settings)[\\/]+)(?!<user>)([^\\/\s\"'<>|:*?]+)", r"\1<user>"),
    (r"(?<![\w.])(/(?:home|Users)/)(?!<user>)([^/\s\"'<>:]+)", r"\1<user>"),
]

_COMPILED = [(re.compile(p), r) for p, r in RULES]

# A line that looks like NAME=value from `env`, `set` or `export -p`.
_ENV_LINE = re.compile(r"^\s*(?:export\s+|set\s+|declare\s+-x\s+)?[A-Za-z_][A-Za-z0-9_]*=")
ENV_DUMP_MIN_LINES = 3

MAX_MESSAGE_CHARS = 500
MAX_EVIDENCE_CHARS = 2000
MAX_EVIDENCE_LINES = 40
MAX_LINE_CHARS = 400


def redact(text: str | None) -> str | None:
    """Replace secret-like values in one string."""
    if not text:
        return text
    for rx, repl in _COMPILED:
        text = rx.sub(repl, text)
    return text


def drop_env_dump(text: str) -> str:
    """Remove blocks of NAME=value lines (an environment dump) from multi-line text."""
    lines = text.splitlines()
    env = [i for i, ln in enumerate(lines) if _ENV_LINE.match(ln)]
    if len(env) < ENV_DUMP_MIN_LINES:
        return text
    drop = set(env)
    out = []
    for i, ln in enumerate(lines):
        if i == env[0]:
            out.append(f"[{len(env)} environment variable line(s) removed]")
        if i not in drop:
            out.append(ln)
    return "\n".join(out)


def clip(text: str, limit: int, note: str = " ... [truncated]") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))] + note


def safe_message(text: str) -> str:
    return clip(redact(text) or "", MAX_MESSAGE_CHARS)


def safe_evidence(text: str) -> str:
    text = drop_env_dump(redact(text) or "")
    lines = [clip(ln, MAX_LINE_CHARS) for ln in text.splitlines()]
    cut = len(lines) > MAX_EVIDENCE_LINES
    text = "\n".join(lines[:MAX_EVIDENCE_LINES])
    if cut or len(text) > MAX_EVIDENCE_CHARS:
        note = "\n... [evidence truncated]"
        text = text[: MAX_EVIDENCE_CHARS - len(note)] + note
    return text


def cap_output(text: str, limit: int, hint: str = "") -> str:
    """Cut text at a line boundary so it stays within `limit` characters."""
    if len(text) <= limit:
        return text
    note = f"\n  ... [output truncated: {len(text)} characters, limit {limit}]{hint}"
    cut = text[: max(0, limit - len(note))]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut + note
