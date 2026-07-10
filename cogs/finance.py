"""Finance / Kassenbuch Cog - monthly cashbook report from Google Sheets.

Functional port of the standalone BearsFinanz/script.py cron script:
same date math, same balance/transaction parsing, same embed content.
Differences: config comes from .env instead of a JSON file, and the
report is posted by the bot itself instead of via a Discord webhook.
"""

import asyncio
import logging
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials

from core.config import config
from core.status_reporter import status_reporter

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

STATUS_REFRESH_HOURS = 6
RECENT_DAYS = 90


def _parse_amount(raw: str) -> Decimal:
    """Parse a German-formatted currency string ('1.234,56 €') into a Decimal."""
    cleaned = raw.replace(" €", "").replace(".", "").replace(",", "") if raw else ""
    if not cleaned:
        return Decimal(0)
    try:
        return Decimal(cleaned) / 100
    except InvalidOperation:
        return Decimal(0)


def _signed_amount(entry: dict) -> Decimal:
    if entry["in"]:
        return _parse_amount(entry["in"])
    if entry["out"]:
        return -_parse_amount(entry["out"])
    return Decimal(0)


class FinanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log = bot.get_cog_logger("finance")
        self.gc = None

    async def cog_load(self):
        try:
            creds = Credentials.from_service_account_file(
                config.kassenbuch_service_account_file, scopes=SCOPES
            )
            self.gc = gspread.authorize(creds)
            self.log.info("Google Sheets client initialized (Kassenbuch)")
            status_reporter.record("finance", sheets_connected=True, last_error=None)
        except Exception as e:
            self.log.error(f"Failed to initialize Google Sheets client: {e}")
            status_reporter.record("finance", sheets_connected=False, last_error=str(e))
            return

        self.monthly_report.start()
        self.refresh_status.start()

    def cog_unload(self):
        self.monthly_report.cancel()
        self.refresh_status.cancel()

    # ─── Sheet access ────────────────────────────────────────────────────────
    def _fetch_entries_sync(self) -> List[dict]:
        """Fetch and parse all Kassenbuch rows (synchronous, run in executor)."""
        ws = self.gc.open_by_key(config.kassenbuch_spreadsheet_id).worksheet(
            config.kassenbuch_worksheet_name
        )
        rows = ws.get_all_records()
        entries = []
        for r in rows:
            try:
                d = datetime.strptime(r["Datum"], "%d.%m.%Y").date()
            except Exception:
                continue
            entries.append({
                "date": d,
                "beleg": r.get("Beleg-Nr") or "",
                "in": r.get("Einnahme") or "",
                "out": r.get("Ausgabe") or "",
                "cat": r.get("Kategorie") or "",
                "note": r.get("Beschreibung / Kommentar") or "",
                "saldo": r.get("Saldo"),
            })
        return entries

    async def _fetch_entries(self) -> List[dict]:
        return await asyncio.get_event_loop().run_in_executor(None, self._fetch_entries_sync)

    # ─── Report building (same logic as BearsFinanz/script.py) ────────────────
    def _build_month_report(self, entries: List[dict], start_prev, end_prev) -> tuple:
        before = [e for e in entries if e["date"] < start_prev]
        saldo_start = sorted(before, key=lambda e: e["date"])[-1]["saldo"] if before else "–"

        month_tx = [e for e in entries if start_prev <= e["date"] <= end_prev]
        saldo_end = month_tx[-1]["saldo"] if month_tx else saldo_start

        lines = []
        bilanz = Decimal(0)
        for e in month_tx:
            amt = _signed_amount(e)
            bilanz += amt
            amt_str = f"{amt:+.2f} €"
            lines.append(f"-# - {e['date']:%d.%m}: **{amt_str}**, {e['cat']} ({e['note']})")

        embed = discord.Embed(
            title=f"Kassenbuch-Report {start_prev:%B %Y}",
            description="**Transaktionen:**\n" + ("\n".join(lines) if lines else "_Keine Buchungen_"),
            color=0xFFD400,
        )
        embed.add_field(name=f"Anfangssaldo ({start_prev:%d.%m}):", value=f"{saldo_start}", inline=True)
        embed.add_field(name=f"Endsaldo ({end_prev:%d.%m}):", value=f"{saldo_end}", inline=True)
        embed.add_field(name="Monatsbilanz:", value=f"{bilanz} €", inline=False)
        return embed, bilanz

    def _sheet_link_view(self) -> discord.ui.View:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Zum Kassenbuch",
            style=discord.ButtonStyle.link,
            url=f"https://docs.google.com/spreadsheets/d/{config.kassenbuch_spreadsheet_id}/edit?usp=sharing",
        ))
        return view

    # ─── Monthly report (replaces the external cron job) ───────────────────────
    @tasks.loop(time=time(hour=6, minute=0))  # UTC; fires daily, only acts on the 1st
    async def monthly_report(self):
        now = datetime.now(timezone.utc)
        if now.day != 1:
            return

        channel = self.bot.get_channel(config.kassenbuch_channel_id)
        if not channel:
            self.log.warning(f"Kassenbuch channel {config.kassenbuch_channel_id} not found")
            status_reporter.record(
                "finance", last_report_result="error",
                last_report_error=f"channel {config.kassenbuch_channel_id} not found",
            )
            return

        try:
            entries = await self._fetch_entries()
        except Exception as e:
            self.log.error(f"Failed to read Kassenbuch spreadsheet: {e}")
            status_reporter.record("finance", last_report_result="error", last_report_error=str(e))
            return

        today = now.date().replace(day=1)
        end_prev = today - timedelta(days=1)
        start_prev = end_prev.replace(day=1)

        embed, bilanz = self._build_month_report(entries, start_prev, end_prev)
        await channel.send(embed=embed, view=self._sheet_link_view())
        self.log.info(f"Sent Kassenbuch monthly report for {start_prev:%B %Y}")
        report_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report_month = start_prev.strftime("%Y-%m")
        status_reporter.record(
            "finance",
            last_report_result="sent",
            last_report_error=None,
            last_report_at=report_at,
            last_report_month=report_month,
            last_report_bilanz=f"{bilanz:+.2f} €",
        )
        status_reporter.record_event(
            "finance", "report_log",
            {"month": report_month, "sent_at": report_at},
            max_len=36,
        )

    @monthly_report.before_loop
    async def before_monthly_report(self):
        await self.bot.wait_until_ready()

    # ─── Status snapshot for the dashboard (data/status.json) ─────────────────
    @tasks.loop(hours=STATUS_REFRESH_HOURS)
    async def refresh_status(self):
        try:
            entries = await self._fetch_entries()
        except Exception as e:
            self.log.error(f"Failed to refresh Kassenbuch status: {e}")
            status_reporter.record("finance", last_error=str(e))
            return
        self._push_status(entries)

    @refresh_status.before_loop
    async def before_refresh_status(self):
        await self.bot.wait_until_ready()

    def _push_status(self, entries: List[dict]):
        if not entries:
            status_reporter.record("finance", sheets_connected=True, last_error=None, current_balance=None, transactions_recent=[])
            return

        sorted_entries = sorted(entries, key=lambda e: e["date"])
        current_balance = sorted_entries[-1]["saldo"]

        cutoff = datetime.now(timezone.utc).date() - timedelta(days=RECENT_DAYS)
        recent = [e for e in sorted_entries if e["date"] >= cutoff]

        status_reporter.record(
            "finance",
            sheets_connected=True,
            last_error=None,
            current_balance=current_balance,
            current_balance_date=sorted_entries[-1]["date"].strftime("%Y-%m-%d"),
            transactions_recent=[
                {
                    "date": e["date"].strftime("%Y-%m-%d"),
                    "amount": str(_signed_amount(e)),
                    "category": e["cat"],
                    "note": e["note"],
                    "saldo": e["saldo"],
                }
                for e in recent
            ],
        )

async def setup(bot: commands.Bot):
    if not config.kassenbuch_channel_id:
        logging.getLogger("roaringbot").info("Finance cog disabled: KASSENBUCH_CHANNEL_ID not set")
        return
    if not config.kassenbuch_spreadsheet_id:
        logging.getLogger("roaringbot").info("Finance cog disabled: KASSENBUCH_SPREADSHEET_ID not set")
        return
    await bot.add_cog(FinanceCog(bot))
