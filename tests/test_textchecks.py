from engine_proof.textchecks import analyze_bytes, find_mojibake, findings_for, line_endings


def double_encode(s: str) -> str:
    return s.encode("utf-8").decode("cp1252")


def test_mojibake_detects_common_double_encodings():
    for original in ("Grün", "Café", "Straße", "50 €", "it’s", "naïve"):
        broken = double_encode(original)
        hits = find_mojibake(f'print("{broken}")')
        assert hits, original
        assert hits[0][2] in original


def test_mojibake_no_false_positive_on_correct_text():
    assert find_mojibake("Größe, Café, naïve, Ärger, 50 €, “quotes”") == []
    assert find_mojibake("Ã alone and © alone") == []


def test_mojibake_reports_line_numbers():
    text = "line one\nline two Ã¼\n"
    hits = find_mojibake(text)
    assert hits == [(2, "Ã¼", "ü")]


def test_line_endings():
    assert line_endings(b"a\nb\n")[0] == "lf"
    assert line_endings(b"a\r\nb\r\n")[0] == "crlf"
    assert line_endings(b"abc")[0] == "none"
    style, counts, minority = line_endings(b"a\r\nb\nc\r\nd\r\n")
    assert style == "mixed"
    assert counts == {"crlf": 3, "lf": 1, "cr": 0}
    assert minority == 2


def test_analyze_bom_and_invalid_utf8():
    info = analyze_bytes(b"\xef\xbb\xbfextends Node\n")
    assert info.encoding == "utf-8-sig" and info.bom
    info = analyze_bytes(b"print('Gr\xfcn')\n")
    assert info.encoding == "invalid-utf-8"
    assert info.first_invalid_offset == 9
    info = analyze_bytes("hi\r\n".encode("utf-16"))
    assert info.encoding.startswith("utf-16")


def test_findings_for_new_vs_preexisting():
    info = analyze_bytes(b"a\r\nb\n")
    new = findings_for("x.gd", info, previous={"eol": "crlf"})
    assert [(f.id, f.severity) for f in new] == [("text.mixed_eol", "warning")]
    old = findings_for("x.gd", info, previous={"eol": "mixed"})
    assert [(f.id, f.severity) for f in old] == [("text.mixed_eol", "info")]


def test_findings_eol_switch_and_bom_added():
    info = analyze_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    fs = {f.id: f.severity for f in findings_for("x.gd", info, previous={"eol": "lf", "bom": False})}
    assert fs == {"text.bom": "warning", "text.eol_changed": "warning"}


def test_findings_mojibake_is_error():
    info = analyze_bytes(double_encode('print("Grün")').encode("utf-8"))
    fs = findings_for("x.gd", info)
    assert fs[0].id == "text.mojibake" and fs[0].severity == "error"
    assert "ü" in fs[0].evidence
