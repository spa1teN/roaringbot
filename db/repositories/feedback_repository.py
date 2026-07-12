"""Feedback repository — user-submitted feedback storage and dashboard queries."""

from typing import Any, Dict, List, Optional
import asyncpg
from db.repositories.base import BaseRepository

VALID_STATUSES = {"new", "important", "in_progress", "archived"}


class FeedbackRepository(BaseRepository):
    """INSERT + aggregate queries + status/note mutations for dashboard."""

    async def submit(
        self,
        guild_id: int,
        user_id: int,
        is_anonymous: bool,
        subject: str,
        message: str,
    ) -> asyncpg.Record:
        return await self.fetchrow(
            """INSERT INTO feedback (guild_id, user_id, is_anonymous, subject, message)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING *""",
            guild_id, user_id, is_anonymous, subject, message,
        )

    async def list_feedback(
        self, guild_id: Optional[int] = None, limit: int = 50
    ) -> List[asyncpg.Record]:
        if guild_id is not None:
            return await self.fetch(
                """SELECT id, guild_id,
                          CASE WHEN is_anonymous THEN 0 ELSE user_id END AS user_id,
                          is_anonymous, subject, message, status, read, admin_note, created_at
                   FROM feedback
                   WHERE guild_id = $1
                   ORDER BY created_at DESC
                   LIMIT $2""",
                guild_id, limit,
            )
        return await self.fetch(
            """SELECT id, guild_id,
                      CASE WHEN is_anonymous THEN 0 ELSE user_id END AS user_id,
                      is_anonymous, subject, message, status, read, admin_note, created_at
               FROM feedback
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )

    async def get_stats_by_guild(self) -> List[Dict[str, Any]]:
        """Per-guild aggregate counts with status breakdown."""
        rows = await self.fetch(
            """SELECT guild_id,
                      COUNT(*)::int AS total,
                      COUNT(*) FILTER (WHERE status = 'new')::int AS new,
                      COUNT(*) FILTER (WHERE status = 'important')::int AS important,
                      COUNT(*) FILTER (WHERE status = 'in_progress')::int AS in_progress,
                      COUNT(*) FILTER (WHERE status = 'archived')::int AS archived
               FROM feedback
               GROUP BY guild_id
               ORDER BY new DESC, total DESC""",
        )
        return [dict(r) for r in rows]

    async def get_recent_entries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Latest feedback entries, message truncated, with status/admin_note."""
        rows = await self.fetch(
            """SELECT id, guild_id,
                      CASE WHEN is_anonymous THEN 0 ELSE user_id END AS user_id,
                      is_anonymous, subject,
                      LEFT(message, 200) AS message,
                      status, read, admin_note, created_at
               FROM feedback
               ORDER BY created_at DESC
               LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    # ── Mutations (called by API server) ─────────────────────────────────

    async def mark_read(self, feedback_id: int) -> bool:
        result = await self.execute(
            "UPDATE feedback SET read = TRUE WHERE id = $1", feedback_id)
        return "UPDATE 1" in result

    async def set_status(self, feedback_id: int, status: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        result = await self.execute(
            "UPDATE feedback SET status = $2 WHERE id = $1", feedback_id, status)
        return "UPDATE 1" in result

    async def set_admin_note(self, feedback_id: int, note: str) -> bool:
        result = await self.execute(
            "UPDATE feedback SET admin_note = $2 WHERE id = $1", feedback_id, note)
        return "UPDATE 1" in result

    async def get_unread_count(self, guild_id: int) -> int:
        return await self.fetchval(
            "SELECT COUNT(*)::int FROM feedback WHERE guild_id = $1 AND read = FALSE",
            guild_id) or 0
