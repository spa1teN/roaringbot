"""
Repository for E-Sports match-monitoring state.

esports.py keeps its working state as plain in-memory dicts/sets (event_to_match,
reminder_to_match, thread_to_match, monitored_matches, known_match_ids,
active_cs_games) and used to dump all of them to one JSON file on every
_save_data() call. To keep that calling code (~13 call sites) unchanged, this
repository offers the same "write everything" shape as one bulk resync per
call, just targeting Postgres tables instead of a JSON file.
"""

import json
from typing import Any, Dict, List, Optional, Set

from db.repositories.base import BaseRepository


class EsportsRepository(BaseRepository):
    """Repository for E-Sports database operations."""

    async def load_all(self) -> Dict[str, Any]:
        """Load everything esports.py needs at startup, same shape as the old JSON file."""
        event_rows = await self.fetch("SELECT event_id, match_id FROM esports_event_map")
        reminder_rows = await self.fetch("SELECT reminder_id, match_id FROM esports_reminder_map")
        thread_rows = await self.fetch("SELECT thread_id, match_id FROM esports_thread_map")
        known_rows = await self.fetch("SELECT match_id, monitored FROM esports_known_matches")
        tracker_rows = await self.fetch("SELECT * FROM cs_trackers")
        summary_row = await self.fetchrow("SELECT value FROM esports_state WHERE key = 'summary_message_id'")

        return {
            "event_to_match": {r["event_id"]: r["match_id"] for r in event_rows},
            "reminder_to_match": {r["reminder_id"]: r["match_id"] for r in reminder_rows},
            "thread_to_match": {r["thread_id"]: r["match_id"] for r in thread_rows},
            "summary_message_id": (json.loads(summary_row["value"]) if summary_row else None),
            "monitored_matches": [r["match_id"] for r in known_rows if r["monitored"]],
            "known_match_ids": [r["match_id"] for r in known_rows],
            "active_cs_trackers": {
                str(r["match_id"]): {
                    "message_id": r["message_id"],
                    "current_map": r["current_map"],
                    "team_a_score": r["team_a_score"],
                    "team_b_score": r["team_b_score"],
                    "team_a_maps": r["team_a_maps"],
                    "team_b_maps": r["team_b_maps"],
                    "overtime_target": r["overtime_target"],
                    "match_maps": json.loads(r["match_maps"]) if isinstance(r["match_maps"], str) else r["match_maps"],
                }
                for r in tracker_rows
            },
        }

    async def save_all(
        self,
        event_to_match: Dict[int, int],
        reminder_to_match: Dict[int, int],
        thread_to_match: Dict[int, int],
        summary_message_id: Optional[int],
        monitored_matches: Set[int],
        known_match_ids: Set[int],
        active_cs_trackers: Dict[int, dict],
    ) -> None:
        """Bulk-resync all E-Sports state (full replace, matches the old
        write-everything-at-once JSON semantics)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM esports_event_map")
                if event_to_match:
                    await conn.executemany(
                        "INSERT INTO esports_event_map (event_id, match_id) VALUES ($1, $2)",
                        list(event_to_match.items()),
                    )

                await conn.execute("DELETE FROM esports_reminder_map")
                if reminder_to_match:
                    await conn.executemany(
                        "INSERT INTO esports_reminder_map (reminder_id, match_id) VALUES ($1, $2)",
                        list(reminder_to_match.items()),
                    )

                await conn.execute("DELETE FROM esports_thread_map")
                if thread_to_match:
                    await conn.executemany(
                        "INSERT INTO esports_thread_map (thread_id, match_id) VALUES ($1, $2)",
                        list(thread_to_match.items()),
                    )

                await conn.execute(
                    """INSERT INTO esports_state (key, value) VALUES ('summary_message_id', $1)
                       ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""",
                    json.dumps(summary_message_id),
                )

                await conn.execute("DELETE FROM esports_known_matches")
                if known_match_ids:
                    await conn.executemany(
                        "INSERT INTO esports_known_matches (match_id, monitored) VALUES ($1, $2)",
                        [(mid, mid in monitored_matches) for mid in known_match_ids],
                    )

                await conn.execute("DELETE FROM cs_trackers")
                if active_cs_trackers:
                    await conn.executemany(
                        """INSERT INTO cs_trackers
                           (match_id, message_id, current_map, team_a_score, team_b_score,
                            team_a_maps, team_b_maps, overtime_target, match_maps)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                        [
                            (
                                int(mid),
                                t.get("message_id"),
                                t.get("current_map", 1),
                                t.get("team_a_score", 0),
                                t.get("team_b_score", 0),
                                t.get("team_a_maps", 0),
                                t.get("team_b_maps", 0),
                                t.get("overtime_target", 13),
                                json.dumps(t.get("match_maps", [])),
                            )
                            for mid, t in active_cs_trackers.items()
                        ],
                    )
