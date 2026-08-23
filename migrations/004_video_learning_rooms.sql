-- PeerVerse Video Learning Rooms upgrade.
-- Run this file ONCE in the Supabase SQL Editor against databases created
-- before the video learning room feature. Fresh installs can skip it because
-- supabase_schema.sql already includes these tables.
--
-- WebRTC audio/video never touches the database. These tables store only call
-- metadata; room chat is persisted in the existing `messages` table so the
-- conversation history and @ivy context stay unified.

CREATE TABLE IF NOT EXISTS call_rooms (
    id TEXT PRIMARY KEY,                    -- unguessable room id used in /call/<room_id>
    conversation_id INT NOT NULL,
    created_by INT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('ringing', 'active', 'ended')),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP NULL DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_call_rooms_conversation_status
    ON call_rooms (conversation_id, status);

CREATE TABLE IF NOT EXISTS call_room_participants (
    room_id TEXT NOT NULL,
    user_id INT NOT NULL,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    left_at TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (room_id, user_id),
    FOREIGN KEY (room_id) REFERENCES call_rooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_call_room_participants_user
    ON call_room_participants (user_id);
