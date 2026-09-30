"""Runs the real Godot executable. Skipped unless env GODOT points to it."""

import json

import pytest

from engine_proof import godot
from engine_proof.cli import main

from conftest import write

EXE = godot.find_godot(None)
pytestmark = pytest.mark.skipif(EXE is None, reason="set GODOT to a Godot 4 executable to run")


def findings(capsys):
    v = json.loads(capsys.readouterr().out)
    return v, {(f["id"], f.get("file")) for f in v["findings"]}


def test_real_godot_reports_parse_error_duplicate_and_missing_resource(godot_project, capsys):
    code = main(["godot", str(godot_project), "--format", "json", "--timeout", "120"])
    v, found = findings(capsys)
    assert code == 1
    assert ("godot.parse_error", "scripts/broken.gd") in found
    # Godot itself only complains about the copy it did not register
    assert ("godot.class_name_conflict", "backup/player.gd") in found \
        or ("godot.class_name_conflict", "scripts/player.gd") in found
    assert ("godot.missing_resource", "art/missing.png") in found
    # mojibake and mixed EOL load fine in Godot; only the static check catches them
    assert ("text.mojibake", "scripts/moji.gd") in found
    assert ("text.mixed_eol", "scripts/mixed.gd") in found
    steps = [r["step"] for r in v["godot"]["runs"]]
    assert steps[:2] == ["import", "boot"] and "load-check" in steps


def test_real_godot_clean_project_passes(clean_godot_project, capsys):
    code = main(["godot", str(clean_godot_project), "--format", "json", "--no-boot"])
    v, found = findings(capsys)
    assert code == 0, v["findings"]


def test_real_godot_test_script_exit_code(clean_godot_project, capsys):
    write(clean_godot_project, "tests/run_test.gd",
          b"extends SceneTree\n\nfunc _init() -> void:\n\tprint(\"test ran\")\n\tquit(3)\n")
    code = main(["godot", str(clean_godot_project), "--format", "json", "--no-boot", "--no-load",
                 "--test-scene", "res://tests/run_test.gd", "--test-timeout", "60"])
    v, found = findings(capsys)
    assert code == 1
    f = next(f for f in v["findings"] if f["id"] == "godot.test_failed")
    assert f["message"] == "test exited with code 3"
    assert "test ran" in f["evidence"]


def test_real_godot_verify_loads_changed_script(tmp_path, clean_godot_project, capsys):
    snap = tmp_path / "s.json"
    main(["snapshot", str(clean_godot_project), "--out", str(snap)])
    capsys.readouterr()
    write(clean_godot_project, "scripts/player.gd", b"extends Node\nclass_name Player\n\nfunc f(:\n")
    code = main(["verify", str(clean_godot_project), "--since", str(snap), "--godot", EXE,
                 "--expect", "scripts/player.gd changed", "--format", "json"])
    v, found = findings(capsys)
    assert code == 1
    assert ("godot.parse_error", "scripts/player.gd") in found


def test_real_godot_verify_new_class_is_registered_before_loading(tmp_path, clean_godot_project, capsys):
    """A script using a class_name added in the same change must not fail on a stale class cache."""
    snap = tmp_path / "s.json"
    main(["godot", str(clean_godot_project), "--no-boot", "--format", "json"])  # creates .godot cache
    main(["snapshot", str(clean_godot_project), "--out", str(snap)])
    capsys.readouterr()
    write(clean_godot_project, "scripts/enemy.gd", b"extends Node\nclass_name Enemy\n\nvar hp := 3\n")
    write(clean_godot_project, "scripts/spawner.gd",
          b"extends Node\n\nfunc spawn() -> Enemy:\n\treturn Enemy.new()\n")
    code = main(["verify", str(clean_godot_project), "--since", str(snap), "--godot", EXE,
                 "--expect", "scripts/*.gd created", "--format", "json"])
    v, found = findings(capsys)
    assert code == 0, v["findings"]
    assert v["godot_runs"][0]["step"] == "import"
