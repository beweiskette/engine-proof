import io
import json
import sys

from engine_proof.cli import main

from conftest import set_readonly, write


def hook(monkeypatch, capsys, payload, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = main(["hook", *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def payload(event, tool, root, file=None):
    p = {"hook_event_name": event, "tool_name": tool, "cwd": str(root), "tool_input": {}}
    if file:
        p["tool_input"]["file_path"] = str(file)
    return p


def test_hook_pre_post_clean_write(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    target = clean_godot_project / "scripts/player.gd"
    code, out, err = hook(monkeypatch, capsys, payload("PreToolUse", "Write", clean_godot_project, target),
                          "--state", state)
    assert (code, out, err) == (0, "", "")
    write(clean_godot_project, "scripts/player.gd", b"extends Node\nclass_name Player\n\nvar speed := 2.0\n")
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Write", clean_godot_project, target),
                          "--state", state)
    assert (code, out, err) == (0, "", "")


def test_hook_blocks_when_write_did_not_reach_disk(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    target = clean_godot_project / "scripts/player.gd"
    set_readonly(target)
    hook(monkeypatch, capsys, payload("PreToolUse", "Edit", clean_godot_project, target), "--state", state)
    # the tool claimed success, but nothing was written
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Edit", clean_godot_project, target),
                          "--state", state)
    assert code == 2
    assert "verify.readonly_not_saved" in err
    assert "scripts/player.gd" in err


def test_hook_reports_mojibake_from_bash(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    hook(monkeypatch, capsys, payload("PreToolUse", "Bash", clean_godot_project), "--state", state)
    write(clean_godot_project, "scripts/ui.gd", "extends Node\n# Menü\n".encode("utf-8").decode("cp1252").encode("utf-8"))
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Bash", clean_godot_project), "--state", state)
    assert code == 2
    assert "text.mojibake" in err and "scripts/ui.gd" in err


def test_hook_warning_goes_to_additional_context(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    hook(monkeypatch, capsys, payload("PreToolUse", "Bash", clean_godot_project), "--state", state)
    write(clean_godot_project, "scripts/player.gd", b"extends Node\r\nclass_name Player\n")
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Bash", clean_godot_project), "--state", state)
    assert code == 0
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    assert "text.mixed_eol" in ctx["additionalContext"]


def test_hook_first_post_creates_baseline(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = tmp_path / "state.json"
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Bash", clean_godot_project),
                          "--state", str(state))
    assert code == 0 and state.is_file()


def test_hook_silent_outside_engine_projects(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out, err = hook(monkeypatch, capsys, payload("PostToolUse", "Write", plain, plain / "a.txt"))
    assert (code, out, err) == (0, "", "")


def test_hook_finds_project_from_nested_file(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    target = clean_godot_project / "scripts/player.gd"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    p = payload("PreToolUse", "Write", elsewhere, target)
    hook(monkeypatch, capsys, p, "--state", state)
    snap = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "scripts/player.gd" in snap["files"]
