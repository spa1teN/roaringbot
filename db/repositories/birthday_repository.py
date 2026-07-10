"""
Repository for the birthday-send idempotency guard.
"""

from datetime import date
from typing import Optional

from db.repositories.base import BaseRepository


class BirthdayRepository(BaseRepository):
    """Prevents a double-send if the bot restarts right around the daily check."""

    async def already_sent(self, guild_id: Optional[int], name: str, date_iso: date) -> bool:
        row = await self.fetchrow(
            "SELECT 1 FROM birthday_sent_log WHERE guild_id IS NOT DISTINCT FROM $1 AND name = $2 AND date_iso = $3",
            guild_id, name, date_iso,
        )
        return row is not None

    async def mark_sent(self, guild_id: Optional[int], name: str, date_iso: date) -> None:
        await self.execute(
            """INSERT INTO birthday_sent_log (guild_id, name, date_iso)
               VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, name, date_iso) DO NOTHING""",
            guild_id, name, date_iso,
        )
