"""Finding and verdict objects plus text/json rendering."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import __version__
from .redact import MAX_LINE_CHARS, clip, redact, safe_evidence, safe_message

SEVERITIES = ("error", "warning", "info")
_RANK = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    id: str
    severity: str
    message: str
    file: str | None = None
    line: int | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity}")

    def sanitized(self) -> "Finding":
        """Copy with secrets redacted and message/evidence capped; used for all output."""
        return Finding(self.id, self.severity, safe_message(self.message),
                       file=redact(self.file), line=self.line,
                       evidence=safe_evidence(self.evidence) if self.evidence else self.evidence)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self.sanitized())
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Verdict:
    command: str
    project: str
    engine: str
    findings: list[Finding] = field(default_factory=list)
    changes: dict[str, list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    strict: bool = False

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            c[f.severity] += 1
        return c

    @property
    def ok(self) -> bool:
        c = self.counts()
        if c["error"]:
            return False
        if self.strict and c["warning"]:
            return False
        return True

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (_RANK[f.severity], f.file or "", f.line or 0, f.id),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "tool": "engine-proof",
            "version": __version__,
            "command": self.command,
            "project": self.project,
            "engine": self.engine,
            "ok": self.ok,
            "summary": self.counts(),
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }
        if self.changes is not None:
            d["changes"] = self.changes
        d.update(self.extra)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_text(self, show_info: bool = True) -> str:
        c = self.counts()
        status = "OK" if self.ok else "FAIL"
        lines = [
            f"engine-proof {self.command}: {status} "
            f"({c['error']} error(s), {c['warning']} warning(s), {c['info']} info)"
        ]
        lines.append(f"  project: {self.project} [{self.engine}]")
        if self.changes is not None:
            parts = [f"{k} {len(v)}" for k, v in self.changes.items()]
            lines.append("  changes: " + ", ".join(parts))
        for f in self.sorted_findings():
            if f.severity == "info" and not show_info:
                continue
            f = f.sanitized()
            where = f.file or "-"
            if f.line:
                where += f":{f.line}"
            lines.append(f"  {f.severity.upper():<7} {f.id}  {where}")
            lines.append(f"          {f.message}")
            if f.evidence:
                for ev in f.evidence.splitlines()[:6]:
                    lines.append(f"          | {clip(ev, MAX_LINE_CHARS // 2)}")
        return "\n".join(lines)

    def render(self, fmt: str) -> str:
        return self.to_json() if fmt == "json" else self.to_text()
