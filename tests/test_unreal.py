import json
import os

import pytest

from engine_proof import unreal, unreal_probe
from engine_proof.cli import main

from conftest import set_readonly, write

# Synthetic editor log lines in the format Unreal Engine 5 writes to Saved/Logs.
SYNTHETIC_LOG = """\
[2026.09.30-10.01.00:100][ 10]LogInit: Display: Engine is initialized.
[2026.09.30-10.01.05:200][ 20]LogSavePackage: Error: Failed to save package '/Game/Blueprints/BP_Door'.
[2026.09.30-10.01.05:210][ 20]LogFileManager: Warning: Could not write to file. The file is read-only: ../Content/Blueprints/BP_Lamp.uasset
[2026.09.30-10.01.06:000][ 21]LogSourceControl: Warning: /Game/Maps/Main is checked out by another user
[2026.09.30-10.01.07:000][ 22]LogBlueprint: Error: [Compiler] In use pin Target no longer exists on node Set Visibility. from Source: /Game/Blueprints/BP_Door.BP_Door
[2026.09.30-10.01.07:100][ 22]LogBlueprint: Warning: [Compiler] Node Print String has an unconnected output
[2026.09.30-10.01.08:000][ 23]LogPython: Error: Traceback (most recent call last):
[2026.09.30-10.01.08:000][ 23]LogPython: Error:   File "<string>", line 3, in <module>
[2026.09.30-10.01.08:000][ 23]LogPython: Error: AttributeError: 'NoneType' object has no attribute 'find_pin'
[2026.09.30-10.01.09:000][ 24]LogPython: script finished
[2026.09.30-10.01.10:000][ 25]LogAssetTools: Error: Failed to create asset '/Game/Blueprints/BP_New'
[2026.09.30-10.01.11:000][ 26]LogUObjectGlobals: Warning: Failed to find object 'Class /Script/Missing.Thing'
[2026.09.30-10.01.12:000][ 27]LogStreaming: Error: Couldn't find file for package /Game/Old/Removed
[2026.09.30-10.01.13:000][ 28]LogTemp: Display: nothing to see
"""


def ids(findings):
    return [f.id for f in findings]


def test_parse_log_line_strips_prefix():
    pl = unreal.parse_log_line("[2026.09.30-10.01.05:200][ 20]LogSavePackage: Error: boom", 7)
    assert (pl.category, pl.verbosity, pl.message, pl.number) == ("LogSavePackage", "Error", "boom", 7)
    pl = unreal.parse_log_line("LogInit: Display: hello", 1)
    assert pl.category == "LogInit" and pl.verbosity == "Display"


def test_scan_synthetic_log():
    fs = unreal.scan_log_lines(SYNTHETIC_LOG.splitlines(), "Saved/Logs/Fixture.log")
    got = ids(fs)
    assert got == [
        "unreal.log.save_failed",
        "unreal.log.readonly",
        "unreal.log.source_control",
        "unreal.log.blueprint_compile",
        "unreal.log.blueprint_warning",
        "unreal.log.python_error",
        "unreal.log.asset_create_failed",
        "unreal.log.load_failed",
        "unreal.log.error",
    ]
    py = fs[5]
    assert py.severity == "error"
    assert "AttributeError" in py.message
    assert py.line == 7
    assert py.evidence.count("\n") == 2  # three folded lines
    assert fs[4].severity == "warning"


def test_scan_caps_repeated_findings():
    lines = [f"LogSavePackage: Error: Failed to save package '/Game/A{i}'" for i in range(40)]
    fs = unreal.scan_log_lines(lines, "x.log", max_per_id=5)
    assert ids(fs).count("unreal.log.save_failed") == 6  # 5 + one summary
    assert fs[-1].severity == "info" and "35 more" in fs[-1].message


def test_asset_refs():
    refs = unreal.asset_refs("Failed to save '/Game/Blueprints/BP_Door.BP_Door'")
    assert refs == ["Content/Blueprints/BP_Door.uasset", "Content/Blueprints/BP_Door.umap"]
    refs = unreal.asset_refs("read-only: ../Content/Blueprints/BP_Lamp.uasset")
    assert refs == ["Content/Blueprints/BP_Lamp.uasset"]


def test_log_findings_marks_readonly_assets(unreal_project):
    set_readonly(unreal_project / "Content/Blueprints/BP_Door.uasset")
    log = write(unreal_project, "Saved/Logs/Fixture.log", SYNTHETIC_LOG.encode())
    fs = unreal.log_findings(unreal_project, [log])
    save = next(f for f in fs if f.id == "unreal.log.save_failed")
    assert "BP_Door.uasset is read-only on disk" in save.evidence


def test_log_offsets_only_new_lines(unreal_project):
    log = unreal_project / "Saved/Logs/Fixture.log"
    offset = log.stat().st_size
    with open(log, "ab") as fh:
        fh.write(b"[2026.09.30-10.02.00:000][ 30]LogSavePackage: Error: Failed to save package '/Game/X'\n")
    fs = unreal.log_findings(unreal_project, [log], {"Saved/Logs/Fixture.log": offset})
    assert ids(fs) == ["unreal.log.save_failed"]
    assert fs[0].line == 4
    # a rotated (shorter) log is read from the start
    fs = unreal.log_findings(unreal_project, [log], {"Saved/Logs/Fixture.log": 10**9})
    assert ids(fs) == ["unreal.log.save_failed"]


