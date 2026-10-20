"""Load R2 S3 credentials without placing them on argv or in logs."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .redact import install as install_redact

KEYS = ("R2_ENDPOINT_URL", "R2_BUCKET_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
SECRET_KEYS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


class CredentialsError(Exception):
    pass


@dataclass
class Credentials:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    def __repr__(self) -> str:
        return "Credentials(redacted)"

    def secret_values(self) -> list[str]:
        return [self.access_key_id, self.secret_access_key]


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        out[key] = value
    return out


def candidate_files(environ: dict[str, str] | None = None, cwd: Path | None = None) -> list[Path]:
    env = environ if environ is not None else os.environ
    root = cwd or Path.cwd()
    paths: list[Path] = []
    explicit = env.get("AUDIOBOOK_R2_ENV")
    if explicit:
        paths.append(Path(explicit))
    paths.append(root / ".env")
    paths.append(Path.home() / ".config" / "audiobook-worker" / "r2.env")
    return paths


def load_credentials(environ: dict[str, str] | None = None, cwd: Path | None = None, prompt: bool | None = None) -> Credentials:
    """Env vars win over a gitignored file. A Cloudflare API token is not an S3 credential."""
    env = dict(os.environ if environ is None else environ)
    file_vals: dict[str, str] = {}
    for path in candidate_files(env, cwd):
        if path.is_file():
            file_vals = parse_env_text(path.read_text())
            break
    found: dict[str, str] = {}
    for key in KEYS:
        if env.get(key):
            found[key] = env[key]
        elif file_vals.get(key):
            found[key] = file_vals[key]
    missing = [key for key in KEYS if not found.get(key)]
    interactive = sys.stdin.isatty() if prompt is None else prompt
    if missing and interactive and environ is None:
        import getpass

        for key in missing:
            if key in SECRET_KEYS:
                found[key] = getpass.getpass(f"{key} (hidden): ")
            else:
                found[key] = input(f"{key}: ")
        missing = [key for key in KEYS if not found.get(key)]
    if missing:
        raise CredentialsError(
            "Missing R2 settings: "
            + ", ".join(missing)
            + ". A Cloudflare API token alone is not enough for S3. "
            + "Set R2_ENDPOINT_URL, R2_BUCKET_NAME, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY "
            + "in the environment or a gitignored env file. Create a bucket-scoped R2 access key."
        )
    creds = Credentials(
        endpoint_url=found["R2_ENDPOINT_URL"],
        bucket=found["R2_BUCKET_NAME"],
        access_key_id=found["R2_ACCESS_KEY_ID"],
        secret_access_key=found["R2_SECRET_ACCESS_KEY"],
    )
    install_redact(creds.secret_values())
    return creds
