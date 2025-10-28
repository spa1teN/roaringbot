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
        
        self.start_time = datetime.fromisoformat(match_data["first_map_at"])
        self.end_time = datetime.fromisoformat(match_data["last_map_end"]) if match_data["last_map_end"] else None
        self.cancelled = bool(match_data["cancelled"])
        self.detail_url = match_data["html_detail_url"]
        self.bestof = match_data["bestof"]
        self.game = match_data["game"]
        self.slug = match_data["slug"]
        self.block_voice_channel = match_data.get("block_voice_channel", "")
        self.matchmaps = match_data.get("matchmaps", [])  # Store matchmaps data
        self.discord_event_id: Optional[int] = None
        self.reminder_message_id: Optional[int] = None  # ID of the 30-min reminder message
    
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
        
        return (
            f"--------------------------\n\n"
            f"🏆  **{self.tournament_name}**\n\n"
            f"{game_emoji}  {game_name} - BO{self.bestof}\n\n"
            f"{self.detail_url}\n\n"
        )
    
    def __eq__(self, other):
        return isinstance(other, EsportsMatch) and self.id == other.id
    
    def __hash__(self):
        return hash(self.id)
    
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
        
        embed = discord.Embed(
            title=f"⏰ Match Starting in 30 Minutes!",
            description=f"{game_emoji} **{self.team_a} vs {self.team_b}**",
            color=0xff6b35,  # Orange color for urgency
            timestamp=self.start_time
        )
        
        # Tournament and game info
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
        
        # Start time in German timezone
        embed.add_field(
            name="🕐 Start Time",
            value=match_time_berlin.strftime("%H:%M (GMT+2)"),
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
        
        embed = discord.Embed(
            title=f"⏰ Match Starting in 30 Minutes!",
            description=f"{game_emoji} **{self.team_a} vs {self.team_b}**",
            color=0xff6b35,  # Orange color for urgency
            timestamp=self.start_time
        )
        
        # Tournament and game info
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
        
        # Start time in German timezone
        embed.add_field(
            name="🕐 Start Time",
            value=match_time_berlin.strftime("%H:%M (GMT+2)"),
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
            await interaction.response.send_message("❌ Only administrators can confirm map results.", ephemeral=True)
            return
        
        # Finalize the map completion
        self.tracker._finalize_map_completion()
        await self.esports_cog._update_score_api(self.tracker)
        
        # Update to next map view or final results
        embed = self.tracker.get_embed()
        if self.tracker.is_finished:
            view = None  # No buttons for finished match
        else:
            view = ScoreUpdateView(self.tracker, self.esports_cog)
        
        await interaction.response.edit_message(embed=embed, view=view)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel map confirmation and continue playing"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can modify map results.", ephemeral=True)
            return
        
        # Revert the map completion
        self.tracker._revert_map_completion()
        
        # Return to normal score tracking
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
            
            self.tracker.team_a_score = team_a_rounds
            self.tracker.team_b_score = team_b_rounds
            
            # Update overtime target based on new scores
            self.tracker._update_overtime_target()
            
            # Update API
            await self.esports_cog._update_score_api(self.tracker)
            
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
            await interaction.response.send_message(f"❌ Error updating score: {e}", ephemeral=True)


class ScoreUpdateView(discord.ui.View):
    """View with buttons for updating CS game scores"""
    
    def __init__(self, tracker: CSGameTracker, esports_cog):
        super().__init__(timeout=14400)  # 4 hours timeout
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
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return
            
        map_finished = self.tracker.add_round_team_a()
        await self.esports_cog._update_score_api(self.tracker)
        
        if map_finished:
            # Show confirmation view for map completion
            winning_team = self.tracker.get_winning_team()
            # Temporarily award the map to check who would win
            if winning_team == self.tracker.match.team_a:
                self.tracker.team_a_maps += 1
            
            embed = self.tracker.get_embed()
            view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Update embed with new score
            embed = self.tracker.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def team_b_callback(self, interaction: discord.Interaction):
        """Handle team B round win"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return
            
        map_finished = self.tracker.add_round_team_b()
        await self.esports_cog._update_score_api(self.tracker)
        
        if map_finished:
            # Show confirmation view for map completion
            winning_team = self.tracker.get_winning_team()
            # Temporarily award the map to check who would win
            if winning_team == self.tracker.match.team_b:
                self.tracker.team_b_maps += 1
            
            embed = self.tracker.get_embed()
            view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Update embed with new score
            embed = self.tracker.get_embed()
            await interaction.response.edit_message(embed=embed, view=self)
    
    
    async def manual_score_callback(self, interaction: discord.Interaction):
        """Handle manual score input"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can update scores.", ephemeral=True)
            return
        
        modal = ManualScoreModal(self.tracker, self.esports_cog)
        await interaction.response.send_modal(modal)