def test_utf16_log(tmp_path):
    p = tmp_path / "old.log"
    p.write_bytes("LogSavePackage: Error: Failed to save package '/Game/A'\n".encode("utf-16"))
    lines, first = unreal.read_log(p)
    assert "Failed to save" in lines[0]


def test_editor_language_from_ini_and_log(unreal_project):
    write(unreal_project, "Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini",
          b"[Internationalization]\nCulture=de\nLanguage=de\n")
    fs = unreal.editor_language_findings(unreal_project)
    assert ids(fs) == ["unreal.localized_editor"]
    assert "'de'" in fs[0].message
    write(unreal_project, "Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini",
          b"[Internationalization]\nCulture=en\n")
    assert unreal.editor_language_findings(unreal_project) == []
    # culture detection also runs on log lines
    log = write(unreal_project, "Saved/Logs/Fixture.log",
                b"LogICUInternationalization: Display: Culture is 'fr-FR'\n")
    assert "unreal.localized_editor" in ids(unreal.log_findings(unreal_project, [log]))


def test_readonly_summary_mentions_lockable(unreal_project):
    set_readonly(unreal_project / "Content/Maps/Main.umap")
    write(unreal_project, ".gitattributes", b"*.umap lockable\n*.uasset filter=lfs diff=lfs merge=lfs -text lockable\n")
    fs = unreal.readonly_asset_summary(unreal_project)
    assert fs[0].id == "unreal.readonly_assets"
    assert "Content/Maps/Main.umap" in fs[0].evidence
    assert "lockable" in fs[0].evidence


# --------------------------------------------------------------------------- CLI


def test_unreal_cli_with_snapshot_and_readonly_asset(tmp_path, unreal_project, capsys):
    door = unreal_project / "Content/Blueprints/BP_Door.uasset"
    set_readonly(door)
    snap = tmp_path / "s.json"
    main(["snapshot", str(unreal_project), "--out", str(snap)])
    capsys.readouterr()
    # Agent changed the lamp, "saved" the door (discarded) and the editor logged a failure.
    set_readonly(unreal_project / "Content/Blueprints/BP_Lamp.uasset", False)
    write(unreal_project, "Content/Blueprints/BP_Lamp.uasset", b"\xc1\x83\x2a\x9e" + b"lamp-v2" * 20)
    with open(unreal_project / "Saved/Logs/Fixture.log", "ab") as fh:
        fh.write(b"[2026.09.30-10.05.00:000][ 50]LogSavePackage: Error: Failed to save package "
                 b"'/Game/Blueprints/BP_Door'\n")
    code = main(["unreal", str(unreal_project), "--since", str(snap), "--format", "json",
                 "--expect", "/Game/Blueprints/BP_Door changed",
                 "--expect", "/Game/Blueprints/BP_Lamp.BP_Lamp changed"])
    v = json.loads(capsys.readouterr().out)
    assert code == 1
    found = {(f["id"], f.get("file")) for f in v["findings"]}
    assert ("verify.readonly_not_saved", "Content/Blueprints/BP_Door.uasset") in found
    assert ("unreal.log.save_failed", "Saved/Logs/Fixture.log") in found
    met = [f for f in v["findings"] if f["id"] == "verify.expectation_met"]
    assert "BP_Lamp.uasset" in met[0]["message"]
    # the startup lines that existed before the snapshot were not re-reported
    assert not any(f.get("line") == 1 for f in v["findings"] if f["id"].startswith("unreal.log"))


def test_unreal_cli_without_snapshot_scans_newest_log(unreal_project, capsys):
    write(unreal_project, "Saved/Logs/Fixture.log", SYNTHETIC_LOG.encode())
    code = main(["unreal", str(unreal_project), "--format", "json"])
    v = json.loads(capsys.readouterr().out)
    assert code == 1
    assert v["logs"] == ["Fixture.log"]
    assert v["summary"]["error"] >= 5


def test_unreal_cli_expect_needs_since(unreal_project, capsys):
    assert main(["unreal", str(unreal_project), "--expect", "x changed"]) == 2


def test_unreal_clean_project_ok(unreal_project, capsys):
    assert main(["unreal", str(unreal_project)]) == 0
    assert "engine-proof unreal: OK" in capsys.readouterr().out


# --------------------------------------------------------------------------- probe (pure parts)


