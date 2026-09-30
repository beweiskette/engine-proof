# engine-proof

[English](README.md)

Eine Prüfschicht für KI-Agenten, die Godot- und Unreal-Engine-Projekte verändern. Der Agent meldet «gespeichert», der MCP-Server meldet «success», und `engine-proof` sieht auf der Festplatte und in der Engine-Ausgabe nach, ob das stimmt.

## Das Problem

Engine-Werkzeuge melden oft Erfolg, obwohl nichts passiert ist:

- Der Unreal-Editor verwirft das Speichern eines Assets, das schreibgeschützt ist, weil es in Git LFS gesperrt oder in Perforce nicht ausgecheckt ist. Der Python-Aufruf kehrt normal zurück.
- Ein Blueprint, das per Python gebaut wird, sucht Knoten und Pins über ihren Anzeigenamen. In einem deutschen oder französischen Editor sind diese Namen übersetzt, die Suche findet nichts, und das Skript endet ohne Fehler.
- Ein GDScript lässt sich nicht mehr parsen, aber niemand lädt es, bis das Spiel läuft.
- Eine Kopie eines Skripts in `backup/` wiederholt einen `class_name`. Godot registriert eine davon und scheitert an der anderen.
- Eine Datei wird als cp1252 gelesen und als UTF-8 zurückgeschrieben, aus `Grün` wird `GrÃ¼n`. Godot 4.7 lädt so eine Datei ohne Meldung.
- Eine Änderung schreibt LF-Zeilenenden in eine CRLF-Datei.

Es gibt mehrere Unreal-MCP-Server, von denen einige nach einer Änderung zum Beispiel die Bounds eines Actors prüfen. Keiner bietet einem Agenten eine engine-unabhängige Prüfung, die er nach jeder Änderung aufrufen kann. Diese Lücke füllt `engine-proof`.

## Funktionsweise

1. `engine-proof snapshot` hält für jede relevante Datei fest: SHA-256, Grösse, Änderungszeit, Schreibschutz, Kodierung, BOM, Art der Zeilenenden, Anzahl Mojibake-Stellen und (bei GDScript) den deklarierten `class_name`. Bei Unreal kommt die Grösse jedes Editor-Logs in `Saved/Logs` dazu.
2. Der Agent macht seine Änderung.
3. `engine-proof verify` vergleicht die Festplatte mit dem Snapshot, prüft die übergebenen Erwartungen (`--expect "scripts/player.gd changed"`), listet Änderungen auf, die niemand erwartet hat, und prüft geänderte Textdateien. Bei Unreal liest es nur die Logzeilen, die seit dem Snapshot dazugekommen sind. Bei Godot kann es die geänderten Skripte in der echten Engine laden.
4. `engine-proof godot` und `engine-proof unreal` prüfen das ganze Projekt engine-spezifisch.

Erfasste Dateien: Godot `.gd .tscn .tres .godot .gdshader`, Unreal `.uasset .umap .ini .cpp .h .py .uproject .uplugin`. Weitere mit `--ext`. Diese Ordner werden in jeder Tiefe übersprungen: `.godot`, `Intermediate`, `Saved`, `DerivedDataCache`, `Binaries` sowie Ordner von Versionsverwaltung und virtuellen Umgebungen.

## Installation

Python 3.10 oder neuer, keine Laufzeitabhängigkeiten.

```
python -m venv .venv
.venv/bin/pip install git+https://github.com/beweiskette/engine-proof.git      # oder: pip install -e . in einem Checkout
```

Unter Windows liegt das Programm in `.venv\Scripts\engine-proof.exe`.

## Schnellstart

```
engine-proof snapshot path/to/game --out snap.json
# ... der Agent ändert Dateien oder steuert den Editor ...
engine-proof verify path/to/game --since snap.json \
    --expect "scripts/player.gd changed" \
    --expect "res://scenes/enemy.tscn created" \
    --expect "/Game/Blueprints/BP_Door changed"
```

Exitcode 0 heisst: alle Erwartungen erfüllt, kein Fehler gefunden. 1 heisst: mindestens ein Fehler (mit `--strict` zählen auch Warnungen). 2 heisst: Bedienungsfehler, etwa ein unlesbarer Snapshot.

### Erwartungen

