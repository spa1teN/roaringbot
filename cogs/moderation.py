import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone
from typing import Optional
import aiohttp
import asyncio

# Import timezone utilities
from core.timezone_util import get_current_time, get_current_timestamp, save_guild_timezone, get_guild_timezone
from core.status_reporter import status_reporter

class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log = bot.get_cog_logger("moderation")
        self.member_join_times = {}  # Store join times for leave duration calculation
        self.recently_banned_kicked = set()  # Track recently banned/kicked users
        self.honeypot_guild_id = 624700952636817448
        self.bot_trap_guild_id = 624700952636817448  # same guild, different feature
        self._bot_trap_ban_count = 0  # session counter for log messages

    async def get_guild_config(self, guild_id: int) -> dict:
        """Get configuration for specific guild"""
        return await self.bot.db.moderation.get_guild_config(guild_id)

    async def set_guild_config(self, guild_id: int, key: str, value):
        """Set configuration value for specific guild"""
        await self.bot.db.moderation.set_guild_config(guild_id, key, value)

    async def clear_guild_config(self, guild_id: int, key: str):
        """Clear a single configuration value for a specific guild"""
        await self.bot.db.moderation.clear_guild_config(guild_id, key)

    # The dashboard panel itself lives in core/mod_views.py
    # (build_dashboard_view) — it is a Components-V2 LayoutView.

    def _build_log_view(self, color: int, body: str, avatar_url: str) -> discord.ui.LayoutView:
        """Compact CV2 member-log message: bold first line, -# detail lines,
        avatar as thumbnail."""
        container = discord.ui.Container(accent_colour=discord.Colour(color))
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(body),
            accessory=discord.ui.Thumbnail(media=avatar_url),
        ))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view

    def build_join_view(self, member: discord.Member, role_assigned=None, role_name=None, role_id=None) -> discord.ui.LayoutView:
        """Log message for member join event"""
        user_link = f"[{member.display_name}](https://discord.com/users/{member.id})"
        created_unix = int(member.created_at.timestamp())
        body = f"**{user_link} joined the server**\n-# Account created: <t:{created_unix}:R>"

        # Add role assignment status if join role is configured
        if role_assigned is not None:
            if role_assigned:
                if role_id:
                    body += f"\n-# Auto Role: <@&{role_id}>"
                else:
                    body += f"\n-# Auto Role: Assigned: @{role_name}"
            else:
                body += "\n-# Auto Role: ❌ Failed to assign"

        return self._build_log_view(0x00FF00, body, member.display_avatar.url)

    def build_leave_view(self, member: discord.Member, join_time: Optional[datetime] = None) -> discord.ui.LayoutView:
        """Log message for member leave event"""
        user_link = f"[{member.display_name}](https://discord.com/users/{member.id})"
        body = f"**{user_link} left the server**"
        if join_time:
            join_unix = int(join_time.timestamp())
            body += f"\n-# Joined: <t:{join_unix}:R>"
        return self._build_log_view(0xFF0000, body, member.display_avatar.url)

    def build_ban_view(self, user: discord.User, moderator: Optional[discord.Member], reason: Optional[str]) -> discord.ui.LayoutView:
        """Log message for ban event"""
        user_link = f"[{user.display_name}](https://discord.com/users/{user.id})"
        body = f"**{user_link} was banned**"
        if moderator:
            mod_link = f"[{moderator.display_name}](https://discord.com/users/{moderator.id})"
            body += f"\n-# Banned by: {mod_link}"
        if reason:
            body += f"\n-# Reason: {reason}"
        return self._build_log_view(0x8B0000, body, user.display_avatar.url)

    def build_kick_view(self, user: discord.User, moderator: Optional[discord.Member], reason: Optional[str]) -> discord.ui.LayoutView:
        """Log message for kick event"""
        user_link = f"[{user.display_name}](https://discord.com/users/{user.id})"
        body = f"**{user_link} was kicked**"
        if moderator:
            mod_link = f"[{moderator.display_name}](https://discord.com/users/{moderator.id})"
            body += f"\n-# Kicked by: {mod_link}"
        if reason:
            body += f"\n-# Reason: {reason}"
        return self._build_log_view(0xFF4500, body, user.display_avatar.url)

    def build_timeout_view(self, member: discord.Member, timed_out_until: datetime, moderator: Optional[discord.Member], reason: Optional[str]) -> discord.ui.LayoutView:
        """Log message for timeout event"""
        user_link = f"[{member.display_name}](https://discord.com/users/{member.id})"
        timeout_unix = int(timed_out_until.timestamp())
        body = f"**{user_link} was timed out**\n-# Ends: <t:{timeout_unix}:R>"
        if moderator:
            mod_link = f"[{moderator.display_name}](https://discord.com/users/{moderator.id})"
            body += f"\n-# Timed out by: {mod_link}"
        if reason:
            body += f"\n-# Reason: {reason}"
        return self._build_log_view(0xFFA500, body, member.display_avatar.url)

    def build_unban_view(self, user: discord.User, moderator: Optional[discord.Member]) -> discord.ui.LayoutView:
        """Log message for unban event"""
        user_link = f"[{user.display_name}](https://discord.com/users/{user.id})"
        body = f"**{user_link} was unbanned**"
        if moderator:
            mod_link = f"[{moderator.display_name}](https://discord.com/users/{moderator.id})"
            body += f"\n-# Unbanned by: {mod_link}"
        return self._build_log_view(0x90EE90, body, user.display_avatar.url)


    async def send_log_message(self, guild_id: int, view: discord.ui.LayoutView):
        """Send a CV2 log message to the configured webhook.

        The view may only contain link buttons — plain channel webhooks cannot
        carry interactive components (discord.py enforces this via
        view.is_dispatchable())."""
        config = await self.get_guild_config(guild_id)
        webhook_url = config.get('member_log_webhook')

        if not webhook_url:
            if guild_id == 624700952636817448:
                status_reporter.record("moderation", member_log_status="disabled")
            return

        # Note: the old code tried avatar_url="attachment://pb.png", which
        # Discord has never accepted (invalid scheme) — every log silently
        # went through the no-avatar fallback. The webhook's own avatar
        # (set at creation) is what actually shows.
        try:
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(webhook_url, session=session)
                await webhook.send(view=view)
            if guild_id == 624700952636817448:
                status_reporter.record("moderation", member_log_status="ok", member_log_last_error=None)
        except (discord.HTTPException, aiohttp.ClientError) as e:
            self.log.warning(f"Member-log webhook failed for guild {guild_id}: {e}")
            if guild_id == 624700952636817448:
                status_reporter.record("moderation", member_log_status="error", member_log_last_error=str(e))

    async def check_for_kick(self, guild: discord.Guild, user_id: int):
        """Check audit logs for recent kick events"""
        try:
            await asyncio.sleep(0.5)  # Small delay to ensure audit log is updated
            async for entry in guild.audit_logs(action=discord.AuditLogAction.kick, limit=5):
                if entry.target and entry.target.id == user_id:
                    # Check if this kick happened recently (within last 10 seconds)
                    time_diff = datetime.now(timezone.utc) - entry.created_at
                    if time_diff.total_seconds() < 10:
                        # Add to banned/kicked set and send kick embed
                        self.recently_banned_kicked.add(user_id)
                        status_reporter.bump_counter("moderation", "kicks")
                        status_reporter.record_event(
                            "moderation", "events",
                            {
                                "type": "kick",
                                "user": str(entry.target),
                                "user_id": entry.target.id,
                                "moderator": str(entry.user) if entry.user else None,
                                "reason": entry.reason,
                                "guild": guild.name,
                            },
                            max_len=300,
                        )

                        view = self.build_kick_view(entry.target, entry.user, entry.reason)
                        await self.send_log_message(guild.id, view)
                        return True
        except (discord.Forbidden, discord.NotFound):
            pass
        return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle member join events"""
        status_reporter.bump_counter("moderation", "joins")
        status_reporter.record_event(
            "moderation", "events",
            {"type": "join", "user": str(member), "user_id": member.id, "guild": member.guild.name},
            max_len=300,
        )
        # Store join time for duration calculation
        self.member_join_times[member.id] = datetime.now(timezone.utc)
        
        # Auto-assign role if configured
        config = await self.get_guild_config(member.guild.id)
        join_role_id = config.get('join_role')
        
        role_assigned = None
        role_name = None
        
        if not join_role_id:
            status_reporter.record("moderation", join_role_status="disabled")
        else:
            role = member.guild.get_role(join_role_id)
            if role:
                role_name = role.name
                try:
                    await member.add_roles(role, reason="Auto-assigned join role")
                    role_assigned = True
                    status_reporter.record("moderation", join_role_status="ok", join_role_last_error=None)
                except discord.Forbidden as e:
                    role_assigned = False  # Bot doesn't have permission
                    self.log.warning(f"Failed to assign join role to {member} in guild {member.guild.id}: {e}")
                    status_reporter.record("moderation", join_role_status="error", join_role_last_error=str(e))
            else:
                role_assigned = False  # Role not found
                self.log.warning(f"Join role {join_role_id} not found in guild {member.guild.id}")
                status_reporter.record("moderation", join_role_status="error", join_role_last_error=f"role {join_role_id} not found")

        # Send log message with role assignment status (profile link button is
        # part of the container)
        view = self.build_join_view(member, role_assigned=role_assigned, role_name=role_name, role_id=join_role_id)
        await self.send_log_message(member.guild.id, view)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Handle member leave events"""
        # Check if this user was recently banned or kicked
        if member.id in self.recently_banned_kicked:
            self.recently_banned_kicked.discard(member.id)  # Remove from set
            return

        # Check for recent kick in audit logs
        if await self.check_for_kick(member.guild, member.id):
            return

        status_reporter.bump_counter("moderation", "leaves")
        status_reporter.record_event(
            "moderation", "events",
            {"type": "leave", "user": str(member), "user_id": member.id, "guild": member.guild.name},
            max_len=300,
        )
        # Calculate duration on server
        join_time = None
        if member.id in self.member_join_times:
            join_time = self.member_join_times[member.id]
            del self.member_join_times[member.id]

        # Send log message
        view = self.build_leave_view(member, join_time)
        await self.send_log_message(member.guild.id, view)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        """Handle member ban events"""
        status_reporter.bump_counter("moderation", "bans")
        # Add user to recently banned set to prevent leave message
        self.recently_banned_kicked.add(user.id)
        
        # Get ban information
        try:
            ban = await guild.fetch_ban(user)
            reason = ban.reason
        except discord.NotFound:
            reason = None
        
        # Try to get moderator from audit log
        moderator = None
        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=1):
                if entry.target.id == user.id:
                    moderator = entry.user
                    break
        except discord.Forbidden:
            pass

        status_reporter.record_event(
            "moderation", "events",
            {
                "type": "ban",
                "user": str(user),
                "user_id": user.id,
                "moderator": str(moderator) if moderator else None,
                "reason": reason,
                "guild": guild.name,
            },
            max_len=300,
        )

        view = self.build_ban_view(user, moderator, reason)
        await self.send_log_message(guild.id, view)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        """Handle member unban events"""
        status_reporter.bump_counter("moderation", "unbans")
        # Try to get moderator from audit log
        moderator = None
        try:
            async for entry in guild.audit_logs(action=discord.AuditLogAction.unban, limit=1):
                if entry.target.id == user.id:
                    moderator = entry.user
                    break
        except discord.Forbidden:
            pass

        status_reporter.record_event(
            "moderation", "events",
            {
                "type": "unban",
                "user": str(user),
                "user_id": user.id,
                "moderator": str(moderator) if moderator else None,
                "guild": guild.name,
            },
            max_len=300,
        )

        view = self.build_unban_view(user, moderator)
        await self.send_log_message(guild.id, view)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Handle member update events (for timeouts)"""
        # Honeypot: if the honeypot role was just added, ban instantly
        await self._check_honeypot_role_added(before, after)

        # Check if timeout status changed
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:  # Member was timed out
                status_reporter.bump_counter("moderation", "timeouts")
                # Try to get moderator and reason from audit log
                moderator = None
                reason = None
                try:
                    async for entry in after.guild.audit_logs(action=discord.AuditLogAction.member_update, limit=5):
                        if entry.target.id == after.id and hasattr(entry.changes, 'after') and hasattr(entry.changes.after, 'timed_out_until'):
                            moderator = entry.user
                            reason = entry.reason
                            break
                except discord.Forbidden:
                    pass
                
                status_reporter.record_event(
                    "moderation", "events",
                    {
                        "type": "timeout",
                        "user": str(after),
                        "user_id": after.id,
                        "moderator": str(moderator) if moderator else None,
                        "reason": reason,
                        "guild": after.guild.name,
                    },
                    max_len=300,
                )

                # Pass timed_out_until for dynamic timestamp
                view = self.build_timeout_view(after, after.timed_out_until, moderator, reason)
                await self.send_log_message(after.guild.id, view)

    @app_commands.command(name="mod_dashboard", description="Manage current moderation configuration")
    @app_commands.default_permissions(administrator=True)
    async def mod_dashboard(self, interaction: discord.Interaction):
        """Display moderation dashboard with interactive buttons"""
        # Import here to avoid circular imports
        from core.mod_views import build_dashboard_view

        view = await build_dashboard_view(self, interaction.guild.id)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="clear", description="Delete a specified number of messages from the current channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    async def clear_messages(self, interaction: discord.Interaction, amount: int):
        """Clear specified number of messages from channel"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You need `Manage Messages` permission to use this command.", ephemeral=True)
            return
        
        if not interaction.guild.me.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ I don't have permission to delete messages in this channel.", ephemeral=True)
            return
        
        if amount < 1 or amount > 100:
            await interaction.response.send_message("❌ Amount must be between 1 and 100.", ephemeral=True)
            return
        
        # Defer the response since message deletion might take time
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get the channel where command was used
            channel = interaction.channel
            
            # Delete messages (Discord API limit is 100 messages at once)
            deleted = await channel.purge(limit=amount)
            deleted_count = len(deleted)
            status_reporter.record(
                "moderation",
                last_clear_count=deleted_count,
                last_clear_channel_id=channel.id,
                last_clear_by=str(interaction.user),
            )

            # Compact CV2 one-liner confirmation
            from core.mod_views import notice_view
            plural = "en" if deleted_count != 1 else ""
            await interaction.followup.send(
                view=notice_view(f"🧹 **{deleted_count} Nachricht{plural} gelöscht**", 0xED4245),
                ephemeral=True,
            )
            
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to delete messages in this channel.", ephemeral=True)
        except discord.HTTPException as e:
            if e.code == 50034:  # You can only bulk delete messages that are under 14 days old
                await interaction.followup.send("❌ Cannot delete messages older than 14 days. Try with a smaller number.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred while deleting messages.", ephemeral=True)

    @app_commands.command(name="spa1timo", description="Lesen, Verstehen, Nachdenken, Schreiben (oder besser nicht)")
    async def spa1timo(self, interaction: discord.Interaction):
        """Respond with the classic spa1timo quote"""
        await interaction.response.send_message('"Lesen, Verstehen, Nachdenken, Schreiben (oder besser nicht)"')


    # ── Periodic config reporter (lightweight — no banning, just status) ──

    async def cog_load(self):
        self._config_reporter.start()

    def cog_unload(self):
        self._config_reporter.cancel()

    @tasks.loop(seconds=60)
    async def _config_reporter(self):
        """Report moderation config state to status.json (no side effects)."""
        guild = self.bot.get_guild(self.honeypot_guild_id)
        if not guild:
            return

        config = await self.get_guild_config(guild.id)

        # Channel IDs
        bot_trap_id = config.get("bot_trap_channel")
        if bot_trap_id:
            channel = guild.get_channel(bot_trap_id)
            bt_status = "ok" if channel else "error"
            bt_error = None if channel else f"channel {bot_trap_id} not found"
        else:
            bt_status = "disabled"
            bt_error = None

        hp_role_id = config.get("honeypot_role")
        if hp_role_id:
            role = guild.get_role(hp_role_id)
            hp_status = "ok" if role else "error"
            hp_error = None if role else f"role {hp_role_id} not found"
        else:
            hp_status = "disabled"
            hp_error = None

        status_reporter.record("moderation",
            member_log_channel_id=config.get("member_log_channel"),
            bot_trap_channel_id=bot_trap_id,
            bot_trap_status=bt_status,
            bot_trap_last_error=bt_error,
            honeypot_status=hp_status,
            honeypot_last_error=hp_error,
        )

    @_config_reporter.before_loop
    async def _before_config_reporter(self):
        await self.bot.wait_until_ready()

    # ── Honeypot: event-driven via on_member_update ───────────────────────

    HONEYPOT_BAN_REASON = (
        "Autobann - user claimed the honeypot role. "
        "All messages up to 7 days ago deleted"
    )

    async def _check_honeypot_role_added(self, before: discord.Member, after: discord.Member):
        """If the honeypot role was just added, ban instantly."""
        guild = after.guild
        if guild.id != self.honeypot_guild_id:
            return

        config = await self.get_guild_config(guild.id)
        hp_role_id = config.get("honeypot_role")
        if not hp_role_id:
            return

        had_role = any(r.id == hp_role_id for r in before.roles)
        has_role = any(r.id == hp_role_id for r in after.roles)
        if had_role or not has_role:
            return  # role was already present, or was removed (not added)

        ban_start = datetime.now(timezone.utc)
        try:
            await guild.ban(after, reason=self.HONEYPOT_BAN_REASON, delete_message_days=7)
            ban_end = datetime.now(timezone.utc)
            time_to_ban_ms = int((ban_end - ban_start).total_seconds() * 1000)

            status_reporter.bump_counter("moderation", "honeypot_bans")
            status_reporter.record("moderation", honeypot_status="ok", honeypot_last_error=None)
            status_reporter.record_event(
                "moderation", "events",
                {
                    "type": "honeypot_ban",
                    "user": str(after),
                    "user_id": after.id,
                    "guild": guild.name,
                    "time_to_ban_ms": time_to_ban_ms,
                },
                max_len=300,
            )
            self.log.info(
                "Honeypot banned %s (ID: %s) in %dms",
                after, after.id, time_to_ban_ms,
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            status_reporter.record("moderation", honeypot_status="error", honeypot_last_error=str(e))
            self.log.warning(f"Honeypot ban failed for {after} in guild {guild.id}: {e}")

    # ── Bot-Trap: instant on_message listener ────────────────────────────

    # Hardcoded: the announcement message in the bot-trap channel that
    # explains what the channel is. Users who post this exact message are
    # NOT banned (it's the bot's own warning).
    BOT_TRAP_ANNOUNCEMENT_ID = 1525846311537213600
    BOT_TRAP_BAN_REASON = (
        "Autobann - user posted to the bot-trap channel. "
        "All messages up to 7 days ago deleted"
    )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Quick guard: only check if this guild has a bot-trap configured
        guild_id = message.guild.id
        if guild_id != self.bot_trap_guild_id:
            return

        config = await self.get_guild_config(guild_id)
        trap_channel_id = config.get('bot_trap_channel')
        if not trap_channel_id or message.channel.id != trap_channel_id:
            return

        # Skip the announcement message
        if message.id == self.BOT_TRAP_ANNOUNCEMENT_ID:
            status_reporter.record("moderation", bot_trap_status="ok", bot_trap_last_error=None)
            return

        ban_start = datetime.now(timezone.utc)
        try:
            await message.guild.ban(
                message.author,
                reason=self.BOT_TRAP_BAN_REASON,
                delete_message_days=7,
            )
            ban_end = datetime.now(timezone.utc)
            time_to_ban_ms = int((ban_end - ban_start).total_seconds() * 1000)

            status_reporter.bump_counter("moderation", "bot_trap_bans")
            self._bot_trap_ban_count += 1
            status_reporter.record("moderation", bot_trap_status="ok", bot_trap_last_error=None)
            status_reporter.record_event(
                "moderation", "events",
                {
                    "type": "bot_trap_ban",
                    "user": str(message.author),
                    "user_id": message.author.id,
                    "guild": message.guild.name,
                    "time_to_ban_ms": time_to_ban_ms,
                },
                max_len=300,
            )
            self.log.info(
                "Bot-trap banned #%d: %s (ID: %s) in %dms",
                self._bot_trap_ban_count, message.author, message.author.id, time_to_ban_ms,
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            status_reporter.record("moderation", bot_trap_status="error", bot_trap_last_error=str(e))
            self.log.warning(f"Bot-trap ban failed for {message.author} in guild {guild_id}: {e}")


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
