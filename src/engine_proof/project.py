"""Project detection and file discovery."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

GODOT_EXTS = (".gd", ".tscn", ".tres", ".godot", ".gdshader")
UNREAL_EXTS = (".uasset", ".umap", ".ini", ".cpp", ".h", ".py", ".uproject", ".uplugin")
BINARY_EXTS = (".uasset", ".umap")

# Directory names skipped at any depth: engine caches and tool folders.
IGNORED_DIRS = frozenset({
    ".godot", ".import", "Intermediate", "Saved", "DerivedDataCache", "Binaries",
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".vs", ".idea", ".engine-proof",
})


def detect_engine(root: Path) -> str:
    if (root / "project.godot").is_file():
        return "godot"
    if any(root.glob("*.uproject")):
        return "unreal"
    return "generic"


def extensions_for(engine: str, extra: list[str] | None = None) -> tuple[str, ...]:
    if engine == "godot":
        exts = list(GODOT_EXTS)
    elif engine == "unreal":
        exts = list(UNREAL_EXTS)
    else:
        exts = list(dict.fromkeys(GODOT_EXTS + UNREAL_EXTS))
    for e in extra or []:
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        if e not in exts:
            exts.append(e)
    return tuple(exts)


def is_binary_ext(path: str) -> bool:
    return path.lower().endswith(BINARY_EXTS)


def to_rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def iter_files(root: Path, exts: tuple[str, ...] | None) -> Iterator[Path]:
    """Yield files below root with one of the extensions (all files if exts is None)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            if exts is None or name.lower().endswith(exts):
                yield Path(dirpath) / name


def godot_ignored_dirs(root: Path) -> set[str]:
    """Relative directories that contain a .gdignore file (Godot skips them)."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        if ".gdignore" in filenames:
            rel = Path(dirpath).relative_to(root).as_posix()
            if rel != ".":
                out.add(rel)
            dirnames[:] = []
    return out


def under_any(rel: str, dirs: set[str]) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in dirs)