`--expect "MUSTER ZUSTAND"`, beliebig oft. Das Muster ist ein Pfad relativ zum Projekt, ein Glob (`*` bleibt in einem Ordner, `**` geht über Ordner hinweg, `{a,b}` für Alternativen), ein Godot-Pfad `res://`, ein absoluter Pfad im Projekt oder ein Unreal-Paketpfad wie `/Game/Maps/Main` (wird zu `Content/Maps/Main.uasset` oder `.umap`). Unter Windows wird Gross- und Kleinschreibung ignoriert.

| Zustand | Bedeutung (mindestens eine passende Datei) |
|---|---|
| `changed` (Standard) | Inhalt weicht vom Snapshot ab |
| `created` | gab es im Snapshot nicht |
| `deleted` | gab es im Snapshot, jetzt nicht mehr |
| `written` | geändert oder neu |
| `touched` | geändert, neu oder mit identischen Bytes neu geschrieben |
| `exists` | existiert jetzt |
| `unchanged` | jede passende Datei ist unverändert, und es gibt mindestens eine |

Schlägt eine Erwartung fehl, nennt der Befund den Grund, wenn er sich feststellen lässt: Die Datei ist schreibgeschützt (`verify.readonly_not_saved`), sie wurde mit identischem Inhalt neu geschrieben, keine erfasste Datei passt, oder der Pfad existiert, aber seine Endung wird nicht erfasst.

Git Bash unter Windows macht aus Argumenten wie `/Game/X` den Pfad `C:/Program Files/Git/Game/X`. `engine-proof` erkennt das und macht es rückgängig.

### Godot

```
engine-proof godot path/to/game --godot /path/to/Godot_v4.x   # oder Umgebungsvariable GODOT
```

Statische Prüfungen, immer:

- doppelte `class_name`-Deklarationen, auch in Sicherungskopien (Ordner mit einer `.gdignore`-Datei werden übersprungen, weil Godot sie ebenfalls überspringt); der Klassen-Cache in `.godot/` zeigt, welche Kopie registriert wurde
- doppelt kodiertes UTF-8 (Mojibake), ungültiges UTF-8, UTF-16-Dateien, UTF-8-BOM
- gemischte Zeilenenden in einer Datei

Mit einem Godot-Programm (`--godot PFAD` oder Umgebungsvariable `GODOT`; ein Ordner, der das Programm enthält, geht auch):

1. `godot --headless --path P --import`, wenn der globale Klassen-Cache fehlt oder älter ist als ein Skript. Ohne diesen Schritt scheitert ein Skript, das einen im selben Zug neu angelegten `class_name` benutzt, mit «Identifier not declared».
2. `godot --headless --path P --quit-after N` startet die Hauptszene. Hat das Projekt keine Hauptszene, entfällt dieser Schritt, denn Godot meldet dann «Can't run project» und beendet sich nicht.
3. Ein kleines Ladeskript (`godot_check.gd`, im Paket enthalten) lädt alle `.gd`, `.tscn` und `.tres` ohne Cache in einem einzigen Prozess. Parse-Fehler, fehlende Ressourcen und ungültiges Unicode werden aus der Godot-Ausgabe gelesen. Godot übersetzt diese Fehlerzeilen nicht, deshalb funktioniert das auch mit einem deutschen Editor.
4. `--test-scene res://tests/run.tscn` (oder ein `.gd`, das von `SceneTree` erbt) führt einen Test aus und hält seinen Exitcode fest. Ein Code ungleich 0 oder eine Zeitüberschreitung gilt als Fehler.

`verify` lädt nur die geänderten Skripte und Szenen, wenn ein Godot-Programm angegeben ist (`--godot` oder `GODOT`; `--no-engine` schaltet das ab).

### Unreal

```
engine-proof unreal path/to/MyGame [--since snap.json --expect "/Game/Blueprints/BP_Door changed"]
```

Ohne Editor:

