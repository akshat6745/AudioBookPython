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

from .exitcodes import EXIT_INTERRUPTED, EXIT_SETUP, EXIT_UNEXPECTED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiobook-worker")
    parser.add_argument("--novel", help="registered novel title, used when no subcommand is given")
    sub = parser.add_subparsers(dest="command")

    def add_common(cmd: argparse.ArgumentParser, default_stage: str) -> None:
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
    run.add_argument("--reset-batch-profile", action="store_true")

    validate = sub.add_parser("validate", help="check R2 manifests and payloads; do not generate")
    add_common(validate, "all")
    status = sub.add_parser("status", help="alias of validate")
    add_common(status, "all")

    sub.add_parser("setup", help="create the local cache: StyleTTS2 commit, patch, artifact download")

    pub_rt = sub.add_parser("publish-runtime", help="upload versioned private runtime artifacts")
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
    pub_novel.add_argument("--name", required=True)
    pub_novel.add_argument("--source-dir", type=Path, required=True)
    pub_novel.add_argument("--tags-dir", type=Path)
    pub_novel.add_argument("--chapters", required=True)
    pub_novel.add_argument("--slug")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    if args.command is None:
        raise SystemExit("choose run, validate, status, setup, publish-runtime, or publish-novel")
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
        args.reset_batch_profile = False
    if not hasattr(args, "report_json"):
        args.report_json = None
    return args


def main(argv: list[str] | None = None, store=None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code or EXIT_SETUP
        print(exc)
        return EXIT_SETUP
    if args.command == "setup":
        from .bootstrap import setup
        from .credentials import CredentialsError, load_credentials
        from .store import BotoStore

        try:
            creds = load_credentials(prompt=False) if store is None else None
        except CredentialsError as exc:
            print(str(exc))
            return EXIT_SETUP
        setup(store or BotoStore.connect(creds))
        return 0
    if args.command == "publish-runtime":
        from .publish import publish_runtime

        return publish_runtime(args, store)
    if args.command == "publish-novel":
        from .publish import publish_novel

        return publish_novel(args, store)
    from .credentials import CredentialsError, load_credentials
    from .orchestrate import execute
    from .store import BotoStore

    try:
        if store is None:
            creds = load_credentials(prompt=False)
            store = BotoStore.connect(creds)
        inline = False if store.__class__.__name__ == "BotoStore" else True
        return int(execute(args, store, inline=inline))
    except CredentialsError as exc:
        print(str(exc))
        return EXIT_SETUP
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as exc:
        from .redact import redact

        print(redact(f"{type(exc).__name__}: {exc}"))
        return EXIT_UNEXPECTED
