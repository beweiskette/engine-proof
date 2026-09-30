"""Expectation matching between a snapshot and the current disk state."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .findings import Finding
from .project import is_binary_ext
from .snapshot import Diff, TEXT_ANALYSIS_LIMIT
from .textchecks import analyze_bytes, findings_for

STATES = {
    "changed": "changed",
    "modified": "changed",
    "created": "created",
    "added": "created",
    "new": "created",
    "deleted": "deleted",
    "removed": "deleted",
    "unchanged": "unchanged",
    "untouched": "unchanged",
    "exists": "exists",
    "written": "written",   # changed or created
    "touched": "touched",   # changed, created or rewritten with identical bytes
}

CASE_INSENSITIVE = os.name == "nt"


@dataclass
class Expectation:
    raw: str
    pattern: str
    state: str
    regex: re.Pattern

    def matches(self, rel: str) -> bool:
        return bool(self.regex.fullmatch(rel))


def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a path glob into a regex. '**' crosses directories, '*' does not."""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        elif c == "{":
            j = pattern.find("}", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                alts = pattern[i + 1:j].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    flags = re.IGNORECASE if CASE_INSENSITIVE else 0
    return re.compile("".join(out), flags)


MSYS_MANGLED_RE = re.compile(r"^[A-Za-z]:/.*?(/Game/.*)$")


def normalize_pattern(pattern: str) -> str:
    """Accept res:// paths (Godot), /Game/ package paths (Unreal) and backslashes."""
    p = pattern.strip().replace("\\", "/")
    m = MSYS_MANGLED_RE.match(p)
    if m and "/Content/" not in p:
        # Git Bash on Windows rewrites "/Game/X" into "C:/Program Files/Git/Game/X".
        p = m.group(1)
    if p.startswith("res://"):
        p = p[len("res://"):]
    elif p.startswith("/Game/"):
        rest = p[len("/Game/"):]
        # Object paths look like /Game/Dir/Asset.Asset; keep only the package part.
        last = rest.rsplit("/", 1)[-1]
        if "." in last:
            stem, suffix = last.split(".", 1)
            if suffix == stem or suffix.endswith("_C") and suffix[:-2] == stem:
                rest = rest[: -len(last)] + stem
        base = "Content/" + rest
        if not re.search(r"\.(uasset|umap)$", base, re.IGNORECASE) and not any(ch in base for ch in "*?"):
            base += ".{uasset,umap}"
        p = base
    elif p.startswith("./"):
        p = p[2:]
    return p


def parse_expectation(raw: str, root: Path | None = None) -> Expectation:
    text = raw.strip()
    state = "changed"
    parts = text.rsplit(None, 1)
    if len(parts) == 2 and parts[1].lower() in STATES:
        text, state = parts[0], STATES[parts[1].lower()]
    elif ":" in text and text.split(":", 1)[0].lower() in STATES:
        s, text = text.split(":", 1)
        state = STATES[s.lower()]
    pattern = normalize_pattern(text)
    if root is not None:
        # Absolute paths inside the project become relative.
        try:
            ap = Path(pattern)
            if ap.is_absolute():
                pattern = ap.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return Expectation(raw=raw, pattern=pattern, state=state, regex=glob_to_regex(pattern))


def evaluate(expectations: list[Expectation], before: dict, after: dict, d: Diff,
             root: Path) -> list[Finding]:
    out: list[Finding] = []
    changed, created, deleted = set(d.changed), set(d.created), set(d.deleted)
    rewritten = set(d.rewritten)
    for exp in expectations:
        in_before = [r for r in before["files"] if exp.matches(r)]
        in_after = [r for r in after["files"] if exp.matches(r)]
        hits: list[str]
        if exp.state == "changed":
            hits = [r for r in in_after if r in changed]
        elif exp.state == "created":
            hits = [r for r in in_after if r in created]
        elif exp.state == "deleted":
            hits = [r for r in in_before if r in deleted]
        elif exp.state == "written":
            hits = [r for r in in_after if r in changed or r in created]
        elif exp.state == "touched":
            hits = [r for r in in_after if r in changed or r in created or r in rewritten]
        elif exp.state == "exists":
            hits = in_after
        elif exp.state == "unchanged":
            bad = [r for r in set(in_before) | set(in_after)
                   if r in changed or r in created or r in deleted]
            if bad or not in_after:
                ev = ("changed: " + ", ".join(sorted(bad)[:10])) if bad else "no matching file"
                out.append(Finding("verify.expectation_failed", "error",
                                   f'expected "{exp.pattern}" unchanged', file=exp.pattern, evidence=ev))
            else:
                out.append(Finding("verify.expectation_met", "info",
                                   f'"{exp.pattern}" unchanged ({len(in_after)} file(s))', file=exp.pattern))
            continue
        else:  # pragma: no cover
            raise ValueError(exp.state)

        if hits:
            out.append(Finding("verify.expectation_met", "info",
                               f'"{exp.pattern}" {exp.state}: ' + ", ".join(sorted(hits)[:10]),
                               file=exp.pattern))
            continue
        out.append(_explain_failure(exp, in_before, in_after, before, after, rewritten, root))
    return out


def _explain_failure(exp: Expectation, in_before: list[str], in_after: list[str], before: dict,
                     after: dict, rewritten: set[str], root: Path) -> Finding:
    evidence: list[str] = []
    fid = "verify.expectation_failed"
    target = in_after[0] if len(in_after) == 1 else (in_before[0] if len(in_before) == 1 else exp.pattern)
    if not in_before and not in_after:
        evidence.append("no tracked file matches this pattern")
        # Maybe the file exists but its extension is not tracked.
        plain = exp.pattern
        if not any(ch in plain for ch in "*?[{") and (root / plain).exists():
            evidence.append("the path exists but its extension is not tracked; "
                            "snapshot again with --ext")
    for rel in in_after[:10]:
        rec = after["files"][rel]
        brec = before["files"].get(rel)
        if rec.get("readonly"):
            fid = "verify.readonly_not_saved"
            evidence.append(f"{rel}: file is read-only (locked or not checked out); "
                            "a save was probably discarded")
        if rel in rewritten:
            evidence.append(f"{rel}: rewritten but content is byte-identical")
        elif brec is not None and exp.state in ("changed", "written"):
            evidence.append(f"{rel}: unchanged since snapshot (sha256 {str(brec.get('sha256', '?'))[:12]})")
        elif brec is not None and exp.state == "created":
            evidence.append(f"{rel}: already existed before the snapshot")
    if exp.state == "deleted" and in_after:
        evidence.append("still present: " + ", ".join(in_after[:10]))
    return Finding(fid, "error", f'expected "{exp.pattern}" {exp.state}, but it was not',
                   file=target, evidence="\n".join(evidence) or None)


def unexpected_changes(expectations: list[Expectation], d: Diff, strict: bool) -> list[Finding]:
    out = []
    kinds = (("changed", d.changed), ("created", d.created), ("deleted", d.deleted))
    for kind, rels in kinds:
        for rel in rels:
            if any(e.matches(rel) for e in expectations):
                continue
            if expectations:
                sev = "error" if strict else "warning"
                out.append(Finding("verify.unexpected_change", sev, f"{kind} but not expected", file=rel))
            else:
                out.append(Finding("verify.change", "info", kind, file=rel))
    for rel in d.readonly_flipped:
        out.append(Finding("verify.readonly_flag_changed", "info",
                           "read-only flag changed since snapshot", file=rel))
    return out


def text_findings_for_changes(root: Path, d: Diff, before: dict) -> list[Finding]:
    out = []
    for rel in sorted(set(d.changed) | set(d.created)):
        if is_binary_ext(rel):
            continue
        p = root / rel
        try:
            if p.stat().st_size > TEXT_ANALYSIS_LIMIT:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        prev = before["files"].get(rel, {}).get("text")
        if rel in d.created:
            prev = None
        info = analyze_bytes(data)
        # For created files, pass an empty dict so everything counts as new.
        out.extend(findings_for(rel, info, previous=prev if prev is not None else {},
                                bom_severity="warning"))
    return out
