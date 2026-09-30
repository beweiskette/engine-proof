# engine-proof

[Deutsch](README.de.md)

A verification layer for AI agents that change Godot and Unreal Engine projects. The agent says "saved", the MCP server says "success", and `engine-proof` checks the disk and the engine output to see whether that is true.

## The problem

Engine tooling often reports success when nothing happened:

- The Unreal editor drops a save to an asset that is read-only because it is locked in Git LFS or not checked out in Perforce. The Python call returns normally.
- A Blueprint built from Python looks nodes and pins up by display name. In a German or French editor those names are translated, the lookup finds nothing, and the script ends without an error.
- A GDScript file stops parsing, but nothing loads it until the game runs.
- A copy of a script in `backup/` repeats a `class_name`. Godot registers one of them and fails on the other.
- A file is read as cp1252 and written back as UTF-8, and `Grün` turns into `GrÃ¼n`. Godot 4.7 loads such a file without complaint.
- An edit writes LF line endings into a CRLF file.

Several Unreal MCP servers check actor bounds or similar after a mutation. None of them gives an agent an engine-independent check it can run after every change. `engine-proof` is that check.

## How it works

1. `engine-proof snapshot` records for every relevant file: SHA-256, size, mtime, read-only flag, encoding, BOM, line-ending style, mojibake count and (for GDScript) the declared `class_name`. For Unreal it also records the size of each editor log in `Saved/Logs`.
2. The agent makes its change.
3. `engine-proof verify` compares the disk with the snapshot, checks the expectations you pass (`--expect "scripts/player.gd changed"`), lists changes nobody expected, and runs text checks on changed files. For Unreal it scans only the log lines written since the snapshot. For Godot it can load the changed scripts in the real engine.
4. `engine-proof godot` and `engine-proof unreal` run the engine-specific checks on the whole project.

Tracked files: Godot `.gd .tscn .tres .godot .gdshader`, Unreal `.uasset .umap .ini .cpp .h .py .uproject .uplugin`. Add more with `--ext`. These folders are skipped at any depth: `.godot`, `Intermediate`, `Saved`, `DerivedDataCache`, `Binaries`, plus VCS and virtualenv folders.

## Install

Python 3.10 or newer, no runtime dependencies.

```
python -m venv .venv
.venv/bin/pip install git+<repository-url>      # or: pip install -e . in a checkout
```

On Windows the script is `.venv\Scripts\engine-proof.exe`.

## Quick start

```
engine-proof snapshot path/to/game --out snap.json
# ... the agent edits files or drives the editor ...
engine-proof verify path/to/game --since snap.json \
    --expect "scripts/player.gd changed" \
    --expect "res://scenes/enemy.tscn created" \
    --expect "/Game/Blueprints/BP_Door changed"
```

Exit code 0 means every expectation held and no error was found, 1 means at least one error (with `--strict`, warnings count too), 2 means a usage problem such as an unreadable snapshot.

### Expectations

`--expect "PATTERN STATE"`, repeatable. The pattern is a path relative to the project, a glob (`*` stays inside one folder, `**` crosses folders, `{a,b}` alternatives), a Godot `res://` path, an absolute path inside the project, or an Unreal package path such as `/Game/Maps/Main` (mapped to `Content/Maps/Main.uasset` or `.umap`). On Windows matching ignores case.

| State | Meaning (at least one matching file) |
|---|---|
| `changed` (default) | content differs from the snapshot |
| `created` | did not exist in the snapshot |
| `deleted` | existed in the snapshot, gone now |
| `written` | changed or created |
| `touched` | changed, created, or rewritten with identical bytes |
| `exists` | exists now |
| `unchanged` | every matching file is unchanged and at least one exists |

When an expectation fails, the finding explains why if it can: the file is read-only (`verify.readonly_not_saved`), the file was rewritten with byte-identical content, no tracked file matches, or the path exists but its extension is not tracked.

Git Bash on Windows rewrites arguments such as `/Game/X` into `C:/Program Files/Git/Game/X`. `engine-proof` detects this and undoes it.

