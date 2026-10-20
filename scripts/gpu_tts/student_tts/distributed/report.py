"""Machine-readable run reports. Credential values are redacted before the file is written."""

from __future__ import annotations

import json
from pathlib import Path

from .exitcodes import code_for_statuses
from .redact import redact


def counts_for(rows: list[dict]) -> dict[str, int]:
    keys = (
        "valid",
        "legacy_valid",
        "missing",
        "leased",
        "publishing",
        "stale",
        "hash_mismatch",
        "conflict",
        "invalid",
        "skipped",
    )
    counts = {key: 0 for key in keys}
    for row in rows:
        status = row.get("status", "invalid")
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_report(*, novel: str, slug: str, stage: str, rows: list[dict], extra: dict | None = None) -> dict:
    statuses = [row.get("status", "invalid") for row in rows]
    report = {
        "novel": novel,
        "slug": slug,
        "stage": stage,
        "chapters": rows,
        "counts": counts_for(rows),
        "exit_code": code_for_statuses(statuses) if rows else 0,
    }
    if extra:
        report.update(extra)
    return report


def write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    text = redact(json.dumps(report, indent=1, sort_keys=True) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def format_summary(report: dict) -> str:
    counts = report.get("counts") or {}
    interesting = {key: value for key, value in counts.items() if value}
    lines = [
        f"{report.get('stage')} {report.get('novel')}: exit {report.get('exit_code')} {interesting or {'valid': 0}}",
    ]
    for row in report.get("chapters") or []:
        if row.get("status") not in {"valid", "legacy_valid", "skipped"}:
            detail = row.get("detail") or ""
            lines.append(f"  chapter {row.get('chapter')}: {row.get('status')} {detail}".rstrip())
    return redact("\n".join(lines))
