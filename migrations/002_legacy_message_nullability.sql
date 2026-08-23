-- Apply this once only if 001_conversation_upgrade.sql was run before
-- sender_id and receiver_id were made nullable.
-- Conversation messages have no single receiver, and IVY messages have no user sender.

USE campus_connect_ai;

ALTER TABLE messages
    MODIFY COLUMN sender_id INT NULL,
    MODIFY COLUMN receiver_id INT NULL;
