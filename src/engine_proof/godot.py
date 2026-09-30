"""Godot checks: static scans plus optional headless runs of the real engine."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .findings import Finding
from .project import GODOT_EXTS, godot_ignored_dirs, iter_files, to_rel, under_any
from .textchecks import analyze_bytes, findings_for

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CLASS_NAME_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\([^)\n]*\))?[ \t]+)*class_name[ \t]+([A-Za-z_]\w*)", re.MULTILINE
)
RES_PATH_RE = re.compile(r"res://([^\s'\"()]+?)(?::(\d+))?[.,;]?(?=[\s'\"()]|$)")
BACKUP_WORDS = {"backup", "backups", "bak", "old", "copy", "archive", "orig", "kopie", "sicherung", "alt"}


def looks_like_backup(rel: str) -> bool:
    """True if a folder or the file name hints at a backup copy (backup/, old_scripts/, x.gd.bak...)."""
    parts = rel.lower().split("/")
    for i, part in enumerate(parts):
        words = set(re.split(r"[^a-z0-9]+", part))
        if words & BACKUP_WORDS or part.endswith("~"):
            return True
        if i == len(parts) - 1 and re.search(r"(?:\s|_|-)?(?:\(\d+\)|copy|kopie)\.gd$", part):
            return True
    return False

# --------------------------------------------------------------------------- executable


def find_godot(explicit: str | None = None) -> str | None:
    """Resolve the Godot executable from --godot, then the GODOT environment variable.

    A directory is accepted too; the Godot binary inside it is picked.
    """
    cand = explicit or os.environ.get("GODOT")
    if not cand:
        return None
    p = Path(cand)
    if p.is_dir():
        bins = [c for c in sorted(p.iterdir())
                if c.is_file() and c.name.lower().startswith("godot")
                and (c.suffix.lower() == ".exe" or os.access(c, os.X_OK))]
        # Prefer the real binary over the Windows console wrapper.
        bins.sort(key=lambda c: ("console" in c.name.lower(), c.name))
        return str(bins[0]) if bins else None
    if p.is_file():
        return str(p)
    found = shutil.which(cand)
    return found


# --------------------------------------------------------------------------- static checks


def scan_class_names(root: Path) -> dict[str, list[str]]:
    """Map class_name -> list of res-relative files that declare it (Godot-visible only)."""
    ignored = godot_ignored_dirs(root)
    decls: dict[str, list[str]] = {}
    for p in iter_files(root, (".gd",)):
        rel = to_rel(root, p)
        if under_any(rel, ignored):
            continue
        try:
            text = p.read_bytes().decode("utf-8-sig", errors="replace")
        except OSError:
            continue
        for m in CLASS_NAME_RE.finditer(text):
            decls.setdefault(m.group(1), []).append(rel)
    return decls


def registered_classes(root: Path) -> dict[str, str]:
    """Read .godot/global_script_class_cache.cfg: class -> registered res path."""
    cache = root / ".godot" / "global_script_class_cache.cfg"
    out: dict[str, str] = {}
    if not cache.is_file():
        return out
    text = cache.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r'"class":\s*&?"([^"]+)".*?"path":\s*"([^"]+)"', text, re.DOTALL):
        out[m.group(1)] = m.group(2)
    return out


def class_name_of(text: str) -> str | None:
    m = CLASS_NAME_RE.search(text)
    return m.group(1) if m else None


def preexisting_duplicates(snapshot: dict, root: Path) -> set[str]:
    """class_names that were already declared more than once when the snapshot was taken."""
    ignored = godot_ignored_dirs(root)
    seen: dict[str, int] = {}
    for rel, rec in snapshot.get("files", {}).items():
        name = rec.get("class_name")
        if name and not under_any(rel, ignored):
            seen[name] = seen.get(name, 0) + 1
    return {n for n, c in seen.items() if c > 1}


def duplicate_class_findings(root: Path, preexisting: set[str] | None = None) -> list[Finding]:
    out = []
    reg = registered_classes(root)
    for name, files in sorted(scan_class_names(root).items()):
        if len(files) < 2:
            continue
        pre = preexisting is not None and name in preexisting
        lines = [f"res://{f}" + ("  (looks like a backup copy)" if looks_like_backup(f) else "")
                 for f in files]
        if name in reg:
            lines.append(f"registered in class cache: {reg[name]}; the others are shadowed")
        lines.append("fix: delete or rename the copies, or put a .gdignore file into the backup folder")
        for f in files:
            out.append(Finding(
                "godot.duplicate_class_name", "info" if pre else "error",
                f'class_name "{name}" is declared in {len(files)} scripts; Godot registers only one '
                "and fails on the others" + (" [already present in snapshot]" if pre else ""),
                file=f, evidence="\n".join(lines),
            ))
    return out


def text_findings(root: Path) -> list[Finding]:
    ignored = godot_ignored_dirs(root)
    out = []
    for p in iter_files(root, GODOT_EXTS):
        rel = to_rel(root, p)
        if under_any(rel, ignored):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        out.extend(findings_for(rel, analyze_bytes(data)))
    return out


# --------------------------------------------------------------------------- output parsing

_HEADS = (
    ("SCRIPT ERROR:", "error"),
    ("USER SCRIPT ERROR:", "error"),
    ("USER ERROR:", "error"),
    ("ERROR:", "error"),
    ("SCRIPT WARNING:", "warning"),
    ("USER WARNING:", "warning"),
    ("WARNING:", "warning"),
)
_FOLLOW_UP = ("godot.load_failed",)


def _res_to_rel(s: str) -> str:
    return s[len("res://"):] if s.startswith("res://") else s


def _classify(msg: str, head: str) -> str:
    low = msg.lower()
    if "hides a global script class" in low:
        return "godot.class_name_conflict"
    if "parse error" in low and head.startswith("SCRIPT"):
        return "godot.parse_error"
    if "invalid unicode" in low:
        return "godot.invalid_unicode"
    if re.search(r"failed to load script .* with error", low) or low.startswith(
            ("failed loading resource", "error loading resource")):
        return "godot.load_failed"
    if "resource file not found" in low or "cannot open file" in low or "no loader found" in low \
            or "file not found" in low:
        return "godot.missing_resource"
    if head.startswith("SCRIPT"):
        return "godot.script_error" if "ERROR" in head else "godot.script_warning"
    return "godot.error" if "ERROR" in head else "godot.warning"


def parse_godot_output(text: str, source: str = "godot") -> list[Finding]:
    """Parse Godot stdout/stderr into findings (engine error lines are not localized)."""
    lines = [ANSI_RE.sub("", ln).rstrip("\r") for ln in text.splitlines()]
    raw: list[Finding] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        head = next((h for h, _ in _HEADS if ln.startswith(h)), None)
        if head is None:
            i += 1
            continue
        sev = dict(_HEADS)[head]
        msg = ln[len(head):].strip()
        at = ""
        j = i + 1
        while j < len(lines) and lines[j].startswith((" ", "\t")) and not lines[j].strip().startswith(
                tuple(h for h, _ in _HEADS)):
            s = lines[j].strip()
            if s.startswith("at:") and not at:
                at = s[3:].strip()
            j += 1
        fid = _classify(msg, head)
        file = line_no = None
        # Prefer the location in the message (missing resources), then the "at:" location.
        for src in (msg, at):
            m = RES_PATH_RE.search(src)
            if m:
                file = m.group(1)
                line_no = int(m.group(2)) if m.group(2) else None
                break
        if fid == "godot.warning" and file is None and not at:
            sev = "info"
        evidence = ln + (f"\n  at: {at}" if at else "")
        raw.append(Finding(fid, sev, f"{source}: {msg}", file=file, line=line_no, evidence=evidence))
        i = j
    # Drop duplicates and generic follow-up errors for files that already have a finding.
    seen: set[tuple] = set()
    strong_files = {f.file for f in raw if f.file and f.id not in _FOLLOW_UP and f.severity == "error"}
    out: list[Finding] = []
    for f in raw:
        if f.id in _FOLLOW_UP and f.file in strong_files:
            continue
        key = (f.id, f.file, f.line, f.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def parse_checker_markers(text: str) -> dict[str, str]:
    """Markers printed by godot_check.gd: res path -> OK / LOAD_FAIL / CANNOT_INSTANTIATE."""
    out = {}
    for ln in text.splitlines():
        ln = ANSI_RE.sub("", ln).strip()
        for tag in ("EP_OK ", "EP_LOAD_FAIL ", "EP_CANNOT_INSTANTIATE "):
            if ln.startswith(tag):
                out[ln[len(tag):].strip()] = tag.strip()[3:]
    return out


# --------------------------------------------------------------------------- running Godot


@dataclass
class RunResult:
    step: str
    args: list[str]
    exit_code: int | None
    seconds: float
    output: str
    timed_out: bool = False

    def summary(self) -> dict:
        return {"step": self.step, "exit_code": self.exit_code,
                "seconds": round(self.seconds, 2), "timed_out": self.timed_out}


def run_godot(exe: str, args: list[str], step: str, timeout: float) -> RunResult:
    t0 = time.monotonic()
    try:
        proc = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
        out = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
        return RunResult(step, args, proc.returncode, time.monotonic() - t0,
                         out.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + b"\n" + (exc.stderr or b"")
        return RunResult(step, args, None, time.monotonic() - t0,
                         out.decode("utf-8", errors="replace"), timed_out=True)


def checker_script_path() -> Path:
    return Path(str(resources.files("engine_proof").joinpath("godot_check.gd")))


def _chunks(items: list[str], max_chars: int = 12000) -> list[list[str]]:
    out, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it) + 3 > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it) + 3
    if cur:
        out.append(cur)
    return out


@dataclass
class GodotRunOptions:
    quit_after: int = 30
    timeout: float = 180.0
    boot: bool = True
    load_scripts: bool = True
    load_resources: bool = True
    only: list[str] | None = None          # restrict loader check to these relative paths
    test_scene: str | None = None
    test_timeout: float = 300.0
    import_first: str = "auto"              # auto | always | never
    runs: list[RunResult] = field(default_factory=list)


def loadable_files(root: Path, opts: GodotRunOptions) -> list[str]:
    ignored = godot_ignored_dirs(root)
    exts: tuple[str, ...] = ()
    if opts.load_scripts:
        exts += (".gd",)
    if opts.load_resources:
        exts += (".tscn", ".tres")
    if not exts:
        return []
    rels = [to_rel(root, p) for p in iter_files(root, exts)]
    rels = [r for r in rels if not under_any(r, ignored)]
    if opts.only is not None:
        wanted = set(opts.only)
        rels = [r for r in rels if r in wanted]
    return rels


def class_cache_stale(root: Path) -> bool:
    """True if the global class cache is missing or older than some script."""
    cache = root / ".godot" / "global_script_class_cache.cfg"
    try:
        cache_mtime = cache.stat().st_mtime_ns
    except OSError:
        return True
    for p in iter_files(root, (".gd",)):
        try:
            if p.stat().st_mtime_ns > cache_mtime:
                return True
        except OSError:
            continue
    return False


def engine_findings(root: Path, exe: str, opts: GodotRunOptions) -> list[Finding]:
    out: list[Finding] = []
    root = root.resolve()
    if opts.import_first == "always" or (opts.import_first == "auto" and class_cache_stale(root)):
        r = run_godot(exe, ["--headless", "--path", str(root), "--import"], "import", opts.timeout)
        opts.runs.append(r)
        out.extend(parse_godot_output(r.output, "import"))
        if r.timed_out:
            out.append(Finding("godot.timeout", "error", f"import did not finish within {opts.timeout}s"))

    if opts.boot and not main_scene(root):
        # Without a main scene Godot prints "Can't run project" and does not exit.
        out.append(Finding("godot.boot_skipped", "info",
                           "project has no run/main_scene; boot run skipped", file="project.godot"))
    elif opts.boot:
        r = run_godot(exe, ["--headless", "--path", str(root), "--quit-after", str(opts.quit_after)],
                      "boot", opts.timeout)
        opts.runs.append(r)
        out.extend(parse_godot_output(r.output, "boot"))
        if r.timed_out:
            out.append(Finding("godot.timeout", "error", f"boot run did not quit within {opts.timeout}s"))
        elif r.exit_code not in (0, None):
            out.append(Finding("godot.boot_failed", "error",
                               f"headless boot exited with code {r.exit_code}",
                               evidence=_tail(r.output)))

    files = loadable_files(root, opts)
    if files:
        script = str(checker_script_path())
        markers: dict[str, str] = {}
        for chunk in _chunks(["res://" + f for f in files]):
            r = run_godot(exe, ["--headless", "--path", str(root), "-s", script, "--", *chunk],
                          "load-check", opts.timeout)
            opts.runs.append(r)
            out.extend(parse_godot_output(r.output, "load-check"))
            markers.update(parse_checker_markers(r.output))
            if r.timed_out:
                out.append(Finding("godot.timeout", "error",
                                   f"load check did not finish within {opts.timeout}s"))
            elif "EP_DONE" not in r.output:
                out.append(Finding("godot.checker_failed", "error",
                                   "the loader check script did not complete", evidence=_tail(r.output)))
        flagged = {f.file for f in out if f.severity == "error" and f.file}
        for res, status in markers.items():
            rel = _res_to_rel(res)
            if status != "OK" and rel not in flagged:
                out.append(Finding("godot.load_failed", "error",
                                   f"Godot could not load this file ({status})", file=rel))

    if opts.test_scene:
        target = opts.test_scene
        args = ["--headless", "--path", str(root)]
        args += ["-s", target] if target.endswith(".gd") else [target]
        r = run_godot(exe, args, "test", opts.test_timeout)
        opts.runs.append(r)
        test_file = _res_to_rel(target)
        found = parse_godot_output(r.output, "test")
        out.extend(found)
        if r.timed_out:
            out.append(Finding("godot.test_timeout", "error",
                               f"test did not quit within {opts.test_timeout}s "
                               "(does it call get_tree().quit(code)?)", file=test_file,
                               evidence=_tail(r.output)))
        elif r.exit_code != 0:
            out.append(Finding("godot.test_failed", "error", f"test exited with code {r.exit_code}",
                               file=test_file, evidence=_tail(r.output)))
        else:
            errs = sum(1 for f in found if f.severity == "error")
            out.append(Finding("godot.test_passed", "info" if not errs else "warning",
                               "test exited with code 0" + (f" but printed {errs} error(s)" if errs else ""),
                               file=test_file))
    return merge_findings(out)


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse the same message reported by several runs (import, boot, load-check)."""
    strong = {f.file for f in findings if f.file and f.severity == "error" and f.id not in _FOLLOW_UP}
    seen: set[tuple] = set()
    out = []
    for f in findings:
        if f.id in _FOLLOW_UP and f.file in strong:
            continue
        msg = f.message.split(": ", 1)[1] if f.message.split(": ", 1)[0] in _SOURCES else f.message
        key = (f.id, f.file, f.line, msg)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


_SOURCES = ("import", "boot", "load-check", "test", "godot")


def main_scene(root: Path) -> str | None:
    try:
        text = (root / "project.godot").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*run/main_scene\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m and m.group(1) else None


def _tail(text: str, n: int = 12) -> str:
    lines = [ANSI_RE.sub("", ln) for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def check_project(root: Path, exe: str | None, opts: GodotRunOptions | None = None,
                  run_engine: bool = True) -> list[Finding]:
    root = root.resolve()
    opts = opts or GodotRunOptions()
    out: list[Finding] = []
    if not (root / "project.godot").is_file():
        out.append(Finding("godot.no_project", "error", "project.godot not found", file="project.godot"))
        return out
    out.extend(duplicate_class_findings(root))
    out.extend(text_findings(root))
    if not run_engine:
        out.append(Finding("godot.not_run", "info", "engine run skipped (--no-run)"))
    elif exe is None:
        out.append(Finding("godot.not_run", "info",
                           "Godot executable not configured (use --godot PATH or env GODOT); "
                           "only static checks ran"))
    else:
        out.extend(engine_findings(root, exe, opts))
    return out
