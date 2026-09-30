import json
import os
import stat

from engine_proof import godot
from engine_proof.cli import main

from conftest import write

# Output captured from a real Godot 4.x headless run, with the local checker path
# replaced by a synthetic one and progress lines shortened.
REAL_OUTPUT = """Godot Engine v4.7.1.stable.official - https://godotengine.org

\x1b[90m[   0% ] first_scan_filesystem | Started\x1b[0m
EP_OK res://scripts/player.gd
SCRIPT ERROR: Parse Error: Expected expression for variable initial value after "=".
   at: GDScript::reload (res://scripts/broken.gd:4)
   GDScript backtrace (most recent call first):
       [0] _init (/tmp/ep/godot_check.gd:5)
ERROR: Failed to load script "res://scripts/broken.gd" with error "Parse error".
   at: load (modules/gdscript/gdscript_resource_format.cpp:46)
EP_CANNOT_INSTANTIATE res://scripts/broken.gd
SCRIPT ERROR: Parse Error: Class "Player" hides a global script class.
   at: GDScript::reload (res://backup/player.gd:2)
ERROR: Failed to load script "res://backup/player.gd" with error "Parse error".
   at: load (modules/gdscript/gdscript_resource_format.cpp:46)
EP_CANNOT_INSTANTIATE res://backup/player.gd
Unicode parsing error, some characters were replaced with (U+FFFD): Invalid UTF-8 leading byte (fc)
ERROR: Script 'res://scripts/latin1.gd' contains invalid unicode (UTF-8), so it was not loaded. Please ensure that scripts are saved in valid UTF-8 unicode.
   at: load_source_code (modules/gdscript/gdscript.cpp:1151)
ERROR: Failed loading resource: res://scripts/latin1.gd.
   at: _load (core/io/resource_loader.cpp:317)
EP_LOAD_FAIL res://scripts/latin1.gd
ERROR: Resource file not found: res://art/missing.png (expected type: Texture2D)
   at: _load (core/io/resource_loader.cpp:325)
EP_OK res://main.tscn
WARNING: Some warning without location.
EP_DONE
"""


def by_id(findings):
    out = {}
    for f in findings:
        out.setdefault(f.id, []).append(f)
    return out


def test_parse_real_output():
    fs = by_id(godot.parse_godot_output(REAL_OUTPUT, "load-check"))
    pe = fs["godot.parse_error"][0]
    assert (pe.file, pe.line, pe.severity) == ("scripts/broken.gd", 4, "error")
    cc = fs["godot.class_name_conflict"][0]
    assert (cc.file, cc.line) == ("backup/player.gd", 2)
    assert fs["godot.invalid_unicode"][0].file == "scripts/latin1.gd"
    assert fs["godot.missing_resource"][0].file == "art/missing.png"
    # follow-up "Failed to load script" lines are folded into the parse error
    assert "godot.load_failed" not in fs
    assert fs["godot.warning"][0].severity == "info"


def test_checker_markers():
    m = godot.parse_checker_markers(REAL_OUTPUT)
    assert m["res://scripts/player.gd"] == "OK"
    assert m["res://scripts/broken.gd"] == "CANNOT_INSTANTIATE"
    assert m["res://scripts/latin1.gd"] == "LOAD_FAIL"


def test_duplicate_class_name_includes_backup_but_not_gdignore(godot_project):
    decls = godot.scan_class_names(godot_project)
    assert sorted(decls["Player"]) == ["backup/player.gd", "scripts/player.gd"]
    fs = godot.duplicate_class_findings(godot_project)
    assert {f.file for f in fs} == {"backup/player.gd", "scripts/player.gd"}
    assert all(f.severity == "error" for f in fs)
    assert "looks like a backup copy" in fs[0].evidence


def test_class_name_regex_variants():
    assert godot.class_name_of("@tool\nclass_name Foo extends Node\n") == "Foo"
    assert godot.class_name_of('@icon("res://i.svg") class_name Bar\n') == "Bar"
    assert godot.class_name_of("# class_name Nope\nextends Node\n") is None


