-- Core feature additions:
--   - is_verified / is_admin / last_seen on users
--   - auth_tokens (email verification + password reset)
--   - reports (report / moderation)
-- Run this file ONCE in the Supabase SQL Editor against databases created
-- before this feature. Fresh installs get the schema from supabase_schema.sql.

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP;

-- Single-use tokens issued when a student asks to verify their email or reset
-- their password. Expiry is enforced in application code as well.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    token_type TEXT NOT NULL CHECK (token_type IN ('verify_email', 'password_reset')),
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_type
    ON auth_tokens (user_id, token_type);

-- One report row per reported piece of content. A single insert per report
-- keeps moderation simple; repeat reports for the same target are allowed.
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    reporter_id INT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('post', 'comment', 'user', 'resource')),
    target_id INT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP NULL DEFAULT NULL,
    resolver_id INT,
    FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (resolver_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_status
    ON reports (status, created_at);