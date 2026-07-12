"""
Repository for moderation operations.

Keeps the exact dict-key vocabulary the cog/UI code already used with the old
JSON file ('member_log_webhook', 'join_role', 'honeypot_role') so moderation.py
and core/mod_views.py only need their config calls turned into `await` calls,
not rewritten.
"""

from typing import Any, Dict, Optional

from db.repositories.base import BaseRepository

_KEY_TO_COLUMN = {
    "member_log_webhook": "member_log_webhook",
    "member_log_channel": "member_log_channel_id",
    "join_role": "join_role_id",
    "honeypot_role": "honeypot_role_id",
    "bot_trap_channel": "bot_trap_channel_id",
}


class ModerationRepository(BaseRepository):
    """Repository for moderation database operations."""

    async def get_guild_config(self, guild_id: int) -> Dict[str, Any]:
        """Get moderation config for a guild as a dict (compatibility shape)."""
        row = await self.fetchrow(
            "SELECT member_log_webhook, member_log_channel_id, join_role_id, "
            "honeypot_role_id, bot_trap_channel_id "
            "FROM moderation_config WHERE guild_id = $1",
            guild_id
        )
        if not row:
            return {}
        return {
            "member_log_webhook": row["member_log_webhook"],
            "member_log_channel": row["member_log_channel_id"],
            "join_role": row["join_role_id"],
            "honeypot_role": row["honeypot_role_id"],
            "bot_trap_channel": row["bot_trap_channel_id"],
        }

    async def set_guild_config(self, guild_id: int, key: str, value: Any) -> None:
        """Set a single config value for a guild, creating the row if needed."""
        column = _KEY_TO_COLUMN.get(key)
        if not column:
            raise ValueError(f"Unknown moderation config key: {key}")

        await self.execute(
            "INSERT INTO guilds (id) VALUES ($1) ON CONFLICT (id) DO NOTHING",
            guild_id
        )
        await self.execute(
            f"""INSERT INTO moderation_config (guild_id, {column})
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET
                    {column} = $2,
                    updated_at = NOW()""",
            guild_id, value
        )

    async def clear_guild_config(self, guild_id: int, key: str) -> None:
        """Clear a single config value (used by the disable buttons)."""
        column = _KEY_TO_COLUMN.get(key)
        if not column:
            raise ValueError(f"Unknown moderation config key: {key}")

        await self.execute(
            f"""UPDATE moderation_config SET {column} = NULL, updated_at = NOW()
                WHERE guild_id = $1""",
            guild_id
        )

    async def get_all_configs(self) -> Dict[int, Dict[str, Any]]:
        """Get all moderation configs as a dict (for the migration script)."""
        rows = await self.fetch(
            "SELECT guild_id, member_log_webhook, member_log_channel_id, "
            "join_role_id, honeypot_role_id, bot_trap_channel_id FROM moderation_config"
        )
        return {
            row["guild_id"]: {
                "member_log_webhook": row["member_log_webhook"],
                "member_log_channel": row["member_log_channel_id"],
                "join_role": row["join_role_id"],
                "honeypot_role": row["honeypot_role_id"],
                "bot_trap_channel": row["bot_trap_channel_id"],
            }
            for row in rows
        }
