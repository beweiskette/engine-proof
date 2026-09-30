"""Secrets in engine logs and engine output must never reach the report or the agent.

All secrets below are synthetic. Token-shaped values are assembled at runtime so the
source file does not contain strings that look like real credentials to scanners.
"""

import io
import json
import sys

import pytest

from engine_proof import godot, unreal, unreal_probe
from engine_proof.cli import main
from engine_proof.findings import Finding, Verdict

from conftest import write

API_KEY = "SYNTHKEY" + "0123456789abcdef"
BEARER = "synthBearer" + "0123456789abcdefXYZ"
OPENAI_LIKE = "sk" + "-proj-" + "Synth0123456789abcdefghijklmnop"
GITHUB_LIKE = "gh" + "p_" + "Synth0123456789abcdefghijklmnopqrstu"
GITHUB_PAT = "github" + "_pat_" + "11SYNTH0123456789_abcdefghijklmnopqrstuvwxyz"
AWS_LIKE = "AK" + "IA" + "SYNTHETIC0123456"
SLACK_LIKE = "xo" + "xb-" + "0123456789-synthetic-token"
JWT_LIKE = "ey" + "JhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJzeW50aCJ9" + ".c3ludGhldGljc2lnbmF0dXJl"
PASSWORD = "SynthPassw0rd!"
URL_PASS = "urlSecret" + "42"
CLI_TOKEN = "cliToken" + "0987654321"
USER_NAME = "synthuser"
HOME_WIN = "C:" + "\\Users\\" + USER_NAME
HOME_NIX = "/home/" + USER_NAME
PRIVATE_KEY_BODY = "MIIEsynthetic" + "privatekeybody0123456789"

SECRETS = [API_KEY, BEARER, OPENAI_LIKE, GITHUB_LIKE, GITHUB_PAT, AWS_LIKE, SLACK_LIKE, JWT_LIKE,
           PASSWORD, URL_PASS, CLI_TOKEN, USER_NAME, PRIVATE_KEY_BODY]

P = "[2026.09.30-10.02.00:000][ 30]"
SECRET_LOG = "\n".join([
    f"{P}LogHttp: Error: Request failed: https://api.example.com/v1/upload?api_key={API_KEY}&mode=x",
    f"{P}LogSavePackage: Error: Failed to save package '/Game/A' Authorization: Bearer {BEARER}",
    f"{P}LogPython: Error: Traceback (most recent call last):",
    f"{P}LogPython: Error:   File \"{HOME_WIN}\\scripts\\tool.py\", line 3, in <module>",
    f"{P}LogPython: Error: AuthenticationError: invalid key {OPENAI_LIKE}",
    f"{P}LogSourceControl: Error: push failed for https://x-access-token:{GITHUB_LIKE}@example.com/r.git",
    f"{P}LogSourceControl: Error: could not check out, token {GITHUB_PAT}",
    f"{P}LogDerivedDataCache: Error: cannot connect: Server=db.example.com;User Id=editor;Password={PASSWORD};",
    f"{P}LogInit: Error: Command Line: -project=x -auth_token={CLI_TOKEN} -aws={AWS_LIKE}",
    f"{P}LogHttp: Error: webhook https://editor:{URL_PASS}@hooks.example.com failed, slack {SLACK_LIKE}",
    f"{P}LogSavePackage: Error: Failed to save {HOME_NIX}/proj/Content/A.uasset jwt={JWT_LIKE}",
    # The secret straddles the 240 character cut applied to messages.
    f"{P}LogSavePackage: Error: Failed to save package " + "x" * 200 + f" password={PASSWORD}",
    f"{P}LogOnline: Error: -----BEGIN RSA PRIVATE KEY----- {PRIVATE_KEY_BODY} -----END RSA PRIVATE KEY-----",
]) + "\n"


def assert_clean(text):
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"secrets leaked: {leaked}"


