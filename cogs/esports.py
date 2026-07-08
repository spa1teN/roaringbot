"""E-Sports Match Monitoring Cog for Discord Bot"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone, time
from typing import Dict, List, Optional, Set, Tuple
from pathlib import Path
import pytz

import discord
from discord.ext import commands, tasks
from discord import app_commands

from core.config import config
from core.http_client import http_client
from core.status_reporter import status_reporter
import base64


class EsportsMatch:
    """Represents an e-sports match with Discord event integration"""
    
    def __init__(self, match_data: Dict):
        self.id = match_data["id"]
        
        # Safely extract tournament name
        tournament = match_data.get("tournament")
        if not tournament:
            raise ValueError(f"Match {match_data['id']}: tournament data is missing")
        self.tournament_name = tournament.get("name", "Unknown Tournament")
        
        # Safely extract team A name
        lineup_a = match_data.get("lineup_a")
        if not lineup_a or not lineup_a.get("team"):
            raise ValueError(f"Match {match_data['id']}: lineup_a or team_a data is missing")
        self.team_a = lineup_a["team"].get("name", "Unknown Team A")
        
        # Safely extract team B name (use TBA if not yet announced)
        lineup_b = match_data.get("lineup_b")
        if not lineup_b or not lineup_b.get("team"):
            self.team_b = "TBA"
        else:
            self.team_b = lineup_b["team"].get("name", "TBA")
        
        self.start_time = datetime.fromisoformat(match_data["first_map_at"].replace('Z', '+00:00'))
        self.end_time = datetime.fromisoformat(match_data["last_map_end"].replace('Z', '+00:00')) if match_data["last_map_end"] else None
        self.cancelled = bool(match_data["cancelled"])
        self.detail_url = match_data["html_detail_url"]
        self.bestof = match_data["bestof"]
        self.game = match_data["game"].lower()
        self.slug = match_data["slug"]
        self.block_voice_channel = match_data.get("block_voice_channel", "")
        self.matchmaps = match_data.get("matchmaps", [])  # Store matchmaps data
        self.hltv_match_id = match_data.get("hltv_match_id")
        self.discord_event_id: Optional[int] = None
        self.reminder_message_id: Optional[int] = None
        self.forum_thread_id: Optional[int] = None
    
    @property
    def event_name(self) -> str:
        """Generate Discord event name"""
        return f"{self.team_a} vs. {self.team_b}"
    
    @property
    def event_description(self) -> str:
        """Generate Discord event description"""
        game_name = {"cs": "Counter-Strike", "tm": "Trackmania", "lol": "League of Legends"}.get(self.game, self.game.upper())
        
        # Use custom emotes for games
        if self.game == "cs":
            game_emoji = "<:cs:1416235161594499092>"
        elif self.game == "lol":
            game_emoji = "<:lol:1416235138307854416>"
        elif self.game == "tm":
            game_emoji = "🏎️"
        else:
            game_emoji = "🎮"
        
        hltv_line = f"\n🔗  {self.hltv_url}" if self.game == "cs" and self.hltv_url else ""
        return (
            f"--------------------------\n\n"
            f"🏆  **{self.tournament_name}**\n\n"
            f"{game_emoji}  {game_name} - BO{self.bestof}\n\n"
            f"{self.detail_url}{hltv_line}\n\n"
        )
    
    def __eq__(self, other):
        return isinstance(other, EsportsMatch) and self.id == other.id

    def __hash__(self):
        return hash(self.id)

    @property
    def hltv_url(self) -> Optional[str]:
        if not self.hltv_match_id:
            return None
        slug = f"{self.team_a.lower().replace(' ', '-')}-vs-{self.team_b.lower().replace(' ', '-')}"
        return f"https://www.hltv.org/matches/{self.hltv_match_id}/{slug}"
    
    def get_reminder_embed(self) -> discord.Embed:
        """Create reminder embed for 30-minute notification"""
        # Convert to German timezone for display
        germany_tz = pytz.timezone("Europe/Berlin")
        match_time_berlin = self.start_time.astimezone(germany_tz)
        
        # Use custom emotes for games
        if self.game == "cs":
            game_emoji = "<:cs:1416235161594499092>"
        elif self.game == "lol":
            game_emoji = "<:lol:1416235138307854416>"
        elif self.game == "tm":
            game_emoji = "🏎️"
        else:
            game_emoji = "🎮"
        
        game_name = {"cs": "Counter-Strike", "tm": "Trackmania", "lol": "League of Legends"}.get(self.game, self.game.upper())
        
        unix_ts = int(self.start_time.timestamp())

        embed = discord.Embed(
            title="⏰ Match Starting Soon!",
            description=f"{game_emoji} **{self.team_a} vs {self.team_b}**",
            color=0xff6b35,
        )

        embed.add_field(
            name="🏆 Tournament",
            value=self.tournament_name,
            inline=True
        )

        embed.add_field(
            name="🎮 Game",
            value=f"{game_name} - Best of {self.bestof}",
            inline=True
        )

        embed.add_field(
            name="🕐 Start Time",
            value=f"<t:{unix_ts}:t> (<t:{unix_ts}:R>)",
            inline=True
        )
        
        # Link to Discord event if available
        if self.discord_event_id:
            # We'll need to get the guild context to create the URL
            embed.add_field(
                name="📅 Discord Event",
                value="Click on the Discord event for more details!",
                inline=False
            )
        
        links = f"[wannspieltbig.de]({self.detail_url})"
        if self.game == "cs" and self.hltv_url:
            links += f" • [HLTV]({self.hltv_url})"
        embed.add_field(
            name="🌐 Match Details",
            value=links,
            inline=False
        )

        return embed


class CSGameTracker:
    """Tracks CS game scores and manages updates to wannspieltbig API"""
    
    def __init__(self, match: EsportsMatch):
        self.match = match
        self.current_map = 1
        self.team_a_score = 0  # rounds won by team A on current map
        self.team_b_score = 0  # rounds won by team B on current map
        self.team_a_maps = 0   # maps won by team A
        self.team_b_maps = 0   # maps won by team B
        self.match_maps = []   # List of match map IDs from API
        self.message_id: Optional[int] = None
        self.is_finished = False
        self.overtime_target = 13  # Current target score (13, 16, 19, 22, etc.)
        
    @property
    def current_map_id(self) -> Optional[int]:
        """Get the current map ID for API updates"""
        if self.current_map <= len(self.match_maps):
            return self.match_maps[self.current_map - 1]
        return None
    
    def add_round_team_a(self):
        """Add a round win for team A"""
        if self.is_finished:
            return False
            
        self.team_a_score += 1
        self._update_overtime_target()
        return self._check_map_winner()
    
    def add_round_team_b(self):
        """Add a round win for team B"""
        if self.is_finished:
            return False
            
        self.team_b_score += 1
        self._update_overtime_target()
        return self._check_map_winner()
    
    
    def _update_overtime_target(self):
        """Update the overtime target based on current scores"""
        # Check if we've hit an overtime tie
        if self.team_a_score == self.team_b_score:
            if self.team_a_score == 12:
                self.overtime_target = 16  # 12-12 -> first to 16
            elif self.team_a_score == 15:
                self.overtime_target = 19  # 15-15 -> first to 19
            elif self.team_a_score == 18:
                self.overtime_target = 22  # 18-18 -> first to 22
            elif self.team_a_score >= 21 and self.team_a_score % 3 == 0:
                self.overtime_target = self.team_a_score + 3  # 21-21, 24-24, etc.
    
    def _finalize_map_completion(self):
        """Finalize the completion of current map (called after confirmation)"""
        maps_to_win = (self.match.bestof + 1) // 2
        if self.team_a_maps >= maps_to_win or self.team_b_maps >= maps_to_win:
            self.is_finished = True
        else:
            # Move to next map
            self.current_map += 1
            self.team_a_score = 0
            self.team_b_score = 0
            self.overtime_target = 13  # Reset to regular time for new map
    
    def _revert_map_completion(self):
        """Revert the map completion (undo the map win)"""
        required_score = self._get_required_score_to_win()
        
        if self.team_a_score >= required_score:
            self.team_a_maps -= 1
        elif self.team_b_score >= required_score:
            self.team_b_maps -= 1
    
    def _get_required_score_to_win(self) -> int:
        """Get the current required score to win"""
        return self.overtime_target
    
    def _check_map_winner(self) -> bool:
        """Check if current map is finished (returns True if map should be finished)"""
        required_score = self._get_required_score_to_win()
        
        # Check if either team has reached the required score
        if self.team_a_score >= required_score:
            return True
        elif self.team_b_score >= required_score:
            return True
        
        return False
    
    def get_winning_team(self) -> Optional[str]:
        """Get the team that would win the current map"""
        required_score = self._get_required_score_to_win()
        
        if self.team_a_score >= required_score:
            return self.match.team_a
        elif self.team_b_score >= required_score:
            return self.match.team_b
        return None
    
    def get_event_score_name(self) -> str:
        """Generate Discord event name with live score: 'BIG vs MIBR - 3:4 (1:0)'"""
        return f"{self.match.team_a} vs {self.match.team_b} - {self.team_a_score}:{self.team_b_score} ({self.team_a_maps}:{self.team_b_maps})"

    def get_embed(self) -> discord.Embed:
        """Create embed showing current game status"""
        if self.is_finished:
            winner = self.match.team_a if self.team_a_maps > self.team_b_maps else self.match.team_b
            title = f"🏆 {winner} wins!"
            color = 0x00ff00
        else:
            title = f"🔴 LIVE: {self.match.team_a} vs {self.match.team_b}"
            color = 0xff0000
        
        embed = discord.Embed(
            title=title,
            description=f"**{self.match.tournament_name}**\n<:cs:1416235161594499092> Counter-Strike • Best of {self.match.bestof}",
            color=color,
            timestamp=datetime.utcnow()
        )
        
        # Overall map score
        embed.add_field(
            name="Maps",
            value=f"**{self.team_a_maps} - {self.team_b_maps}**",
            inline=True
        )
        
        if not self.is_finished:
            # Current map score with overtime indication
            required_score = self._get_required_score_to_win()
            
            map_status = f"Map {self.current_map}"
            score_text = f"**{self.team_a_score} - {self.team_b_score}**"
            
            # Show overtime status when target is above 13
            if required_score > 13:
                if required_score == 16:
                    map_status += " (OT1)"
                elif required_score == 19:
                    map_status += " (OT2)"  
                elif required_score == 22:
                    map_status += " (OT3)"
                elif required_score > 22:
                    ot_num = (required_score - 13) // 3
                    map_status += f" (OT{ot_num})"
                
                score_text += f"\nFirst to {required_score}"
            
            embed.add_field(
                name=map_status,
                value=score_text,
                inline=True
            )
        
        embed.add_field(
            name="Teams",
            value=f"**{self.match.team_a}** vs **{self.match.team_b}**",
            inline=False
        )

        links = f"[wannspieltbig.de]({self.match.detail_url})"
        if self.match.hltv_url:
            links += f" • [HLTV]({self.match.hltv_url})"
        embed.add_field(name="🌐 Links", value=links, inline=False)

        if not self.is_finished:
            embed.set_footer(text="Click buttons below to update scores")
        else:
            embed.set_footer(text="Match finished")

        return embed
    
    def get_reminder_embed(self) -> discord.Embed:
        """Create reminder embed for 30-minute notification"""
        # Convert to German timezone for display
        germany_tz = pytz.timezone("Europe/Berlin")
        match_time_berlin = self.start_time.astimezone(germany_tz)
        
        # Use custom emotes for games
        if self.game == "cs":
            game_emoji = "<:cs:1416235161594499092>"
        elif self.game == "lol":
            game_emoji = "<:lol:1416235138307854416>"
        elif self.game == "tm":
            game_emoji = "🏎️"
        else:
            game_emoji = "🎮"
        
        game_name = {"cs": "Counter-Strike", "tm": "Trackmania", "lol": "League of Legends"}.get(self.game, self.game.upper())
        
        unix_ts = int(self.start_time.timestamp())

        embed = discord.Embed(
            title="⏰ Match Starting Soon!",
            description=f"{game_emoji} **{self.team_a} vs {self.team_b}**",
            color=0xff6b35,
        )

        embed.add_field(
            name="🏆 Tournament",
            value=self.tournament_name,
            inline=True
        )

        embed.add_field(
            name="🎮 Game",
            value=f"{game_name} - Best of {self.bestof}",
            inline=True
        )

        embed.add_field(
            name="🕐 Start Time",
            value=f"<t:{unix_ts}:t> (<t:{unix_ts}:R>)",
            inline=True
        )
        
        # Link to Discord event if available
        if self.discord_event_id:
            # We'll need to get the guild context to create the URL
            embed.add_field(
                name="📅 Discord Event",
                value="Click on the Discord event for more details!",
                inline=False
            )
        
        embed.add_field(
            name="🌐 Match Details",
            value=f"[View on wannspieltbig.de]({self.detail_url})",
            inline=False
        )
                
        return embed


class MatchSelectionView(discord.ui.View):
    """View for selecting upcoming CS matches to track"""
    
    def __init__(self, cs_matches: List[EsportsMatch], esports_cog):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.cs_matches = cs_matches
        self.esports_cog = esports_cog
        
        # Add buttons for each CS match (max 25 per view)
        for i, match in enumerate(cs_matches[:25]):
            # Create button label with team names and start time
            match_time = match.start_time.strftime("%H:%M")
            button_label = f"{match.team_a} vs {match.team_b} ({match_time})"
            
            # Truncate label if too long
            if len(button_label) > 80:
                button_label = button_label[:77] + "..."
            
            button = discord.ui.Button(
                label=button_label,
                style=discord.ButtonStyle.primary,
                custom_id=f"select_match_{match.id}",
                row=i // 5  # 5 buttons per row
            )
            button.callback = self.create_match_callback(match)
            self.add_item(button)
    
    def create_match_callback(self, match: EsportsMatch):
        """Create callback function for a specific match button"""
        async def match_callback(interaction: discord.Interaction):
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Only administrators can start CS tracking.", ephemeral=True)
                return
            
            # Check if tracking is already active
            if match.id in self.esports_cog.active_cs_games:
                await interaction.response.send_message(f"❌ CS tracking already active for this match.", ephemeral=True)
                return
            
            await interaction.response.defer()
            
            try:
                await self.esports_cog._start_cs_game_tracking(match)
                await interaction.followup.send(
                    f"✅ Started CS game tracking for {match.team_a} vs {match.team_b}",
                    ephemeral=True
                )
                # Edit original message to show selection was made
                embed = discord.Embed(
                    title="✅ CS Game Tracking Started",
                    description=f"Now tracking: **{match.team_a} vs {match.team_b}**",
                    color=0x00ff00
                )
                await interaction.edit_original_response(embed=embed, view=None)
            except Exception as e:
                await interaction.followup.send(f"❌ Error starting CS tracking: {e}", ephemeral=True)
        
        return match_callback


class MapConfirmationView(discord.ui.View):
    """View for confirming map completion"""
    
    def __init__(self, tracker: CSGameTracker, esports_cog, winning_team: str):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.tracker = tracker
        self.esports_cog = esports_cog
        self.winning_team = winning_team
        
        # Create confirmation buttons
        self.confirm_button = discord.ui.Button(
            label=f"Confirm: {winning_team} wins Map {tracker.current_map}",
            style=discord.ButtonStyle.green,
            custom_id=f"confirm_map_{tracker.match.id}"
        )
        self.cancel_button = discord.ui.Button(
            label="Cancel (Continue Playing)",
            style=discord.ButtonStyle.red,
            custom_id=f"cancel_map_{tracker.match.id}"
        )
        
        self.confirm_button.callback = self.confirm_callback
        self.cancel_button.callback = self.cancel_callback
        
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)
    
    async def confirm_callback(self, interaction: discord.Interaction):
        """Confirm the map is finished"""
        if not interaction.user.guild_permissions.administrator:
            self.esports_cog.log.warning(f"Button rejected: {interaction.user} (no admin) tried to confirm map {self.tracker.current_map} in match {self.tracker.match.id}")
            await interaction.response.send_message("❌ Only administrators can confirm map results.", ephemeral=True)
            return

        self.esports_cog.log.info(f"Button: {interaction.user} confirmed map {self.tracker.current_map} win for {self.winning_team} (match {self.tracker.match.id})")
        try:
            self.tracker._finalize_map_completion()
            await self.esports_cog._update_score_api(self.tracker)
            await self.esports_cog._update_event_name_with_score(self.tracker)

            embed = self.tracker.get_embed()
            view = None if self.tracker.is_finished else ScoreUpdateView(self.tracker, self.esports_cog)
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception as e:
            self.esports_cog.log.error(f"Button error (confirm_map, match {self.tracker.match.id}): {e}", exc_info=True)
            await interaction.response.send_message(f"❌ Error confirming map: {e}", ephemeral=True)

    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel map confirmation and continue playing"""
        if not interaction.user.guild_permissions.administrator:
            self.esports_cog.log.warning(f"Button rejected: {interaction.user} (no admin) tried to cancel map confirmation in match {self.tracker.match.id}")
            await interaction.response.send_message("❌ Only administrators can modify map results.", ephemeral=True)
            return

        self.esports_cog.log.info(f"Button: {interaction.user} cancelled map {self.tracker.current_map} confirmation (match {self.tracker.match.id}) - continuing play")
        self.tracker._revert_map_completion()

        embed = self.tracker.get_embed()
        view = ScoreUpdateView(self.tracker, self.esports_cog)
        await interaction.response.edit_message(embed=embed, view=view)


