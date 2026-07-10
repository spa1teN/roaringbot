import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional
import aiohttp

class ChannelSelectView(discord.ui.View):
    def __init__(self, moderation_cog):
        super().__init__(timeout=300)
        self.moderation_cog = moderation_cog

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel for member logging...",
        min_values=1,
        max_values=1,
        channel_types=[discord.ChannelType.text]
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        # Get the actual channel object from the guild using the ID
        selected_channel = select.values[0]
        channel = interaction.guild.get_channel(selected_channel.id)
        
        if not channel:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return

        # Check if bot has permission to create webhooks in the channel
        bot_permissions = channel.permissions_for(interaction.guild.me)
        if not bot_permissions.manage_webhooks:
            await interaction.response.send_message("❌ I don't have permission to create webhooks in that channel.", ephemeral=True)
            return

        try:
            # Create webhook
            webhook = await channel.create_webhook(
                name="Member Logger",
                avatar=await self.moderation_cog.bot.user.display_avatar.read(),
                reason="Moderation logging setup"
            )
            
            # Save webhook URL to config
            await self.moderation_cog.set_guild_config(interaction.guild.id, 'member_log_webhook', webhook.url)

            # Replace channel selection with updated dashboard
            dashboard_embed = await self.moderation_cog.create_dashboard_embed(interaction.guild.id)
            dashboard_view = ModerationDashboardView(self.moderation_cog)
            await dashboard_view.update_buttons(interaction.guild.id)
            
            await interaction.response.edit_message(
                content=None,
                embed=dashboard_embed, 
                view=dashboard_view
            )
            
            # Send success message as followup
            success_embed = discord.Embed(
                title="✅ Member Logging Enabled",
                description=f"Successfully configured member logging for {channel.mention}!",
                color=0x00ff00
            )
            await interaction.followup.send(embed=success_embed, ephemeral=True)

        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Failed to create webhook: {str(e)}", ephemeral=True)

    async def on_timeout(self):
        """Called when the view times out"""
        for item in self.children:
            item.disabled = True

class RoleSelectView(discord.ui.View):
    """Generic role-picker used both for the join-role and the honeypot-role
    setup flow — which config key it writes to and what it tells the user is
    parametrized rather than hardcoded, so both flows share one implementation."""

    def __init__(self, moderation_cog, config_key: str, placeholder: str, success_title: str, success_description: str):
        super().__init__(timeout=300)
        self.moderation_cog = moderation_cog
        self.config_key = config_key
        self.success_title = success_title
        self.success_description = success_description
        self.children[0].placeholder = placeholder

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select a role...",
        min_values=1,
        max_values=1
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]

        # Both use cases need the bot positioned above the role: join-role to
        # be able to assign it, honeypot-role to be able to ban members
        # holding it (Discord blocks moderation actions against higher roles).
        bot_member = interaction.guild.get_member(self.moderation_cog.bot.user.id)
        if role >= bot_member.top_role:
            await interaction.response.send_message(
                "❌ Diese Rolle ist höher als oder gleich meiner höchsten Rolle — das wird nicht funktionieren.",
                ephemeral=True,
            )
            return

        await self.moderation_cog.set_guild_config(interaction.guild.id, self.config_key, role.id)

        # Replace role selection with updated dashboard
        dashboard_embed = await self.moderation_cog.create_dashboard_embed(interaction.guild.id)
        dashboard_view = ModerationDashboardView(self.moderation_cog)
        await dashboard_view.update_buttons(interaction.guild.id)

        await interaction.response.edit_message(
            content=None,
            embed=dashboard_embed,
            view=dashboard_view
        )

        # Send success message as followup
        success_embed = discord.Embed(
            title=self.success_title,
            description=self.success_description.format(role=role.mention),
            color=0x00ff00
        )
        await interaction.followup.send(embed=success_embed, ephemeral=True)

    async def on_timeout(self):
        """Called when the view times out"""
        for item in self.children:
            item.disabled = True