def test_unreal_log_secrets_not_in_json_or_text(unreal_project, capsys):
    write(unreal_project, "Saved/Logs/Fixture.log", SECRET_LOG.encode("utf-8"))
    main(["unreal", str(unreal_project), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["findings"], "log lines should still produce findings"
    assert_clean(out)
    assert "[REDACTED]" in out
    main(["unreal", str(unreal_project)])
    out = capsys.readouterr().out
    assert_clean(out)
    # Non-secret context stays readable.
    assert "api.example.com" in out and "db.example.com" in out


def test_scan_log_lines_redacts_before_truncating():
    fs = unreal.scan_log_lines(SECRET_LOG.splitlines(), "Saved/Logs/Fixture.log")
    for f in fs:
        assert_clean(f.message + "\n" + (f.evidence or ""))
    cut = [f for f in fs if "x" * 50 in f.message]
    assert cut
    # A partial secret must not survive the cut either.
    assert "SynthPass" not in cut[0].message and "SynthPass" not in (cut[0].evidence or "")


def test_godot_output_secrets_redacted():
    out = "\n".join([
        f"ERROR: HTTPRequest failed: https://example.com/api?access_token={API_KEY}",
        f"   at: _request (res://net/client.gd:12)",
        f"USER ERROR: auth failed with header Authorization: Bearer {BEARER}",
        f"ERROR: Cannot open file '{HOME_WIN}\\AppData\\Roaming\\Godot\\x.cfg'.",
    ])
    fs = godot.parse_godot_output(out, "boot")
    assert len(fs) == 3
    v = Verdict("godot", "proj", "godot", findings=fs)
    assert_clean(v.to_json())
    assert_clean(v.to_text())


def test_environment_dump_is_not_forwarded():
    dump = "\n".join([
        "boot failed",
        f"OPENAI_API_KEY={OPENAI_LIKE}",
        f"HOME={HOME_NIX}",
        "PATH=/usr/bin:/bin",
        "EDITOR=vim",
        "LANG=C.UTF-8",
        "ERROR: Script failed",
    ])
    v = Verdict("godot", "proj", "godot",
                findings=[Finding("godot.test_failed", "error", "test exited with code 1", evidence=dump)])
    for text in (v.to_json(), v.to_text()):
        assert_clean(text)
        assert "PATH=" not in text and "EDITOR=vim" not in text
        assert "environment" in text
        assert "ERROR: Script failed" in text


def test_evidence_and_message_are_capped():
    huge = "\n".join(f"line {i}: " + "y" * 300 for i in range(500))
    v = Verdict("godot", "proj", "godot",
                findings=[Finding("godot.test_failed", "error", "m" * 5000, evidence=huge)])
    d = json.loads(v.to_json())
    f = d["findings"][0]
    assert len(f["message"]) <= 600
    assert len(f["evidence"]) <= 2100
    assert "truncated" in f["evidence"]


def _hook(monkeypatch, capsys, payload, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = main(["hook", *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_hook_redacts_and_caps_output(tmp_path, unreal_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    pl = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(unreal_project), "tool_input": {}}
    _hook(monkeypatch, capsys, pl, "--state", state)
    log = unreal_project / "Saved/Logs/Fixture.log"
    many = "".join(f"{P}LogSavePackage: Error: Failed to save package '/Game/M{i}' token={CLI_TOKEN}"
                   + " z" * 150 + "\n" for i in range(400))
    with log.open("ab") as fh:
        fh.write(SECRET_LOG.encode("utf-8") + many.encode("utf-8"))
    pl["hook_event_name"] = "PostToolUse"
    code, out, err = _hook(monkeypatch, capsys, pl, "--state", state)
    assert code == 2
    assert_clean(out + err)
    assert "unreal.log.save_failed" in err
    assert len(err) <= 8200
    assert "truncated" in err


def test_hook_additional_context_is_capped(tmp_path, clean_godot_project, monkeypatch, capsys):
    state = str(tmp_path / "state.json")
    pl = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(clean_godot_project),
          "tool_input": {}}
    _hook(monkeypatch, capsys, pl, "--state", state)
    for i in range(300):
        write(clean_godot_project, f"scripts/m{i:03d}_" + "n" * 60 + ".gd", b"extends Node\r\nvar a = 1\n")
    pl["hook_event_name"] = "PostToolUse"
    code, out, err = _hook(monkeypatch, capsys, pl, "--state", state)
    assert code == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "text.mixed_eol" in ctx
    assert len(ctx) <= 8200
    assert "truncated" in ctx


def test_probe_result_secrets_redacted(unreal_project, tmp_path, capsys):
    rec = {"asset": "/Game/A", "package": "/Game/A", "exists_in_registry": True, "on_disk": True,
           "errors": [f"load_asset failed: {HOME_WIN}\\x password={PASSWORD} key {OPENAI_LIKE}"]}
    result = unreal_probe.build_result([rec])
    text = json.dumps(result)
    assert_clean(text)  # the probe itself must not print secrets inside the editor
    raw = {"tool": "engine-proof-probe", "assets": [
        {"asset": "/Game/A", "package": "/Game/A", "exists_in_registry": True, "on_disk": True,
         "errors": [f"query failed: Authorization: Bearer {BEARER} {GITHUB_LIKE}"]}]}
    pr = tmp_path / "probe.json"
    pr.write_text(json.dumps(raw), encoding="utf-8")
    main(["unreal", str(unreal_project), "--probe-result", str(pr), "--format", "json"])
    out = capsys.readouterr().out
    assert "probe.query_error" in out
    assert_clean(out)


@pytest.mark.parametrize("fn", ["package", "probe"])
def test_redact_functions_agree_on_corpus(fn):
    from engine_proof.redact import redact
    f = redact if fn == "package" else unreal_probe.redact_text
    for line in SECRET_LOG.splitlines():
        assert_clean(f(line))


def test_redact_keeps_ordinary_text():
    from engine_proof.redact import redact
    for s in ["LogSavePackage: Error: Failed to save package '/Game/Blueprints/BP_Door'",
              "res://scripts/player.gd:12 - Parse Error: Expected expression",
              "crlf=3 lf=2 cr=0",
              "line 4: 'Ã¼' (U+00C3 U+00BC) should be 'ü'",
              "[Internationalization] culture=de"]:
        assert redact(s) == s


def test_probe_rules_match_package_rules():
    from engine_proof import redact
    assert unreal_probe.REDACT_RULES == redact.RULES, "sync REDACT_RULES in unreal_probe.py with redact.RULES"


def test_redaction_keeps_context_around_secrets():
    from engine_proof.redact import redact
    s = redact(f"push failed for https://x-access-token:{GITHUB_LIKE}@example.com/r.git")
    assert GITHUB_LIKE not in s and "@example.com/r.git" in s
    s = redact(f'{{"api_key": "{API_KEY}", "mode": "fast"}}')
    assert API_KEY not in s and '"mode": "fast"' in s