class EsportsCog(commands.Cog):
    """Cog for monitoring e-sports matches and creating Discord events"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log = logging.getLogger("roaringbot.esports")
        
        # Storage for matches and events
        self.matches: Dict[int, EsportsMatch] = {}
        self.event_to_match: Dict[int, int] = {}  # Discord event ID -> match ID
        self.reminder_to_match: Dict[int, int] = {}  # Reminder message ID -> match ID
        self.summary_message_id: Optional[int] = None  # Latest summary message ID
        self.storage_file = Path("config/esports_data.json")
        
        # CS game tracking
        self.active_cs_games: Dict[int, CSGameTracker] = {}  # match ID -> tracker
        self.monitored_matches: Set[int] = set()  # Matches currently being monitored for start time
        
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
                
                # Load summary message ID
                self.summary_message_id = data.get("summary_message_id")
                
                # Load monitored matches
                self.monitored_matches = set(data.get("monitored_matches", []))
                
                self.log.info(f"Loaded {len(self.event_to_match)} event mappings and {len(self.monitored_matches)} monitored matches")
        except Exception as e:
            self.log.error(f"Error loading esports data: {e}")
    
    def _save_data(self):
        """Save current match and event data"""
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                "event_to_match": self.event_to_match,
                "reminder_to_match": self.reminder_to_match,
                "summary_message_id": self.summary_message_id,
                "monitored_matches": list(self.monitored_matches),
                "last_update": datetime.now().isoformat()
            }
            
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            self.log.error(f"Error saving esports data: {e}")
    
    async def cog_load(self):
        """Called when the cog is loaded"""
        if config.esports_enabled:
            self.match_monitor.start()
            self.weekly_summary.start()
            self.log.info("Started e-sports monitoring tasks")
    
    def cog_unload(self):
        """Called when the cog is unloaded"""
        self.match_monitor.cancel()
        self.weekly_summary.cancel()
        self._save_data()
        self.log.info("Stopped e-sports monitoring tasks")
    
    @tasks.loop(minutes=15)  # Default, will be overridden by config
    async def match_monitor(self):
        """Periodically poll the API for match updates"""
        try:
            self.log.debug("Polling e-sports API for updates")
            
            # Fetch matches from API
            session = await http_client.get_session()
            async with session.get(config.esports_api_url) as response:
                if response.status != 200:
                    self.log.error(f"API request failed with status {response.status}")
                    return
                
                try:
                    data = await response.json()
                except Exception as e:
                    self.log.error(f"Failed to parse JSON response: {e}")
                    return
                
                # Check if data is None or not a dictionary
                if data is None:
                    self.log.error("API returned None response")
                    return
                
                if not isinstance(data, dict):
                    self.log.error(f"API returned unexpected data type: {type(data)}")
                    return
                
                matches_data = data.get("results", [])
                
                # Ensure matches_data is a list
                if matches_data is None:
                    self.log.warning("API results field is None, using empty list")
                    matches_data = []
                elif not isinstance(matches_data, list):
                    self.log.error(f"API results field is not a list: {type(matches_data)}")
                    return
            
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
            
        except Exception as e:
            self.log.error(f"Error in match monitoring: {e}")
    
    @match_monitor.before_loop
    async def before_match_monitor(self):
        """Wait for bot to be ready and set correct interval"""
        await self.bot.wait_until_ready()
        
        # Update loop interval from config
        self.match_monitor.change_interval(minutes=config.esports_poll_interval_minutes)
        self.log.info(f"Set match monitoring interval to {config.esports_poll_interval_minutes} minutes")
    
    @tasks.loop(time=time(hour=19, minute=0))  # 8 PM German time (CET) = 7 PM UTC  
    async def weekly_summary(self):
        """Send weekly summary every Sunday at 8 PM German time"""
        try:
            # Check if today is Sunday in German timezone
            now_berlin = datetime.now(self.germany_tz)
            if now_berlin.weekday() != 6:  # 6 = Sunday
                return
            
            if not config.esports_summary_channel_id:
                self.log.warning("No summary channel configured, skipping weekly summary")
                return
            
            channel = self.bot.get_channel(config.esports_summary_channel_id)
            if not channel:
                self.log.error(f"Summary channel {config.esports_summary_channel_id} not found")
                return
            
            await self._send_weekly_summary(channel)
            
        except Exception as e:
            self.log.error(f"Error in weekly summary: {e}")
    
    @weekly_summary.before_loop
    async def before_weekly_summary(self):
        """Wait for bot to be ready before starting weekly summary"""
        await self.bot.wait_until_ready()
    
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
            if match_id not in self.matches:
                # New match - check if event already exists before creating
                if not match.cancelled and not match.discord_event_id:
                    await self._create_discord_event(match)
            else:
                # Existing match - check for updates
                old_match = self.matches[match_id]
                if not match.cancelled and old_match.cancelled:
                    # Match was uncancelled - only create event if one doesn't exist
                    if not match.discord_event_id:
                        await self._create_discord_event(match)
                elif not match.cancelled and self._match_needs_update(old_match, match):
                    # Match details changed
                    await self._update_discord_event(match)
        
        # Update our local cache
        self.matches = current_matches
        self._save_data()
        
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
                self.log.warning(f"No Discord event ID for match {match.id}")
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
                self.log.warning(f"Discord event {match.discord_event_id} not found for match {match.id}")
                # Remove invalid mapping
                if match.discord_event_id in self.event_to_match:
                    del self.event_to_match[match.discord_event_id]
                match.discord_event_id = None
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
            
            # Update the event
            if entity_type == discord.EntityType.voice and voice_channel:
                await event.edit(
                    name=match.event_name,
                    description=match.event_description,
                    start_time=match.start_time,
                    end_time=end_time,
                    entity_type=entity_type,
                    channel=voice_channel
                )
            else:
                await event.edit(
                    name=match.event_name,
                    description=match.event_description,
                    start_time=match.start_time,
                    end_time=end_time,
                    entity_type=discord.EntityType.external,
                    location="wannspieltbig.de"
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
            
            if not upcoming_matches:
                embed.add_field(
                    name="No Matches Scheduled",
                    value="No matches are scheduled for the upcoming week.",
                    inline=False
                )
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
                
                # Add fields for each day
                for day, day_matches in matches_by_day.items():
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
                    
                    embed.add_field(
                        name=f"{day}",
                        value="\n".join(match_lines),
                        inline=False
                    )
            
            embed.set_footer(text="wannspieltbig.de")
            
            # Send the new summary with big.png thumbnail
            file = discord.File("big.png", filename="big.png")
            message = await channel.send(file=file, embed=embed)
            self.summary_message_id = message.id
            self._save_data()  # Save the new message ID
            
            self.log.info(f"Sent weekly summary to channel {channel.id}, message ID: {message.id}")
            
        except Exception as e:
            self.log.error(f"Error sending weekly summary: {e}")
    
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
            
            if not upcoming_matches:
                embed.add_field(
                    name="No Matches Scheduled",
                    value="No matches are scheduled for this week.",
                    inline=False
                )
            else:
                # Group matches by day
                matches_by_day = {}
                for match in upcoming_matches:
                    match_time_berlin = match.start_time.astimezone(self.germany_tz)
                    day_key = match_time_berlin.strftime("%A, %B %d")
                    
                    if day_key not in matches_by_day:
                        matches_by_day[day_key] = []
                    matches_by_day[day_key].append((match, match_time_berlin))
                
                # Add fields for each day
                for day, day_matches in matches_by_day.items():
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
                    
                    embed.add_field(
                        name=f"{day}",
                        value="\n".join(match_lines),
                        inline=False
                    )
            
            embed.set_footer(text="wannspieltbig.de")
            
            # Update the existing message
            message = await channel.fetch_message(self.summary_message_id)
            file = discord.File("big.png", filename="big.png")
            await message.edit(embed=embed, attachments=[file])
            
            self.log.info(f"Updated weekly summary message {self.summary_message_id}")
            
        except discord.NotFound:
            self.log.warning(f"Summary message {self.summary_message_id} not found, will create new one")
            self.summary_message_id = None
            await self._send_weekly_summary(channel)
        except Exception as e:
            self.log.error(f"Error updating existing summary: {e}")
    
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
            
            session = await http_client.get_session()
            headers = await self._get_auth_headers()
            
            # Prepare the update data
            update_data = {
                "map_nr": tracker.current_map,
                "rounds_won_team_a": tracker.team_a_score,
                "rounds_won_team_b": tracker.team_b_score
            }
            
            # Update the score via API
            url = f"https://wannspieltbig.de/api/matchmap_update/{tracker.current_map_id}/"
            async with session.put(url, json=update_data, headers=headers) as response:
                if response.status in [200, 204]:
                    self.log.info(f"Successfully updated scores for match {tracker.match.id}, map {tracker.current_map}")
                else:
                    self.log.error(f"Failed to update scores: HTTP {response.status}")
                    error_text = await response.text()
                    self.log.error(f"API response: {error_text}")
                    
        except Exception as e:
            self.log.error(f"Error updating scores for match {tracker.match.id}: {e}")
    
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
                # Clean up invalid mapping
                del self.event_to_match[event_id]
                continue
            
            # Check if event should be started
            if (event.status == discord.EventStatus.scheduled and 
                match.start_time <= now):
                try:
                    await event.start()
                    self.log.info(f"Started Discord event {event_id} for match {match_id}: {match.event_name}")
                except Exception as e:
                    self.log.error(f"Failed to start event {event_id}: {e}")
            
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
                match.start_time > now):  # Match hasn't started yet
                
                # Check if match is starting within 30-35 minutes (5-minute window for polling)
                time_to_start = (match.start_time - now).total_seconds()
                if 1800 <= time_to_start <= 2100:  # 30-35 minutes
                    await self._send_match_reminder(match, channel)
    
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
                    # Update the Discord Event field with the actual link
                    for i, field in enumerate(embed.fields):
                        if field.name == "📅 Discord Event":
                            embed.set_field_at(
                                i, 
                                name="📅 Discord Event", 
                                value=f"[Join Event]({event_url})",
                                inline=False
                            )
                            break
            
            # Send the message
            message = await channel.send(content=mention_text, embed=embed)
            
            # Store the reminder message ID
            match.reminder_message_id = message.id
            self.reminder_to_match[message.id] = match.id
            self._save_data()
            
            self.log.info(f"Sent 30-minute reminder for match {match.id}: {match.event_name}")
            
        except Exception as e:
            self.log.error(f"Error sending reminder for match {match.id}: {e}")
    
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
            try:
                message = await channel.fetch_message(reminder_id)
                await message.delete()
                self.log.info(f"Deleted reminder message {reminder_id}")
            except discord.NotFound:
                self.log.debug(f"Reminder message {reminder_id} already deleted")
            except Exception as e:
                self.log.warning(f"Failed to delete reminder message {reminder_id}: {e}")
            
            # Clean up mappings
            match_id = self.reminder_to_match.get(reminder_id)
            if match_id and match_id in self.matches:
                self.matches[match_id].reminder_message_id = None
            del self.reminder_to_match[reminder_id]
        
        if reminders_to_delete:
            self._save_data()
    
    async def _cleanup_match_reminder(self, match: EsportsMatch):
        """Clean up reminder message for a specific match"""
        if not match.reminder_message_id or not config.esports_summary_channel_id:
            return
            
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
        summary_status = "🟢 Running" if not self.weekly_summary.is_being_cancelled() else "🔴 Stopped"
        
        embed.add_field(
            name="⚙️ Tasks",
            value=(
                f"**Match Monitor:** {monitor_status}\n"
                f"**Weekly Summary:** {summary_status}"
            ),
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
        
        # Get upcoming CS matches
        now = datetime.now(timezone.utc)
        upcoming_cs_matches = [
            match for match in self.matches.values()
            if (match.game == "cs" and 
                not match.cancelled and 
                match.start_time > now and
                match.id not in self.active_cs_games)
        ]
        
        # Sort by start time
        upcoming_cs_matches.sort(key=lambda m: m.start_time)
        
        if not upcoming_cs_matches:
            await interaction.response.send_message(
                "❌ No upcoming CS matches found that aren't already being tracked.",
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
