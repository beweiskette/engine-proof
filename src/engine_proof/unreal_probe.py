"""engine-proof probe for the Unreal Editor Python environment.

Run inside the editor, for example from the Python console or the Output Log:

    py "path/to/unreal_probe.py" /Game/Blueprints/BP_Door /Game/Maps/Main --out probe.json

or from an MCP server that can execute Python in the editor:

    import runpy; runpy.run_path("path/to/unreal_probe.py")["run"](["/Game/Blueprints/BP_Door"])

For every asset it reports: does the asset exist in the asset registry, does its
package file exist on disk, is that file read-only, is the package dirty (unsaved),
and for Blueprints the compile status. The JSON result is printed between the
markers ENGINE_PROOF_PROBE_BEGIN and ENGINE_PROOF_PROBE_END and can be fed back
with `engine-proof unreal PROJECT --probe-result probe.json`.

The functions above the "editor part" line are pure and importable without Unreal.
This file must stay compatible with the Python version bundled with older
engines (3.9), so it avoids newer syntax.
"""

from __future__ import annotations

import json
import os
import stat
import sys

PROBE_TOOL = "engine-proof-probe"
BEGIN = "ENGINE_PROOF_PROBE_BEGIN"
END = "ENGINE_PROOF_PROBE_END"

# EBlueprintStatus in declaration order.
_BP_STATUS = ["UNKNOWN", "DIRTY", "ERROR", "UP_TO_DATE", "BEING_CREATED", "UP_TO_DATE_WITH_WARNINGS"]


# ----------------------------------------------------------------------------- pure part


def package_name(asset_path):
    """'/Game/A/B.B', "Blueprint'/Game/A/B.B'" or '/Game/A/B.B_C' -> '/Game/A/B'."""
    p = asset_path.strip()
    if "'" in p:
        parts = p.split("'")
        if len(parts) >= 2 and parts[1]:
            p = parts[1]
    p = p.replace("\\", "/").rstrip("/")
    last = p.rsplit("/", 1)[-1]
    if "." in last:
        p = p[: len(p) - len(last)] + last.split(".", 1)[0]
    if ":" in p:
        p = p.split(":", 1)[0]
    return p


def object_path(asset_path):
    """'/Game/A/B' -> '/Game/A/B.B' (the form EditorAssetLibrary accepts everywhere)."""
    pkg = package_name(asset_path)
    return pkg + "." + pkg.rsplit("/", 1)[-1]


def package_to_relative_files(pkg, mounts=None):
    """Candidate files for a package, relative to the project directory.

    mounts maps a mount point like '/Game/' to a relative content directory
    like 'Content/'. Plugin mounts can be added by the caller.
    """
    table = {"/Game/": "Content/"}
    if mounts:
        table.update(mounts)
    for mount in sorted(table, key=len, reverse=True):
        if pkg.startswith(mount):
            base = table[mount].rstrip("/") + "/" + pkg[len(mount):]
            return [base + ".uasset", base + ".umap"]
    return []


def status_name(value):
    """Normalize a Blueprint status from an enum object, int or string."""
    if value is None:
        return "UNKNOWN"
    if isinstance(value, int):
        return _BP_STATUS[value] if 0 <= value < len(_BP_STATUS) else "UNKNOWN"
    name = getattr(value, "name", None) or str(value)
    name = name.rsplit(".", 1)[-1].upper()
    if name.startswith("BS_"):
        name = name[3:]
    name = name.replace("UPTODATE", "UP_TO_DATE")
    return name if name in _BP_STATUS else "UNKNOWN"


def assess(record):
    """Return a list of problems {id, severity, message} for one asset record."""
    problems = []
    exists = record.get("exists_in_registry")
    on_disk = record.get("on_disk")
    dirty = record.get("dirty")
    readonly = record.get("readonly")
    status = record.get("blueprint_status")

    def add(pid, sev, msg):
        problems.append({"id": pid, "severity": sev, "message": msg})

    if not exists and not on_disk:
        add("probe.missing", "error", "asset does not exist in the registry or on disk")
    elif exists and on_disk is False:
        add("probe.not_on_disk", "error", "asset exists in the editor but its package was never saved to disk")
    if dirty:
        if readonly:
            add("probe.dirty_readonly", "error",
                "package has unsaved changes and its file is read-only; saving will fail or be discarded")
        else:
            add("probe.dirty", "warning", "package has unsaved changes (not on disk yet)")
    elif readonly and record.get("expected_change"):
        add("probe.readonly", "error", "file is read-only; the expected change cannot have been saved")
    if status in ("ERROR",):
        add("probe.blueprint_error", "error", "Blueprint failed to compile")
    elif status in ("DIRTY", "BEING_CREATED"):
        add("probe.blueprint_not_compiled", "warning", "Blueprint is not compiled (status %s)" % status)
    elif status == "UP_TO_DATE_WITH_WARNINGS":
        add("probe.blueprint_warnings", "info", "Blueprint compiled with warnings")
    if record.get("generated_class_ok") is False:
        add("probe.no_generated_class", "error",
            "Blueprint has no valid generated class (compile failed or never ran)")
    for err in record.get("errors", []):
        add("probe.query_error", "warning", err)
    return problems


