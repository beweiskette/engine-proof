"""Offline Unreal Engine checks: editor logs, read-only assets, editor language."""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

from .findings import Finding
from .project import iter_files, to_rel
from .snapshot import is_readonly

LOG_PREFIX_RE = re.compile(r"^\[[^\]]*\]\[\s*\d+\]")
LINE_RE = re.compile(r"^(?P<cat>Log\w+|Cmd|Warning|Error):\s*(?:(?P<verb>Fatal|Error|Warning|Display|Log|Verbose)\s*:\s*)?(?P<msg>.*)$")
ASSET_RE = re.compile(r"(/Game/[\w\-./]+)|((?:[\w\-]+/)*Content/[\w\-/. ]+?\.(?:uasset|umap))", re.IGNORECASE)

# (id, severity, pattern). The first match wins, so the order matters.
LOG_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("unreal.log.crash", "error",
     re.compile(r"(?i)fatal error|assertion failed|=== critical error ===|unhandled exception")),
    ("unreal.log.readonly", "error",
     re.compile(r"(?i)read[- ]?only|write[- ]protected|access (?:is )?denied|permission denied")),
    ("unreal.log.source_control", "error",
     re.compile(r"(?i)not checked out|checked out by (?:another|other)|locked by (?:another|other|someone)"
                r"|could not (?:be )?check(?:ed)? out|exclusive checkout|^LogSourceControl: (?:Error|Warning)")),
    ("unreal.log.save_failed", "error",
     re.compile(r"(?i)failed to save|could(?: not|n't) save|unable to save|error saving|save (?:has )?failed"
                r"|saving .{0,120} failed|^LogSavePackage: (?:Error|Warning)|^LogFileHelpers: (?:Error|Warning)")),
    ("unreal.log.blueprint_compile", "error",
     re.compile(r"(?i)^Log(?:Blueprint|K2Compiler|Kismet\w*): Error|\[Compiler\].*\berror\b"
                r"|blueprint .{0,160}(?:failed to compile|compil\w* (?:error|fail))|compile(?:r)? (?:error|failed)")),
    ("unreal.log.blueprint_warning", "warning",
     re.compile(r"(?i)^Log(?:Blueprint|K2Compiler|Kismet\w*): Warning|\[Compiler\].*\bwarning\b")),
    ("unreal.log.asset_create_failed", "error",
     re.compile(r"(?i)^Log(?:AssetTools|EditorAssetSubsystem|EditorScripting|AssetRegistry): Error"
                r"|failed to create (?:asset|package|new)|asset already exists|could not create (?:asset|package)")),
    ("unreal.log.load_failed", "warning",
     re.compile(r"(?i)failed to (?:find|load) (?:object|package|asset|')|^LogLinker: (?:Error|Warning)"
                r"|can't find file")),
    ("unreal.log.error", "warning", re.compile(r"^Log\w+: Error:")),
]

CULTURE_LOG_RE = re.compile(
    r"(?i)^Log(?:Init|Internationalization|ICUInternationalization|TextLocalizationManager|Localization)\b"
    r".*\b(?:culture|language)\b[^A-Za-z]{0,6}['\"]?([a-z]{2,3}(?:[-_][A-Za-z0-9]{2,4})?)['\"]?"
)


@dataclass
class LogLine:
    number: int
    text: str       # without the timestamp prefix
    category: str
    verbosity: str
    message: str


def parse_log_line(raw: str, number: int) -> LogLine | None:
    s = LOG_PREFIX_RE.sub("", raw.rstrip("\r\n"))
    if not s.strip():
        return None
    m = LINE_RE.match(s)
    if m:
        return LogLine(number, s, m.group("cat"), m.group("verb") or "", m.group("msg"))
    return LogLine(number, s, "", "", s)


def read_log(path: Path, start: int = 0) -> tuple[list[str], int]:
    """Read a log from a byte offset. Returns (lines, first line number)."""
    data = path.read_bytes()
    if start > len(data):  # log was rotated or truncated
        start = 0
    first_line = data.count(b"\n", 0, start) + 1
    # Editor logs are UTF-8 on current engines, UTF-16 on some older ones.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
        return text.splitlines(), 1
    return data[start:].decode("utf-8", errors="replace").splitlines(), first_line


