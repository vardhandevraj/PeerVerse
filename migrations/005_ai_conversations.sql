-- PeerVerse AI Assistant multi-conversation upgrade.
-- Run this file ONCE in the Supabase SQL Editor against databases created
-- before this feature. Fresh installs can skip it because supabase_schema.sql
-- already includes these tables.
--
-- Each student owns many AI conversations; each conversation owns its own
-- ordered message history. The existing `chat_history` table stays untouched.

CREATE TABLE IF NOT EXISTS ai_conversations (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New Chat',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES ai_conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ai_conversations_user_updated
    ON ai_conversations (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation_created
    ON ai_messages (conversation_id, created_at, id);

-- Keep updated_at fresh automatically on every row update (renames).
DROP TRIGGER IF EXISTS trg_ai_conversations_updated_at ON ai_conversations;
CREATE TRIGGER trg_ai_conversations_updated_at
    BEFORE UPDATE ON ai_conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