def test_probe_path_helpers():
    assert unreal_probe.package_name("/Game/A/BP_X.BP_X") == "/Game/A/BP_X"
    assert unreal_probe.package_name("Blueprint'/Game/A/BP_X.BP_X'") == "/Game/A/BP_X"
    assert unreal_probe.package_name("/Game/A/BP_X.BP_X_C") == "/Game/A/BP_X"
    assert unreal_probe.package_name("/Game/Maps/Main.Main:PersistentLevel") == "/Game/Maps/Main"
    assert unreal_probe.object_path("/Game/A/BP_X") == "/Game/A/BP_X.BP_X"
    assert unreal_probe.package_to_relative_files("/Game/A/BP_X") == ["Content/A/BP_X.uasset", "Content/A/BP_X.umap"]
    assert unreal_probe.package_to_relative_files("/MyPlugin/Thing", {"/MyPlugin/": "Plugins/MyPlugin/Content/"}) == [
        "Plugins/MyPlugin/Content/Thing.uasset", "Plugins/MyPlugin/Content/Thing.umap"]
    assert unreal_probe.package_to_relative_files("/Engine/X") == []


class FakeEnum:
    def __init__(self, name):
        self.name = name


def test_probe_status_name():
    assert unreal_probe.status_name(2) == "ERROR"
    assert unreal_probe.status_name(FakeEnum("BS_UP_TO_DATE")) == "UP_TO_DATE"
    assert unreal_probe.status_name("BlueprintStatus.BS_UPTODATE") == "UP_TO_DATE"
    assert unreal_probe.status_name("BS_UP_TO_DATE_WITH_WARNINGS") == "UP_TO_DATE_WITH_WARNINGS"
    assert unreal_probe.status_name(None) == "UNKNOWN"
    assert unreal_probe.status_name(99) == "UNKNOWN"


def test_probe_assess():
    a = unreal_probe.assess
    assert [p["id"] for p in a({"exists_in_registry": False, "on_disk": False})] == ["probe.missing"]
    assert [p["id"] for p in a({"exists_in_registry": True, "on_disk": False, "dirty": True})] == [
        "probe.not_on_disk", "probe.dirty"]
    assert [p["id"] for p in a({"exists_in_registry": True, "on_disk": True, "dirty": True, "readonly": True})] == [
        "probe.dirty_readonly"]
    assert [p["id"] for p in a({"exists_in_registry": True, "on_disk": True, "blueprint_status": "ERROR",
                                "generated_class_ok": False})] == ["probe.blueprint_error", "probe.no_generated_class"]
    assert a({"exists_in_registry": True, "on_disk": True, "dirty": False, "blueprint_status": "UP_TO_DATE"}) == []


def test_probe_result_roundtrip_and_cli(tmp_path, unreal_project, capsys):
    result = unreal_probe.build_result([
        {"asset": "/Game/Blueprints/BP_Door", "package": "/Game/Blueprints/BP_Door",
         "exists_in_registry": True, "on_disk": True, "file": "Content/Blueprints/BP_Door.uasset",
         "readonly": True, "dirty": True, "blueprint_status": "UP_TO_DATE", "errors": []},
        {"asset": "/Game/Blueprints/BP_Lamp", "package": "/Game/Blueprints/BP_Lamp",
         "exists_in_registry": True, "on_disk": True, "file": "Content/Blueprints/BP_Lamp.uasset",
         "readonly": False, "dirty": False, "blueprint_status": "UP_TO_DATE", "errors": []},
    ], culture="de-CH", language="de")
    assert result["ok"] is False
    # As it appears in an editor log: every printed line gets a LogPython prefix.
    text = "\n".join(f"[2026.09.30-10.00.00:000][  1]LogPython: {ln}" for ln in
                     [unreal_probe.BEGIN, *json.dumps(result, indent=1).splitlines(), unreal_probe.END])
    parsed = unreal_probe.extract_json("noise\n" + text + "\nmore noise")
    assert parsed == result
    probe_file = tmp_path / "probe.log"
    probe_file.write_text(text, encoding="utf-8")
    code = main(["unreal", str(unreal_project), "--probe-result", str(probe_file), "--format", "json"])
    v = json.loads(capsys.readouterr().out)
    assert code == 1
    found = {(f["id"], f.get("file")) for f in v["findings"]}
    assert ("probe.dirty_readonly", "Content/Blueprints/BP_Door.uasset") in found
    assert ("probe.ok", "Content/Blueprints/BP_Lamp.uasset") in found
    assert any(f["id"] == "unreal.localized_editor" for f in v["findings"])


def test_probe_parse_args():
    a = unreal_probe.parse_args(["/Game/A", "--compile", "/Game/B", "--out", "p.json", "--expect-change"])
    assert a == {"assets": ["/Game/A", "/Game/B"], "out": "p.json", "compile": True, "expect_change": True}


def test_probe_module_imports_without_unreal():
    # collect() needs the editor; importing the module must not.
    with pytest.raises(ImportError):
        unreal_probe.collect(["/Game/A"])


def test_probe_path_command(tmp_path, capsys):
    assert main(["probe-path"]) == 0
    path = capsys.readouterr().out.strip()
    assert path.endswith("unreal_probe.py") and os.path.isfile(path)
    assert main(["probe-path", "--copy", str(tmp_path)]) == 0
    assert (tmp_path / "unreal_probe.py").is_file()


def test_probe_uses_old_python_grammar():
    """The editor may bundle an older Python; keep the probe parseable by 3.7."""
    import ast
    src = open(unreal_probe.__file__, encoding="utf-8").read()
    ast.parse(src, feature_version=(3, 7))
