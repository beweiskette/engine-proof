"""Snapshots of project files and the diff between a snapshot and the disk."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .project import detect_engine, extensions_for, is_binary_ext, iter_files, to_rel
from .textchecks import analyze_bytes

SCHEMA = 1
# Text analysis is skipped for very large files to keep snapshots fast.
TEXT_ANALYSIS_LIMIT = 8 * 1024 * 1024
# Files changed less than this long before the previous snapshot are always re-hashed.
RACY_WINDOW_NS = 3_000_000_000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_readonly(st: os.stat_result) -> bool:
    # Mode bits instead of os.access so that the answer is the same for root.
    return not (st.st_mode & stat.S_IWUSR)


def file_record(path: Path, rel: str, previous: dict | None = None, hash_files: bool = True,
                trust_before_ns: int | None = None) -> dict:
    st = path.stat()
    rec: dict = {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "readonly": is_readonly(st),
    }
    reuse = (
        previous is not None
        and previous.get("size") == st.st_size
        and previous.get("mtime_ns") == st.st_mtime_ns
        # "Racy" files: modified shortly before the previous snapshot, a same-size
        # rewrite in the same timestamp tick would be invisible. Hash them again.
        and (trust_before_ns is None or st.st_mtime_ns < trust_before_ns)
    )
    if reuse:
        # Same size and mtime: trust the stored hash and text summary (like git's stat cache).
        for key in ("sha256", "text", "class_name"):
            if key in previous:
                rec[key] = previous[key]
        if "sha256" in rec or not hash_files:
            return rec
    need_text = not is_binary_ext(rel) and st.st_size <= TEXT_ANALYSIS_LIMIT
    data: bytes | None = None
    if need_text and "text" not in rec:
        data = path.read_bytes()
        rec["text"] = analyze_bytes(data).summary()
        if rel.lower().endswith(".gd"):
            from .godot import class_name_of
            name = class_name_of(data.decode("utf-8-sig", errors="replace"))
            if name:
                rec["class_name"] = name
    if hash_files and "sha256" not in rec:
        rec["sha256"] = hashlib.sha256(data).hexdigest() if data is not None else _sha256(path)
    return rec


def log_offsets(root: Path) -> dict[str, int]:
    """Sizes of Unreal editor logs, so later runs read only the new part."""
    logs = root / "Saved" / "Logs"
    out: dict[str, int] = {}
    if logs.is_dir():
        for p in sorted(logs.glob("*.log")):
            try:
                out[to_rel(root, p)] = p.stat().st_size
            except OSError:
                pass
    return out


def take_snapshot(root: Path, engine: str | None = None, extra_exts: list[str] | None = None,
                  previous: dict | None = None, hash_files: bool = True,
                  exts: tuple[str, ...] | None = None) -> dict:
    root = root.resolve()
    engine = engine or detect_engine(root)
    if exts is None:
        exts = extensions_for(engine, extra_exts)
    started_ns = time.time_ns()
    prev_files = (previous or {}).get("files", {})
    prev_start = (previous or {}).get("started_ns")
    trust_before = prev_start - RACY_WINDOW_NS if prev_start else 0
    files: dict[str, dict] = {}
    for p in iter_files(root, exts):
        rel = to_rel(root, p)
        try:
            files[rel] = file_record(p, rel, prev_files.get(rel), hash_files, trust_before)
        except OSError:
            continue
    snap = {
        "tool": "engine-proof",
        "schema": SCHEMA,
        "version": __version__,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "started_ns": started_ns,
        "root": str(root),
        "engine": engine,
        "extensions": list(exts),
        "hashed": hash_files,
        "files": files,
    }
    if engine in ("unreal", "generic"):
        snap["logs"] = log_offsets(root)
    return snap


def save_snapshot(snap: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, out)


def load_snapshot(path: Path) -> dict:
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    if snap.get("tool") != "engine-proof" or "files" not in snap:
        raise ValueError(f"{path} is not an engine-proof snapshot")
    if snap.get("schema", 0) > SCHEMA:
        raise ValueError(f"{path} uses snapshot schema {snap['schema']}, this version reads {SCHEMA}")
    return snap


@dataclass
class Diff:
    changed: list[str] = field(default_factory=list)    # content differs
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)  # mtime changed, content identical
    unchanged: list[str] = field(default_factory=list)
    readonly_flipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "changed": self.changed,
            "created": self.created,
            "deleted": self.deleted,
            "rewritten": self.rewritten,
        }

    @property
    def touched(self) -> list[str]:
        return sorted(set(self.changed) | set(self.created) | set(self.rewritten))


def diff(before: dict, after: dict) -> Diff:
    d = Diff()
    b, a = before["files"], after["files"]
    for rel in sorted(set(b) | set(a)):
        if rel not in a:
            d.deleted.append(rel)
            continue
        if rel not in b:
            d.created.append(rel)
            continue
        rb, ra = b[rel], a[rel]
        if rb.get("readonly") != ra.get("readonly"):
            d.readonly_flipped.append(rel)
        if "sha256" in rb and "sha256" in ra:
            same_content = rb["sha256"] == ra["sha256"]
        else:
            same_content = rb["size"] == ra["size"] and rb["mtime_ns"] == ra["mtime_ns"]
        if not same_content:
            d.changed.append(rel)
        elif rb["mtime_ns"] != ra["mtime_ns"]:
            d.rewritten.append(rel)
        else:
            d.unchanged.append(rel)
    return d
