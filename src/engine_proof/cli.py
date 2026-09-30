"""Command line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from . import godot as godot_mod
from . import unreal as unreal_mod
from .findings import Finding, Verdict
from .project import detect_engine
from .snapshot import diff, load_snapshot, save_snapshot, take_snapshot
from .verify import (evaluate, parse_expectation, text_findings_for_changes,
                     unexpected_changes)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def _root(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise SystemExit(f"engine-proof: project directory not found: {path}")
    return p


def _emit(verdict: Verdict, fmt: str, show_info: bool = True) -> int:
    text = verdict.to_json() if fmt == "json" else verdict.to_text(show_info=show_info)
    _print(text)
    return EXIT_OK if verdict.ok else EXIT_FAIL


def _print(text: str, stream=None) -> None:
    stream = stream or sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc), file=stream)


# --------------------------------------------------------------------------- snapshot


def cmd_snapshot(a: argparse.Namespace) -> int:
    root = _root(a.project)
    engine = None if a.engine == "auto" else a.engine
    previous = None
    if a.reuse and Path(a.out).is_file():
        try:
            previous = load_snapshot(Path(a.out))
        except (ValueError, OSError, json.JSONDecodeError):
            previous = None
    snap = take_snapshot(root, engine, a.ext, previous=previous, hash_files=not a.no_hash)
    save_snapshot(snap, Path(a.out))
    ro = sum(1 for r in snap["files"].values() if r.get("readonly"))
    msg = {"snapshot": str(Path(a.out)), "engine": snap["engine"], "files": len(snap["files"]),
           "readonly": ro, "logs": len(snap.get("logs", {}))}
    if a.format == "json":
        _print(json.dumps(msg, indent=2))
    else:
        _print(f"engine-proof snapshot: {msg['files']} file(s), {ro} read-only, engine {msg['engine']} "
               f"-> {msg['snapshot']}")
    return EXIT_OK


# --------------------------------------------------------------------------- verify core


def run_verify(root: Path, before: dict, expects: list[str], strict: bool,
               godot_exe: str | None = None, check_logs: bool = True) -> tuple[Verdict, dict]:
    engine = before.get("engine") or detect_engine(root)
    after = take_snapshot(root, engine, previous=before, hash_files=before.get("hashed", True),
                          exts=tuple(before.get("extensions") or ()) or None)
    d = diff(before, after)
    exps = [parse_expectation(e, root) for e in expects]
    v = Verdict("verify", str(root), engine, strict=strict)
    v.changes = d.as_dict()
    v.extend(evaluate(exps, before, after, d, root))
    v.extend(unexpected_changes(exps, d, strict))
    v.extend(text_findings_for_changes(root, d, before))

    touched = set(d.changed) | set(d.created)
    pre_dups: set[str] = set()
    if engine == "godot":
        pre_dups = godot_mod.preexisting_duplicates(before, root)
    if engine == "godot" and any(r.endswith(".gd") for r in touched):
        for f in godot_mod.duplicate_class_findings(root, pre_dups):
            if f.file in touched:
                v.add(f)
    if engine == "godot" and godot_exe:
        loadable = sorted(r for r in touched if r.endswith((".gd", ".tscn", ".tres")))
        if loadable:
            # A new, renamed or removed class_name makes the global class cache stale.
            classes_moved = any(
                before["files"].get(r, {}).get("class_name") != after["files"].get(r, {}).get("class_name")
                for r in touched | set(d.deleted) if r.endswith(".gd"))
            opts = godot_mod.GodotRunOptions(boot=False, only=loadable, timeout=120,
                                             import_first="always" if classes_moved else "auto")
            for f in godot_mod.engine_findings(root, godot_exe, opts):
                cls = after["files"].get(f.file or "", {}).get("class_name")
                if f.id == "godot.class_name_conflict" and cls in pre_dups:
                    f.severity = "info"
                    f.message += " [duplicate already present in snapshot]"
                elif f.file and f.file not in touched and f.severity == "error":
                    f.severity = "warning"
                    f.message += " [in a file this change did not touch]"
                v.add(f)
            v.extra["godot_runs"] = [r.summary() for r in opts.runs]
    if check_logs and engine in ("unreal", "generic"):
        offsets = before.get("logs", {})
        logs = [root / rel for rel in after.get("logs", {})]
        v.extend(unreal_mod.log_findings(root, logs, offsets))
    return v, after


def cmd_verify(a: argparse.Namespace) -> int:
    root = _root(a.project)
    try:
        before = load_snapshot(Path(a.since))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _print(f"engine-proof: cannot read snapshot: {exc}", sys.stderr)
        return EXIT_USAGE
    exe = godot_mod.find_godot(a.godot) if not a.no_engine else None
    v, after = run_verify(root, before, a.expect or [], a.strict, exe, check_logs=not a.no_logs)
    if a.update:
        save_snapshot(after, Path(a.since))
    return _emit(v, a.format, show_info=not a.no_info)


# --------------------------------------------------------------------------- godot


def cmd_godot(a: argparse.Namespace) -> int:
    root = _root(a.project)
    exe = godot_mod.find_godot(a.godot)
    if a.godot and exe is None:
        _print(f"engine-proof: Godot executable not found: {a.godot}", sys.stderr)
        return EXIT_USAGE
    opts = godot_mod.GodotRunOptions(
        quit_after=a.quit_after, timeout=a.timeout, boot=not a.no_boot,
        load_scripts=not a.no_load, load_resources=not a.no_load and not a.scripts_only,
        test_scene=a.test_scene, test_timeout=a.test_timeout, import_first=a.import_mode,
    )
    v = Verdict("godot", str(root), "godot", strict=a.strict)
    v.extend(godot_mod.check_project(root, exe, opts, run_engine=not a.no_run))
    if exe and not a.no_run:
        v.extra["godot"] = {"executable": Path(exe).name, "runs": [r.summary() for r in opts.runs]}
    return _emit(v, a.format, show_info=not a.no_info)


# --------------------------------------------------------------------------- unreal


def cmd_unreal(a: argparse.Namespace) -> int:
    root = _root(a.project)
    if a.since:
        try:
            before = load_snapshot(Path(a.since))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _print(f"engine-proof: cannot read snapshot: {exc}", sys.stderr)
            return EXIT_USAGE
        v, after = run_verify(root, before, a.expect or [], a.strict, None, check_logs=False)
        v.command = "unreal"
        offsets = before.get("logs", {})
        logs = unreal_mod.log_files(root, a.log, all_logs=True) if a.log else \
            [root / rel for rel in after.get("logs", {})]
        if a.update:
            save_snapshot(after, Path(a.since))
    else:
        if a.expect:
            _print("engine-proof: --expect needs --since SNAPSHOT", sys.stderr)
            return EXIT_USAGE
        v = Verdict("unreal", str(root), "unreal", strict=a.strict)
        offsets = {}
        logs = unreal_mod.log_files(root, a.log, a.all_logs)
    if not any(root.glob("*.uproject")):
        v.add(Finding("unreal.no_uproject", "warning", "no .uproject file in the project directory"))
    v.extend(unreal_mod.log_findings(root, logs, offsets))
    if not logs:
        v.add(Finding("unreal.no_logs", "info", "no editor log found under Saved/Logs"))
    else:
        v.extra["logs"] = [p.name for p in logs]
    v.extend(unreal_mod.editor_language_findings(root))
    v.extend(unreal_mod.readonly_asset_summary(root))
    for pr in a.probe_result or []:
        try:
            from .unreal_probe import extract_json
            result = extract_json(Path(pr).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            v.add(Finding("probe.unreadable", "error", f"cannot read probe result: {exc}", file=pr))
            continue
        v.extend(unreal_mod.probe_findings(result, source=Path(pr).name))
    return _emit(v, a.format, show_info=not a.no_info)


def cmd_probe_path(a: argparse.Namespace) -> int:
    from importlib import resources
    src = Path(str(resources.files("engine_proof").joinpath("unreal_probe.py")))
    if a.copy:
        dest = Path(a.copy)
        if dest.is_dir():
            dest = dest / "unreal_probe.py"
        dest.write_bytes(src.read_bytes())
        _print(str(dest))
    else:
        _print(str(src))
    return EXIT_OK


# --------------------------------------------------------------------------- hook


def find_project_root(start: Path) -> Path | None:
    p = start.resolve()
    if p.is_file():
        p = p.parent
    for cand in (p, *p.parents):
        if (cand / "project.godot").is_file() or any(cand.glob("*.uproject")):
            return cand
    return None


def default_state_path(root: Path) -> Path:
    key = hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "engine-proof" / f"{key}.json"


FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def cmd_hook(a: argparse.Namespace) -> int:
    payload = _read_payload()
    event = (a.event or payload.get("hook_event_name") or "PostToolUse").lower()
    tool = payload.get("tool_name", "")
    tinput = payload.get("tool_input") or {}
    file_path = tinput.get("file_path") or tinput.get("notebook_path")

    root: Path | None
    if a.project:
        root = Path(a.project).resolve()
    else:
        starts = [Path(file_path)] if file_path else []
        starts.append(Path(payload.get("cwd") or os.getcwd()))
        root = next((r for r in (find_project_root(s) for s in starts if s.exists() or s.parent.exists())
                     if r), None)
    if root is None or not root.is_dir():
        return EXIT_OK  # not an engine project: stay silent
    state = Path(a.state) if a.state else default_state_path(root)

    def refresh(previous: dict | None) -> dict:
        snap = take_snapshot(root, previous=previous)
        save_snapshot(snap, state)
        return snap

    previous = None
    if state.is_file():
        try:
            previous = load_snapshot(state)
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None

    if event in ("pretooluse", "pre", "sessionstart", "start"):
        refresh(previous)
        return EXIT_OK
    if previous is None:
        refresh(None)  # first call: create the baseline, nothing to compare yet
        return EXIT_OK

    expects = list(a.expect or [])
    if tool in FILE_TOOLS and file_path:
        try:
            rel = Path(file_path).resolve().relative_to(root).as_posix()
            if rel.lower().endswith(tuple(previous.get("extensions", []))):
                expects.append(f"{rel} touched")
        except ValueError:
            pass
    exe = godot_mod.find_godot(None) if a.godot_load else None
    v, after = run_verify(root, previous, expects, strict=False, godot_exe=exe)
    v.command = "hook"
    save_snapshot(after, state)
    # Plain changes are expected in a hook (the agent just acted); keep only real signals.
    v.findings = [f for f in v.findings if f.id not in ("verify.change", "verify.expectation_met",
                                                        "verify.unexpected_change")]
    c = v.counts()
    if c["error"]:
        _print(v.to_text(show_info=False), sys.stderr)
        return 2  # Claude Code shows stderr of exit code 2 to the model
    if c["warning"]:
        _print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                  "additionalContext": v.to_text(show_info=False)}}))
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="engine-proof",
        description="Verify that changes to Godot and Unreal projects really reached disk.")
    p.add_argument("--version", action="version", version=f"engine-proof {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, strict: bool = True) -> None:
        sp.add_argument("--format", choices=("text", "json"), default="text")
        sp.add_argument("--no-info", action="store_true", help="hide info findings in text output")
        if strict:
            sp.add_argument("--strict", action="store_true", help="warnings also fail (exit 1)")

    s = sub.add_parser("snapshot", help="record hashes, flags, encodings and line endings")
    s.add_argument("project")
    s.add_argument("--out", required=True, help="snapshot file to write (JSON)")
    s.add_argument("--engine", choices=("auto", "godot", "unreal", "generic"), default="auto")
    s.add_argument("--ext", action="append", help="extra file extension to track (repeatable)")
    s.add_argument("--no-hash", action="store_true", help="compare by size and mtime only (faster)")
    s.add_argument("--reuse", action="store_true",
                   help="reuse hashes from an existing --out file when size and mtime match")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("verify", help="compare the disk with a snapshot and check expectations")
    s.add_argument("project")
    s.add_argument("--since", required=True, help="snapshot file from 'engine-proof snapshot'")
    s.add_argument("--expect", action="append",
                   help='"PATH_OR_GLOB STATE", STATE one of changed, created, deleted, unchanged, '
                        'exists, written, touched (default changed). Repeatable.')
    s.add_argument("--update", action="store_true", help="write the new state back to --since")
    s.add_argument("--godot", help="Godot executable; loads changed scripts/scenes (env GODOT works too)")
    s.add_argument("--no-engine", action="store_true", help="never start an engine")
    s.add_argument("--no-logs", action="store_true", help="skip Unreal log scanning")
    common(s)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("godot", help="static checks plus optional headless Godot run")
    s.add_argument("project")
    s.add_argument("--godot", help="Godot executable or its folder (default: env GODOT)")
    s.add_argument("--no-run", action="store_true", help="static checks only")
    s.add_argument("--no-boot", action="store_true", help="skip the --quit-after boot run")
    s.add_argument("--no-load", action="store_true", help="skip loading every script and resource")
    s.add_argument("--scripts-only", action="store_true", help="load .gd files only, not .tscn/.tres")
    s.add_argument("--quit-after", type=int, default=30, help="frames for the boot run (default 30)")
    s.add_argument("--timeout", type=float, default=180.0, help="seconds per Godot run")
    s.add_argument("--import", dest="import_mode", choices=("auto", "always", "never"), default="auto",
                   help="run godot --import first (auto: when the class cache is missing or older than a script)")
    s.add_argument("--test-scene", help="res:// scene or .gd script to run as a test")
    s.add_argument("--test-timeout", type=float, default=300.0)
    common(s)
    s.set_defaults(func=cmd_godot)

    s = sub.add_parser("unreal", help="offline Unreal checks: logs, read-only assets, editor language")
    s.add_argument("project")
    s.add_argument("--since", help="snapshot; enables change detection and --expect")
    s.add_argument("--expect", action="append", help='as in verify; "/Game/Path/Asset" works too')
    s.add_argument("--update", action="store_true", help="write the new state back to --since")
    s.add_argument("--log", action="append", help="log file to scan (default: newest Saved/Logs/*.log)")
    s.add_argument("--all-logs", action="store_true", help="scan every Saved/Logs/*.log")
    s.add_argument("--probe-result", action="append",
                   help="JSON (or log text) printed by unreal_probe.py inside the editor")
    common(s)
    s.set_defaults(func=cmd_unreal)

    s = sub.add_parser("probe-path", help="print the path of unreal_probe.py (or copy it)")
    s.add_argument("--copy", help="copy the probe script to this file or folder")
    s.set_defaults(func=cmd_probe_path)

    s = sub.add_parser("hook", help="agent hook: snapshot before a tool call, verify after it")
    s.add_argument("--project", help="engine project (default: found from the payload's file or cwd)")
    s.add_argument("--state", help="state snapshot path (default: in the system temp folder)")
    s.add_argument("--event", help="override the hook event: pre or post")
    s.add_argument("--expect", action="append", help="extra expectations, as in verify")
    s.add_argument("--godot-load", action="store_true",
                   help="load changed Godot scripts with the executable from env GODOT")
    s.set_defaults(func=cmd_hook)
    return p


def _utf8_streams() -> None:
    # Findings quote non-ASCII text (mojibake); a cp1252 console would garble it again.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = build_parser()
    a = parser.parse_args(argv)
    return a.func(a)