### Godot

```
engine-proof godot path/to/game --godot /path/to/Godot_v4.x   # or set env GODOT
```

Static checks, always:

- duplicate `class_name` declarations, including copies in backup folders (folders with a `.gdignore` file are skipped, as Godot skips them); the class cache in `.godot/` tells which copy was registered
- double-encoded UTF-8 (mojibake), invalid UTF-8, UTF-16 files, UTF-8 BOM
- mixed line endings within one file

With a Godot executable (`--godot PATH`, or env `GODOT`; a folder that contains the binary works too):

1. `godot --headless --path P --import` when the global class cache is missing or older than a script. Without this, a script that uses a `class_name` added in the same change fails with "Identifier not declared".
2. `godot --headless --path P --quit-after N` boots the main scene (skipped when the project has no main scene, because Godot then prints "Can't run project" and does not exit).
3. A small loader script (`godot_check.gd`, shipped with the package) loads every `.gd`, `.tscn` and `.tres` with the cache disabled, in one process. Parse errors, missing resources and invalid Unicode are read from Godot's output. Godot does not translate these error lines, so this works in a localized editor.
4. `--test-scene res://tests/run.tscn` (or a `.gd` that extends `SceneTree`) runs a test and records its exit code; a non-zero code or a timeout is an error.

`verify` loads only the changed scripts and scenes when a Godot executable is configured (`--godot` or env `GODOT`; `--no-engine` turns it off).

### Unreal

```
engine-proof unreal path/to/MyGame [--since snap.json --expect "/Game/Blueprints/BP_Door changed"]
```

Offline, no editor needed:

- Editor log scan (newest `Saved/Logs/*.log`, or with `--since` only the lines written after the snapshot). Patterns: save failures, read-only and access-denied messages, source control locks, Blueprint compiler errors and warnings, Python tracebacks (folded into one finding), failed asset creation, failed loads, crashes, and other `Error:` lines. A log finding that names an asset gets a note if that asset's file is read-only.
- Read-only `.uasset`/`.umap` files, with the `lockable` patterns from `.gitattributes`.
- Editor language: `[Internationalization]` in `Config/*.ini` and `Saved/Config/**.ini`, and culture lines in the log. A non-English editor is reported as a warning because display-name lookups from Python fail silently there.
- With `--since`: the same expectation checks as `verify`.

Inside the editor: `unreal_probe.py`. Print its location with `engine-proof probe-path`, or copy it with `engine-proof probe-path --copy DIR`. Run it in the editor's Python console or through any MCP server that can execute editor Python:

```
py "path/to/unreal_probe.py" /Game/Blueprints/BP_Door /Game/Maps/Main --out probe.json
```

For each asset it reports whether it exists in the asset registry, whether its package file is on disk, whether that file is read-only, whether the package is dirty (unsaved), the Blueprint compile status and whether a generated class exists, plus the editor culture. The JSON is printed between `ENGINE_PROOF_PROBE_BEGIN` and `ENGINE_PROOF_PROBE_END`. Pass it back (the JSON file, or a log that contains the printed block):

```
engine-proof unreal path/to/MyGame --probe-result probe.json
```

### Output

`--format text` (default) or `--format json`. JSON is one verdict object:

```json
{
  "tool": "engine-proof",
  "version": "0.1.0",
  "command": "verify",
  "project": "/work/MyGame",
  "engine": "unreal",
  "ok": false,
  "summary": {"error": 1, "warning": 0, "info": 1},
  "findings": [
    {
      "id": "verify.readonly_not_saved",
      "severity": "error",
      "message": "expected \"Content/Blueprints/BP_Door.{uasset,umap}\" changed, but it was not",
      "file": "Content/Blueprints/BP_Door.uasset",
      "evidence": "Content/Blueprints/BP_Door.uasset: file is read-only (locked or not checked out); a save was probably discarded"
    }
  ],
  "changes": {"changed": [], "created": [], "deleted": [], "rewritten": []}
}
```

