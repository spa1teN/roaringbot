-- RoaringBot Discord Bot - PostgreSQL Schema
-- Version: 1.0.0
-- Mirrors the pattern used by the sibling Tausendsassa bot (see its
-- db/schema.sql) but scoped down to what RoaringBot actually needs:
-- no guild name/icon sync, no multi-feature guild metadata.

-- ============================================
-- CORE TABLES
-- ============================================

-- Guilds RoaringBot has moderation/timezone config for. Minimal - just a
-- parent row for the FKs below, populated lazily on first config write.
CREATE TABLE IF NOT EXISTS guilds (
    id              BIGINT PRIMARY KEY,     -- Discord Guild ID
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Per-guild timezone (currently unused in practice, but the code path
-- exists - core/timezone_util.py)
CREATE TABLE IF NOT EXISTS guild_timezones (
    guild_id        BIGINT PRIMARY KEY REFERENCES guilds(id) ON DELETE CASCADE,
    timezone        VARCHAR(64) NOT NULL DEFAULT 'Europe/Berlin',
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- MODERATION
-- ============================================

-- Moderation configuration per guild: member-log webhook, auto join role,
-- honeypot role, bot-trap channel.
CREATE TABLE IF NOT EXISTS moderation_config (
    guild_id                BIGINT PRIMARY KEY REFERENCES guilds(id) ON DELETE CASCADE,
    member_log_webhook      TEXT,
    member_log_channel_id   BIGINT,
    join_role_id            BIGINT,
    honeypot_role_id        BIGINT,
    bot_trap_channel_id     BIGINT,
    created_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at              TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Migration: add columns if upgrading from older schema.
ALTER TABLE moderation_config ADD COLUMN IF NOT EXISTS member_log_channel_id BIGINT;
ALTER TABLE moderation_config ADD COLUMN IF NOT EXISTS bot_trap_channel_id BIGINT;

-- Discord scheduled-event ID -> internal wannspieltbig match ID
CREATE TABLE IF NOT EXISTS esports_event_map (
    event_id        BIGINT PRIMARY KEY,
    match_id        BIGINT NOT NULL
);

-- 30-minute reminder message ID -> match ID
CREATE TABLE IF NOT EXISTS esports_reminder_map (
    reminder_id     BIGINT PRIMARY KEY,
    match_id        BIGINT NOT NULL
);

-- Forum thread ID -> match ID
CREATE TABLE IF NOT EXISTS esports_thread_map (
    thread_id       BIGINT PRIMARY KEY,
    match_id        BIGINT NOT NULL
);

-- Matches already seen, to avoid duplicate Discord events on restart, plus
-- whether a CS score tracker has already been (or should be) started for it.
CREATE TABLE IF NOT EXISTS esports_known_matches (
    match_id        BIGINT PRIMARY KEY,
    monitored       BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Generic key-value slot for small pieces of scalar state (currently just
-- the weekly summary message ID) - mirrors Tausendsassa's map_global_config.
CREATE TABLE IF NOT EXISTS esports_state (
    key             TEXT PRIMARY KEY,
    value           JSONB NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Active CS live-score trackers, so an in-progress score survives a restart.
CREATE TABLE IF NOT EXISTS cs_trackers (
    match_id            BIGINT PRIMARY KEY,
    message_id          BIGINT,
    current_map         INTEGER NOT NULL DEFAULT 1,
    team_a_score        INTEGER NOT NULL DEFAULT 0,
    team_b_score        INTEGER NOT NULL DEFAULT 0,
    team_a_maps         INTEGER NOT NULL DEFAULT 0,
    team_b_maps         INTEGER NOT NULL DEFAULT 0,
    overtime_target     INTEGER NOT NULL DEFAULT 13,
    match_maps          JSONB NOT NULL DEFAULT '[]',
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ============================================
-- BIRTHDAYS
-- ============================================

-- Idempotency guard for the daily birthday post: prevents a double-send if
-- the bot restarts right around the 10:00 German-time check.
CREATE TABLE IF NOT EXISTS birthday_sent_log (
    id              SERIAL PRIMARY KEY,
    guild_id        BIGINT,
    name            TEXT NOT NULL,
    date_iso        DATE NOT NULL,
    sent_at         TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (guild_id, name, date_iso)
);

-- ============================================
-- FEEDBACK
-- ============================================

-- User-submitted feedback via /feedback command.
CREATE TABLE IF NOT EXISTS feedback (
    id              SERIAL PRIMARY KEY,
    guild_id        BIGINT REFERENCES guilds(id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL,
    is_anonymous    BOOLEAN NOT NULL DEFAULT FALSE,
    subject         VARCHAR(32) NOT NULL DEFAULT 'other',
    message         TEXT NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'new',   -- new | important | in_progress | archived
    read            BOOLEAN NOT NULL DEFAULT FALSE,
    admin_note      TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Migration: add columns if upgrading from schema without status/admin_note.
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'new';
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS admin_note TEXT;

-- ============================================
-- INDEXES
-- ============================================

CREATE INDEX IF NOT EXISTS idx_esports_event_map_match ON esports_event_map(match_id);
CREATE INDEX IF NOT EXISTS idx_esports_reminder_map_match ON esports_reminder_map(match_id);
CREATE INDEX IF NOT EXISTS idx_esports_thread_map_match ON esports_thread_map(match_id);
CREATE INDEX IF NOT EXISTS idx_birthday_sent_log_date ON birthday_sent_log(date_iso);

-- ============================================
-- updated_at TRIGGER
-- ============================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['guilds', 'guild_timezones', 'moderation_config',
                              'esports_state', 'cs_trackers']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS update_%s_updated_at ON %s', t, t);
        EXECUTE format('CREATE TRIGGER update_%s_updated_at
                        BEFORE UPDATE ON %s
                        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()', t, t);
    END LOOP;
END $$;

-- ============================================
-- COMMENTS
-- ============================================

COMMENT ON TABLE guilds IS 'Guilds RoaringBot has config for (parent row for FKs)';
COMMENT ON TABLE guild_timezones IS 'Per-guild timezone settings';
COMMENT ON TABLE moderation_config IS 'Moderation settings per guild (member-log webhook, join role, honeypot role)';
COMMENT ON TABLE esports_event_map IS 'Discord scheduled-event ID -> wannspieltbig match ID';
COMMENT ON TABLE esports_reminder_map IS '30-minute reminder message ID -> match ID';
COMMENT ON TABLE esports_thread_map IS 'Forum thread ID -> match ID';
COMMENT ON TABLE esports_known_matches IS 'Matches already seen, to prevent duplicate events on restart';
COMMENT ON TABLE esports_state IS 'Small scalar E-Sports state, e.g. the weekly summary message ID';
COMMENT ON TABLE cs_trackers IS 'Active CS live-score trackers, survives restarts';
COMMENT ON TABLE birthday_sent_log IS 'Idempotency guard so a restart around 10:00 cannot double-send a birthday post';