class ManualScoreModal(discord.ui.Modal):
    """Modal for manually inputting scores"""
    
    def __init__(self, tracker: CSGameTracker, esports_cog):
        super().__init__(title=f"Set Score - Map {tracker.current_map}")
        self.tracker = tracker
        self.esports_cog = esports_cog
        
        self.team_a_score = discord.ui.TextInput(
            label=f"{tracker.match.team_a} Rounds",
            placeholder=f"Current: {tracker.team_a_score}",
            default=str(tracker.team_a_score),
            min_length=1,
            max_length=2
        )
        self.team_b_score = discord.ui.TextInput(
            label=f"{tracker.match.team_b} Rounds",
            placeholder=f"Current: {tracker.team_b_score}",
            default=str(tracker.team_b_score),
            min_length=1,
            max_length=2
        )
        
        self.add_item(self.team_a_score)
        self.add_item(self.team_b_score)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            team_a_rounds = int(self.team_a_score.value)
            team_b_rounds = int(self.team_b_score.value)

            if team_a_rounds < 0 or team_b_rounds < 0:
                await interaction.response.send_message("❌ Round scores cannot be negative.", ephemeral=True)
                return

            if team_a_rounds > 30 or team_b_rounds > 30:
                await interaction.response.send_message("❌ Round scores cannot exceed 30.", ephemeral=True)
                return

            # Set the scores
            old_team_a_score = self.tracker.team_a_score
            old_team_b_score = self.tracker.team_b_score

            self.esports_cog.log.info(f"Modal: {interaction.user} set manual score for match {self.tracker.match.id} map {self.tracker.current_map}: {old_team_a_score}-{old_team_b_score} → {team_a_rounds}-{team_b_rounds}")

            self.tracker.team_a_score = team_a_rounds
            self.tracker.team_b_score = team_b_rounds
            
            # Update overtime target based on new scores
            self.tracker._update_overtime_target()
            
            # Update API and event name
            await self.esports_cog._update_score_api(self.tracker)
            await self.esports_cog._update_event_name_with_score(self.tracker)

            # Check if map is finished with new scores
            map_finished = self.tracker._check_map_winner()
            
            if map_finished:
                # Show confirmation view for map completion
                winning_team = self.tracker.get_winning_team()
                # Temporarily award the map to check who would win
                if winning_team == self.tracker.match.team_a:
                    self.tracker.team_a_maps += 1
                elif winning_team == self.tracker.match.team_b:
                    self.tracker.team_b_maps += 1
                
                embed = self.tracker.get_embed()
                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(embed=embed, view=view)
                
                # Send confirmation as followup
                await interaction.followup.send(
                    f"✅ Score updated: {old_team_a_score}-{old_team_b_score} → {team_a_rounds}-{team_b_rounds}\n🏆 {winning_team} reached winning score!",
                    ephemeral=True
                )
            else:
                # Update the message with normal view
                embed = self.tracker.get_embed()
                view = ScoreUpdateView(self.tracker, self.esports_cog)
                await interaction.response.edit_message(embed=embed, view=view)
                
                # Send confirmation as followup
                await interaction.followup.send(
                    f"✅ Score updated: {old_team_a_score}-{old_team_b_score} → {team_a_rounds}-{team_b_rounds}",
                    ephemeral=True
                )
            
        except ValueError:
            await interaction.response.send_message("❌ Please enter valid numbers for round scores.", ephemeral=True)
        except Exception as e:
            self.esports_cog.log.error(f"Modal error (match {self.tracker.match.id}): {e}", exc_info=True)
            await interaction.response.send_message(f"❌ Error updating score: {e}", ephemeral=True)


class ScoreUpdateView(discord.ui.View):
    """View with buttons for updating CS game scores"""
    
    def __init__(self, tracker: CSGameTracker, esports_cog):
        super().__init__(timeout=21600)  # 6 hours timeout
        self.tracker = tracker
        self.esports_cog = esports_cog
        
        # Create buttons with team names
        self.team_a_button = discord.ui.Button(
            label=f"{tracker.match.team_a} won round",
            style=discord.ButtonStyle.primary,
            custom_id=f"team_a_{tracker.match.id}"
        )
        self.team_b_button = discord.ui.Button(
            label=f"{tracker.match.team_b} won round",
            style=discord.ButtonStyle.primary,
            custom_id=f"team_b_{tracker.match.id}"
        )
        self.manual_score_button = discord.ui.Button(
            label="Set Score Manually",
            style=discord.ButtonStyle.secondary,
            custom_id=f"manual_{tracker.match.id}",
            emoji="📝"
        )
        
        self.team_a_button.callback = self.team_a_callback
        self.team_b_button.callback = self.team_b_callback
        self.manual_score_button.callback = self.manual_score_callback
        
        self.add_item(self.team_a_button)
        self.add_item(self.team_b_button)
        self.add_item(self.manual_score_button)
        
        # Disable buttons if match is finished
        if tracker.is_finished:
            self.team_a_button.disabled = True
            self.team_b_button.disabled = True
            self.manual_score_button.disabled = True
    
    async def team_a_callback(self, interaction: discord.Interaction):
        """Handle team A round win"""
        if not interaction.user.guild_permissions.administrator:
            self.esports_cog.log.warning(f"Button rejected: {interaction.user} (no admin) tried to add round for {self.tracker.match.team_a} in match {self.tracker.match.id}")
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return

        self.esports_cog.log.info(f"Button: {interaction.user} added round for {self.tracker.match.team_a} (match {self.tracker.match.id}, map {self.tracker.current_map}, score {self.tracker.team_a_score}-{self.tracker.team_b_score})")
        try:
            map_finished = self.tracker.add_round_team_a()
            await self.esports_cog._update_score_api(self.tracker)
            await self.esports_cog._update_event_name_with_score(self.tracker)

            if map_finished:
                winning_team = self.tracker.get_winning_team()
                if winning_team == self.tracker.match.team_a:
                    self.tracker.team_a_maps += 1
                embed = self.tracker.get_embed()
                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                embed = self.tracker.get_embed()
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            self.esports_cog.log.error(f"Button error (team_a, match {self.tracker.match.id}): {e}", exc_info=True)
            await interaction.response.send_message(f"❌ Error updating score: {e}", ephemeral=True)

    async def team_b_callback(self, interaction: discord.Interaction):
        """Handle team B round win"""
        if not interaction.user.guild_permissions.administrator:
            self.esports_cog.log.warning(f"Button rejected: {interaction.user} (no admin) tried to add round for {self.tracker.match.team_b} in match {self.tracker.match.id}")
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return

        self.esports_cog.log.info(f"Button: {interaction.user} added round for {self.tracker.match.team_b} (match {self.tracker.match.id}, map {self.tracker.current_map}, score {self.tracker.team_a_score}-{self.tracker.team_b_score})")
        try:
            map_finished = self.tracker.add_round_team_b()
            await self.esports_cog._update_score_api(self.tracker)
            await self.esports_cog._update_event_name_with_score(self.tracker)

            if map_finished:
                winning_team = self.tracker.get_winning_team()
                if winning_team == self.tracker.match.team_b:
                    self.tracker.team_b_maps += 1
                embed = self.tracker.get_embed()
                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                embed = self.tracker.get_embed()
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            self.esports_cog.log.error(f"Button error (team_b, match {self.tracker.match.id}): {e}", exc_info=True)
            await interaction.response.send_message(f"❌ Error updating score: {e}", ephemeral=True)

    async def manual_score_callback(self, interaction: discord.Interaction):
        """Handle manual score input"""
        if not interaction.user.guild_permissions.administrator:
            self.esports_cog.log.warning(f"Button rejected: {interaction.user} (no admin) tried manual score for match {self.tracker.match.id}")
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return

        self.esports_cog.log.info(f"Button: {interaction.user} opened manual score modal (match {self.tracker.match.id}, map {self.tracker.current_map})")
        modal = ManualScoreModal(self.tracker, self.esports_cog)
        await interaction.response.send_modal(modal)


