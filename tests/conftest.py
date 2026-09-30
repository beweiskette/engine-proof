"""Synthetic fixture projects. Nothing here refers to real projects."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

GOOD_GD = b'extends Node\nclass_name Player\n\nfunc _ready() -> void:\n\tprint("ready")\n'
BACKUP_GD = b'extends Node\nclass_name Player\n\nfunc _ready() -> void:\n\tprint("old copy")\n'
BROKEN_GD = b"extends Node\n\nfunc _ready() -> void:\n\tvar x = \n\tprint(x\n"
# "Grün" saved as UTF-8, read back as cp1252 and saved again: "GrÃ¼n".
MOJIBAKE_GD = 'extends Node\n\nfunc _ready() -> void:\n\tprint("GrÃ¼n")\n'.encode("utf-8")
MIXED_GD = b'extends Node\r\n\nfunc _ready() -> void:\r\n\tprint("mixed")\r\n'
TEST_GD = b"extends SceneTree\n\nfunc _init() -> void:\n\tprint(\"test ran\")\n\tquit(3)\n"
PROJECT_GODOT = (
    b"config_version=5\n\n[application]\n\nconfig/name=\"Fixture\"\n"
    b"run/main_scene=\"res://main.tscn\"\n"
)
MAIN_TSCN = (
    b'[gd_scene load_steps=3 format=3]\n\n'
    b'[ext_resource type="Script" path="res://scripts/player.gd" id="1"]\n'
    b'[ext_resource type="Texture2D" path="res://art/missing.png" id="2"]\n\n'
    b'[node name="Main" type="Node"]\nscript = ExtResource("1")\n'
)


def write(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def set_readonly(p: Path, readonly: bool = True) -> None:
    mode = p.stat().st_mode
    if readonly:
        os.chmod(p, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    else:
        os.chmod(p, mode | stat.S_IWUSR)


def _unlock_tree(root: Path) -> None:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                set_readonly(Path(dirpath) / f, False)
            except OSError:
                pass


@pytest.fixture
def godot_project(tmp_path: Path) -> Path:
    root = tmp_path / "gproj"
    write(root, "project.godot", PROJECT_GODOT)
    write(root, "main.tscn", MAIN_TSCN)
    write(root, "scripts/player.gd", GOOD_GD)
    write(root, "scripts/broken.gd", BROKEN_GD)
    write(root, "scripts/moji.gd", MOJIBAKE_GD)
    write(root, "scripts/mixed.gd", MIXED_GD)
    write(root, "backup/player.gd", BACKUP_GD)
    write(root, "tests/run_test.gd", TEST_GD)
    # A folder with .gdignore is invisible to Godot, so its duplicate is harmless.
    write(root, "ignored/.gdignore", b"")
    write(root, "ignored/player.gd", BACKUP_GD)
    # Cache folder must be ignored by snapshots.
    write(root, ".godot/editor/cache.cfg", b"x")
    yield root
    _unlock_tree(root)


@pytest.fixture
def clean_godot_project(tmp_path: Path) -> Path:
    root = tmp_path / "clean"
    write(root, "project.godot", PROJECT_GODOT.replace(b'run/main_scene="res://main.tscn"\n', b""))
    write(root, "scripts/player.gd", GOOD_GD)
    yield root
    _unlock_tree(root)


UE_LOG_BEFORE = (
    "Log file open, 09/30/26 10:00:00\n"
    "LogInit: Display: Running engine for game: Fixture\n"
    "[2026.09.30-10.00.01:000][  0]LogInit: Display: Engine is initialized.\n"
)


@pytest.fixture
def unreal_project(tmp_path: Path) -> Path:
    root = tmp_path / "uproj"
    write(root, "Fixture.uproject", b'{"FileVersion": 3, "EngineAssociation": "5.4"}\n')
    write(root, "Content/Blueprints/BP_Door.uasset", b"\xc1\x83\x2a\x9e" + b"door-v1" * 20)
    write(root, "Content/Blueprints/BP_Lamp.uasset", b"\xc1\x83\x2a\x9e" + b"lamp-v1" * 20)
    write(root, "Content/Maps/Main.umap", b"\xc1\x83\x2a\x9e" + b"map-v1" * 20)
    write(root, "Config/DefaultEngine.ini", b"[/Script/EngineSettings.GameMapsSettings]\nGameDefaultMap=/Game/Maps/Main\n")
    write(root, "Source/Fixture/Fixture.cpp", b"#include \"Fixture.h\"\n")
    write(root, "Saved/Logs/Fixture.log", UE_LOG_BEFORE.encode("utf-8"))
    write(root, "Intermediate/Build/ignored.cpp", b"// cache\n")
    yield root
    _unlock_tree(root)