def test_registered_class_cache(godot_project):
    write(godot_project, ".godot/global_script_class_cache.cfg",
          b'list=[{\n"base": &"Node",\n"class": &"Player",\n"icon": "",\n"path": "res://scripts/player.gd"\n}]\n')
    assert godot.registered_classes(godot_project) == {"Player": "res://scripts/player.gd"}
    fs = godot.duplicate_class_findings(godot_project)
    assert "registered in class cache: res://scripts/player.gd" in fs[0].evidence


def test_static_godot_command(godot_project, capsys):
    code = main(["godot", str(godot_project), "--no-run", "--format", "json"])
    v = json.loads(capsys.readouterr().out)
    assert code == 1
    found = {(f["id"], f.get("file")) for f in v["findings"]}
    assert ("godot.duplicate_class_name", "backup/player.gd") in found
    assert ("text.mojibake", "scripts/moji.gd") in found
    assert ("text.mixed_eol", "scripts/mixed.gd") in found
    assert ("godot.not_run", None) in found
    # the .gdignore folder is invisible to Godot and to this check
    assert not any(f.get("file", "").startswith("ignored/") for f in v["findings"])


def test_static_clean_project_passes(clean_godot_project, capsys, monkeypatch):
    monkeypatch.delenv("GODOT", raising=False)
    assert main(["godot", str(clean_godot_project), "--format", "json"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["ok"] is True


def test_missing_project_godot(tmp_path, capsys):
    assert main(["godot", str(tmp_path), "--no-run"]) == 1
    assert "godot.no_project" in capsys.readouterr().out


def test_find_godot_accepts_folder(tmp_path, monkeypatch):
    folder = tmp_path / "godot_dist"
    folder.mkdir()
    for name in ("Godot_v4_win64_console.exe", "Godot_v4_win64.exe", "readme.txt"):
        p = folder / name
        p.write_bytes(b"")
        os.chmod(p, p.stat().st_mode | stat.S_IXUSR)
    assert godot.find_godot(str(folder)).endswith("Godot_v4_win64.exe")
    monkeypatch.setenv("GODOT", str(folder / "Godot_v4_win64.exe"))
    assert godot.find_godot(None).endswith("Godot_v4_win64.exe")
    monkeypatch.delenv("GODOT")
    assert godot.find_godot(None) is None


def test_engine_findings_with_fake_executable(godot_project, monkeypatch):
    """Drive engine_findings with a stubbed runner to test the orchestration."""
    calls = []

    def fake_run(exe, args, step, timeout):
        calls.append(step)
        if step == "load-check":
            return godot.RunResult(step, args, 1, 0.1, REAL_OUTPUT)
        if step == "test":
            return godot.RunResult(step, args, 3, 0.1, "test ran\n")
        return godot.RunResult(step, args, 0, 0.1, "Godot Engine\n")

    monkeypatch.setattr(godot, "run_godot", fake_run)
    opts = godot.GodotRunOptions(test_scene="res://tests/run_test.gd")
    fs = by_id(godot.engine_findings(godot_project, "godot", opts))
    assert calls == ["import", "boot", "load-check", "test"]
    assert fs["godot.test_failed"][0].message == "test exited with code 3"
    assert fs["godot.parse_error"][0].file == "scripts/broken.gd"


def test_chunks_split_long_argument_lists():
    items = [f"res://dir/file_{i:04d}.gd" for i in range(2000)]
    chunks = godot._chunks(items, max_chars=5000)
    assert sum(len(c) for c in chunks) == 2000
    assert all(sum(len(x) + 3 for x in c) <= 5000 for c in chunks)


def test_looks_like_backup():
    assert godot.looks_like_backup("backup/player.gd")
    assert godot.looks_like_backup("scripts/old_player/player.gd")
    assert godot.looks_like_backup("scripts/player (2).gd")
    assert godot.looks_like_backup("Sicherung 2026/player.gd")
    assert not godot.looks_like_backup("scripts/player.gd")
    assert not godot.looks_like_backup("salt/altitude/default.gd")