class EsportsCog(commands.Cog):
    """Cog for monitoring e-sports matches and creating Discord events"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log = logging.getLogger("roaringbot.esports")
        
        # Storage for matches and events
        self.matches: Dict[int, EsportsMatch] = {}
        self.known_match_ids: Set[int] = set()  # Persisted across restarts to prevent duplicate events
        self.event_to_match: Dict[int, int] = {}  # Discord event ID -> match ID
        self.reminder_to_match: Dict[int, int] = {}  # Reminder message ID -> match ID
        self.thread_to_match: Dict[int, int] = {}  # Forum thread ID -> match ID
        self.event_start_failures: Dict[int, int] = {}  # event ID -> consecutive failure count
        self.event_not_found_count: Dict[int, int] = {}  # event ID -> consecutive NotFound count
        self.summary_message_id: Optional[int] = None  # Latest summary message ID
        self.storage_file = Path("config/esports_data.json")
        
        # CS game tracking
        self.active_cs_games: Dict[int, CSGameTracker] = {}  # match ID -> tracker
        self.monitored_matches: Set[int] = set()  # Matches currently being monitored for start time
        self._pending_tracker_restore: Dict[int, dict] = {}  # Loaded from JSON, applied after first API poll
        
        # German timezone for weekly summary scheduling
        self.germany_tz = pytz.timezone("Europe/Berlin")
        
        # Load persisted data
        self._load_data()
        
        # Start polling if enabled
        if config.esports_enabled:
            self.log.info("E-Sports monitoring enabled")
        else:
            self.log.info("E-Sports monitoring disabled")
    
    def _load_data(self):
        """Load persisted match and event data"""
        try:
            if self.storage_file.exists():
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Reconstruct event mappings
                self.event_to_match = {int(k): v for k, v in data.get("event_to_match", {}).items()}

                # Reconstruct reminder mappings
                self.reminder_to_match = {int(k): v for k, v in data.get("reminder_to_match", {}).items()}
                self.thread_to_match = {int(k): v for k, v in data.get("thread_to_match", {}).items()}

                # Load summary message ID
                self.summary_message_id = data.get("summary_message_id")

                # Load monitored matches
                self.monitored_matches = set(data.get("monitored_matches", []))

                # Load known match IDs to prevent duplicate event creation on restart
                self.known_match_ids = set(data.get("known_match_ids", []))

                # Load pending tracker restorations (applied after first API poll)
                self._pending_tracker_restore = {
                    int(k): v for k, v in data.get("active_cs_trackers", {}).items()
                }

                self.log.info(f"Loaded {len(self.event_to_match)} event mappings and {len(self.monitored_matches)} monitored matches")
                if self._pending_tracker_restore:
                    self.log.info(f"Pending CS tracker restore for {len(self._pending_tracker_restore)} match(es)")
        except Exception as e:
            self.log.error(f"Error loading esports data: {e}")
    
    def _save_data(self):
        """Save current match and event data"""
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)

            # Serialize active CS trackers so they survive restarts
            active_cs_trackers = {}
            for match_id, tracker in self.active_cs_games.items():
                if not tracker.is_finished and tracker.message_id:
                    active_cs_trackers[str(match_id)] = {
                        "message_id": tracker.message_id,
                        "current_map": tracker.current_map,
                        "team_a_score": tracker.team_a_score,
                        "team_b_score": tracker.team_b_score,
                        "team_a_maps": tracker.team_a_maps,
                        "team_b_maps": tracker.team_b_maps,
                        "overtime_target": tracker.overtime_target,
                        "match_maps": tracker.match_maps,
                    }

            data = {
                "event_to_match": self.event_to_match,
                "reminder_to_match": self.reminder_to_match,
                "thread_to_match": self.thread_to_match,
                "summary_message_id": self.summary_message_id,
                "monitored_matches": list(self.monitored_matches),
                "known_match_ids": list(self.matches.keys()),
                "active_cs_trackers": active_cs_trackers,
                "last_update": datetime.now().isoformat()
            }
            
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            self.log.error(f"Error saving esports data: {e}")

    async def _restore_cs_trackers(self):
        """Restore active CS trackers from persisted JSON state after first API poll."""
        restored = 0
        for match_id, state in list(self._pending_tracker_restore.items()):
            match = self.matches.get(match_id)
            if not match:
                self.log.info(f"Skipping tracker restore for match {match_id} — no longer in API")
                continue
            if match_id in self.active_cs_games:
                continue  # Already tracking

            tracker = CSGameTracker(match)
            tracker.message_id = state.get("message_id")
            tracker.current_map = state.get("current_map", 1)
            tracker.team_a_score = state.get("team_a_score", 0)
            tracker.team_b_score = state.get("team_b_score", 0)
            tracker.team_a_maps = state.get("team_a_maps", 0)
            tracker.team_b_maps = state.get("team_b_maps", 0)
            tracker.overtime_target = state.get("overtime_target", 13)
            tracker.match_maps = state.get("match_maps", [])

            self.active_cs_games[match_id] = tracker
            self.monitored_matches.add(match_id)
            restored += 1
            self.log.info(f"Restored CS tracker for match {match_id} ({match.event_name}): "
                          f"Map {tracker.current_map}, score {tracker.team_a_score}-{tracker.team_b_score}, "
                          f"maps {tracker.team_a_maps}-{tracker.team_b_maps}")

        self._pending_tracker_restore.clear()
        if restored:
            self.log.info(f"Restored {restored} CS tracker(s) from persisted state")

    async def cog_load(self):
        """Called when the cog is loaded"""
        if config.esports_enabled:
            self.match_monitor.start()
            self.live_score_updater.start()
            self.log.info("Started e-sports monitoring tasks")
    
    def cog_unload(self):
        """Called when the cog is unloaded"""
        self.match_monitor.cancel()
        self.live_score_updater.cancel()
        self._save_data()
        self.log.info("Stopped e-sports monitoring tasks")

    @commands.Cog.listener()
    async def on_ready(self):
        """Reconcile event_to_match against actual Discord guild events on startup."""
        guild_id = config.esports_guild_id
        guild = self.bot.get_guild(guild_id) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        if not guild or not self.event_to_match:
            return

        try:
            live_events = {e.id for e in await guild.fetch_scheduled_events()}
        except Exception as e:
            self.log.warning(f"Could not fetch guild events for startup reconciliation: {e}")
            return

        stale = [eid for eid in self.event_to_match if eid not in live_events]
        if stale:
            for eid in stale:
                mid = self.event_to_match.pop(eid)
                self.log.info(f"Removed stale event_to_match entry on startup: event {eid} -> match {mid}")
            self._save_data()

    @commands.Cog.listener()
    async def on_resumed(self):
        """Refresh all active CS score messages after a Gateway RESUME.
        During a disconnect, Discord may drop button interactions and show
        'This interaction failed' to users. Re-sending the message with a
        fresh View ensures buttons work again after reconnect.
        """
        if not self.active_cs_games:
            return

        self.log.info(f"Gateway RESUME detected — refreshing {len(self.active_cs_games)} active CS score message(s)")
        channel = self.bot.get_channel(config.esports_update_channel_id)
        if not channel:
            return

        for match_id, tracker in list(self.active_cs_games.items()):
            if not tracker.message_id or tracker.is_finished:
                continue
            try:
                message = await channel.fetch_message(tracker.message_id)
                embed = tracker.get_embed()
                view = ScoreUpdateView(tracker, self)
                await message.edit(embed=embed, view=view)
                self.log.info(f"Refreshed score message for match {match_id} after RESUME")
            except discord.NotFound:
                self.log.warning(f"Score message {tracker.message_id} not found during RESUME refresh")
                del self.active_cs_games[match_id]
            except Exception as e:
                self.log.error(f"Failed to refresh score message for match {match_id} after RESUME: {e}")

    @tasks.loop(minutes=15)  # Default, will be overridden by config
    async def match_monitor(self):
        """Periodically poll the API for match updates"""
        try:
            self.log.debug("Polling e-sports API for updates")
            status_reporter.record("esports", last_poll_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

            # Fetch all pages from the API
            matches_data = []
            next_url = config.esports_api_url
            while next_url:
                response = await http_client.get(next_url)
                try:
                    if response.status != 200:
                        self.log.error(f"API request failed with status {response.status}")
                        status_reporter.bump_counter("esports", "api_errors")
                        status_reporter.record("esports", last_poll_success=False, last_poll_error=f"HTTP {response.status}")
                        return

                    try:
                        data = await response.json()
                    except Exception as e:
                        self.log.error(f"Failed to parse JSON response: {e}")
                        status_reporter.bump_counter("esports", "api_errors")
                        status_reporter.record("esports", last_poll_success=False, last_poll_error=str(e))
                        return

                    if data is None:
                        self.log.error("API returned None response")
                        status_reporter.bump_counter("esports", "api_errors")
                        status_reporter.record("esports", last_poll_success=False, last_poll_error="API returned None")
                        return

                    if not isinstance(data, dict):
                        self.log.error(f"API returned unexpected data type: {type(data)}")
                        status_reporter.bump_counter("esports", "api_errors")
                        status_reporter.record("esports", last_poll_success=False, last_poll_error="unexpected data type")
                        return

                    page_results = data.get("results", [])
                    if page_results is None:
                        self.log.warning("API results field is None, stopping pagination")
                        break
                    elif not isinstance(page_results, list):
                        self.log.error(f"API results field is not a list: {type(page_results)}")
                        status_reporter.bump_counter("esports", "api_errors")
                        status_reporter.record("esports", last_poll_success=False, last_poll_error="results not a list")
                        return

                    matches_data.extend(page_results)
                    next_url = data.get("next")  # None when no more pages
                finally:
                    await response.release()
            
            # Process matches
            current_matches = {}
            for match_data in matches_data:
                # Skip None entries
                if match_data is None:
                    self.log.warning("Skipping None match entry in API response")
                    continue
                
                # Try to create EsportsMatch with error handling
                try:
                    match = EsportsMatch(match_data)
                except Exception as e:
                    self.log.error(f"Error creating EsportsMatch from data: {e}")
                    self.log.debug(f"Problematic match data: {match_data}")
                    continue

                # Restore event ID from stored mappings
                for event_id, stored_match_id in self.event_to_match.items():
                    if stored_match_id == match.id:
                        match.discord_event_id = event_id
                        break
                
                # Restore reminder message ID from stored mappings
                for reminder_id, stored_match_id in self.reminder_to_match.items():
                    if stored_match_id == match.id:
                        match.reminder_message_id = reminder_id
                        break

                # Restore forum thread ID from stored mappings
                for thread_id, stored_match_id in self.thread_to_match.items():
                    if stored_match_id == match.id:
                        match.forum_thread_id = thread_id
                        break
                
                current_matches[match.id] = match
            
            # Handle new, updated, and cancelled matches
            await self._process_match_updates(current_matches)
            
            # Check for CS matches starting soon
            await self._check_for_starting_matches()
            
            # Check for Discord events needing status updates
            await self._check_event_status_updates()
            
            # Check for matches needing 30-minute reminders
            await self._check_for_match_reminders()
            
            # Check for reminder messages that should be cleaned up
            await self._check_for_reminder_cleanup()
            
            self.log.debug(f"Processed {len(current_matches)} matches from API")

            now = datetime.now(timezone.utc)
            week_end = now + timedelta(days=7)
            upcoming = sorted(
                (m for m in self.matches.values() if not m.cancelled and now <= m.start_time <= week_end),
                key=lambda m: m.start_time,
            )

            status_reporter.record(
                "esports",
                monitoring_enabled=config.esports_enabled,
                poll_interval_minutes=config.esports_poll_interval_minutes,
                last_poll_success=True,
                last_poll_error=None,
                total_matches=len(self.matches),
                active_matches=len([m for m in self.matches.values() if not m.cancelled]),
                active_discord_events=len(self.event_to_match),
                active_cs_trackers=len(self.active_cs_games),
                summary_message_id=self.summary_message_id,
                upcoming_matches=[
                    {
                        "teams": f"{m.team_a} vs. {m.team_b}",
                        "tournament": m.tournament_name,
                        "game": m.game,
                        "start_time": m.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    for m in upcoming
                ],
            )

        except Exception as e:
            self.log.error(f"Error in match monitoring: {e}")
            status_reporter.bump_counter("esports", "api_errors")
            status_reporter.record("esports", last_poll_success=False, last_poll_error=str(e))
    
    @match_monitor.before_loop
    async def before_match_monitor(self):
        """Wait for bot to be ready and set correct interval"""
        await self.bot.wait_until_ready()
        
        # Update loop interval from config
        self.match_monitor.change_interval(minutes=config.esports_poll_interval_minutes)
        self.log.info(f"Set match monitoring interval to {config.esports_poll_interval_minutes} minutes")
    
    @tasks.loop(seconds=30)
    async def live_score_updater(self):
        """Poll wannspieltbig API every 30s during active matches to sync scores"""
        try:
            if not self.active_cs_games:
                return  # No active games to update

            self.log.debug(f"Polling livescore API for {len(self.active_cs_games)} active games")
            status_reporter.record("esports", last_livescore_poll_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

            # Fetch current match data from livescore API
            response = await http_client.get("https://wannspieltbig.de/api/match_livescore/")
            try:
                if response.status != 200:
                    self.log.warning(f"Livescore API request failed with status {response.status}")
                    status_reporter.bump_counter("esports", "api_errors")
                    return

                data = await response.json()
            finally:
                await response.release()

            if not data or not isinstance(data, dict):
                return

            matches_data = data.get("results", [])
            if not matches_data:
                return

            # Update scores for active games
            for match_id, tracker in list(self.active_cs_games.items()):
                # Find this match in API data
                api_match = None
                for m in matches_data:
                    if m and m.get("id") == match_id:
                        api_match = m
                        break

                if not api_match:
                    continue

                # Check if match has ended according to API
                has_ended = api_match.get("has_ended", False)

                # Get matchmaps from API
                matchmaps = api_match.get("matchmaps", [])
                if not matchmaps:
                    continue

                # Calculate map scores from API data
                team_a_maps = 0
                team_b_maps = 0
                current_map_idx = 0

                for i, mm in enumerate(matchmaps):
                    rounds_a = mm.get("rounds_won_team_a", 0) or 0
                    rounds_b = mm.get("rounds_won_team_b", 0) or 0

                    # Check if this map is finished (someone reached winning score)
                    # Standard: 13 rounds, OT: 16, 19, 22, etc.
                    map_finished = False
                    if rounds_a >= 13 or rounds_b >= 13:
                        # Check for overtime scenarios
                        if rounds_a >= 13 and rounds_b < rounds_a - 1:
                            map_finished = True
                            if rounds_a > rounds_b:
                                team_a_maps += 1
                            else:
                                team_b_maps += 1
                        elif rounds_b >= 13 and rounds_a < rounds_b - 1:
                            map_finished = True
                            if rounds_b > rounds_a:
                                team_b_maps += 1
                            else:
                                team_a_maps += 1
                        elif rounds_a >= 13 and rounds_a > rounds_b and (rounds_a - rounds_b) >= 2:
                            map_finished = True
                            team_a_maps += 1
                        elif rounds_b >= 13 and rounds_b > rounds_a and (rounds_b - rounds_a) >= 2:
                            map_finished = True
                            team_b_maps += 1

                    if not map_finished:
                        current_map_idx = i
                        break
                    else:
                        current_map_idx = i + 1

                scores_changed = False

                # Update map scores
                if team_a_maps != tracker.team_a_maps or team_b_maps != tracker.team_b_maps:
                    self.log.info(f"Match {match_id}: Map score updated from API - "
                                 f"{tracker.team_a_maps}-{tracker.team_b_maps} -> {team_a_maps}-{team_b_maps}")
                    tracker.team_a_maps = team_a_maps
                    tracker.team_b_maps = team_b_maps
                    scores_changed = True

                # Update current map scores
                if current_map_idx < len(matchmaps):
                    api_map = matchmaps[current_map_idx]
                    api_team_a_score = api_map.get("rounds_won_team_a", 0) or 0
                    api_team_b_score = api_map.get("rounds_won_team_b", 0) or 0

                    # Update current map number
                    new_current_map = current_map_idx + 1
                    if new_current_map != tracker.current_map:
                        tracker.current_map = new_current_map
                        tracker.team_a_score = 0
                        tracker.team_b_score = 0
                        tracker.overtime_target = 13
                        scores_changed = True

                    if (api_team_a_score != tracker.team_a_score or
                        api_team_b_score != tracker.team_b_score):
                        self.log.info(f"Match {match_id}: Round score updated from API - "
                                     f"Map {tracker.current_map}: {tracker.team_a_score}-{tracker.team_b_score} -> "
                                     f"{api_team_a_score}-{api_team_b_score}")
                        tracker.team_a_score = api_team_a_score
                        tracker.team_b_score = api_team_b_score
                        tracker._update_overtime_target()
                        scores_changed = True

                # Check if match is finished — require actual map score to confirm,
                # has_ended alone is unreliable (API sometimes sends it prematurely)
                maps_to_win = (tracker.match.bestof + 1) // 2
                map_score_finished = tracker.team_a_maps >= maps_to_win or tracker.team_b_maps >= maps_to_win
                maps_played = tracker.team_a_maps + tracker.team_b_maps > 0
                match_finished = map_score_finished or (has_ended and maps_played)

                # Update Discord message and event name if scores changed or match finished
                if (scores_changed or match_finished) and tracker.message_id:
                    await self._update_event_name_with_score(tracker)
                    try:
                        channel = self.bot.get_channel(config.esports_update_channel_id)
                        if channel:
                            message = await channel.fetch_message(tracker.message_id)

                            if match_finished and not tracker.is_finished:
                                tracker.is_finished = True
                                embed = tracker.get_embed()  # This shows winner embed when is_finished=True
                                await message.edit(embed=embed, view=None)
                                self.log.info(f"Match {match_id} finished via API sync - "
                                             f"{tracker.match.team_a} {tracker.team_a_maps}-{tracker.team_b_maps} {tracker.match.team_b}")

                                # End the Discord event
                                await self._end_match_event(tracker.match)

                                # Remove from active games
                                del self.active_cs_games[match_id]
                            else:
                                embed = tracker.get_embed()
                                view = ScoreUpdateView(tracker, self)
                                await message.edit(embed=embed, view=view)
                    except discord.NotFound:
                        self.log.warning(f"Score update message {tracker.message_id} not found")
                        if match_id in self.active_cs_games:
                            del self.active_cs_games[match_id]
                    except Exception as e:
                        self.log.error(f"Error updating score message: {e}")

            status_reporter.record(
                "esports",
                cs_trackers=[
                    {
                        "match_id": mid,
                        "teams": f"{t.match.team_a} vs {t.match.team_b}",
                        "map": t.current_map,
                        "score": f"{t.team_a_score}-{t.team_b_score}",
                        "maps": f"{t.team_a_maps}-{t.team_b_maps}",
                        "is_finished": t.is_finished,
                    }
                    for mid, t in self.active_cs_games.items()
                ],
            )

        except Exception as e:
            self.log.error(f"Error in live score updater: {e}")
            status_reporter.bump_counter("esports", "api_errors")
            status_reporter.record("esports", last_livescore_error=str(e))

    @live_score_updater.before_loop
    async def before_live_score_updater(self):
        """Wait for bot to be ready before starting live score updater"""
        await self.bot.wait_until_ready()

    async def _end_match_event(self, match: EsportsMatch):
        """End the Discord event for a finished match"""
        try:
            if not match.discord_event_id:
                return

            guild = None
            event = None

            if config.esports_guild_id:
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                    except discord.NotFound:
                        pass
            else:
                for g in self.bot.guilds:
                    try:
                        event = await g.fetch_scheduled_event(match.discord_event_id)
                        guild = g
                        break
                    except discord.NotFound:
                        continue

            if event:
                if event.status == discord.EventStatus.active:
                    await event.end()
                    self.log.info(f"Ended Discord event {match.discord_event_id} for finished match {match.id}")
                elif event.status == discord.EventStatus.scheduled:
                    # Match finished before the event was ever started — delete the stale event
                    await event.delete()
                    self.log.info(f"Deleted stale scheduled event {match.discord_event_id} for finished match {match.id}")

            # Clean up mappings
            self.event_start_failures.pop(match.discord_event_id, None)
            if match.discord_event_id in self.event_to_match:
                del self.event_to_match[match.discord_event_id]
            match.discord_event_id = None

        except Exception as e:
            self.log.error(f"Error ending match event: {e}")

    async def _process_match_updates(self, current_matches: Dict[int, EsportsMatch]):
        """Process match updates and manage Discord events"""
        
        # Handle matches that disappeared from API (finished matches)
        for match_id, old_match in self.matches.items():
            if match_id not in current_matches:
                # Match disappeared from API - likely finished
                await self._handle_match_finished(old_match)
        
        # Handle cancelled matches
        for match_id, old_match in self.matches.items():
            if match_id in current_matches:
                new_match = current_matches[match_id]
                if new_match.cancelled and not old_match.cancelled:
                    await self._handle_match_cancelled(new_match)
        
        # Handle new and updated matches
        for match_id, match in current_matches.items():
            if match_id not in self.matches and match_id not in self.known_match_ids:
                # Genuinely new match - check if event already exists before creating
                if not match.cancelled and not match.discord_event_id:
                    await self._create_discord_event(match)
            elif match_id in self.matches:
                # Existing match - check for updates
                old_match = self.matches[match_id]
                if not match.cancelled and old_match.cancelled:
                    # Match was uncancelled - only create event if one doesn't exist
                    if not match.discord_event_id:
                        await self._create_discord_event(match)
                elif not match.cancelled and self._match_needs_update(old_match, match):
                    # Match details changed
                    if old_match.start_time != match.start_time:
                        if match.reminder_message_id:
                            # Edit the existing reminder message with the new time
                            await self._edit_reminder_message(match)
                        # else: no reminder sent yet, will fire normally at 30-min mark
                    await self._update_discord_event(match)
                elif not match.cancelled and not match.discord_event_id and match.start_time > datetime.now(timezone.utc):
                    # Belt-and-suspenders: existing match lost its event (e.g., mapping cleared last cycle)
                    self.log.info(f"Existing match {match_id} ({match.event_name}) has no Discord event — recreating")
                    await self._create_discord_event(match)
        
        # Update our local cache
        self.matches = current_matches
        self._save_data()

        # Restore CS trackers from JSON after first API poll (matches are now populated)
        if self._pending_tracker_restore:
            await self._restore_cs_trackers()

        # Update the weekly summary with any changes
        await self._update_weekly_summary()
    
    def _match_needs_update(self, old_match: EsportsMatch, new_match: EsportsMatch) -> bool:
        """Check if a match has significant changes that require event update"""
        return (
            old_match.start_time != new_match.start_time or
            old_match.team_a != new_match.team_a or
            old_match.team_b != new_match.team_b or
            old_match.tournament_name != new_match.tournament_name
        )
    
    async def _create_discord_event(self, match: EsportsMatch):
        """Create a Discord scheduled event for a match"""
        try:
            # Skip if event already exists
            if match.discord_event_id:
                self.log.debug(f"Discord event already exists for match {match.id}: {match.discord_event_id}")
                return
                
            # Only create events for matches that haven't started yet
            if match.start_time <= datetime.now(timezone.utc):
                self.log.debug(f"Skipping event creation for past match {match.id}")
                return
            
            # Find a guild to create the event in
            guild = None
            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if not guild:
                    self.log.error(f"Configured esports guild {config.esports_guild_id} not found")
                    return
                elif not guild.me.guild_permissions.manage_events:
                    self.log.error(f"Bot lacks manage_events permission in configured guild {config.esports_guild_id}")
                    return
            else:
                # Use first available guild with permissions (original behavior)
                for g in self.bot.guilds:
                    if g.me.guild_permissions.manage_events:
                        guild = g
                        break
            
            if not guild:
                self.log.error("No guild found with manage events permission")
                return
            
            # Calculate end time (default to 2 hours after start if not provided)
            end_time = match.end_time or (match.start_time + timedelta(hours=2))
            
            # Determine voice channel and entity type
            voice_channel = None
            entity_type = discord.EntityType.external
            location = "wannspieltbig.de"
            
            # Check for voice channel assignment
            if match.block_voice_channel == "VC 1" and config.esports_vc1_id:
                voice_channel = guild.get_channel(config.esports_vc1_id)
                if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                    entity_type = discord.EntityType.voice
                    location = None
            elif match.block_voice_channel == "VC 2" and config.esports_vc2_id:
                voice_channel = guild.get_channel(config.esports_vc2_id)
                if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                    entity_type = discord.EntityType.voice
                    location = None
            
            # Create the event
            if entity_type == discord.EntityType.voice and voice_channel:
                event = await guild.create_scheduled_event(
                    name=match.event_name,
                    description=match.event_description,
                    start_time=match.start_time,
                    end_time=end_time,
                    entity_type=entity_type,
                    channel=voice_channel,
                    privacy_level=discord.PrivacyLevel.guild_only
                )
            else:
                event = await guild.create_scheduled_event(
                    name=match.event_name,
                    description=match.event_description,
                    start_time=match.start_time,
                    end_time=end_time,
                    entity_type=discord.EntityType.external,
                    location="wannspieltbig.de",
                    privacy_level=discord.PrivacyLevel.guild_only
                )
            
            # Store the mapping
            self.event_to_match[event.id] = match.id
            match.discord_event_id = event.id
            
            self.log.info(f"Created Discord event {event.id} for match {match.id}: {match.event_name}")
            
        except Exception as e:
            self.log.error(f"Error creating Discord event for match {match.id}: {e}")
    
    async def _update_discord_event(self, match: EsportsMatch):
        """Update an existing Discord event"""
        try:
            if not match.discord_event_id:
                # Instead of just warning, try to create the event
                self.log.info(f"No event ID for match {match.id}, attempting to create event")
                await self._create_discord_event(match)
                return
            
            # Find the guild and event
            guild = None
            event = None
            
            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                    except discord.NotFound:
                        self.log.warning(f"Event {match.discord_event_id} not found in configured guild {config.esports_guild_id}")
                    except Exception as e:
                        self.log.debug(f"Error fetching event from configured guild: {e}")
            else:
                # Search through all guilds (original behavior)
                for g in self.bot.guilds:
                    try:
                        event = await g.fetch_scheduled_event(match.discord_event_id)
                        guild = g
                        break
                    except discord.NotFound:
                        continue
                    except Exception as e:
                        self.log.debug(f"Error fetching event from guild {g.id}: {e}")
                        continue
            
            if not event:
                self.log.warning(
                    f"Discord event {match.discord_event_id} not found for match {match.id} — will recreate"
                )
                # Remove invalid mapping
                if match.discord_event_id in self.event_to_match:
                    del self.event_to_match[match.discord_event_id]
                match.discord_event_id = None
                # Recreate so the match still has a Discord presence
                await self._create_discord_event(match)
                return
            
            # Calculate end time
            end_time = match.end_time or (match.start_time + timedelta(hours=2))
            
            # Determine voice channel and entity type
            voice_channel = None
            entity_type = discord.EntityType.external
            location = "wannspieltbig.de"
            
            # Check for voice channel assignment
            if match.block_voice_channel == "VC 1" and config.esports_vc1_id:
                voice_channel = guild.get_channel(config.esports_vc1_id)
                if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                    entity_type = discord.EntityType.voice
                    location = None
            elif match.block_voice_channel == "VC 2" and config.esports_vc2_id:
                voice_channel = guild.get_channel(config.esports_vc2_id)
                if voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                    entity_type = discord.EntityType.voice
                    location = None
            
            # Only update start_time if event is still scheduled (not active/completed)
            # Discord API error 50035 occurs when trying to update start_time of non-scheduled event
            now = datetime.now(timezone.utc)
            can_update_start_time = event.status == discord.EventStatus.scheduled

            # If the event is active but the match was rescheduled to the future,
            # end the stale event and create a fresh scheduled one
            if (not can_update_start_time and
                    event.status == discord.EventStatus.active and
                    match.start_time > now + timedelta(hours=1)):
                self.log.info(
                    f"Match {match.id} rescheduled to {match.start_time} while event {event.id} "
                    f"was already active — ending stale event and recreating"
                )
                try:
                    await event.end()
                except Exception as e:
                    self.log.warning(f"Could not end stale event {event.id}: {e}")
                if match.discord_event_id in self.event_to_match:
                    del self.event_to_match[match.discord_event_id]
                match.discord_event_id = None
                self._save_data()
                await self._create_discord_event(match)
                return

            # Update the event
            if entity_type == discord.EntityType.voice and voice_channel:
                if can_update_start_time:
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        start_time=match.start_time,
                        end_time=end_time,
                        entity_type=entity_type,
                        channel=voice_channel
                    )
                else:
                    # Event already started - only update what Discord allows
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        end_time=end_time
                    )
            else:
                if can_update_start_time:
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        start_time=match.start_time,
                        end_time=end_time,
                        entity_type=discord.EntityType.external,
                        location="wannspieltbig.de"
                    )
                else:
                    # Event already started - only update what Discord allows
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        end_time=end_time
                    )

            self.log.info(f"Updated Discord event {event.id} for match {match.id}")
            
        except Exception as e:
            self.log.error(f"Error updating Discord event for match {match.id}: {e}")
    
    async def _handle_match_cancelled(self, match: EsportsMatch):
        """Handle a cancelled match by deleting its Discord event"""
        try:
            if not match.discord_event_id:
                self.log.debug(f"No Discord event to cancel for match {match.id}")
                return
            
            # Find and delete the event
            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                        await event.delete()
                        self.log.info(f"Deleted Discord event {match.discord_event_id} for cancelled match {match.id}")
                    except discord.NotFound:
                        self.log.debug(f"Event {match.discord_event_id} not found in configured guild")
                    except Exception as e:
                        self.log.error(f"Error deleting event from configured guild: {e}")
            else:
                # Search through all guilds (original behavior)
                for guild in self.bot.guilds:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                        await event.delete()
                        
                        self.log.info(f"Deleted Discord event {match.discord_event_id} for cancelled match {match.id}")
                        break
                        
                    except discord.NotFound:
                        continue
                    except Exception as e:
                        self.log.debug(f"Error deleting event from guild {guild.id}: {e}")
                        continue
            
            # Clean up mappings
            if match.discord_event_id in self.event_to_match:
                del self.event_to_match[match.discord_event_id]
            match.discord_event_id = None
            
            # Also clean up reminder message if it exists
            await self._cleanup_match_reminder(match)
            
        except Exception as e:
            self.log.error(f"Error handling cancelled match {match.id}: {e}")
    
    async def _handle_match_finished(self, match: EsportsMatch):
        """Handle a finished match by ending its Discord event"""
        try:
            if not match.discord_event_id:
                self.log.debug(f"No Discord event to end for finished match {match.id}")
                return
            
            # Find and end the event
            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                        if event.status == discord.EventStatus.active:
                            await event.end()
                            self.log.info(f"Ended Discord event {match.discord_event_id} for finished match {match.id}: {match.event_name}")
                        elif event.status == discord.EventStatus.scheduled:
                            # If match finished before it was supposed to start, delete the event
                            await event.delete()
                            self.log.info(f"Deleted Discord event {match.discord_event_id} for finished match {match.id}: {match.event_name}")
                    except discord.NotFound:
                        self.log.debug(f"Event {match.discord_event_id} not found in configured guild")
                    except Exception as e:
                        self.log.error(f"Error ending event from configured guild: {e}")
            else:
                # Search through all guilds (original behavior)
                for guild in self.bot.guilds:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                        if event.status == discord.EventStatus.active:
                            await event.end()
                            self.log.info(f"Ended Discord event {match.discord_event_id} for finished match {match.id}: {match.event_name}")
                        elif event.status == discord.EventStatus.scheduled:
                            # If match finished before it was supposed to start, delete the event
                            await event.delete()
                            self.log.info(f"Deleted Discord event {match.discord_event_id} for finished match {match.id}: {match.event_name}")
                        break
                        
                    except discord.NotFound:
                        continue
                    except Exception as e:
                        self.log.debug(f"Error ending event from guild {guild.id}: {e}")
                        continue
            
            # Clean up mappings
            if match.discord_event_id in self.event_to_match:
                del self.event_to_match[match.discord_event_id]
            match.discord_event_id = None
            
            # Clean up reminder message if it exists
            await self._cleanup_match_reminder(match)
            
            # Clean up CS game tracking if active
            if match.id in self.active_cs_games:
                del self.active_cs_games[match.id]
                self.log.info(f"Cleaned up CS game tracking for finished match {match.id}")
            
            # Remove from monitored matches
            if match.id in self.monitored_matches:
                self.monitored_matches.remove(match.id)
            
        except Exception as e:
            self.log.error(f"Error handling finished match {match.id}: {e}")
    
    async def _send_weekly_summary(self, channel: discord.TextChannel):
        """Send weekly summary of upcoming matches"""
        try:
            # Delete old summary message if it exists
            if self.summary_message_id:
                try:
                    old_message = await channel.fetch_message(self.summary_message_id)
                    await old_message.delete()
                    self.log.info(f"Deleted old summary message {self.summary_message_id}")
                except discord.NotFound:
                    self.log.debug(f"Old summary message {self.summary_message_id} not found")
                except Exception as e:
                    self.log.warning(f"Failed to delete old summary message: {e}")
                finally:
                    self.summary_message_id = None
            
            # Get matches for the current week (Monday to Sunday)
            now = datetime.now(timezone.utc)
            
            # Convert to German timezone to get proper week boundaries
            now_berlin = now.astimezone(self.germany_tz)
            
            # Calculate start of current week (Monday)
            days_since_monday = now_berlin.weekday()
            week_start = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
            
            # Calculate end of current week (Sunday)
            week_end = week_start + timedelta(days=7)
            
            # Convert back to UTC for comparison
            week_start_utc = week_start.astimezone(timezone.utc)
            week_end_utc = week_end.astimezone(timezone.utc)
            
            upcoming_matches = [
                match for match in self.matches.values()
                if not match.cancelled and week_start_utc <= match.start_time < week_end_utc
            ]
            
            # Sort by start time
            upcoming_matches.sort(key=lambda m: m.start_time)
            
            # Create embed with thumbnail
            week_start_str = week_start.strftime("%B %d")
            week_end_str = (week_end - timedelta(days=1)).strftime("%B %d")
            embed = discord.Embed(
                title=f"This Week ({week_start_str} - {week_end_str}) • {len(upcoming_matches)} matches",
                color=0x00ff88
            )
            embed.set_thumbnail(url="attachment://big.png")
            
            powered_by = "-# Powered by [wannspieltbig.de](https://wannspieltbig.de)"

            if not upcoming_matches:
                embed.add_field(
                    name="No Matches Scheduled",
                    value="No matches are scheduled for the upcoming week.",
                    inline=False
                )
                embed.add_field(name="​", value=powered_by, inline=False)
            else:
                # Group matches by day
                matches_by_day = {}
                for match in upcoming_matches:
                    # Convert to German timezone for display
                    match_time_berlin = match.start_time.astimezone(self.germany_tz)
                    day_key = match_time_berlin.strftime("%A, %B %d")

                    if day_key not in matches_by_day:
                        matches_by_day[day_key] = []
                    matches_by_day[day_key].append((match, match_time_berlin))

                # Add fields for each day; append powered_by to the last day's field
                days_list = list(matches_by_day.items())
                for i, (day, day_matches) in enumerate(days_list):
                    match_lines = []
                    for match, match_time in day_matches:
                        time_str = match_time.strftime("%H:%M")
                        # Use custom emotes for specific games
                        if match.game == "cs":
                            game_emoji = "<:cs:1416235161594499092>"
                        elif match.game == "lol":
                            game_emoji = "<:lol:1416235138307854416>"
                        elif match.game == "tm":
                            game_emoji = "🏎️"
                        else:
                            game_emoji = "🎮"

                        # Create clickable link to Discord event if event exists
                        if match.discord_event_id:
                            # Get guild to construct event URL
                            guild = None
                            if config.esports_guild_id:
                                guild = self.bot.get_guild(config.esports_guild_id)
                            else:
                                for g in self.bot.guilds:
                                    if g.me.guild_permissions.manage_events:
                                        guild = g
                                        break

                            if guild:
                                event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                                match_line = f"{game_emoji} **[{time_str} - {match.team_a} vs {match.team_b}]({event_url})**"
                            else:
                                match_line = f"{game_emoji} **{time_str} - {match.team_a} vs {match.team_b}**"
                        else:
                            match_line = f"{game_emoji} **{time_str} - {match.team_a} vs {match.team_b}**"

                        match_lines.append(match_line)

                    value = "\n".join(match_lines)
                    if i == len(days_list) - 1:
                        value += f"\n\n{powered_by}"
                    embed.add_field(name=f"{day}", value=value, inline=False)

            # Send the new summary with big.png thumbnail
            file = discord.File("big.png", filename="big.png")
            message = await channel.send(file=file, embed=embed)
            self.summary_message_id = message.id
            self._save_data()  # Save the new message ID
            
            self.log.info(f"Sent weekly summary to channel {channel.id}, message ID: {message.id}")
            status_reporter.record(
                "esports",
                weekly_summary_last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                weekly_summary_message_id=message.id,
                weekly_summary_last_error=None,
            )

        except Exception as e:
            self.log.error(f"Error sending weekly summary: {e}")
            status_reporter.record("esports", weekly_summary_last_error=str(e))
    
    async def _update_weekly_summary(self):
        """Update the weekly summary message with current matches"""
        try:
            if not config.esports_summary_channel_id:
                return  # No channel configured
                
            channel = self.bot.get_channel(config.esports_summary_channel_id)
            if not channel:
                return  # Channel not found
            
            # Check if we need to create a new weekly message (if it's a new week or no message exists)
            should_create_new = False
            
            if self.summary_message_id:
                try:
                    existing_message = await channel.fetch_message(self.summary_message_id)
                    
                    # Check if the message is from a previous week
                    message_created = existing_message.created_at
                    now_berlin = datetime.now(self.germany_tz)
                    
                    # Calculate start of current week (Monday)
                    days_since_monday = now_berlin.weekday()
                    current_week_start = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
                    
                    # If message was created before this week started, delete it and create new
                    if message_created.astimezone(self.germany_tz) < current_week_start:
                        await existing_message.delete()
                        self.log.info(f"Deleted old weekly summary from previous week")
                        should_create_new = True
                        self.summary_message_id = None
                    
                except discord.NotFound:
                    self.log.debug(f"Summary message {self.summary_message_id} no longer exists")
                    should_create_new = True
                    self.summary_message_id = None
            else:
                should_create_new = True
            
            # Create new summary or update existing one
            if should_create_new:
                self.log.info("Creating new weekly summary")
                await self._send_weekly_summary(channel)
            else:
                # Update the existing message
                await self._update_existing_summary(channel)
                
        except Exception as e:
            self.log.error(f"Error updating weekly summary: {e}")
    
    async def _update_existing_summary(self, channel: discord.TextChannel):
        """Update the existing weekly summary message"""
        try:
            if not self.summary_message_id:
                return
                
            # Get matches for the current week (same logic as _send_weekly_summary)
            now = datetime.now(timezone.utc)
            now_berlin = now.astimezone(self.germany_tz)
            
            # Calculate start of current week (Monday)
            days_since_monday = now_berlin.weekday()
            week_start = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
            week_end = week_start + timedelta(days=7)
            
            # Convert back to UTC for comparison
            week_start_utc = week_start.astimezone(timezone.utc)
            week_end_utc = week_end.astimezone(timezone.utc)
            
            upcoming_matches = [
                match for match in self.matches.values()
                if not match.cancelled and week_start_utc <= match.start_time < week_end_utc
            ]
            
            # Sort by start time
            upcoming_matches.sort(key=lambda m: m.start_time)
            
            # Create updated embed (same logic as _send_weekly_summary)
            week_start_str = week_start.strftime("%B %d")
            week_end_str = (week_end - timedelta(days=1)).strftime("%B %d")
            embed = discord.Embed(
                title=f"This Week ({week_start_str} - {week_end_str}) • {len(upcoming_matches)} matches",
                color=0x00ff88
            )
            embed.set_thumbnail(url="attachment://big.png")
            
            powered_by = "-# Powered by [wannspieltbig.de](https://wannspieltbig.de)"

            if not upcoming_matches:
                embed.add_field(
                    name="No Matches Scheduled",
                    value="No matches are scheduled for this week.",
                    inline=False
                )
                embed.add_field(name="​", value=powered_by, inline=False)
            else:
                # Group matches by day
                matches_by_day = {}
                for match in upcoming_matches:
                    match_time_berlin = match.start_time.astimezone(self.germany_tz)
                    day_key = match_time_berlin.strftime("%A, %B %d")

                    if day_key not in matches_by_day:
                        matches_by_day[day_key] = []
                    matches_by_day[day_key].append((match, match_time_berlin))

                # Add fields for each day; append powered_by to the last day's field
                days_list = list(matches_by_day.items())
                for i, (day, day_matches) in enumerate(days_list):
                    match_lines = []
                    for match, match_time in day_matches:
                        time_str = match_time.strftime("%H:%M")

                        # Use custom emotes for specific games
                        if match.game == "cs":
                            game_emoji = "<:cs:1416235161594499092>"
                        elif match.game == "lol":
                            game_emoji = "<:lol:1416235138307854416>"
                        elif match.game == "tm":
                            game_emoji = "🏎️"
                        else:
                            game_emoji = "🎮"

                        # Create clickable link to Discord event if event exists
                        if match.discord_event_id:
                            guild = channel.guild
                            if guild:
                                event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                                match_line = f"{game_emoji} **[{time_str} - {match.team_a} vs {match.team_b}]({event_url})**"
                            else:
                                match_line = f"{game_emoji} **{time_str} - {match.team_a} vs {match.team_b}**"
                        else:
                            match_line = f"{game_emoji} **{time_str} - {match.team_a} vs {match.team_b}**"

                        match_lines.append(match_line)

                    value = "\n".join(match_lines)
                    if i == len(days_list) - 1:
                        value += f"\n\n{powered_by}"
                    embed.add_field(name=f"{day}", value=value, inline=False)
            
            # Update the existing message
            message = await channel.fetch_message(self.summary_message_id)
            file = discord.File("big.png", filename="big.png")
            await message.edit(embed=embed, attachments=[file])
            
            self.log.info(f"Updated weekly summary message {self.summary_message_id}")
            status_reporter.record(
                "esports",
                weekly_summary_last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                weekly_summary_message_id=self.summary_message_id,
                weekly_summary_last_error=None,
            )

        except discord.NotFound:
            self.log.warning(f"Summary message {self.summary_message_id} not found, will create new one")
            self.summary_message_id = None
            await self._send_weekly_summary(channel)
        except Exception as e:
            self.log.error(f"Error updating existing summary: {e}")
            status_reporter.record("esports", weekly_summary_last_error=str(e))
    
    async def _get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers for wannspieltbig API"""
        if not config.wsb_username or not config.wsb_password:
            raise ValueError("WSB credentials not configured")
        
        # Create basic auth header
        credentials = f"{config.wsb_username}:{config.wsb_password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        
        return {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/json"
        }
    
    async def _fetch_match_maps(self, match_id: int) -> List[int]:
        """Extract match map IDs from stored match data"""
        try:
            match = self.matches.get(match_id)
            if not match:
                self.log.error(f"Match {match_id} not found in stored matches")
                return []
            
            # Extract matchmap IDs from the stored matchmaps data
            return [matchmap["id"] for matchmap in match.matchmaps]
            
        except Exception as e:
            self.log.error(f"Error extracting match maps for match {match_id}: {e}")
            return []
    
    async def _update_score_api(self, tracker: CSGameTracker):
        """Update scores on wannspieltbig API"""
        try:
            if not tracker.current_map_id:
                self.log.warning(f"No map ID available for match {tracker.match.id}")
                return
            
            headers = await self._get_auth_headers()
            
            # Get the played_map_name from matchmaps data
            played_map_name = None
            current_map_idx = tracker.current_map - 1
            if current_map_idx < len(tracker.match.matchmaps):
                matchmap = tracker.match.matchmaps[current_map_idx]
                played_map = matchmap.get("played_map")
                if played_map:
                    played_map_name = played_map.get("cs_name") or played_map.get("name")

            if not played_map_name:
                self.log.warning(f"No played_map_name found for match {tracker.match.id}, map {tracker.current_map}")
                return

            # Prepare the update data
            update_data = {
                "map_nr": tracker.current_map,
                "rounds_won_team_a": tracker.team_a_score,
                "rounds_won_team_b": tracker.team_b_score,
                "played_map_name": played_map_name
            }
            
            # Update the score via API
            url = f"https://wannspieltbig.de/api/matchmap_update/{tracker.current_map_id}/"
            response = await http_client.put(url, json=update_data, headers=headers)
            try:
                if response.status in [200, 204]:
                    self.log.info(f"Successfully updated scores for match {tracker.match.id}, map {tracker.current_map}")
                else:
                    self.log.error(f"Failed to update scores: HTTP {response.status}")
                    error_text = await response.text()
                    self.log.error(f"API response: {error_text}")
            finally:
                await response.release()

        except Exception as e:
            self.log.error(f"Error updating scores for match {tracker.match.id}: {e}")
        finally:
            # Always persist tracker state so restarts don't lose the current score
            self._save_data()

    async def _update_event_name_with_score(self, tracker: CSGameTracker):
        """Update Discord event name with live score, e.g. 'BIG vs MIBR - 3:4 (1:0)'"""
        match = tracker.match
        if not match.discord_event_id:
            return
        try:
            guild_id = config.esports_guild_id
            guild = self.bot.get_guild(guild_id) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
            if not guild:
                return
            event = guild.get_scheduled_event(match.discord_event_id)
            if event is None:
                try:
                    event = await guild.fetch_scheduled_event(match.discord_event_id)
                except (discord.NotFound, discord.Forbidden):
                    return
            await event.edit(name=tracker.get_event_score_name())
        except discord.Forbidden as e:
            # Discord event is already completed — stop trying to update it
            self.log.info(f"Discord event for match {match.id} is finished, stopping tracker")
            tracker.is_finished = True
            self.active_cs_games.pop(match.id, None)
            self._save_data()
        except Exception as e:
            self.log.warning(f"Failed to update event name with score for match {match.id}: {e}")

    async def _check_for_starting_matches(self):
        """Check if any CS matches are starting soon and create score trackers"""
        if not config.esports_update_channel_id:
            return
            
        now = datetime.now(timezone.utc)
        
        for match in self.matches.values():
            if (match.game == "cs" and 
                not match.cancelled and 
                match.id not in self.active_cs_games and
                match.id not in self.monitored_matches):
                
                # Check if match is starting within the next 20 minutes
                time_to_start = (match.start_time - now).total_seconds()
                if 0 <= time_to_start <= 1200:  # 20 minutes
                    self.monitored_matches.add(match.id)
                    await self._start_cs_game_tracking(match)
    
    async def _check_event_status_updates(self):
        """Check for Discord events that need status updates (start/end)"""
        now = datetime.now(timezone.utc)
        
        for event_id, match_id in list(self.event_to_match.items()):
            match = self.matches.get(match_id)
            if not match or match.cancelled:
                continue
                
            # Find the Discord event
            event = None
            guild = None
            
            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(event_id)
                    except discord.NotFound:
                        pass
                    except Exception:
                        pass
            else:
                # Search through all guilds (original behavior)
                for g in self.bot.guilds:
                    try:
                        event = await g.fetch_scheduled_event(event_id)
                        guild = g
                        break
                    except discord.NotFound:
                        continue
                    except Exception:
                        continue
            
            if not event:
                # Only recreate after 2 consecutive NotFound to guard against transient API errors
                count = self.event_not_found_count.get(event_id, 0) + 1
                self.event_not_found_count[event_id] = count
                if count < 2:
                    self.log.warning(
                        f"Event {event_id} for match {match_id} not found on Discord "
                        f"(attempt {count}/2) — will verify next cycle before recreating"
                    )
                    continue
                # Second consecutive NotFound — treat as genuinely gone
                self.event_not_found_count.pop(event_id, None)
                del self.event_to_match[event_id]
                if match:
                    match.discord_event_id = None
                    if not match.cancelled and match.start_time > now:
                        self.log.info(
                            f"Event {event_id} for match {match_id} ({match.event_name}) "
                            f"no longer exists on Discord — recreating"
                        )
                        await self._create_discord_event(match)
                continue

            # Event was found — reset NotFound counter
            self.event_not_found_count.pop(event_id, None)

            # Check if event is completed/ended but match is still upcoming — stale completed event
            if (event.status == discord.EventStatus.completed and
                    match.start_time > now):
                self.log.info(
                    f"Event {event_id} for match {match_id} ({match.event_name}) "
                    f"is completed but match is still upcoming — cleaning up and recreating"
                )
                if event_id in self.event_to_match:
                    del self.event_to_match[event_id]
                match.discord_event_id = None
                await self._create_discord_event(match)
                continue

            # Check if event should be started
            if (event.status == discord.EventStatus.scheduled and
                    match.start_time <= now):
                try:
                    await event.start()
                    self.log.info(f"Started Discord event {event_id} for match {match_id}: {match.event_name}")
                    self.event_start_failures.pop(event_id, None)  # Reset on success
                except Exception as e:
                    failures = self.event_start_failures.get(event_id, 0) + 1
                    self.event_start_failures[event_id] = failures
                    self.log.error(f"Failed to start event {event_id} (attempt {failures}): {e}")
                    if failures >= 3:
                        # Give up — delete the stale event and clean up mapping
                        self.log.warning(
                            f"Giving up on event {event_id} after {failures} failed start attempts — deleting"
                        )
                        try:
                            await event.delete()
                        except Exception as del_e:
                            self.log.warning(f"Could not delete stuck event {event_id}: {del_e}")
                        self.event_start_failures.pop(event_id, None)
                        if event_id in self.event_to_match:
                            del self.event_to_match[event_id]
                        match.discord_event_id = None
                        self._save_data()
                        # Recreate the event so the match still has a Discord presence
                        await self._create_discord_event(match)
            
            # Check if event should be ended
            elif (event.status == discord.EventStatus.active and 
                  match.end_time and match.end_time <= now):
                try:
                    await event.end()
                    self.log.info(f"Ended Discord event {event_id} for match {match_id}: {match.event_name}")
                except Exception as e:
                    self.log.error(f"Failed to end event {event_id}: {e}")
            
            # Auto-end events that have been active for more than 4 hours (fallback)
            elif (event.status == discord.EventStatus.active and
                  (now - match.start_time).total_seconds() > 14400):  # 4 hours
                try:
                    await event.end()
                    self.log.info(f"Auto-ended Discord event {event_id} after 4 hours for match {match_id}")
                except Exception as e:
                    self.log.error(f"Failed to auto-end event {event_id}: {e}")
    
    async def _start_cs_game_tracking(self, match: EsportsMatch):
        """Start tracking a CS game"""
        try:
            # Create tracker
            tracker = CSGameTracker(match)
            
            # Fetch match map IDs
            tracker.match_maps = await self._fetch_match_maps(match.id)
            
            if not tracker.match_maps:
                self.log.warning(f"No map data available for CS match {match.id}")
                return
            
            # Get update channel
            channel = self.bot.get_channel(config.esports_update_channel_id)
            if not channel:
                self.log.error(f"Update channel {config.esports_update_channel_id} not found")
                return
            
            # Create initial embed and view
            embed = tracker.get_embed()
            view = ScoreUpdateView(tracker, self)
            
            # Send message
            message = await channel.send(embed=embed, view=view)
            tracker.message_id = message.id
            
            # Store tracker
            self.active_cs_games[match.id] = tracker
            
            self.log.info(f"Started CS game tracking for match {match.id}: {match.event_name}")
            
        except Exception as e:
            self.log.error(f"Error starting CS game tracking for match {match.id}: {e}")
            if match.id in self.monitored_matches:
                self.monitored_matches.remove(match.id)
    
    async def _check_for_match_reminders(self):
        """Check for matches that need 30-minute reminders"""
        if not config.esports_summary_channel_id:
            return
        
        channel = self.bot.get_channel(config.esports_summary_channel_id)
        if not channel:
            return
            
        now = datetime.now(timezone.utc)
        
        for match in self.matches.values():
            if (not match.cancelled and
                not match.reminder_message_id and  # No reminder sent yet
                not match.forum_thread_id and      # No thread created yet
                match.start_time > now):  # Match hasn't started yet

                # Check if match is starting within 30-35 minutes (5-minute window for polling)
                time_to_start = (match.start_time - now).total_seconds()
                if 1800 <= time_to_start <= 2100:  # 30-35 minutes
                    await self._send_match_reminder(match, channel)
    
    async def _edit_reminder_message(self, match: EsportsMatch):
        """Edit an existing reminder message after a reschedule"""
        try:
            embed = match.get_reminder_embed()

            if match.discord_event_id:
                for guild in self.bot.guilds:
                    event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                    for i, field in enumerate(embed.fields):
                        if field.name == "📅 Discord Event":
                            embed.set_field_at(i, name="📅 Discord Event", value=f"[Join Event]({event_url})", inline=False)
                            break
                    break

            message = None

            # Try forum thread first
            if match.forum_thread_id:
                thread = self.bot.get_channel(match.forum_thread_id)
                if thread is None:
                    try:
                        thread = await self.bot.fetch_channel(match.forum_thread_id)
                    except (discord.NotFound, discord.Forbidden):
                        thread = None
                if thread and isinstance(thread, discord.Thread):
                    try:
                        message = await thread.fetch_message(match.reminder_message_id)
                    except discord.NotFound:
                        pass

            # Fallback: summary channel
            if message is None and config.esports_summary_channel_id:
                channel = self.bot.get_channel(config.esports_summary_channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(match.reminder_message_id)
                    except discord.NotFound:
                        pass

            if message:
                await message.edit(embed=embed)
                self.log.info(f"Edited reminder for rescheduled match {match.id}: {match.event_name}")
            else:
                # Message no longer exists — clear tracking so a fresh reminder fires at 30-min mark
                self.reminder_to_match = {k: v for k, v in self.reminder_to_match.items() if v != match.id}
                match.reminder_message_id = None
                self.log.debug(f"Reminder message for match {match.id} not found, cleared tracking")

        except Exception as e:
            self.log.error(f"Error editing reminder for match {match.id}: {e}")

    async def _send_match_reminder(self, match: EsportsMatch, channel: discord.TextChannel):
        """Send 30-minute reminder for a match"""
        try:
            # Get the appropriate ping role based on game
            ping_role_id = None
            if match.game == "cs":
                ping_role_id = config.ping_cs_role_id
            elif match.game == "lol":
                ping_role_id = config.ping_lol_role_id
            elif match.game == "tm":
                ping_role_id = config.ping_tm_role_id
            
            # Create mention string
            mention_text = ""
            if ping_role_id:
                mention_text = f"<@&{ping_role_id}>"
            
            # Generate embed
            embed = match.get_reminder_embed()
            
            # Add Discord event link if available
            if match.discord_event_id:
                guild = channel.guild
                if guild:
                    event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                    for i, field in enumerate(embed.fields):
                        if field.name == "📅 Discord Event":
                            embed.set_field_at(
                                i,
                                name="📅 Discord Event",
                                value=f"[Join Event]({event_url})",
                                inline=False
                            )
                            break

            # Create/reuse forum thread if configured — ping goes into the thread
            if config.esports_forum_channel_id:
                forum_channel = self.bot.get_channel(config.esports_forum_channel_id)
                if isinstance(forum_channel, discord.ForumChannel):
                    try:
                        # Reuse existing thread if match was rescheduled
                        if match.forum_thread_id:
                            existing = self.bot.get_channel(match.forum_thread_id)
                            if existing is None:
                                try:
                                    existing = await self.bot.fetch_channel(match.forum_thread_id)
                                except (discord.NotFound, discord.Forbidden):
                                    existing = None
                            if existing and isinstance(existing, discord.Thread):
                                if existing.archived:
                                    await existing.edit(archived=False)
                                msg = await existing.send(content=mention_text or None, embed=embed)
                                match.reminder_message_id = msg.id
                                self.reminder_to_match[msg.id] = match.id
                                self._save_data()
                                self.log.info(f"Sent rescheduled reminder in existing thread for match {match.id}: {match.event_name}")
                                return
                            else:
                                # Thread no longer accessible — clear stale tracking so a fresh thread can be created
                                self.log.warning(f"Existing thread {match.forum_thread_id} for match {match.id} not found, clearing stale tracking")
                                self._close_forum_thread(match.forum_thread_id, match.id)

                        # No existing thread — create a new one
                        game_name = {"cs": "Counter-Strike", "tm": "Trackmania", "lol": "League of Legends"}.get(match.game, match.game.upper())
                        thread_with_msg = await forum_channel.create_thread(
                            name=f"{match.team_a} vs {match.team_b} – {game_name}",
                            embed=embed,
                        )
                        forum_thread = thread_with_msg.thread
                        match.forum_thread_id = forum_thread.id
                        self.thread_to_match[forum_thread.id] = match.id
                        match.reminder_message_id = thread_with_msg.message.id
                        self.reminder_to_match[thread_with_msg.message.id] = match.id
                        # Send ping as separate message so Discord triggers proper notifications
                        if mention_text:
                            await forum_thread.send(content=mention_text)
                        self._save_data()
                        self.log.info(f"Sent 30-minute reminder (thread) for match {match.id}: {match.event_name}")
                        status_reporter.record(
                            "esports",
                            last_reminder_sent_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            last_reminder_match=match.event_name,
                        )
                        return
                    except Exception as e:
                        self.log.error(f"Error creating/reusing forum thread for match {match.id}: {e}")

            # Fallback: post to games channel if no forum channel configured
            message = await channel.send(content=mention_text, embed=embed)
            match.reminder_message_id = message.id
            self.reminder_to_match[message.id] = match.id
            self._save_data()
            
            self.log.info(f"Sent 30-minute reminder for match {match.id}: {match.event_name}")
            status_reporter.record(
                "esports",
                last_reminder_sent_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                last_reminder_match=match.event_name,
            )

        except Exception as e:
            self.log.error(f"Error sending reminder for match {match.id}: {e}")
            status_reporter.bump_counter("esports", "reminder_errors")
    
    async def _check_for_reminder_cleanup(self):
        """Check for reminders that should be deleted after match ends"""
        if not config.esports_summary_channel_id:
            return
            
        channel = self.bot.get_channel(config.esports_summary_channel_id)
        if not channel:
            return
            
        now = datetime.now(timezone.utc)
        reminders_to_delete = []
        
        for reminder_id, match_id in self.reminder_to_match.items():
            match = self.matches.get(match_id)
            if not match:
                # Match no longer exists, clean up reminder
                reminders_to_delete.append(reminder_id)
                continue
            
            # Check if match has ended (including fallback for matches without end time)
            match_ended = False
            if match.end_time and match.end_time <= now:
                match_ended = True
            elif (now - match.start_time).total_seconds() > 14400:  # 4 hours after start
                match_ended = True
            elif match.cancelled:
                match_ended = True
            
            if match_ended:
                reminders_to_delete.append(reminder_id)
        
        # Delete reminder messages
        for reminder_id in reminders_to_delete:
            match_id = self.reminder_to_match.get(reminder_id)
            match = self.matches.get(match_id) if match_id else None

            if match and match.forum_thread_id:
                # Close forum thread tracking (thread stays in Discord, archived by inactivity)
                self._close_forum_thread(match.forum_thread_id, match_id)
            elif not match:
                # Match no longer exists — also clean up stale thread_to_match entries for this match_id
                stale_thread_ids = [tid for tid, mid in self.thread_to_match.items() if mid == match_id]
                for tid in stale_thread_ids:
                    del self.thread_to_match[tid]
            else:
                # No thread — delete the channel message
                try:
                    message = await channel.fetch_message(reminder_id)
                    await message.delete()
                    self.log.info(f"Deleted reminder message {reminder_id}")
                except discord.NotFound:
                    self.log.debug(f"Reminder message {reminder_id} already deleted")
                except Exception as e:
                    self.log.warning(f"Failed to delete reminder message {reminder_id}: {e}")

            # Clean up mappings
            if match:
                match.reminder_message_id = None
            del self.reminder_to_match[reminder_id]

        if reminders_to_delete:
            self._save_data()
    
    def _close_forum_thread(self, thread_id: int, match_id: int):
        """Clean up forum thread tracking data (Discord handles archiving via inactivity setting)"""
        if thread_id in self.thread_to_match:
            del self.thread_to_match[thread_id]
        if match_id in self.matches:
            self.matches[match_id].forum_thread_id = None

    async def _cleanup_match_reminder(self, match: EsportsMatch):
        """Clean up reminder message for a specific match"""
        if not match.reminder_message_id:
            return

        if match.forum_thread_id:
            # Thread message — just archive the thread
            self._close_forum_thread(match.forum_thread_id, match.id)
        elif config.esports_summary_channel_id:
            # Channel message — delete it
            try:
                channel = self.bot.get_channel(config.esports_summary_channel_id)
                if channel:
                    message = await channel.fetch_message(match.reminder_message_id)
                    await message.delete()
                    self.log.info(f"Deleted reminder message {match.reminder_message_id} for match {match.id}")
            except discord.NotFound:
                self.log.debug(f"Reminder message {match.reminder_message_id} already deleted")
            except Exception as e:
                self.log.warning(f"Failed to delete reminder message {match.reminder_message_id}: {e}")

        # Clean up mappings
        if match.reminder_message_id in self.reminder_to_match:
            del self.reminder_to_match[match.reminder_message_id]
        match.reminder_message_id = None
        self._save_data()
    
    @app_commands.command(name="wannspieltbig_status", description="Show match monitoring status")
    @app_commands.default_permissions(administrator=True)
    async def wannspieltbig_status(self, interaction: discord.Interaction):
        """Show status of e-sports monitoring"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🎮 E-Sports Monitoring Status",
            color=0x7289da,
            timestamp=datetime.utcnow()
        )
        
        # Basic status
        embed.add_field(
            name="Status",
            value="🟢 Enabled" if config.esports_enabled else "🔴 Disabled",
            inline=True
        )
        
        embed.add_field(
            name="Poll Interval",
            value=f"{config.esports_poll_interval_minutes} minutes",
            inline=True
        )
        
        summary_channel_info = "Not configured"
        if config.esports_summary_channel_id:
            if self.summary_message_id:
                summary_channel_info = f"<#{config.esports_summary_channel_id}>\nMessage ID: {self.summary_message_id}"
            else:
                summary_channel_info = f"<#{config.esports_summary_channel_id}>\nNo summary posted yet"
        
        embed.add_field(
            name="Summary Channel",
            value=summary_channel_info,
            inline=True
        )
        
        # Match statistics
        total_matches = len(self.matches)
        active_matches = len([m for m in self.matches.values() if not m.cancelled])
        cancelled_matches = total_matches - active_matches
        events_created = len(self.event_to_match)
        
        embed.add_field(
            name="📊 Statistics",
            value=(
                f"**Total Matches:** {total_matches}\n"
                f"**Active Matches:** {active_matches}\n"
                f"**Cancelled Matches:** {cancelled_matches}\n"
                f"**Discord Events:** {events_created}"
            ),
            inline=False
        )
        
        # Task status
        monitor_status = "🟢 Running" if not self.match_monitor.is_being_cancelled() else "🔴 Stopped"

        embed.add_field(
            name="⚙️ Tasks",
            value=f"**Match Monitor:** {monitor_status}",
            inline=False
        )
        
        embed.set_footer(text=f"API: {config.esports_api_url}")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="wannspieltbig_summary", description="Send weekly summary now")
    @app_commands.default_permissions(administrator=True)
    async def wannspieltbig_summary_now(self, interaction: discord.Interaction):
        """Manually trigger weekly summary"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return
            
        if not config.esports_summary_channel_id:
            await interaction.response.send_message(
                "❌ No summary channel configured. Set ESPORTS_SUMMARY_CHANNEL_ID environment variable.",
                ephemeral=True
            )
            return
        
        channel = self.bot.get_channel(config.esports_summary_channel_id)
        if not channel:
            await interaction.response.send_message(
                f"❌ Summary channel {config.esports_summary_channel_id} not found.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self._send_weekly_summary(channel)
            await interaction.followup.send("✅ Weekly summary sent successfully!", ephemeral=True)
        except Exception as e:
            self.log.error(f"Error sending manual summary: {e}")
            await interaction.followup.send(f"❌ Error sending summary: {e}", ephemeral=True)
    
    @app_commands.command(name="wannspieltbig_refresh", description="Manually refresh match data")
    @app_commands.default_permissions(administrator=True)
    async def wannspieltbig_refresh(self, interaction: discord.Interaction):
        """Manually trigger match data refresh"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        
        try:
            await self.match_monitor()
            await interaction.followup.send("✅ Match data refreshed successfully!", ephemeral=True)
        except Exception as e:
            self.log.error(f"Error in manual refresh: {e}")
            await interaction.followup.send(f"❌ Error refreshing data: {e}", ephemeral=True)
    
    @app_commands.command(name="wannspieltbig_start", description="Start CS game tracking - choose from upcoming matches")
    @app_commands.default_permissions(administrator=True)
    async def wannspieltbig_start(self, interaction: discord.Interaction):
        """Show upcoming CS matches to start tracking"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to use this command.", ephemeral=True)
            return
            
        if not config.esports_update_channel_id:
            await interaction.response.send_message("❌ No update channel configured.", ephemeral=True)
            return
        
        # Get CS matches from the current week (Monday to Sunday, German timezone)
        now = datetime.now(timezone.utc)
        now_berlin = now.astimezone(self.germany_tz)
        days_since_monday = now_berlin.weekday()
        week_start = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
        week_end = week_start + timedelta(days=7)
        week_start_utc = week_start.astimezone(timezone.utc)
        week_end_utc = week_end.astimezone(timezone.utc)

        upcoming_cs_matches = [
            match for match in self.matches.values()
            if (match.game == "cs" and
                not match.cancelled and
                match.start_time > now and
                week_start_utc <= match.start_time < week_end_utc and
                match.id not in self.active_cs_games)
        ]
        
        # Sort by start time
        upcoming_cs_matches.sort(key=lambda m: m.start_time)
        
        if not upcoming_cs_matches:
            await interaction.response.send_message(
                "❌ Keine CS-Matches in dieser Woche gefunden, die nicht bereits getrackt werden.",
                ephemeral=True
            )
            return
        
        # Create embed showing available matches
        embed = discord.Embed(
            title="🎮 Select CS Match to Track",
            description="Choose an upcoming Counter-Strike match to start live score tracking:",
            color=0x7289da
        )
        
        # Add field showing matches
        match_list = []
        for i, match in enumerate(upcoming_cs_matches[:10]):  # Show first 10
            match_time = match.start_time.strftime("%H:%M")
            match_list.append(f"**{match.team_a} vs {match.team_b}** - {match_time}")
        
        embed.add_field(
            name="Available Matches",
            value="\n".join(match_list),
            inline=False
        )
        
        if len(upcoming_cs_matches) > 10:
            embed.set_footer(text=f"Showing first 10 of {len(upcoming_cs_matches)} matches")
        
        # Create view with match selection buttons
        view = MatchSelectionView(upcoming_cs_matches, self)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    


async def setup(bot: commands.Bot):
    if config.esports_enabled:
        await bot.add_cog(EsportsCog(bot))
    else:
        logging.getLogger("roaringbot").info("E-Sports cog disabled via configuration")
