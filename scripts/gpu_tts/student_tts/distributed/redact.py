"""Redact credential material from logs and reports."""

from __future__ import annotations

_SECRETS: tuple[str, ...] = ()


def install(values: list[str] | tuple[str, ...]) -> None:
    global _SECRETS
    found = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if len(text) >= 6 and text not in found:
            found.append(text)
    _SECRETS = tuple(found)


def clear() -> None:
    global _SECRETS
    _SECRETS = ()


def redact(text: str) -> str:
    out = text
    for secret in _SECRETS:
        if secret and secret in out:
            out = out.replace(secret, "***")
    return out


class RedactFilter:
    """logging.Filter compatible wrapper."""

    def filter(self, record) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True