## Example output

Real run of Godot 4.7.1 on a synthetic test project (evidence lines shortened, one entry left out):

```
$ engine-proof godot . --godot ~/bin/godot
engine-proof godot: FAIL (6 error(s), 1 warning(s), 0 info)
  project: /work/my_game [godot]
  ERROR   godot.missing_resource  art/missing.png
          boot: Resource file not found: res://art/missing.png (expected type: Texture2D)
  ERROR   godot.duplicate_class_name  backup/player.gd
          class_name "Player" is declared in 2 scripts; Godot registers only one and fails on the others
          | res://backup/player.gd  (looks like a backup copy)
          | res://scripts/player.gd
          | fix: delete or rename the copies, or put a .gdignore file into the backup folder
  ERROR   godot.class_name_conflict  backup/player.gd:2
          load-check: Parse Error: Class "Player" hides a global script class.
  ERROR   godot.parse_error  scripts/broken.gd:4
          load-check: Parse Error: Expected expression for variable initial value after "=".
  ERROR   text.mojibake  scripts/moji.gd:4
          1 double-encoded UTF-8 sequence(s) (text was decoded as cp1252/latin-1 and saved again)
          | line 4: 'Ã¼' (U+00C3 U+00BC) should be 'ü'
  ...
  WARNING text.mixed_eol  scripts/mixed.gd:2
          file mixes line endings
          | crlf=3 lf=1 cr=0
```

Unreal, after an agent "saved" a locked Blueprint and changed another one (synthetic project and log):

```
$ engine-proof unreal MyGame --since snap.json --expect "/Game/Blueprints/BP_Door changed"
engine-proof unreal: FAIL (3 error(s), 2 warning(s), 1 info)
  project: /work/MyGame [unreal]
  changes: changed 1, created 0, deleted 0, rewritten 0
  ERROR   verify.readonly_not_saved  Content/Blueprints/BP_Door.uasset
          expected "Content/Blueprints/BP_Door.{uasset,umap}" changed, but it was not
          | Content/Blueprints/BP_Door.uasset: file is read-only (locked or not checked out); a save was probably discarded
          | Content/Blueprints/BP_Door.uasset: unchanged since snapshot (sha256 581e0a28bf66)
  ERROR   unreal.log.save_failed  Saved/Logs/MyGame.log:2
          Failed to save package '/Game/Blueprints/BP_Door'
          | LogSavePackage: Error: Failed to save package '/Game/Blueprints/BP_Door'
          | -> Content/Blueprints/BP_Door.uasset is read-only on disk (locked or not checked out)
  ERROR   unreal.log.python_error  Saved/Logs/MyGame.log:3
          Python error: AttributeError: 'NoneType' object has no attribute 'get_editor_property'
          | LogPython: Error: Traceback (most recent call last):
          | LogPython: Error:   File "<string>", line 12, in <module>
          | LogPython: Error: AttributeError: 'NoneType' object has no attribute 'get_editor_property'
  WARNING verify.unexpected_change  Content/Blueprints/BP_Lamp.uasset
          changed but not expected
  WARNING unreal.localized_editor  Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini
          editor runs with culture/language 'de'. Python scripts that look up nodes, pins, menus or categories by display name can silently find nothing; use internal names
          | [Internationalization] culture=de
  INFO    unreal.readonly_assets  -
          1 asset file(s) are read-only; the editor may drop saves to them without an error the agent sees. Lock or check them out before changing them
          | Content/Blueprints/BP_Door.uasset
```

## Using it from agents

### Claude Code hook

`engine-proof hook` reads the hook payload that Claude Code sends on stdin. On `PreToolUse` it refreshes a snapshot of the project; on `PostToolUse` it verifies against that snapshot and then stores the new state. For `Write`, `Edit`, `MultiEdit` and `NotebookEdit` it expects the edited file to be touched. The project is found by walking up from the edited file or the working directory to a folder with `project.godot` or a `.uproject`; outside engine projects the hook does nothing.