def scan_log_lines(lines: list[str], logname: str, first_line: int = 1,
                   max_per_id: int = 25) -> list[Finding]:
    out: list[Finding] = []
    counts: dict[str, int] = {}
    suppressed: dict[str, int] = {}
    parsed = [parse_log_line(ln, first_line + i) for i, ln in enumerate(lines)]
    i = 0
    while i < len(parsed):
        pl = parsed[i]
        if pl is None:
            i += 1
            continue
        # Python tracebacks span several LogPython lines: fold them into one finding.
        if pl.category == "LogPython" and ("Traceback (most recent call last)" in pl.message
                                           or (pl.verbosity == "Error")):
            block = [pl]
            j = i + 1
            while j < len(parsed) and parsed[j] is not None and parsed[j].category == "LogPython" \
                    and parsed[j].verbosity == "Error" and len(block) < 40 \
                    and not parsed[j].message.startswith("Traceback"):
                block.append(parsed[j])
                j += 1
            last = next((b.message for b in reversed(block) if re.search(r"\w+(Error|Exception)\b", b.message)),
                        block[-1].message)
            _emit(out, counts, suppressed, max_per_id, Finding(
                "unreal.log.python_error", "error", f"Python error: {last.strip()[:200]}",
                file=logname, line=pl.number, evidence="\n".join(b.text for b in block[-8:])))
            i = j
            continue
        for fid, sev, rx in LOG_PATTERNS:
            if rx.search(pl.text):
                if fid == "unreal.log.error" and pl.verbosity != "Error":
                    break
                if fid == "unreal.log.readonly" and pl.category in ("LogInit", "LogConfig"):
                    break  # startup notes about read-only config are not asset failures
                _emit(out, counts, suppressed, max_per_id, Finding(
                    fid, sev, pl.message.strip()[:240] or pl.text[:240], file=logname, line=pl.number,
                    evidence=pl.text[:400]))
                break
        i += 1
    for fid, n in suppressed.items():
        out.append(Finding(fid, "info", f"{n} more line(s) of this kind not shown", file=logname))
    return out


def _emit(out: list[Finding], counts: dict[str, int], suppressed: dict[str, int], cap: int,
          f: Finding) -> None:
    counts[f.id] = counts.get(f.id, 0) + 1
    if counts[f.id] > cap:
        suppressed[f.id] = suppressed.get(f.id, 0) + 1
        return
    out.append(f)


def asset_refs(text: str) -> list[str]:
    """Relative Content file candidates for asset references in a log line."""
    out = []
    for m in ASSET_RE.finditer(text):
        if m.group(1):
            pkg = m.group(1).rstrip(".'\",")
            last = pkg.rsplit("/", 1)[-1]
            if "." in last:
                pkg = pkg[: len(pkg) - len(last)] + last.split(".", 1)[0]
            rel = "Content/" + pkg[len("/Game/"):]
            out += [rel + ".uasset", rel + ".umap"]
        else:
            path = m.group(2).replace("\\", "/")
            idx = path.find("Content/")
            out.append(path[idx:])
    return out


def enrich_with_readonly(findings: list[Finding], root: Path) -> None:
    """Add evidence when a log finding refers to an asset that is read-only on disk."""
    for f in findings:
        if not f.evidence or not f.id.startswith("unreal.log."):
            continue
        for rel in asset_refs(f.evidence):
            p = root / rel
            try:
                if p.is_file() and is_readonly(p.stat()):
                    f.evidence += f"\n-> {rel} is read-only on disk (locked or not checked out)"
                    if f.severity != "error":
                        f.severity = "error"
                    break
            except OSError:
                continue


def log_files(root: Path, explicit: list[str] | None, all_logs: bool) -> list[Path]:
    if explicit:
        return [Path(p) if Path(p).is_absolute() else root / p for p in explicit]
    logs = root / "Saved" / "Logs"
    if not logs.is_dir():
        return []
    found = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found if all_logs else found[:1]


