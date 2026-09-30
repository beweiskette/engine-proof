"""Encoding, BOM, mojibake and line-ending analysis for text files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .findings import Finding

BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# Characters that appear when UTF-8 bytes are decoded as cp1252 or latin-1.
# Lead characters come from the bytes 0xC2..0xF4, continuation characters
# from 0x80..0xBF (in cp1252 some of those map to typographic characters).
_CP1252_HIGH = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ"
_CONT = "\u0080-¿" + re.escape(_CP1252_HIGH)
_LEAD = "Â-ô"
_MOJIBAKE_RE = re.compile(f"[{_LEAD}][{_CONT}]{{1,3}}(?:[{_LEAD}][{_CONT}]{{1,3}})*")


@dataclass
class TextInfo:
    encoding: str  # "utf-8", "utf-8-sig", "utf-16-le", ..., "invalid-utf-8", "binary"
    bom: bool = False
    eol: str = "none"  # "lf", "crlf", "cr", "mixed", "none"
    eol_counts: dict[str, int] = field(default_factory=dict)
    mojibake: list[tuple[int, str, str]] = field(default_factory=list)  # (line, seen, fixed)
    first_invalid_offset: int | None = None
    minority_eol_line: int | None = None

    def summary(self) -> dict:
        return {
            "encoding": self.encoding,
            "bom": self.bom,
            "eol": self.eol,
            "mojibake": len(self.mojibake),
        }


def _repair(seq: str) -> str | None:
    """Return the repaired text if seq is UTF-8 that was decoded as cp1252/latin-1."""
    for codec in ("cp1252", "latin-1"):
        try:
            raw = seq.encode(codec)
        except UnicodeEncodeError:
            continue
        try:
            fixed = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if fixed != seq and any(ord(ch) > 127 for ch in fixed):
            return fixed
    return None


def find_mojibake(text: str, limit: int = 50) -> list[tuple[int, str, str]]:
    """Find double-encoded UTF-8 sequences such as 'Ã¼' (for 'ü')."""
    hits: list[tuple[int, str, str]] = []
    for m in _MOJIBAKE_RE.finditer(text):
        seq = m.group(0)
        fixed = _repair(seq)
        if fixed is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        hits.append((line, seq, fixed))
        if len(hits) >= limit:
            break
    return hits


def line_endings(data: bytes) -> tuple[str, dict[str, int], int | None]:
    """Classify line endings. Returns (style, counts, first line of minority style)."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    counts = {"crlf": crlf, "lf": lf, "cr": cr}
    used = [k for k, v in counts.items() if v]
    if not used:
        return "none", counts, None
    if len(used) == 1:
        return used[0], counts, None
    minority = min(used, key=lambda k: counts[k])
    token = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r"}[minority]
    # find first occurrence of the minority style that is really that style
    idx = 0
    while True:
        pos = data.find(token, idx)
        if pos < 0:
            return "mixed", counts, None
        if minority == "lf" and pos > 0 and data[pos - 1 : pos] == b"\r":
            idx = pos + 1
            continue
        if minority == "cr" and data[pos + 1 : pos + 2] == b"\n":
            idx = pos + 1
            continue
        line = data.count(b"\n", 0, pos) + data.count(b"\r", 0, pos) - data.count(b"\r\n", 0, pos) + 1
        return "mixed", counts, line


def analyze_bytes(data: bytes) -> TextInfo:
    enc = "utf-8"
    bom = False
    body = data
    for mark, name in BOMS:
        if data.startswith(mark):
            enc, bom = name, True
            body = data[len(mark):]
            break
    if enc.startswith(("utf-16", "utf-32")):
        try:
            text = body.decode(enc)
        except UnicodeDecodeError:
            return TextInfo(encoding=enc, bom=bom)
        style, counts, minority = line_endings(text.encode("utf-8"))
        return TextInfo(encoding=enc, bom=bom, eol=style, eol_counts=counts,
                        mojibake=find_mojibake(text), minority_eol_line=minority)
    style, counts, minority = line_endings(body)
    info = TextInfo(encoding=enc, bom=bom, eol=style, eol_counts=counts, minority_eol_line=minority)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        info.encoding = "invalid-utf-8"
        info.first_invalid_offset = exc.start + (len(data) - len(body))
        text = body.decode("utf-8", errors="replace")
    info.mojibake = find_mojibake(text)
    return info


def findings_for(relpath: str, info: TextInfo, previous: dict | None = None,
                 bom_severity: str = "info") -> list[Finding]:
    """Turn a TextInfo into findings. `previous` is the snapshot summary, if any.

    Problems that already existed in the snapshot are downgraded to info so that
    an agent is only blamed for what it introduced.
    """
    out: list[Finding] = []
    prev = previous or {}

    def sev(default: str, pre_existing: bool) -> str:
        return "info" if pre_existing else default

    if info.encoding == "invalid-utf-8":
        pre = prev.get("encoding") == "invalid-utf-8"
        out.append(Finding(
            "text.invalid_utf8", sev("error", pre),
            "file is not valid UTF-8 (engines usually refuse to load it)"
            + (" [already present in snapshot]" if pre else ""),
            file=relpath,
            evidence=f"first invalid byte at offset {info.first_invalid_offset}",
        ))
    elif info.encoding.startswith(("utf-16", "utf-32")):
        pre = prev.get("encoding") == info.encoding
        out.append(Finding(
            "text.utf16", sev("error", pre),
            f"file is encoded as {info.encoding}; engine text formats expect UTF-8",
            file=relpath,
        ))
    if info.mojibake:
        pre = bool(prev.get("mojibake"))
        line, seen, fixed = info.mojibake[0]
        ev = "\n".join(
            f"line {ln}: '{s}' ({' '.join(f'U+{ord(ch):04X}' for ch in s)}) should be '{f}'"
            for ln, s, f in info.mojibake[:5]
        )
        out.append(Finding(
            "text.mojibake", sev("error", pre),
            f"{len(info.mojibake)} double-encoded UTF-8 sequence(s) (text was decoded as "
            f"cp1252/latin-1 and saved again)" + (" [already present in snapshot]" if pre else ""),
            file=relpath, line=line, evidence=ev,
        ))
    if info.eol == "mixed":
        pre = prev.get("eol") == "mixed"
        c = info.eol_counts
        out.append(Finding(
            "text.mixed_eol", sev("warning", pre),
            "file mixes line endings" + (" [already present in snapshot]" if pre else ""),
            file=relpath, line=info.minority_eol_line,
            evidence=f"crlf={c.get('crlf', 0)} lf={c.get('lf', 0)} cr={c.get('cr', 0)}",
        ))
    if info.bom and info.encoding == "utf-8-sig":
        pre = bool(prev.get("bom"))
        if not pre:
            out.append(Finding(
                "text.bom", bom_severity if previous is None else "warning",
                "file starts with a UTF-8 byte order mark"
                + (" (added since snapshot)" if previous is not None else ""),
                file=relpath,
            ))
    elif prev.get("bom") and not info.bom:
        out.append(Finding("text.bom_removed", "info", "UTF-8 byte order mark was removed", file=relpath))
    if previous is not None and prev.get("eol") in ("lf", "crlf") and info.eol in ("lf", "crlf") \
            and prev.get("eol") != info.eol:
        out.append(Finding(
            "text.eol_changed", "warning",
            f"line endings switched from {prev['eol']} to {info.eol} for the whole file "
            "(diffs will show every line as changed)",
            file=relpath,
        ))
    return out
