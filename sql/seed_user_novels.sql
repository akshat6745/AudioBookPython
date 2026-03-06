-- =========================================================
-- Migration: Tag novels to users + mark public novels
-- Run via:  wrangler d1 execute audiobookpython-db --file=sql/seed_user_novels.sql
-- =========================================================

-- Add new columns (safe to re-run — ALTER TABLE will error if column exists)
ALTER TABLE novels ADD COLUMN user_id TEXT REFERENCES users(id);
ALTER TABLE novels ADD COLUMN is_public INTEGER DEFAULT 0;

-- Tag all existing novels to the first (only) user
UPDATE novels SET user_id = (SELECT id FROM users LIMIT 1);

-- Make "The Adventures of Sherlock Holmes" public
UPDATE novels SET is_public = 1 WHERE LOWER(title) LIKE '%adventures of sherlock holmes%';