- errors: exit code 2, report on stderr. Claude Code shows it to the model.
- warnings: exit code 0 with `hookSpecificOutput.additionalContext` on stdout.
- nothing found: exit code 0, no output.

The state file lives in the system temp folder (`engine-proof/<hash>.json`), so nothing is written into the project. Snapshots after the first reuse stored hashes when size and mtime are unchanged, so a hook call only reads files that changed.

Add this to `.claude/settings.json` in the game project (or your user settings). Use the full path to `engine-proof` if it is not on `PATH`. The `mcp__.*` matcher covers MCP servers that drive the editor.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash|mcp__.*",
        "hooks": [{ "type": "command", "command": "engine-proof hook" }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash|mcp__.*",
        "hooks": [{ "type": "command", "command": "engine-proof hook" }]
      }
    ]
  }
}
```

For Godot, `engine-proof hook --godot-load` with env `GODOT` set also loads changed scripts in the engine. That adds a Godot start (well under a second on a small project) to every tool call.

### Codex and other agents

The CLI is enough. Put the rule into `AGENTS.md` of the game project, for example:

```
After you change files in this Godot/Unreal project:
1. Before the change: engine-proof snapshot . --out .engine-proof/snap.json
2. After the change:  engine-proof verify . --since .engine-proof/snap.json --expect "<file you changed> changed"
3. Exit code 1 means the change did not land or broke something. Read the findings and fix them.
   Do not report success before verify exits with 0.
For Unreal editor changes, also run unreal_probe.py in the editor (engine-proof probe-path)
and pass its output with engine-proof unreal . --probe-result probe.json
```

A one-off run: `codex exec "Rename the door Blueprint variable, then prove it with engine-proof verify"`. Agents without a hook payload can use `engine-proof hook --event pre --project .` and `engine-proof hook --event post --project . --expect "..."`.

Add `.engine-proof/` to `.gitignore` if snapshots are kept inside the project.

## Limitations

- `unreal_probe.py` has not been run in an Unreal editor during development; no editor was available. Its pure parts (path mapping, status mapping, problem assessment, JSON extraction from logs, argument parsing) are unit-tested. The editor calls (`EditorAssetLibrary`, `EditorLoadingAndSavingUtils.get_dirty_content_packages`, `load_blueprint_class`) differ between engine versions and are wrapped in `try`. Blueprint `status` is not exposed to Python in every engine version; the probe then reports `UNKNOWN` and relies on the generated-class check. Package paths outside `/Game/` (plugin content) are not mapped to files.
- The Unreal log patterns are written from known message formats and tested on synthetic log lines, not on logs from a range of engine versions. Expect false positives from generic `Error:` lines (reported as warnings) and possibly missed messages with unusual wording.
- Log culture detection only covers a few log categories; the `.ini` check is more reliable.
- The Godot integration tests ran against Godot 4.7.1 on Windows. Other 4.x versions should work (if your version has no `--import` flag, pass `--import never`); Godot 3 is not supported.
- Change detection reuses hashes from the earlier snapshot when size and mtime are unchanged, as git does. Files modified less than 3 seconds before that snapshot are always hashed again, so a same-size rewrite within one timestamp tick is caught. A tool that rewrites a file with the same size and then sets the old mtime back would not be noticed. `snapshot` without `--reuse` always hashes everything; `--no-hash` compares size and mtime only.
- Read-only detection uses the file mode bits. A save that fails for another reason (a file locked by another process, an antivirus scanner) is only visible through the log or the probe.
- Mojibake detection looks for UTF-8 read as cp1252 or latin-1. Other encoding mix-ups are not detected.
- The load check executes script loading code in the engine. `@tool` scripts and static initializers can run.

## Development

```
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
GODOT=/path/to/godot .venv/bin/python -m pytest -q tests/test_godot_integration.py
```

The integration tests are skipped when `GODOT` is not set.

## License

MIT, see [LICENSE](LICENSE).