class ModerationDashboardView(discord.ui.View):
    def __init__(self, moderation_cog):
        super().__init__(timeout=300)
        self.moderation_cog = moderation_cog

    @discord.ui.button(label="Setup Member Log", style=discord.ButtonStyle.green, emoji="📋")
    async def setup_member_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need `Manage Server` permission to use this feature.", ephemeral=True)
            return

        config = await self.moderation_cog.get_guild_config(interaction.guild.id)

        if config.get('member_log_webhook'):
            # Disable member logging - also delete the webhook
            await self._disable_member_logging(interaction, config)
        else:
            # Replace dashboard with channel selection
            view = ChannelSelectView(self.moderation_cog)
            await interaction.response.edit_message(
                content="Select a channel for member logging:",
                embed=None,
                view=view
            )

    @discord.ui.button(label="Setup Join Role", style=discord.ButtonStyle.blurple, emoji="👤")
    async def setup_join_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need `Manage Roles` permission to use this feature.", ephemeral=True)
            return

        config = await self.moderation_cog.get_guild_config(interaction.guild.id)

        if config.get('join_role'):
            # Disable join role
            await self._disable_join_role(interaction)
        else:
            # Replace dashboard with role selection
            view = RoleSelectView(
                self.moderation_cog,
                config_key='join_role',
                placeholder="Select a role to assign to new members...",
                success_title="✅ Auto Join Role Enabled",
                success_description="New members will automatically receive the {role} role when they join.",
            )
            await interaction.response.edit_message(
                content="Select a role to assign to new members:",
                embed=None,
                view=view
            )

    @discord.ui.button(label="Setup Honeypot", style=discord.ButtonStyle.gray, emoji="🍯")
    async def setup_honeypot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need `Manage Roles` permission to use this feature.", ephemeral=True)
            return

        config = await self.moderation_cog.get_guild_config(interaction.guild.id)

        if config.get('honeypot_role'):
            # Disable honeypot
            await self._disable_honeypot(interaction)
        else:
            # Replace dashboard with role selection
            view = RoleSelectView(
                self.moderation_cog,
                config_key='honeypot_role',
                placeholder="Select the honeypot role...",
                success_title="✅ Honeypot Enabled",
                success_description="Members holding the {role} role will be automatically banned.",
            )
            await interaction.response.edit_message(
                content="Select the honeypot role (anyone holding it gets auto-banned):",
                embed=None,
                view=view
            )

    async def _disable_member_logging(self, interaction: discord.Interaction, config: dict):
        """Helper method to disable member logging"""
        webhook_url = config.get('member_log_webhook')
        if webhook_url:
            try:
                # Create a new aiohttp session for webhook deletion
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(webhook_url, session=session)
                    await webhook.delete(reason="Member logging disabled")
            except (discord.HTTPException, aiohttp.ClientError):
                pass  # Webhook might already be deleted
        
        # Remove webhook from config
        await self.moderation_cog.clear_guild_config(interaction.guild.id, 'member_log_webhook')

        # Update the dashboard in place
        dashboard_embed = await self.moderation_cog.create_dashboard_embed(interaction.guild.id)
        view = ModerationDashboardView(self.moderation_cog)
        await view.update_buttons(interaction.guild.id)

        await interaction.response.edit_message(
            content=None,
            embed=dashboard_embed,
            view=view
        )

        # Send success message as followup
        success_embed = discord.Embed(
            title="✅ Member Logging Disabled",
            description="Member logging has been disabled and webhook deleted.",
            color=0xff0000
        )
        await interaction.followup.send(embed=success_embed, ephemeral=True)

    async def _disable_join_role(self, interaction: discord.Interaction):
        """Helper method to disable join role"""
        await self.moderation_cog.clear_guild_config(interaction.guild.id, 'join_role')

        # Update the dashboard in place
        dashboard_embed = await self.moderation_cog.create_dashboard_embed(interaction.guild.id)
        view = ModerationDashboardView(self.moderation_cog)
        await view.update_buttons(interaction.guild.id)

        await interaction.response.edit_message(
            content=None,
            embed=dashboard_embed,
            view=view
        )

        # Send success message as followup
        success_embed = discord.Embed(
            title="✅ Auto Join Role Disabled",
            description="Auto role assignment for new members has been disabled.",
            color=0xff0000
        )
        await interaction.followup.send(embed=success_embed, ephemeral=True)

    async def _disable_honeypot(self, interaction: discord.Interaction):
        """Helper method to disable the honeypot"""
        await self.moderation_cog.clear_guild_config(interaction.guild.id, 'honeypot_role')

        # Update the dashboard in place
        dashboard_embed = await self.moderation_cog.create_dashboard_embed(interaction.guild.id)
        view = ModerationDashboardView(self.moderation_cog)
        await view.update_buttons(interaction.guild.id)

        await interaction.response.edit_message(
            content=None,
            embed=dashboard_embed,
            view=view
        )

        # Send success message as followup
        success_embed = discord.Embed(
            title="✅ Honeypot Disabled",
            description="Auto-ban on the honeypot role has been disabled.",
            color=0xff0000
        )
        await interaction.followup.send(embed=success_embed, ephemeral=True)

    async def on_timeout(self):
        """Called when the view times out"""
        for item in self.children:
            item.disabled = True

    async def update_buttons(self, guild_id: int):
        """Update button states based on current configuration"""
        config = await self.moderation_cog.get_guild_config(guild_id)

        # Update member log button
        if config.get('member_log_webhook'):
            self.children[0].label = "Disable Member Log"
            self.children[0].style = discord.ButtonStyle.red
            self.children[0].emoji = "🗑️"
        else:
            self.children[0].label = "Setup Member Log"
            self.children[0].style = discord.ButtonStyle.green
            self.children[0].emoji = "📋"

        # Update join role button
        if config.get('join_role'):
            self.children[1].label = "Disable Join Role"
            self.children[1].style = discord.ButtonStyle.red
            self.children[1].emoji = "🗑️"
        else:
            self.children[1].label = "Setup Join Role"
            self.children[1].style = discord.ButtonStyle.blurple
            self.children[1].emoji = "👤"

        # Update honeypot button
        if config.get('honeypot_role'):
            self.children[2].label = "Disable Honeypot"
            self.children[2].style = discord.ButtonStyle.red
            self.children[2].emoji = "🗑️"
        else:
            self.children[2].label = "Setup Honeypot"
            self.children[2].style = discord.ButtonStyle.gray
            self.children[2].emoji = "🍯"