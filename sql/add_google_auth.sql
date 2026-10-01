-- =========================================================
-- Migration: Google Sign-In support on users
-- Run via:  wrangler d1 execute audiobookpython-db --file=sql/add_google_auth.sql
--
-- Pair this with scripts/hash_existing_passwords.py, which converts the
-- existing plaintext `password` values to bcrypt hashes. See the rollout
-- order in that script's docstring.
-- =========================================================

-- Add new columns (safe to re-run — ALTER TABLE will error if column exists)
ALTER TABLE users ADD COLUMN google_sub    TEXT;
ALTER TABLE users ADD COLUMN email         TEXT;
ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'password';

-- Google's `sub` is the stable per-user key. The column is nullable because
-- password-only accounts have none; SQLite treats NULLs as distinct, so many
-- password users coexist fine under a UNIQUE index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub);

-- `password` stays NOT NULL (SQLite cannot drop that without rebuilding the
-- table), so Google-only accounts store the sentinel '!nopassword'. It is not
-- a bcrypt hash, so app/core/passwords.verify_password always rejects it.
