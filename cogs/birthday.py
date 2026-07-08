"""Birthday Reminder Cog - Reads birthdays from Google Spreadsheet and posts daily messages"""

import asyncio
import logging
from datetime import datetime, time, timezone

import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import pytz

from core.config import config
from core.status_reporter import status_reporter

GERMAN_TZ = pytz.timezone("Europe/Berlin")

# Google Sheets API scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log = bot.get_cog_logger("birthday")
        self.gc = None

    async def cog_load(self):
        try:
            creds = Credentials.from_service_account_file(
                config.google_service_account_file, scopes=SCOPES
            )
            self.gc = gspread.authorize(creds)
            self.log.info("Google Sheets client initialized")
            status_reporter.record("birthday", sheets_connected=True, last_error=None)
        except Exception as e:
            self.log.error(f"Failed to initialize Google Sheets client: {e}")
            status_reporter.record("birthday", sheets_connected=False, last_error=str(e))
            return

        status_reporter.record(
            "birthday",
            channel_id=config.birthday_channel_id,
            spreadsheet_id=config.birthday_spreadsheet_id,
        )

        try:
            records = await asyncio.get_event_loop().run_in_executor(None, self._fetch_birthdays)
            status_reporter.record(
                "birthday",
                upcoming_birthdays=self._compute_upcoming(records, datetime.now(GERMAN_TZ).date()),
            )
        except Exception as e:
            self.log.warning(f"Initial upcoming-birthdays fetch failed (will retry on daily check): {e}")

        self.check_birthdays.start()

    def cog_unload(self):
        self.check_birthdays.cancel()

    @tasks.loop(time=[time(hour=8, minute=0), time(hour=9, minute=0)])
    async def check_birthdays(self):
        """Check Google Spreadsheet for today's birthdays and post messages"""
        now_berlin = datetime.now(GERMAN_TZ)

        # Only run at 10:00 German time (DST-aware):
        # Winter (CET = UTC+1): 09:00 UTC = 10:00 CET  → fires on second entry
        # Summer (CEST = UTC+2): 08:00 UTC = 10:00 CEST → fires on first entry
        if now_berlin.hour != 10:
            return

        channel = self.bot.get_channel(config.birthday_channel_id)
        if not channel:
            self.log.warning(f"Birthday channel {config.birthday_channel_id} not found")
            status_reporter.record(
                "birthday", last_check_at=_now_iso(), last_check_result="error",
                last_error=f"channel {config.birthday_channel_id} not found",
            )
            return

        try:
            records = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_birthdays
            )
        except Exception as e:
            self.log.error(f"Failed to read birthday spreadsheet: {e}")
            status_reporter.record(
                "birthday", last_check_at=_now_iso(), last_check_result="error", last_error=str(e)
            )
            return

        today_str = now_berlin.strftime("%d.%m")
        upcoming_birthdays = self._compute_upcoming(records, now_berlin.date())

        birthday_names = []
        invalid_dates = 0
        for record in records:
            discord_name = record.get("Discord", "").strip()
            date_str = record.get("Geburtsdatum", "").strip()
            left_date = record.get("Datum Austritt", "").strip()

            # Skip members without Discord name, birthday, or who have left
            if not discord_name or discord_name == "-" or not date_str or left_date:
                continue

            try:
                if len(date_str.split(".")) == 3:
                    parsed = datetime.strptime(date_str, "%d.%m.%Y")
                elif len(date_str.split(".")) == 2:
                    parsed = datetime.strptime(date_str, "%d.%m")
                else:
                    continue
                row_day_month = parsed.strftime("%d.%m")
            except ValueError:
                self.log.warning(f"Invalid date format for '{discord_name}': '{date_str}'")
                invalid_dates += 1
                continue

            if row_day_month == today_str:
                birthday_names.append(discord_name)

        if not birthday_names:
            self.log.info("No birthdays today")
            status_reporter.record(
                "birthday",
                last_check_at=_now_iso(),
                last_check_result="no_birthdays",
                last_error=None,
                invalid_date_entries=invalid_dates,
                registered_entries=len(records),
                upcoming_birthdays=upcoming_birthdays,
            )
            return

        # Build birthday emote string
        emote_str = ""
        if config.birthday_emote_id:
            emote_str = f" <:tabsSax:{config.birthday_emote_id}>"

        if len(birthday_names) == 1:
            description = f"**{birthday_names[0]}** hat heute Geburtstag!"
        else:
            names_formatted = ", ".join(f"**{n}**" for n in birthday_names[:-1])
            names_formatted += f" und **{birthday_names[-1]}**"
            description = f"{names_formatted} haben heute Geburtstag!"

        embed = discord.Embed(
            title=f"Happy Birthday!{emote_str}",
            description=description,
            color=0xFFD700,
        )
        await channel.send(embed=embed)
        self.log.info(f"Sent birthday message for: {', '.join(birthday_names)}")
        status_reporter.record(
            "birthday",
            last_check_at=_now_iso(),
            last_check_result="sent",
            last_error=None,
            invalid_date_entries=invalid_dates,
            registered_entries=len(records),
            last_birthday_names=birthday_names,
            upcoming_birthdays=upcoming_birthdays,
        )

    def _compute_upcoming(self, records: list, today, limit: int = 5) -> list:
        """Return the next `limit` upcoming birthdays, sorted by days until next occurrence."""
        upcoming = []
        for record in records:
            discord_name = record.get("Discord", "").strip()
            date_str = record.get("Geburtsdatum", "").strip()
            left_date = record.get("Datum Austritt", "").strip()
            if not discord_name or discord_name == "-" or not date_str or left_date:
                continue
            try:
                parts = date_str.split(".")
                if len(parts) == 3:
                    parsed = datetime.strptime(date_str, "%d.%m.%Y")
                elif len(parts) == 2:
                    parsed = datetime.strptime(date_str, "%d.%m")
                else:
                    continue
            except ValueError:
                continue

            try:
                next_date = parsed.replace(year=today.year).date()
                if next_date < today:
                    next_date = next_date.replace(year=today.year + 1)
            except ValueError:
                continue  # e.g. 29.02 in a non-leap year

            upcoming.append({
                "name": discord_name,
                "date": parsed.strftime("%d.%m"),
                "days_until": (next_date - today).days,
            })

        upcoming.sort(key=lambda u: u["days_until"])
        return upcoming[:limit]

    def _fetch_birthdays(self) -> list:
        """Fetch all records from the Register worksheet (synchronous, run in executor)"""
        ws = self.gc.open_by_key(config.birthday_spreadsheet_id).worksheet("Register")
        header = ws.row_values(1)
        # Find column indices for the fields we need
        try:
            discord_col = header.index("Discord")
            birthday_col = header.index("Geburtsdatum")
            left_col = header.index("Datum Austritt")
        except ValueError as e:
            raise ValueError(f"Required column not found in header: {e}")

        rows = ws.get_all_values()[1:]  # Skip header
        result = []
        for row in rows:
            if len(row) > max(discord_col, birthday_col, left_col):
                result.append({
                    "Discord": row[discord_col],
                    "Geburtsdatum": row[birthday_col],
                    "Datum Austritt": row[left_col],
                })
        return result

    @check_birthdays.before_loop
    async def before_check_birthdays(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    if not config.birthday_channel_id:
        logging.getLogger("roaringbot").info("Birthday cog disabled: BIRTHDAY_CHANNEL_ID not set")
        return
    if not config.birthday_spreadsheet_id:
        logging.getLogger("roaringbot").info("Birthday cog disabled: BIRTHDAY_SPREADSHEET_ID not set")
        return
    await bot.add_cog(BirthdayCog(bot))
