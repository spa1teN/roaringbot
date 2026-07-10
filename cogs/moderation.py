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

    async def get_guild_config(self, guild_id: int) -> dict:
        """Get configuration for specific guild"""
        return await self.bot.db.moderation.get_guild_config(guild_id)

    async def set_guild_config(self, guild_id: int, key: str, value):
        """Set configuration value for specific guild"""
        await self.bot.db.moderation.set_guild_config(guild_id, key, value)

    async def clear_guild_config(self, guild_id: int, key: str):
        """Clear a single configuration value for a specific guild"""
        await self.bot.db.moderation.clear_guild_config(guild_id, key)

    async def create_dashboard_embed(self, guild_id: int) -> discord.Embed:
        """Create embed for moderation dashboard"""
        config = await self.get_guild_config(guild_id)
        
        embed = discord.Embed(
            title="🛡️ Moderation Dashboard",
            color=0x5865f2
        )
        
        # Member logging configuration
        webhook_url = config.get('member_log_webhook')
        if webhook_url:
            embed.add_field(
                name="📋 Member Logging",
                value="✅ **Enabled**\nLogging joins, leaves, bans, kicks, and timeouts",
                inline=False
            )
        else:
            embed.add_field(
                name="📋 Member Logging",
                value="❌ **Disabled**\nClick 'Setup Member Log' to enable",
                inline=False
            )
        
        # Join role configuration
        join_role_id = config.get('join_role')
        if join_role_id:
            # We need the guild object to get the role
            guild = self.bot.get_guild(guild_id)
            if guild:
                role = guild.get_role(join_role_id)
                role_mention = role.mention if role else f"Role not found (ID: {join_role_id})"
            else:
                role_mention = f"Role ID: {join_role_id}"
            
            embed.add_field(
                name="👤 Auto Join Role",
                value=f"✅ **Enabled**\nAssigning role: {role_mention}",
                inline=False
            )
        else:
            embed.add_field(
                name="👤 Auto Join Role",
                value="❌ **Disabled**\nClick 'Setup Join Role' to enable",
                inline=False
            )

        # Honeypot configuration
        honeypot_role_id = config.get('honeypot_role')
        if honeypot_role_id:
            guild = self.bot.get_guild(guild_id)
            if guild:
                role = guild.get_role(honeypot_role_id)
                role_mention = role.mention if role else f"Role not found (ID: {honeypot_role_id})"
            else:
                role_mention = f"Role ID: {honeypot_role_id}"

            embed.add_field(
                name="🍯 Honeypot",
                value=f"✅ **Enabled**\nAuto-banning members with role: {role_mention}",
                inline=False
            )
        else:
            embed.add_field(
                name="🍯 Honeypot",
                value="❌ **Disabled**\nClick 'Setup Honeypot' to enable",
                inline=False
            )

        embed.set_footer(text=f"Guild ID: {guild_id}")
        
        return embed

    def create_join_embed(self, member: discord.Member, role_assigned=None, role_name=None, role_id=None) -> discord.Embed:
        """Create embed for member join event"""
        embed = discord.Embed(
            title=f"{member.display_name} joined the server",
            color=0x00ff00  # Green for joins
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        # Calculate account age
        account_age = datetime.now(timezone.utc) - member.created_at
        days = account_age.days
        if days == 0:
            age_str = "Less than 1 day"
        elif days == 1:
            age_str = "1 day"
        else:
            age_str = f"{days} days"
        
        embed.add_field(name="Account Age", value=age_str, inline=True)
        
        # Add role assignment status if join role is configured
        if role_assigned is not None:
            if role_assigned:
                if role_id:
                    embed.add_field(name="Auto Role", value=f"<@&{role_id}>", inline=True)
                else:
                    embed.add_field(name="Auto Role", value=f"Assigned: @{role_name}", inline=True)
            else:
                embed.add_field(name="Auto Role", value="❌ Failed to assign", inline=True)
        
        return embed

    def create_leave_embed(self, member: discord.Member, duration: Optional[str] = None) -> discord.Embed:
        """Create embed for member leave event"""
        embed = discord.Embed(
            title=f"{member.display_name} left the server",
            color=0xff0000  # Red for leaves
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        if duration:
            embed.add_field(name="Time on Server", value=duration, inline=True)
        
        
        return embed

    def create_ban_embed(self, user: discord.User, moderator: Optional[discord.Member], reason: Optional[str], guild: discord.Guild) -> discord.Embed:
        """Create embed for ban event"""
        embed = discord.Embed(
            title=f"{user.display_name} was banned",
            color=0x8b0000  # Dark red for bans
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        if moderator:
            embed.add_field(name="Banned by", value=f"<@{moderator.id}>", inline=True)
        
        if reason:
            embed.add_field(name="Reason", value=reason, inline=True)
        
        
        return embed

    def create_kick_embed(self, user: discord.User, moderator: Optional[discord.Member], reason: Optional[str], guild: discord.Guild) -> discord.Embed:
        """Create embed for kick event"""
        embed = discord.Embed(
            title=f"{user.display_name} was kicked",
            color=0xff4500  # Orange red for kicks
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        if moderator:
            embed.add_field(name="Kicked by", value=f"<@{moderator.id}>", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=True)
        
        
        
        return embed

    def create_timeout_embed(self, member: discord.Member, duration: str, moderator: Optional[discord.Member], reason: Optional[str]) -> discord.Embed:
        """Create embed for timeout event"""
        embed = discord.Embed(
            title=f"{member.display_name} was timed out",
            color=0xffa500  # Orange for timeouts
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="Duration", value=duration, inline=True)
        if moderator:
            embed.add_field(name="Timed out by", value=f"<@{moderator.id}>", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        
        return embed

    def create_unban_embed(self, user: discord.User, moderator: Optional[discord.Member], guild: discord.Guild) -> discord.Embed:
        """Create embed for unban event"""
        embed = discord.Embed(
            title=f"{user.display_name} was unbanned",
            color=0x90ee90  # Light green for unbans
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        if moderator:
            embed.add_field(name="Unbanned by", value=f"<@{moderator.id}>", inline=True)
        
        
        
        return embed

    def calculate_duration(self, start_time: datetime) -> str:
        """Calculate duration between start time and now"""
        duration = datetime.now(timezone.utc) - start_time
        days = duration.days
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        
        if not parts:
            return "Less than 1 minute"
        
        return ", ".join(parts)

    async def send_log_message(self, guild_id: int, embed: discord.Embed, view: Optional[discord.ui.View] = None):
        """Send log message to configured webhook.

        `view` may only contain link buttons — plain channel webhooks cannot
        carry interactive components (discord.py enforces this via
        view.is_dispatchable())."""
        config = await self.get_guild_config(guild_id)
        webhook_url = config.get('member_log_webhook')

        if not webhook_url:
            status_reporter.record("moderation", member_log_status="disabled")
            return

        view_kwargs = {"view": view} if view is not None else {}
        try:
            # Create a new aiohttp session for webhook requests
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(webhook_url, session=session)
                # Read pb.png file and send it with the webhook
                with open('pb.png', 'rb') as f:
                    pb_file = discord.File(f, 'pb.png')
                    await webhook.send(embed=embed, file=pb_file, avatar_url="attachment://pb.png", **view_kwargs)
            status_reporter.record("moderation", member_log_status="ok", member_log_last_error=None)
        except (discord.HTTPException, aiohttp.ClientError, FileNotFoundError):
            # Fallback to sending without profile picture if pb.png is not found
            try:
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(webhook_url, session=session)
                    await webhook.send(embed=embed, **view_kwargs)
                status_reporter.record("moderation", member_log_status="ok", member_log_last_error=None)
            except (discord.HTTPException, aiohttp.ClientError) as e:
                self.log.warning(f"Member-log webhook failed for guild {guild_id}: {e}")
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

                        embed = self.create_kick_embed(entry.target, entry.user, entry.reason, guild)
                        await self.send_log_message(guild.id, embed)
                        return True
        except discord.Forbidden:
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

        # Send log message with role assignment status and a profile link button
        embed = self.create_join_embed(member, role_assigned=role_assigned, role_name=role_name, role_id=join_role_id)
        profile_view = discord.ui.View(timeout=None)
        profile_view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label="Profil",
            url=f"https://discord.com/users/{member.id}",
        ))
        await self.send_log_message(member.guild.id, embed, view=profile_view)

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
        duration = None
        if member.id in self.member_join_times:
            join_time = self.member_join_times[member.id]
            duration = self.calculate_duration(join_time)
            del self.member_join_times[member.id]

        # Send log message
        embed = self.create_leave_embed(member, duration)
        await self.send_log_message(member.guild.id, embed)

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

        embed = self.create_ban_embed(user, moderator, reason, guild)
        await self.send_log_message(guild.id, embed)

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

        embed = self.create_unban_embed(user, moderator, guild)
        await self.send_log_message(guild.id, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Handle member update events (for timeouts)"""
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
                
                # Calculate timeout duration
                duration_delta = after.timed_out_until - datetime.now(timezone.utc)
                duration = self.calculate_duration(datetime.now(timezone.utc) - duration_delta)

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

                embed = self.create_timeout_embed(after, duration, moderator, reason)
                await self.send_log_message(after.guild.id, embed)

    @app_commands.command(name="mod_dashboard", description="Manage current moderation configuration")
    @app_commands.default_permissions(administrator=True)
    async def mod_dashboard(self, interaction: discord.Interaction):
        """Display moderation dashboard with interactive buttons"""
        # Import here to avoid circular imports
        from core.mod_views import ModerationDashboardView
        
        embed = await self.create_dashboard_embed(interaction.guild.id)
        view = ModerationDashboardView(self)
        await view.update_buttons(interaction.guild.id)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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

            # Create success embed
            embed = discord.Embed(
                title="🧹 Messages Cleared",
                description=f"Successfully deleted {deleted_count} message{'s' if deleted_count != 1 else ''} from {channel.mention}",
                color=0x00ff00
            )
            embed.set_footer(text=f"Cleared by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to delete messages in this channel.", ephemeral=True)
        except discord.HTTPException as e:
            if e.code == 50034:  # You can only bulk delete messages that are under 14 days old
                await interaction.followup.send("❌ Cannot delete messages older than 14 days. Try with a smaller number.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred while deleting messages.", ephemeral=True)


    async def cog_load(self):
        self.honeypot_ban_loop.start()

    def cog_unload(self):
        self.honeypot_ban_loop.cancel()

    @tasks.loop(seconds=60)
    async def honeypot_ban_loop(self):
        status_reporter.record("moderation", honeypot_loop_alive=True)
        guild = self.bot.get_guild(self.honeypot_guild_id)
        if not guild:
            status_reporter.record("moderation", honeypot_status="error", honeypot_last_error="configured guild not found")
            return

        config = await self.get_guild_config(guild.id)
        honeypot_role_id = config.get('honeypot_role')
        if not honeypot_role_id:
            status_reporter.record("moderation", honeypot_status="disabled")
            return

        role = guild.get_role(honeypot_role_id)
        if not role:
            self.log.warning(f"Honeypot role {honeypot_role_id} not found in guild {guild.id}")
            status_reporter.record("moderation", honeypot_status="error", honeypot_last_error=f"role {honeypot_role_id} not found")
            return

        ban_error = None
        for member in list(role.members):
            try:
                await guild.ban(member, reason="Autobann", delete_message_days=0)
                status_reporter.bump_counter("moderation", "honeypot_bans")
            except (discord.Forbidden, discord.HTTPException) as e:
                ban_error = str(e)
                self.log.warning(f"Honeypot ban failed for {member} in guild {guild.id}: {e}")

        if ban_error:
            status_reporter.record("moderation", honeypot_status="error", honeypot_last_error=ban_error)
        else:
            status_reporter.record("moderation", honeypot_status="ok", honeypot_last_error=None)

    @honeypot_ban_loop.before_loop
    async def before_honeypot_ban_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
