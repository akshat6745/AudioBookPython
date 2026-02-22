"""
One-time migration: Supabase users_data → Cloudflare D1 (users + user_progress)
Usage: python scripts/migrate_users_to_d1.py
"""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client
from app.core.d1_client import get_d1_client


async def migrate():
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    d1 = get_d1_client()

    users = sb.table("users_data").select("*").execute().data
    print(f"Found {len(users)} Supabase users")

    for u in users:
        uname, pwd = u["username"], u["password"]

        existing = await d1.query("SELECT id FROM users WHERE username=?", [uname])
        if existing:
            uid = existing[0]["id"]
            print(f"  [{uname}] already in D1 ({uid[:8]}…)")
        else:
            await d1.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)", [uname, pwd]
            )
            r = await d1.query("SELECT id FROM users WHERE username=?", [uname])
            uid = r[0]["id"]
            print(f"  [{uname}] inserted ({uid[:8]}…)")

        progress = u.get("progress") or []
        ok, skip = 0, 0
        for p in progress:
            nname = p.get("novelName", "")
            ch    = p.get("lastChapterRead", 1)

            # Resolve by slug then by title
            nr = await d1.query("SELECT id FROM novels WHERE id=?", [nname])
            if not nr:
                nr = await d1.query(
                    "SELECT id FROM novels WHERE LOWER(title)=LOWER(?)", [nname]
                )
            if not nr:
                print(f"    ⚠  novel not found: {nname!r}")
                skip += 1
                continue

            nid = nr[0]["id"]
            await d1.execute(
                """
                INSERT INTO user_progress (user_id, novel_id, chapter_number, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, novel_id)
                DO UPDATE SET chapter_number = excluded.chapter_number,
                              updated_at     = excluded.updated_at
                """,
                [uid, nid, ch],
            )
            ok += 1

        print(f"    progress: {ok} migrated, {skip} skipped")

    print("\nUser migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
