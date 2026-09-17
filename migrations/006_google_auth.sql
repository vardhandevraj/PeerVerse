-- Google Sign-In support.
-- Run this file ONCE in the Supabase SQL Editor against databases created
-- before this feature. Fresh installs get the new columns from
-- supabase_schema.sql automatically (the DROP/CASCADE lines make old columns
-- nullable; they are already nullable in a fresh schema so it is safe to run
-- always).

-- Google-only accounts have no password, and Google does not supply a
-- department or academic year, so those columns must allow NULL.
ALTER TABLE users ALTER COLUMN password DROP NOT NULL;
ALTER TABLE users ALTER COLUMN department DROP NOT NULL;
ALTER TABLE users ALTER COLUMN study_year DROP NOT NULL;

-- Track which provider a row was created with and store Google's stable
-- account id so Google sign-ins resolve to the same user every time.
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT NOT NULL DEFAULT 'password';
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT UNIQUE;