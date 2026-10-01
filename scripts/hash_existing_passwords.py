"""One-time migration: plaintext `users.password` → bcrypt hashes.

Usage (PYTHONPATH=. so `app` resolves when run as a script):
    PYTHONPATH=. python scripts/hash_existing_passwords.py --dry-run  # report only
    PYTHONPATH=. python scripts/hash_existing_passwords.py            # apply

Requires a full local .env (it imports app.core.d1_client, which loads
settings — SUPABASE_URL/SUPABASE_KEY are required fields there, not just the
Cloudflare credentials).

ROLLOUT ORDER — logins break if these are done out of order:
  1. Apply sql/add_google_auth.sql via wrangler. MUST be first: the new code
     selects users.google_sub / users.auth_provider, and adding the columns is
     backward compatible with the currently deployed code.
  2. Deploy the code that verifies bcrypt, with ALLOW_PLAINTEXT_LOGIN=true so
     not-yet-migrated users can still log in.
  3. Run this script.
  4. Set ALLOW_PLAINTEXT_LOGIN=false (a config change; no redeploy needed).
  5. Delete the plaintext fallback from app/api/user.py + app/core/passwords.py.

Idempotent: rows already holding a bcrypt hash (or the Google-only sentinel)
are skipped, so re-running is safe.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from app.core.d1_client import get_d1_client  # noqa: E402
from app.core.passwords import (  # noqa: E402
    MAX_PASSWORD_BYTES,
    NO_PASSWORD_SENTINEL,
    hash_password,
    is_hashed,
)


async def migrate(dry_run: bool) -> int:
    d1 = get_d1_client()
    users = await d1.query("SELECT id, username, password FROM users")
    print(f"Found {len(users)} users\n")

    hashed = skipped = failed = 0

    for user in users:
        uid, uname, stored = user["id"], user["username"], user.get("password")

        if is_hashed(stored):
            print(f"  [{uname}] already hashed — skipping")
            skipped += 1
            continue

        if stored == NO_PASSWORD_SENTINEL:
            print(f"  [{uname}] Google-only account — skipping")
            skipped += 1
            continue

        if not stored:
            print(f"  [{uname}] !! empty password — needs a manual reset")
            failed += 1
            continue

        # bcrypt refuses inputs over 72 bytes. Hashing a truncated copy would
        # silently change the password; leave these for a manual reset instead
        # of locking the user out.
        if len(stored.encode("utf-8")) > MAX_PASSWORD_BYTES:
            print(
                f"  [{uname}] !! password exceeds {MAX_PASSWORD_BYTES} bytes — "
                "needs a manual reset"
            )
            failed += 1
            continue

        if dry_run:
            print(f"  [{uname}] would hash")
            hashed += 1
            continue

        # Conditional on the plaintext we just read: D1 gives us no
        # transaction, so this is what stops a concurrent password change
        # from being clobbered.
        meta = await d1.execute(
            "UPDATE users SET password = ?, updated_at = datetime('now') "
            "WHERE id = ? AND password = ?",
            [hash_password(stored), uid, stored],
        )
        changed = next(
            (
                int(meta[k])
                for k in ("changes", "rows_written", "changed_db")
                if isinstance(meta.get(k), (int, float))
            ),
            0,
        )
        if changed:
            print(f"  [{uname}] hashed")
            hashed += 1
        else:
            print(f"  [{uname}] !! changed concurrently — re-run the script")
            failed += 1

    verb = "would hash" if dry_run else "hashed"
    print(f"\n{verb}: {hashed}   skipped: {skipped}   needs attention: {failed}")
    if dry_run:
        print("\nDry run — nothing was written. Re-run without --dry-run to apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(migrate(args.dry_run)))
