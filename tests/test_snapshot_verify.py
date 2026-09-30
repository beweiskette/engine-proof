import json
import os
from pathlib import Path

import pytest

from engine_proof.cli import main
from engine_proof.snapshot import diff, load_snapshot, save_snapshot, take_snapshot
from engine_proof.verify import glob_to_regex, normalize_pattern, parse_expectation

from conftest import set_readonly, write


def bump_mtime(p: Path, seconds: int = 5) -> None:
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def run_json(argv, capsys):
    code = main(argv + ["--format", "json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def ids(verdict):
    return [(f["id"], f["severity"], f.get("file")) for f in verdict["findings"]]


# --------------------------------------------------------------------------- snapshot


def test_snapshot_records_metadata_and_ignores_caches(godot_project):
    set_readonly(godot_project / "scripts/player.gd")
    snap = take_snapshot(godot_project)
    files = snap["files"]
    assert snap["engine"] == "godot"
    assert "scripts/player.gd" in files and "project.godot" in files
    assert not any(k.startswith(".godot/") for k in files)
    rec = files["scripts/player.gd"]
    assert rec["readonly"] is True
    assert len(rec["sha256"]) == 64
    assert files["scripts/mixed.gd"]["text"]["eol"] == "mixed"
    assert files["scripts/moji.gd"]["text"]["mojibake"] == 1
    assert files["scripts/broken.gd"]["readonly"] is False


def test_snapshot_unreal_skips_intermediate_and_tracks_logs(unreal_project):
    snap = take_snapshot(unreal_project)
    assert snap["engine"] == "unreal"
    assert "Content/Blueprints/BP_Door.uasset" in snap["files"]
    assert "text" not in snap["files"]["Content/Blueprints/BP_Door.uasset"]
    assert not any(k.startswith(("Intermediate/", "Saved/")) for k in snap["files"])
    assert snap["logs"] == {"Saved/Logs/Fixture.log": (unreal_project / "Saved/Logs/Fixture.log").stat().st_size}


def test_snapshot_roundtrip_and_reuse(tmp_path, godot_project):
    snap = take_snapshot(godot_project)
    out = tmp_path / "s.json"
    save_snapshot(snap, out)
    loaded = load_snapshot(out)
    assert loaded["files"] == snap["files"]
    again = take_snapshot(godot_project, previous=loaded)
    assert again["files"] == snap["files"]


def test_load_snapshot_rejects_other_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": 1}')
    with pytest.raises(ValueError):
        load_snapshot(p)


def test_diff_classifies_changes(godot_project):
    before = take_snapshot(godot_project)
    write(godot_project, "scripts/player.gd", b"extends Node\nclass_name Player\n")
    write(godot_project, "scripts/new.gd", b"extends Node\n")
    (godot_project / "scripts/broken.gd").unlink()
    bump_mtime(godot_project / "main.tscn")  # rewritten with identical bytes
    after = take_snapshot(godot_project, previous=before)
    d = diff(before, after)
    assert d.changed == ["scripts/player.gd"]
    assert d.created == ["scripts/new.gd"]
    assert d.deleted == ["scripts/broken.gd"]
    assert d.rewritten == ["main.tscn"]


# --------------------------------------------------------------------------- patterns


def test_glob_rules():
    rx = glob_to_regex("scripts/**/*.gd")
    assert rx.fullmatch("scripts/a.gd") and rx.fullmatch("scripts/x/y/a.gd")
    assert not rx.fullmatch("other/a.gd")
    assert glob_to_regex("*.gd").fullmatch("a.gd")
    assert not glob_to_regex("*.gd").fullmatch("dir/a.gd")
    assert glob_to_regex("Content/{A,B}.uasset").fullmatch("Content/B.uasset")


def test_normalize_engine_paths():
    assert normalize_pattern("res://scripts/a.gd") == "scripts/a.gd"
    assert normalize_pattern("/Game/Blueprints/BP_Door") == "Content/Blueprints/BP_Door.{uasset,umap}"
    assert normalize_pattern("/Game/Blueprints/BP_Door.BP_Door") == "Content/Blueprints/BP_Door.{uasset,umap}"
    assert normalize_pattern("/Game/Blueprints/BP_Door.BP_Door_C") == "Content/Blueprints/BP_Door.{uasset,umap}"
    assert normalize_pattern("scripts\\a.gd") == "scripts/a.gd"


def test_parse_expectation_forms(tmp_path):
    e = parse_expectation("scripts/a.gd created")
    assert (e.pattern, e.state) == ("scripts/a.gd", "created")
    e = parse_expectation("scripts/a.gd")
    assert e.state == "changed"
    e = parse_expectation("deleted:scripts/a.gd")
    assert (e.pattern, e.state) == ("scripts/a.gd", "deleted")
    e = parse_expectation(f"{tmp_path / 'x' / 'a.gd'} modified", root=tmp_path)
    assert (e.pattern, e.state) == ("x/a.gd", "changed")


# --------------------------------------------------------------------------- verify CLI


def test_verify_expectations_met(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    assert main(["snapshot", str(godot_project), "--out", str(snap)]) == 0
    capsys.readouterr()
    write(godot_project, "scripts/player.gd", b"extends Node\nclass_name Player\n\nvar hp := 3\n")
    write(godot_project, "scripts/enemy.gd", b"extends Node\n")
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "scripts/player.gd changed",
                        "--expect", "res://scripts/enemy.gd created",
                        "--expect", "project.godot unchanged"], capsys)
    assert code == 0, v
    assert v["ok"] is True
    assert v["changes"]["changed"] == ["scripts/player.gd"]
    assert v["changes"]["created"] == ["scripts/enemy.gd"]
    assert [f["id"] for f in v["findings"]].count("verify.expectation_met") == 3


def test_verify_expectation_fails_when_nothing_written(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "scripts/player.gd changed",
                        "--expect", "scripts/nothere.gd created"], capsys)
    assert code == 1
    failed = [f for f in v["findings"] if f["id"] == "verify.expectation_failed"]
    assert len(failed) == 2
    assert "unchanged since snapshot" in failed[0]["evidence"] or "unchanged since snapshot" in failed[1]["evidence"]


