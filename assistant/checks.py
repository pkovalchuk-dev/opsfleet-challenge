"""Deterministic output safeguards; SQL policy remains the access boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"(?<![\w/.-])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?![\w/.-])")
STREET = re.compile(r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Lane|Ln\.?|Drive|Dr\.?|Boulevard|Blvd\.?|Court|Ct\.?|Way|Place|Pl\.?)\b")
COORDS = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}(?![\d.])")
PII_PATTERNS = (("email", EMAIL), ("phone", PHONE), ("street address", STREET), ("coordinates", COORDS))


@dataclass
class CheckResult:
    ok: bool = True
    blocked_reason: str = ""
    findings: list[str] = field(default_factory=list)


def find_pii(text: str) -> list[str]:
    findings = []
    for name, pattern in PII_PATTERNS:
        if pattern.search(text):
            findings.append(name)
    return findings


def redact_pii(text: str) -> str:
    """Remove recognized personal data from titles and other display labels."""
    for _, pattern in PII_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def run_output_check(answer: str) -> CheckResult:
    result = CheckResult()
    if not answer.strip():
        return CheckResult(ok=False, blocked_reason="empty answer", findings=["empty answer"])
    pii = find_pii(answer)
    if pii:
        result.ok = False
        result.blocked_reason = "personal data"
        result.findings.extend(f"pii:{p}" for p in pii)
        return result
    return result


def rows_to_markdown(columns: list[str], rows: list[dict[str, Any]], max_rows: int = 50,
                     max_cell_chars: int = 500) -> str:
    if not rows:
        return "_(no rows)_"
    cols = columns or list(rows[0].keys())

    shortened = False

    def cell(text: str) -> str:
        nonlocal shortened
        if len(text) > max_cell_chars:
            text = text[:max_cell_chars] + " …"
            shortened = True
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")

    def fmt(column: str, value: Any) -> str:
        if value is None:
            return ""
        key = column.strip().lower().replace(" ", "_")
        if key == "id" or key.endswith("_id"):
            return cell(str(value))
        if isinstance(value, float):
            return f"{value:,.2f}" if abs(value) >= 1 or value == 0 else f"{value:.4f}"
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value:,}"
        return cell(str(value))

    lines = ["| " + " | ".join(cell(c) for c in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows[:max_rows]:
        lines.append("| " + " | ".join(fmt(c, row.get(c)) for c in cols) + " |")
    if len(rows) > max_rows:
        lines.append(f"\n_{len(rows) - max_rows} more rows not shown_")
    if shortened:
        lines.append(f"\n_Text cells longer than {max_cell_chars} characters were shortened._")
    return "\n".join(lines)