- Durchsucht das Editor-Log (das neueste `Saved/Logs/*.log`, mit `--since` nur die Zeilen nach dem Snapshot). Gesucht wird nach fehlgeschlagenem Speichern, Meldungen zu Schreibschutz und verweigertem Zugriff, Sperren der Versionsverwaltung, Fehlern und Warnungen des Blueprint-Compilers, Python-Tracebacks (zu einem Befund zusammengefasst), gescheitertem Anlegen von Assets, gescheitertem Laden, Abstürzen und übrigen `Error:`-Zeilen. Nennt ein Befund ein Asset, dessen Datei schreibgeschützt ist, steht das dabei.
- Schreibgeschützte `.uasset`/`.umap`-Dateien, zusammen mit den `lockable`-Mustern aus `.gitattributes`.
- Editorsprache: `[Internationalization]` in `Config/*.ini` und `Saved/Config/**.ini` sowie Kulturzeilen im Log. Ein nicht englischer Editor ergibt eine Warnung, weil Suchen nach Anzeigenamen aus Python dort still ins Leere gehen.
- Mit `--since`: dieselben Erwartungsprüfungen wie `verify`.

Im Editor: `unreal_probe.py`. Den Pfad zeigt `engine-proof probe-path`, kopieren lässt es sich mit `engine-proof probe-path --copy ORDNER`. Das Skript läuft in der Python-Konsole des Editors oder über jeden MCP-Server, der Python im Editor ausführen kann:

```
py "path/to/unreal_probe.py" /Game/Blueprints/BP_Door /Game/Maps/Main --out probe.json
```

Für jedes Asset meldet es, ob es in der Asset-Registry steht, ob die Paketdatei auf der Festplatte liegt, ob sie schreibgeschützt ist, ob das Paket ungespeicherte Änderungen hat, den Compile-Status eines Blueprints und ob eine erzeugte Klasse existiert, dazu die Kultur des Editors. Das JSON steht zwischen `ENGINE_PROOF_PROBE_BEGIN` und `ENGINE_PROOF_PROBE_END`. Zurück geht es als JSON-Datei oder als Log, das den Block enthält:

```
engine-proof unreal path/to/MyGame --probe-result probe.json
```

### Ausgabe

