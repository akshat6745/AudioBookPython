"""Command line for the R2 audiobook worker.

Examples:
  ./audiobook-worker run --novel "The Eye of the World" --stage tag --workers 4 --batch-size 32 --validate
  ./audiobook-worker validate --novel "The Eye of the World" --stage tag
  ./audiobook-worker run --novel "The Eye of the World" --stage audio --workers 2 --batch-size 4 --validate
  ./audiobook-worker validate --novel "The Eye of the World" --stage audio
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .exitcodes import EXIT_INTERRUPTED, EXIT_OK, EXIT_SETUP, EXIT_UNEXPECTED

COMMANDS = ("run", "validate", "status", "setup", "publish-runtime", "publish-novel", "list")
HELP_FLAGS = {"-h", "--help"}


def _add_noninteractive(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--non-interactive",
        action="store_true",
        help="do not prompt for R2 settings; missing names exit 4",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiobook-worker")
    parser.add_argument("--novel", help="registered novel title, used when no subcommand is given")
    parser.add_argument("--list", action="store_true", help="list registered novels on R2 and exit")
    _add_noninteractive(parser)
    sub = parser.add_subparsers(dest="command")

    def add_common(cmd: argparse.ArgumentParser, default_stage: str) -> None:
        _add_noninteractive(cmd)
        cmd.add_argument("--novel", required=True)
        cmd.add_argument("--stage", default=default_stage, choices=("tag", "audio", "render", "all"),
                         help="tag, audio (alias render), or all. Default: %(default)s")
        cmd.add_argument("--workers", type=int, default=1, help="local worker processes. Default 1. Not the batch size.")
        cmd.add_argument("--chapters", help="1-54 or 1,2,5-7. Must be inside the registered source manifest.")
        cmd.add_argument("--report-json", type=Path)
        cmd.add_argument("--device", default="auto", choices=("auto", "cuda", "mps", "cpu"))

    run = sub.add_parser("run", help="tag and/or render chapters that are missing on R2")
    add_common(run, "all")
    run.add_argument("--batch-size", type=int, help="tag: paragraphs per forward. audio: max TTS chunks per forward.")
    run.add_argument("--tag-batch-size", type=int, help="overrides --batch-size for tagging, including --stage all")
    run.add_argument("--audio-batch-size", type=int, help="overrides --batch-size for audio, including --stage all")
    run.add_argument("--validate", action="store_true", help="after the stage, validate it and exit non-zero on failure")
    run.add_argument("--status", action="store_true", help="validate only; do not tag or render")
    run.add_argument("--allow-partial-tags", action="store_true",
                     help="audio stage renders chapters that already have valid tags. Default is to stop if any requested chapter is untagged.")
    run.add_argument("--replace-stale", action="store_true",
                     help="replace chapters from an older voice bank in place under chapter_<n>/ so the player uses the new voice.")
    run.add_argument("--reset-batch-profile", action="store_true")

    validate = sub.add_parser("validate", help="check R2 manifests and payloads; do not generate")
    add_common(validate, "all")
    status = sub.add_parser("status", help="alias of validate")
    add_common(status, "all")

    setup = sub.add_parser("setup", help="create the local cache: StyleTTS2 commit, patch, artifact download")
    _add_noninteractive(setup)

    list_cmd = sub.add_parser("list", help="list registered novels on R2")
    _add_noninteractive(list_cmd)

    pub_rt = sub.add_parser("publish-runtime", help="upload versioned private runtime artifacts")
    _add_noninteractive(pub_rt)
    pub_rt.add_argument("--checkpoint", type=Path, required=True)
    pub_rt.add_argument("--config", type=Path, required=True)
    pub_rt.add_argument("--asr", type=Path, required=True)
    pub_rt.add_argument("--asr-config", type=Path, required=True)
    pub_rt.add_argument("--jdc", type=Path, required=True)
    pub_rt.add_argument("--plbert", type=Path, required=True)
    pub_rt.add_argument("--plbert-config", type=Path, required=True)
    pub_rt.add_argument("--bank-npz", type=Path, required=True)
    pub_rt.add_argument("--bank-json", type=Path, required=True)
    pub_rt.add_argument("--deberta-large", type=Path, required=True)
    pub_rt.add_argument("--deberta-base", type=Path, required=True)

    pub_novel = sub.add_parser("publish-novel", help="register a private novel source and optional tags")
    _add_noninteractive(pub_novel)
    pub_novel.add_argument("--name", required=True)
    pub_novel.add_argument("--source-dir", type=Path, required=True)
    pub_novel.add_argument("--tags-dir", type=Path)
    pub_novel.add_argument("--chapters", required=True)
    pub_novel.add_argument("--slug")
    return parser


def _prepare_argv(argv: list[str]) -> list[str]:
    """Keep top-level help as top-level help. A leading option otherwise selects ``run``."""
    if not argv or argv[0] in HELP_FLAGS:
        return argv
    noninteractive = False
    if argv[0] == "--non-interactive":
        noninteractive = True
        argv = list(argv[1:])
        while argv and argv[0] == "--non-interactive":
            argv = argv[1:]
        if not argv or argv[0] in HELP_FLAGS:
            return argv
    if argv[0] in COMMANDS:
        if noninteractive:
            return [argv[0], "--non-interactive", *argv[1:]]
        return argv
    if argv[0].startswith("--") and argv[0] != "--list":
        head = ["run", "--non-interactive"] if noninteractive else ["run"]
        return [*head, *argv]
    if noninteractive:
        return ["--non-interactive", *argv]
    return argv


def _status_from_exit(exc: SystemExit) -> int:
    """Argparse help uses status 0. Do not turn that into the setup code."""
    code = exc.code
    if code is None or code == 0:
        return EXIT_OK
    if isinstance(code, int):
        return code
    print(exc)
    return EXIT_SETUP


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = _prepare_argv(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    if getattr(args, "list", False):
        args.command = "list"
    if args.command is None:
        raise SystemExit("choose run, validate, status, setup, list, publish-runtime, or publish-novel")
    if args.command == "status":
        args.command = "validate"
    stage = getattr(args, "stage", None)
    if stage == "render":
        args.stage = "audio"
    if getattr(args, "workers", 1) < 1:
        raise SystemExit("--workers must be >= 1")
    if getattr(args, "status", False):
        args.status_only = True
        args.command = "validate"
    else:
        args.status_only = False
    if not hasattr(args, "batch_size"):
        args.batch_size = None
        args.tag_batch_size = None
        args.audio_batch_size = None
        args.validate = False
        args.allow_partial_tags = False
        args.replace_stale = False
        args.reset_batch_profile = False
    if not hasattr(args, "report_json"):
        args.report_json = None
    if not hasattr(args, "non_interactive"):
        args.non_interactive = False
    return args


def _open_store(args, store):
    """Return (store, credentials). Credentials stay in memory and are not written out."""
    if store is not None:
        return store, None
    from .credentials import load_credentials
    from .store import BotoStore

    creds = load_credentials(prompt=False if args.non_interactive else None)
    return BotoStore.connect(creds), creds


def main(argv: list[str] | None = None, store=None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return _status_from_exit(exc)
    from .credentials import CredentialsError

    try:
        store, creds = _open_store(args, store)
    except CredentialsError as exc:
        print(str(exc))
        return EXIT_SETUP
    if args.command == "setup":
        from .bootstrap import setup

        setup(store)
        return 0
    if args.command == "list":
        from .registry import load_index

        index = load_index(store)
        novels = index.get("novels") or []
        for item in novels:
            chapters = item.get("chapters") or []
            print(f"{item['slug']:48}  ({len(chapters):4d} ch)  {item['name']}")
        return 0
    if args.command == "publish-runtime":
        from .publish import publish_runtime

        return publish_runtime(args, store)
    if args.command == "publish-novel":
        from .publish import publish_novel

        return publish_novel(args, store)
    from .orchestrate import execute

    try:
        inline = False if store.__class__.__name__ == "BotoStore" else True
        return int(execute(args, store, inline=inline, credentials=creds))
    except CredentialsError as exc:
        print(str(exc))
        return EXIT_SETUP
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as exc:
        from .redact import redact

        print(redact(f"{type(exc).__name__}: {exc}"))
        return EXIT_UNEXPECTED
