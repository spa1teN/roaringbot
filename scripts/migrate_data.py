"""
One-off migration: reads the old JSON config files (config/esports_data.json,
config/moderation_config.json) and writes them into Postgres.

Mirrors the pattern used by the sibling Tausendsassa bot's own
scripts/migrate_data.py. Run from inside the bot container (needs the same
DB_* env vars as the bot itself):

    docker compose exec bot python scripts/migrate_data.py --dry-run
    docker compose exec bot python scripts/migrate_data.py --migrate
    docker compose exec bot python scripts/migrate_data.py --validate
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import get_db  # noqa: E402


class MigrationStats:
    def __init__(self):
        self.event_maps = 0
        self.reminder_maps = 0
        self.thread_maps = 0
        self.known_matches = 0
        self.cs_trackers = 0
        self.moderation_configs = 0
        self.errors = 0

    def report(self):
        print("--- Migration summary ---")
        print(f"  event_to_match entries:    {self.event_maps}")
        print(f"  reminder_to_match entries: {self.reminder_maps}")
        print(f"  thread_to_match entries:   {self.thread_maps}")
        print(f"  known match ids:           {self.known_matches}")
        print(f"  active CS trackers:        {self.cs_trackers}")
        print(f"  moderation configs:        {self.moderation_configs}")
        print(f"  errors:                    {self.errors}")


def load_esports_json() -> dict:
    path = Path("config/esports_data.json")
    if not path.exists():
        print(f"  (no {path} found, skipping esports migration)")
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_moderation_json() -> dict:
    path = Path("config/moderation_config.json")
    if not path.exists():
        print(f"  (no {path} found, skipping moderation migration)")
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


async def migrate(dry_run: bool) -> MigrationStats:
    stats = MigrationStats()
    db = await get_db()

    esports_data = load_esports_json()
    moderation_data = load_moderation_json()

    event_to_match = {int(k): v for k, v in esports_data.get("event_to_match", {}).items()}
    reminder_to_match = {int(k): v for k, v in esports_data.get("reminder_to_match", {}).items()}
    thread_to_match = {int(k): v for k, v in esports_data.get("thread_to_match", {}).items()}
    summary_message_id = esports_data.get("summary_message_id")
    monitored_matches = set(esports_data.get("monitored_matches", []))
    known_match_ids = set(esports_data.get("known_match_ids", []))
    active_cs_trackers = {
        int(k): v for k, v in esports_data.get("active_cs_trackers", {}).items()
    }

    stats.event_maps = len(event_to_match)
    stats.reminder_maps = len(reminder_to_match)
    stats.thread_maps = len(thread_to_match)
    stats.known_matches = len(known_match_ids)
    stats.cs_trackers = len(active_cs_trackers)

    print(f"Found {stats.event_maps} event mappings, {stats.reminder_maps} reminder mappings, "
          f"{stats.thread_maps} thread mappings, {stats.known_matches} known matches, "
          f"{stats.cs_trackers} active CS trackers")

    if not dry_run and (event_to_match or reminder_to_match or thread_to_match or known_match_ids or active_cs_trackers or summary_message_id):
        try:
            await db.esports.save_all(
                event_to_match=event_to_match,
                reminder_to_match=reminder_to_match,
                thread_to_match=thread_to_match,
                summary_message_id=summary_message_id,
                monitored_matches=monitored_matches,
                known_match_ids=known_match_ids,
                active_cs_trackers=active_cs_trackers,
            )
            print("  -> E-Sports state written to Postgres")
        except Exception as e:
            print(f"  ERROR writing E-Sports state: {e}")
            stats.errors += 1

    for guild_str, guild_config in moderation_data.items():
        guild_id = int(guild_str)
        stats.moderation_configs += 1
        print(f"Found moderation config for guild {guild_id}: "
              f"member_log_webhook={'set' if guild_config.get('member_log_webhook') else 'unset'}, "
              f"join_role={guild_config.get('join_role')}, honeypot_role={guild_config.get('honeypot_role')}")

        if not dry_run:
            try:
                if guild_config.get("member_log_webhook"):
                    await db.moderation.set_guild_config(guild_id, "member_log_webhook", guild_config["member_log_webhook"])
                if guild_config.get("join_role"):
                    await db.moderation.set_guild_config(guild_id, "join_role", guild_config["join_role"])
                if guild_config.get("honeypot_role"):
                    await db.moderation.set_guild_config(guild_id, "honeypot_role", guild_config["honeypot_role"])
                print(f"  -> moderation config for guild {guild_id} written to Postgres")
            except Exception as e:
                print(f"  ERROR writing moderation config for guild {guild_id}: {e}")
                stats.errors += 1

    return stats


async def validate() -> None:
    db = await get_db()
    esports_data = load_esports_json()
    moderation_data = load_moderation_json()

    db_state = await db.esports.load_all()
    old_event_count = len(esports_data.get("event_to_match", {}))
    new_event_count = len(db_state["event_to_match"])
    print(f"event_to_match: JSON={old_event_count} DB={new_event_count} "
          f"{'OK' if old_event_count == new_event_count else 'MISMATCH'}")

    old_known_count = len(esports_data.get("known_match_ids", []))
    new_known_count = len(db_state["known_match_ids"])
    print(f"known_match_ids: JSON={old_known_count} DB={new_known_count} "
          f"{'OK' if old_known_count == new_known_count else 'MISMATCH'}")

    for guild_str in moderation_data:
        guild_id = int(guild_str)
        db_config = await db.moderation.get_guild_config(guild_id)
        json_config = moderation_data[guild_str]
        match = (
            db_config.get("member_log_webhook") == json_config.get("member_log_webhook")
            and db_config.get("join_role") == json_config.get("join_role")
            and db_config.get("honeypot_role") == json_config.get("honeypot_role")
        )
        print(f"moderation_config[{guild_id}]: {'OK' if match else 'MISMATCH'} "
              f"(JSON={json_config}, DB={db_config})")


def main():
    parser = argparse.ArgumentParser(description="Migrate RoaringBot JSON config to Postgres")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without writing")
    parser.add_argument("--migrate", action="store_true", help="Actually perform the migration")
    parser.add_argument("--validate", action="store_true", help="Compare JSON files against what's now in the DB")
    args = parser.parse_args()

    if not any([args.dry_run, args.migrate, args.validate]):
        parser.print_help()
        return

    if args.validate:
        asyncio.run(validate())
        return

    stats = asyncio.run(migrate(dry_run=not args.migrate))
    stats.report()
    if args.dry_run:
        print("\n(dry run - nothing was written; re-run with --migrate to apply)")


if __name__ == "__main__":
    main()