def test_verify_readonly_file_explains_discarded_save(tmp_path, godot_project, capsys):
    target = godot_project / "scripts/player.gd"
    set_readonly(target)
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    # The "agent" believes it saved, but the file is read-only and nothing changed.
    try:
        target.write_bytes(b"changed")
    except PermissionError:
        pass
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "scripts/player.gd changed"], capsys)
    if os.name != "nt" and os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        pytest.skip("running as root, read-only bits are not enforced")
    assert code == 1
    f = next(f for f in v["findings"] if f["id"] == "verify.readonly_not_saved")
    assert f["severity"] == "error"
    assert "read-only" in f["evidence"]


def test_verify_rewritten_identical_bytes(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    bump_mtime(godot_project / "main.tscn")
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "main.tscn changed"], capsys)
    assert code == 1
    f = next(f for f in v["findings"] if f["id"] == "verify.expectation_failed")
    assert "byte-identical" in f["evidence"]
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "main.tscn touched"], capsys)
    assert code == 0


def test_verify_unexpected_changes_warn_and_strict_fails(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    write(godot_project, "scripts/player.gd", b"extends Node\nclass_name Player\n# edit\n")
    write(godot_project, "main.tscn", b"[gd_scene format=3]\n\n[node name=\"Main\" type=\"Node\"]\n")
    code, v = run_json(["verify", str(godot_project), "--since", str(snap),
                        "--expect", "scripts/*.gd changed"], capsys)
    assert code == 0
    assert ("verify.unexpected_change", "warning", "main.tscn") in ids(v)
    code, v = run_json(["verify", str(godot_project), "--since", str(snap), "--strict",
                        "--expect", "scripts/*.gd changed"], capsys)
    assert code == 1


def test_verify_flags_new_text_problems_in_changed_files(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    write(godot_project, "scripts/player.gd",
          'extends Node\nclass_name Player\n# Grün\n'.encode("utf-8").decode("cp1252").encode("utf-8"))
    write(godot_project, "scripts/mixed.gd", b"extends Node\r\n\n# still mixed\r\n")
    code, v = run_json(["verify", str(godot_project), "--since", str(snap)], capsys)
    found = ids(v)
    assert ("text.mojibake", "error", "scripts/player.gd") in found
    # mixed.gd was already mixed in the snapshot: reported, but only as info
    assert ("text.mixed_eol", "info", "scripts/mixed.gd") in found
    assert code == 1


def test_verify_duplicate_class_name_in_new_file(tmp_path, clean_godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(clean_godot_project), "--out", str(snap)])
    capsys.readouterr()
    write(clean_godot_project, "backup/player_copy.gd", b"extends Node\nclass_name Player\n")
    code, v = run_json(["verify", str(clean_godot_project), "--since", str(snap), "--no-engine",
                        "--expect", "backup/player_copy.gd created"], capsys)
    assert code == 1
    assert ("godot.duplicate_class_name", "error", "backup/player_copy.gd") in ids(v)


def test_verify_update_moves_baseline(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    write(godot_project, "scripts/enemy.gd", b"extends Node\n")
    main(["verify", str(godot_project), "--since", str(snap), "--update"])
    capsys.readouterr()
    code, v = run_json(["verify", str(godot_project), "--since", str(snap)], capsys)
    assert v["changes"]["created"] == []


def test_verify_missing_snapshot_is_usage_error(tmp_path, godot_project, capsys):
    assert main(["verify", str(godot_project), "--since", str(tmp_path / "none.json")]) == 2


def test_text_output_is_readable(tmp_path, godot_project, capsys):
    snap = tmp_path / "snap.json"
    main(["snapshot", str(godot_project), "--out", str(snap)])
    capsys.readouterr()
    code = main(["verify", str(godot_project), "--since", str(snap), "--expect", "scripts/player.gd changed"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("engine-proof verify: FAIL")
    assert "verify.expectation_failed" in out


def test_normalize_undoes_git_bash_path_conversion():
    assert normalize_pattern("C:/Program Files/Git/Game/Blueprints/BP_Door") == \
        "Content/Blueprints/BP_Door.{uasset,umap}"
    assert normalize_pattern("D:/work/MyGame/Content/Game/X.uasset") == "D:/work/MyGame/Content/Game/X.uasset"


def test_same_size_rewrite_in_same_mtime_tick_is_detected(godot_project):
    """Recently modified files are re-hashed even if size and mtime look unchanged."""
    target = godot_project / "scripts/player.gd"
    before = take_snapshot(godot_project)
    st = target.stat()
    data = target.read_bytes()
    target.write_bytes(data.replace(b"ready", b"READY"))  # same size
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime
    after = take_snapshot(godot_project, previous=before)
    assert diff(before, after).changed == ["scripts/player.gd"]


def test_old_files_reuse_stored_hash(godot_project):
    """Files older than the racy window keep their stored hash (no re-read)."""
    before = take_snapshot(godot_project)
    before["started_ns"] += 10 * 1_000_000_000  # pretend the snapshot was taken later
    before["files"]["project.godot"]["sha256"] = "stored"
    after = take_snapshot(godot_project, previous=before)
    assert after["files"]["project.godot"]["sha256"] == "stored"
