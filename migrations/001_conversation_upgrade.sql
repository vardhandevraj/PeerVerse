-- PeerVerse conversation upgrade for an existing CampusConnectAI database.
-- Back up the database before running this migration, for example:
-- mysqldump -u root -p campus_connect_ai > campus_connect_ai_before_conversations.sql
--
-- Run this file ONCE against databases created before the conversation upgrade.
-- Fresh installs should use ../database.sql, which already includes these fields.

USE campus_connect_ai;

ALTER TABLE conversations
    ADD COLUMN created_by INT NULL AFTER is_group,
    ADD COLUMN avatar_path VARCHAR(255) NULL AFTER created_by,
    ADD CONSTRAINT fk_conversations_created_by
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;

-- Older conversations get a sensible creator so group administration remains available.
UPDATE conversations c
JOIN (
    SELECT conversation_id, MIN(user_id) AS creator_id
    FROM conversation_participants
    GROUP BY conversation_id
) cp ON cp.conversation_id = c.id
SET c.created_by = cp.creator_id
WHERE c.created_by IS NULL;

ALTER TABLE conversation_participants
    ADD COLUMN last_read_at TIMESTAMP NULL DEFAULT NULL AFTER joined_at;

ALTER TABLE messages
    MODIFY COLUMN sender_id INT NULL,
    MODIFY COLUMN receiver_id INT NULL,
    ADD COLUMN message_type ENUM('text', 'ai') NOT NULL DEFAULT 'text' AFTER message_text;

-- Existing system messages were IVY messages in the original implementation.
UPDATE messages
SET message_type = 'ai'
WHERE sender_id IS NULL;

CREATE INDEX idx_messages_conversation_created ON messages (conversation_id, created_at);
CREATE INDEX idx_conversation_participants_user_read ON conversation_participants (user_id, last_read_at);
