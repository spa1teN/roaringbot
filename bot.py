# bot.py
import os
import yaml
import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
import aiohttp
import asyncio
from datetime import datetime, timezone
import traceback
from typing import Dict, Optional

import discord
from discord.ext import commands
from core.config import config
from core.cache_manager import cache_manager
from core.http_client import http_client
from core.validation import run_full_validation, log_validation_results
from core.status_reporter import status_reporter
from db import get_db

from dotenv import load_dotenv
load_dotenv()

# ─── Error Tracker Handler ──────────────────────────────────────────────────
class ErrorTrackerHandler(logging.Handler):
    """Feeds log records into the dashboard status instead of posting them to
    a Discord webhook. Every INFO+ record bumps bot.counters.log_messages (the
    "activity" line so the dashboard log graph is never empty), ERROR+
    records additionally bump bot.counters.log_errors, and WARNING+ records
    are appended to the rolling bot.error_log so the click-to-inspect popup
    only shows things worth looking at."""

    def emit(self, record):
        try:
            status_reporter.bump_counter("bot", "log_messages")
            if record.levelno >= logging.ERROR:
                status_reporter.bump_counter("bot", "log_errors")
            if record.levelno >= logging.WARNING:
                status_reporter.record_event(
                    "bot", "error_log",
                    {
                        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                    },
                    max_len=200,
                )
        except Exception:
            pass  # logging must never itself raise

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_FORMAT  = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

error_tracker_handler = ErrorTrackerHandler()
error_tracker_handler.setLevel(logging.INFO)

# Base logging setup
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        logging.StreamHandler(),
        TimedRotatingFileHandler("logs/roaringbot.log", when="midnight", interval=1, backupCount=30, encoding="utf-8"),
        error_tracker_handler,
    ]
)

# Main bot logger
log = logging.getLogger("roaringbot")

# ─── Intents & COG-Liste ────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

COGS = ["cogs.moderation", "cogs.esports", "cogs.birthday", "cogs.finance"]

# ─── Enhanced Bot-Klasse ────────────────────────────────────────────────────
class RoaringBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

        self.cog_loggers: Dict[str, logging.Logger] = {}
        self.owner_id = config.owner_id
        self.db = None  # set in setup_hook once connected

    def get_cog_logger(self, cog_name: str) -> logging.Logger:
        """Get or create a logger for a specific cog"""
        logger_name = f"roaringbot.{cog_name}"

        if logger_name not in self.cog_loggers:
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.INFO)

            # Don't add handlers if they're already inherited from parent logger
            if not logger.handlers:
                # Console handler
                console_handler = logging.StreamHandler()
                console_handler.setLevel(logging.INFO)
                formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
                console_handler.setFormatter(formatter)
                logger.addHandler(console_handler)

                # File handler with 24-hour rotation (30 days retention)
                file_handler = TimedRotatingFileHandler(
                    f"logs/{cog_name}.log",
                    when="midnight",
                    interval=1,
                    backupCount=30,
                    encoding="utf-8"
                )
                file_handler.setLevel(logging.INFO)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

                logger.addHandler(error_tracker_handler)
                logger.propagate = False

            self.cog_loggers[logger_name] = logger

        return self.cog_loggers[logger_name]

    async def setup_hook(self):
        # Restore rolling event logs (match events, sent birthday posts, ...) from the
        # previous run's status.json, so a restart doesn't wipe the dashboard's history.
        status_reporter.load()

        # Connect to Postgres (moderation config, E-Sports state, birthday dedup)
        self.db = await get_db()
        log.info("✅ Database connected")

        # Start cache management
        await cache_manager.start_cleanup_task()
        log.info("✅ Cache manager started")

        # Create logs directory for cog-specific logs
        os.makedirs("logs", exist_ok=True)

        # Load extensions
        for ext in COGS:
            try:
                await self.load_extension(ext)
                log.info(f"✅ Loaded extension {ext}")
            except Exception as e:
                log.exception(f"❌ Failed to load extension {ext}: {e}")

        await self.tree.sync()
        log.info("✅ All slash commands synced")

        status_reporter.record("bot", loaded_cogs=list(self.cogs.keys()))
        await status_reporter.start(asyncio)

    async def on_ready(self):
        status = discord.Status.online
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="/help"
        )
        #await self.change_presence(status=status, activity=activity)
        log.info(f"🤖 Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"📊 Connected to {len(self.guilds)} guild(s)")
        status_reporter.record(
            "bot",
            user=str(self.user),
            user_id=self.user.id,
            guild_count=len(self.guilds),
            latency_ms=round(self.latency * 1000) if self.latency else None,
            gateway_status="connected",
        )
        print("------")

    async def on_disconnect(self):
        status_reporter.record("bot", gateway_status="disconnected")

    async def on_resumed(self):
        status_reporter.bump_counter("bot", "reconnects")
        status_reporter.record(
            "bot",
            gateway_status="connected",
            latency_ms=round(self.latency * 1000) if self.latency else None,
        )

    async def on_command_error(self, ctx, error):
        """Handle command errors and log them"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore unknown commands

        status_reporter.bump_counter("bot", "command_errors")
        log.error(
            f"Command error in '{ctx.command}' used by {ctx.author} (ID: {ctx.author.id}) "
            f"in guild {ctx.guild.id if ctx.guild else 'DM'}: {error}",
            exc_info=True
        )

    async def on_error(self, event, *args, **kwargs):
        """Handle general bot errors"""
        status_reporter.bump_counter("bot", "errors")
        log.error(f"Bot error in event '{event}'", exc_info=True)

    async def close(self):
        """Cleanup when bot is shutting down"""
        log.info("🔄 Bot is shutting down...")

        status_reporter.record("bot", gateway_status="disconnected")
        try:
            status_reporter.write()
        except Exception:
            pass
        await status_reporter.stop()

        # Stop cache manager
        await cache_manager.stop_cleanup_task()
        log.info("✅ Cache manager stopped")

        # Stop HTTP client
        await http_client.close()
        log.info("✅ HTTP client closed")

        # Close DB pool
        if getattr(self, "db", None):
            await self.db.close()
            log.info("✅ Database connection closed")

        await super().close()

# ─── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        # Run comprehensive validation before starting
        log.info("🔍 Running configuration validation...")
        validation_results = run_full_validation()
        log_validation_results(validation_results)
        
        if not validation_results["valid"]:
            log.error("❌ Validation failed. Please fix the issues above and restart.")
            exit(1)
        
        # Log configuration for debugging
        config.log_configuration()

        log.info("🚀 Starting RoaringBot...")
        bot = RoaringBot()
        
        bot.run(config.discord_token)
    except ValueError as e:
        log.error(f"Configuration error: {e}")
        exit(1)
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
    except Exception as e:
        log.error(f"Bot crashed: {e}", exc_info=True)