def build_result(records, culture=None, language=None, engine_version=None):
    for r in records:
        r["problems"] = assess(r)
    n_err = sum(1 for r in records for p in r["problems"] if p["severity"] == "error")
    return {
        "tool": PROBE_TOOL,
        "schema": 1,
        "engine_version": engine_version,
        "culture": culture,
        "language": language,
        "ok": n_err == 0,
        "assets": records,
    }


def extract_json(text):
    """Pull the probe JSON out of arbitrary output (e.g. an editor log)."""
    if BEGIN in text:
        start = text.rindex(BEGIN) + len(BEGIN)
        end = text.find(END, start)
        chunk = text[start:end if end >= 0 else None]
        # Log lines may carry a 'LogPython: ' prefix; strip everything before the JSON.
        lines = []
        for ln in chunk.splitlines():
            idx = ln.find("LogPython:")
            if idx >= 0:
                ln = ln[idx + len("LogPython:"):].lstrip()
            lines.append(ln)
        return json.loads("\n".join(lines))
    return json.loads(text)


def parse_args(argv):
    assets, out, compile_bp, expect = [], None, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
            continue
        if a == "--compile":
            compile_bp = True
        elif a == "--expect-change":
            expect = True
        elif a.startswith("/"):
            assets.append(a)
        i += 1
    return {"assets": assets, "out": out, "compile": compile_bp, "expect_change": expect}


def file_readonly(path):
    try:
        return not (os.stat(path).st_mode & stat.S_IWUSR)
    except OSError:
        return None


# ----------------------------------------------------------------------------- editor part
# Everything below needs the `unreal` module and is not covered by automated tests.


def _dirty_packages(unreal):
    names = set()
    utils = getattr(unreal, "EditorLoadingAndSavingUtils", None)
    for fn in ("get_dirty_content_packages", "get_dirty_map_packages"):
        try:
            for pkg in getattr(utils, fn)():
                names.add(pkg.get_name())
        except Exception:  # noqa: BLE001 - API differs between engine versions
            pass
    return names


def _culture(unreal):
    for lib_name in ("KismetInternationalizationLibrary", "InternationalizationLibrary"):
        lib = getattr(unreal, lib_name, None)
        if lib is None:
            continue
        try:
            return lib.get_current_culture(), lib.get_current_language()
        except Exception:  # noqa: BLE001
            continue
    return None, None


def _project_dir(unreal):
    return os.path.abspath(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir()))


def collect(asset_paths, compile_bp=False, expect_change=False):
    import unreal  # only available inside the editor

    project = _project_dir(unreal)
    dirty = _dirty_packages(unreal)
    eal = unreal.EditorAssetLibrary
    records = []
    for raw in asset_paths:
        pkg = package_name(raw)
        rec = {"asset": raw, "package": pkg, "errors": [], "expected_change": expect_change}
        try:
            rec["exists_in_registry"] = bool(eal.does_asset_exist(object_path(pkg)))
        except Exception as exc:  # noqa: BLE001
            rec["exists_in_registry"] = None
            rec["errors"].append("does_asset_exist failed: %s" % exc)
        rec["dirty"] = pkg in dirty
        files = package_to_relative_files(pkg)
        rec["on_disk"] = None if not files else False
        for rel in files:
            full = os.path.join(project, rel)
            if os.path.isfile(full):
                rec["on_disk"] = True
                rec["file"] = rel
                rec["readonly"] = file_readonly(full)
                break
        if not files:
            rec["errors"].append("package is not under /Game/; file location not resolved")
        if rec.get("exists_in_registry"):
            try:
                asset = eal.load_asset(object_path(pkg))
                rec["class"] = asset.get_class().get_name() if asset else None
                if asset is not None and isinstance(asset, unreal.Blueprint):
                    if compile_bp:
                        unreal.BlueprintEditorLibrary.compile_blueprint(asset)
                    try:
                        rec["blueprint_status"] = status_name(asset.get_editor_property("status"))
                    except Exception:  # noqa: BLE001 - not exposed in every engine version
                        rec["blueprint_status"] = "UNKNOWN"
                    try:
                        gen = eal.load_blueprint_class(object_path(pkg))
                        gname = gen.get_name() if gen else ""
                        rec["generated_class_ok"] = bool(gen) and not gname.startswith(
                            ("REINST_", "SKEL_", "TRASHCLASS_"))
                    except Exception as exc:  # noqa: BLE001
                        rec["errors"].append("load_blueprint_class failed: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                rec["errors"].append("load_asset failed: %s" % exc)
        records.append(rec)
    culture, language = _culture(unreal)
    try:
        version = unreal.SystemLibrary.get_engine_version()
    except Exception:  # noqa: BLE001
        version = None
    return build_result(records, culture, language, version)


def run(asset_paths, out=None, compile_bp=False, expect_change=False):
    result = collect(asset_paths, compile_bp=compile_bp, expect_change=expect_change)
    text = json.dumps(result, indent=1)
    print(BEGIN)
    print(text)
    print(END)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return result


if __name__ == "__main__":
    _args = parse_args(sys.argv[1:])
    run(_args["assets"], _args["out"], _args["compile"], _args["expect_change"])