def log_findings(root: Path, logs: list[Path], offsets: dict[str, int] | None = None) -> list[Finding]:
    out: list[Finding] = []
    for p in logs:
        try:
            rel = to_rel(root, p.resolve())
        except ValueError:
            rel = p.name
        start = (offsets or {}).get(rel, 0)
        try:
            lines, first = read_log(p, start)
        except OSError as exc:
            out.append(Finding("unreal.log.unreadable", "warning", f"cannot read log: {exc}", file=rel))
            continue
        out.extend(scan_log_lines(lines, rel, first))
        for ln in lines:
            pl = parse_log_line(ln, 0)
            if pl is None:
                continue
            m = CULTURE_LOG_RE.search(pl.text)
            if m and not m.group(1).lower().startswith("en"):
                out.append(_localized(m.group(1), rel, pl.text))
                break
    enrich_with_readonly(out, root)
    return out


def _localized(culture: str, file: str, evidence: str) -> Finding:
    return Finding(
        "unreal.localized_editor", "warning",
        f"editor runs with culture/language '{culture}'. Python scripts that look up nodes, pins, "
        "menus or categories by display name can silently find nothing; use internal names",
        file=file, evidence=evidence[:300])


def editor_language_findings(root: Path) -> list[Finding]:
    out = []
    candidates = list((root / "Config").glob("*.ini")) if (root / "Config").is_dir() else []
    saved_cfg = root / "Saved" / "Config"
    if saved_cfg.is_dir():
        candidates += sorted(saved_cfg.rglob("*.ini"))
    for ini in candidates:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            cp.read_string(ini.read_text(encoding="utf-8-sig", errors="replace"))
        except (configparser.Error, OSError):
            continue
        for sect in cp.sections():
            if sect.strip().lower() != "internationalization":
                continue
            for key in ("culture", "language", "locale"):
                val = cp.get(sect, key, fallback="").strip().strip('"')
                if val and not val.lower().startswith("en"):
                    out.append(_localized(val, to_rel(root, ini), f"[{sect}] {key}={val}"))
                    break
    return out


def gitattributes_lockable(root: Path) -> list[str]:
    ga = root / ".gitattributes"
    if not ga.is_file():
        return []
    pats = []
    for ln in ga.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = ln.split()
        if len(parts) >= 2 and not parts[0].startswith("#") and "lockable" in parts[1:]:
            pats.append(parts[0])
    return pats


def readonly_asset_summary(root: Path) -> list[Finding]:
    ro = []
    for p in iter_files(root, (".uasset", ".umap")):
        try:
            if is_readonly(p.stat()):
                ro.append(to_rel(root, p))
        except OSError:
            continue
    out = []
    lockable = gitattributes_lockable(root)
    if ro:
        ev = "\n".join(ro[:10]) + (f"\n... and {len(ro) - 10} more" if len(ro) > 10 else "")
        if lockable:
            ev += "\n.gitattributes marks as lockable: " + " ".join(lockable[:8])
        out.append(Finding(
            "unreal.readonly_assets", "info",
            f"{len(ro)} asset file(s) are read-only; the editor may drop saves to them without an "
            "error the agent sees. Lock or check them out before changing them",
            evidence=ev))
    return out


def probe_findings(result: dict, source: str = "probe") -> list[Finding]:
    """Findings from the JSON printed by unreal_probe.py inside the editor."""
    from .unreal_probe import assess

    out: list[Finding] = []
    for rec in result.get("assets", []):
        problems = rec.get("problems")
        if problems is None:
            problems = assess(rec)
        target = rec.get("file") or rec.get("package") or rec.get("asset")
        ev = ", ".join(f"{k}={rec.get(k)}" for k in
                       ("exists_in_registry", "on_disk", "readonly", "dirty", "blueprint_status")
                       if k in rec)
        if not problems:
            out.append(Finding("probe.ok", "info", f"{rec.get('package') or rec.get('asset')}: no problem",
                               file=target, evidence=ev))
        for p in problems:
            out.append(Finding(p["id"], p["severity"], p["message"], file=target, evidence=ev))
    culture = result.get("culture") or result.get("language")
    if culture and not str(culture).lower().startswith("en"):
        out.append(_localized(str(culture), source, f"culture={result.get('culture')} "
                                                    f"language={result.get('language')}"))
    return out