`--format text` (Standard) oder `--format json`. Das JSON ist ein einzelnes Urteil mit Befunden, jeder mit `id`, `severity`, `message`, `file` und `evidence`. Ein vollständiges Beispiel steht im [englischen README](README.md#output).

## Beispielausgabe

Echter Lauf mit Godot 4.7.1 auf einem künstlichen Testprojekt (Belegzeilen gekürzt, ein Eintrag weggelassen):

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

Die Ausgabe von `engine-proof` ist englisch. Ein Unreal-Beispiel (gesperrtes Blueprint, dessen Speichern verworfen wurde) steht im [englischen README](README.md#example-output).

## Einbindung in Agenten

### Hook für Claude Code

`engine-proof hook` liest die Hook-Daten, die Claude Code über stdin schickt. Bei `PreToolUse` erneuert es einen Snapshot des Projekts, bei `PostToolUse` prüft es gegen diesen Snapshot und speichert danach den neuen Stand. Bei `Write`, `Edit`, `MultiEdit` und `NotebookEdit` erwartet es, dass die bearbeitete Datei tatsächlich geschrieben wurde. Das Projekt findet es, indem es von der bearbeiteten Datei oder vom Arbeitsverzeichnis aufwärts nach einem Ordner mit `project.godot` oder einer `.uproject`-Datei sucht. Ausserhalb von Engine-Projekten tut der Hook nichts.

- Fehler: Exitcode 2, Bericht auf stderr. Claude Code zeigt ihn dem Modell.
- Warnungen: Exitcode 0 mit `hookSpecificOutput.additionalContext` auf stdout.
- nichts gefunden: Exitcode 0, keine Ausgabe.

Die Zustandsdatei liegt im temporären Ordner des Systems (`engine-proof/<hash>.json`), ins Projekt wird nichts geschrieben. Ab dem zweiten Snapshot werden gespeicherte Hashes weiterverwendet, solange Grösse und Änderungszeit gleich sind. Ein Hook-Aufruf liest also nur geänderte Dateien.

In `.claude/settings.json` des Spielprojekts (oder in die Benutzereinstellungen) eintragen. Liegt `engine-proof` nicht im `PATH`, den vollen Pfad angeben. Der Matcher `mcp__.*` deckt MCP-Server ab, die den Editor steuern.

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

Bei Godot lädt `engine-proof hook --godot-load` mit gesetztem `GODOT` die geänderten Skripte zusätzlich in der Engine. Das kostet bei jedem Werkzeugaufruf einen Godot-Start (bei einem kleinen Projekt deutlich unter einer Sekunde).

### Codex und andere Agenten

Die Kommandozeile genügt. Die Regel kommt in die `AGENTS.md` des Spielprojekts, zum Beispiel:

```
After you change files in this Godot/Unreal project:
1. Before the change: engine-proof snapshot . --out .engine-proof/snap.json
2. After the change:  engine-proof verify . --since .engine-proof/snap.json --expect "<file you changed> changed"
3. Exit code 1 means the change did not land or broke something. Read the findings and fix them.
   Do not report success before verify exits with 0.
For Unreal editor changes, also run unreal_probe.py in the editor (engine-proof probe-path)
and pass its output with engine-proof unreal . --probe-result probe.json
```

Ein einzelner Lauf: `codex exec "Rename the door Blueprint variable, then prove it with engine-proof verify"`. Agenten ohne Hook-Daten rufen `engine-proof hook --event pre --project .` und `engine-proof hook --event post --project . --expect "..."` auf.

Liegen die Snapshots im Projekt, gehört `.engine-proof/` in die `.gitignore`.

## Grenzen

- `unreal_probe.py` lief während der Entwicklung in keinem Unreal-Editor, es stand keiner zur Verfügung. Die reinen Teile (Pfadzuordnung, Statuszuordnung, Bewertung der Probleme, JSON aus Logs herauslösen, Argumente) sind mit Unit-Tests abgedeckt. Die Editor-Aufrufe (`EditorAssetLibrary`, `EditorLoadingAndSavingUtils.get_dirty_content_packages`, `load_blueprint_class`) unterscheiden sich zwischen Engine-Versionen und stehen in `try`-Blöcken. Der Blueprint-`status` ist nicht in jeder Version aus Python lesbar; dann meldet die Probe `UNKNOWN` und stützt sich auf die Prüfung der erzeugten Klasse. Paketpfade ausserhalb von `/Game/` (Plugin-Inhalte) werden keiner Datei zugeordnet.
- Die Unreal-Logmuster beruhen auf bekannten Meldungsformaten und sind mit künstlichen Logzeilen getestet, nicht mit Logs verschiedener Engine-Versionen. Allgemeine `Error:`-Zeilen können Fehlalarme auslösen (sie erscheinen als Warnung), und Meldungen mit ungewohntem Wortlaut können durchrutschen.
- Die Erkennung der Kultur im Log deckt nur wenige Log-Kategorien ab; die Prüfung der `.ini`-Dateien ist verlässlicher.
- Die Godot-Integrationstests liefen mit Godot 4.7.1 unter Windows. Andere 4.x-Versionen sollten funktionieren (fehlt der Schalter `--import`, hilft `--import never`); Godot 3 wird nicht unterstützt.
- Die Änderungserkennung übernimmt wie git die Hashes aus dem früheren Snapshot, wenn Grösse und Änderungszeit gleich sind. Dateien, die weniger als 3 Sekunden vor diesem Snapshot geändert wurden, werden immer neu gehasht; ein Neuschreiben mit gleicher Grösse im selben Zeitstempel-Takt fällt also auf. Setzt ein Werkzeug nach dem Neuschreiben die alte Änderungszeit zurück, bleibt es unbemerkt. `snapshot` ohne `--reuse` berechnet immer alle Hashes neu, `--no-hash` vergleicht nur Grösse und Änderungszeit.
- Der Schreibschutz wird an den Modusbits der Datei erkannt. Scheitert ein Speichern aus einem anderen Grund (Datei von einem anderen Prozess gesperrt, Virenscanner), zeigt sich das nur im Log oder in der Probe.
- Die Mojibake-Erkennung sucht nach UTF-8, das als cp1252 oder latin-1 gelesen wurde. Andere Verwechslungen der Kodierung erkennt sie nicht.
- Die Ladeprüfung führt Ladecode in der Engine aus. `@tool`-Skripte und statische Initialisierungen können dabei laufen.

## Entwicklung

```
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
GODOT=/path/to/godot .venv/bin/python -m pytest -q tests/test_godot_integration.py
```

Ohne `GODOT` werden die Integrationstests übersprungen.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
