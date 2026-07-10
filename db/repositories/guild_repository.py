"""
Repository for guild and timezone operations.
"""

from typing import Optional

from db.repositories.base import BaseRepository


class GuildRepository(BaseRepository):
    """Repository for guild-related database operations."""

    async def ensure_exists(self, guild_id: int) -> None:
        """Ensure a guild row exists, creating it if necessary (parent for FKs)."""
        await self.execute(
            """INSERT INTO guilds (id) VALUES ($1)
               ON CONFLICT (id) DO NOTHING""",
            guild_id
        )

    # Timezone operations

    async def get_timezone(self, guild_id: int) -> str:
        """Get timezone for a guild, defaulting to Europe/Berlin."""
        row = await self.fetchrow(
            "SELECT timezone FROM guild_timezones WHERE guild_id = $1",
            guild_id
        )
        return row['timezone'] if row else 'Europe/Berlin'

    async def set_timezone(self, guild_id: int, timezone: str) -> None:
        """Set timezone for a guild."""
        await self.ensure_exists(guild_id)
        await self.execute(
            """INSERT INTO guild_timezones (guild_id, timezone)
               VALUES ($1, $2)
               ON CONFLICT (guild_id) DO UPDATE SET
                   timezone = $2,
                   updated_at = NOW()""",
            guild_id, timezone
        )

    async def get_all_timezones(self) -> dict:
        """Get all guild timezones as a dict."""
        rows = await self.fetch("SELECT guild_id, timezone FROM guild_timezones")
        return {row['guild_id']: row['timezone'] for row in rows}
