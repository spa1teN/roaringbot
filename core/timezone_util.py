"""
Timezone utility module for consistent time handling across the bot.
Provides MEZ/MESZ (Central European Time/Central European Summer Time) support
with guild-specific timezone configuration, backed by Postgres (guild_timezones
table) instead of per-guild YAML files - mirrors the pattern already used by
the sibling Tausendsassa bot. Currently unused (no guild has set a custom
timezone), kept for when a per-guild override is needed.
"""

import datetime
import logging
from typing import Optional

import pytz

from db import get_db

# German timezone (default fallback)
GERMAN_TZ = pytz.timezone('Europe/Berlin')

log = logging.getLogger("roaringbot.timezone")


async def save_guild_timezone(guild_id: int, timezone_str: str) -> bool:
    """Save timezone configuration for a specific guild"""
    try:
        pytz.timezone(timezone_str)  # validate, raises if unknown
        db = await get_db()
        await db.guilds.set_timezone(guild_id, timezone_str)
        log.info(f"Saved timezone {timezone_str} for guild {guild_id}")
        return True
    except Exception as e:
        log.error(f"Failed to save timezone config for guild {guild_id}: {e}")
        return False


async def get_guild_timezone(guild_id: Optional[int] = None) -> pytz.BaseTzInfo:
    """Get timezone for a specific guild, falling back to German timezone"""
    if guild_id:
        guild_id = int(guild_id) if isinstance(guild_id, str) else guild_id
        try:
            db = await get_db()
            timezone_str = await db.guilds.get_timezone(guild_id)
            return pytz.timezone(timezone_str)
        except Exception as e:
            log.warning(f"Could not resolve timezone for guild {guild_id}, falling back to German time: {e}")

    return GERMAN_TZ


async def get_current_time(guild_id: Optional[int] = None) -> datetime.datetime:
    """Get current time in guild's configured timezone or German timezone."""
    tz = await get_guild_timezone(guild_id)
    return datetime.datetime.now(tz)


async def get_current_timestamp(guild_id: Optional[int] = None) -> int:
    """Get current timestamp for Discord embeds in guild's timezone."""
    return int((await get_current_time(guild_id)).timestamp())


async def format_time(dt: datetime.datetime = None, guild_id: Optional[int] = None, format_str: str = "%d.%m.%Y %H:%M:%S") -> str:
    """Format a datetime object to guild's timezone string."""
    tz = await get_guild_timezone(guild_id)

    if dt is None:
        dt = datetime.datetime.now(tz)
    elif dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)

    target_dt = dt.astimezone(tz)
    return target_dt.strftime(format_str)


async def to_guild_timezone(dt: datetime.datetime, guild_id: Optional[int] = None) -> datetime.datetime:
    """Convert a datetime object to guild's timezone."""
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)

    if guild_id:
        guild_id = int(guild_id) if isinstance(guild_id, str) else guild_id

    tz = await get_guild_timezone(guild_id)
    return dt.astimezone(tz)
