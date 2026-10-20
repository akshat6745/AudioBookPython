"""Stable process exit codes for the audiobook worker."""

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INCOMPLETE = 2
EXIT_INVALID = 3
EXIT_SETUP = 4
EXIT_INTERRUPTED = 130

INCOMPLETE_STATUSES = frozenset({"missing", "leased", "publishing"})
INVALID_STATUSES = frozenset({"invalid", "stale", "hash_mismatch", "conflict"})


def merge_codes(*codes: int) -> int:
    """Prefer setup, then invalid/conflict, then incomplete, then unexpected."""
    present = [c for c in codes if c]
    if not present:
        return EXIT_OK
    if EXIT_SETUP in present:
        return EXIT_SETUP
    if EXIT_INVALID in present:
        return EXIT_INVALID
    if EXIT_INCOMPLETE in present:
        return EXIT_INCOMPLETE
    if EXIT_INTERRUPTED in present and set(present) <= {EXIT_INTERRUPTED}:
        return EXIT_INTERRUPTED
    if EXIT_INTERRUPTED in present:
        return EXIT_INTERRUPTED
    return EXIT_UNEXPECTED


def code_for_statuses(statuses: list[str]) -> int:
    if any(s in INVALID_STATUSES for s in statuses):
        return EXIT_INVALID
    if any(s in INCOMPLETE_STATUSES for s in statuses):
        return EXIT_INCOMPLETE
    if any(s not in {"valid", "legacy_valid", "skipped"} for s in statuses):
        return EXIT_UNEXPECTED
    return EXIT_OK
