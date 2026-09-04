"""E-Sports Match Monitoring Cog for Discord Bot"""

import asyncio
import io
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone, time
from typing import Dict, List, Optional, Set, Tuple
import pytz

import discord
from discord.ext import commands, tasks

from core.config import config
from core.http_client import http_client
from core.status_reporter import status_reporter
from core.versus_image import compose_versus_image
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
        self.team_b_logo_url = (lineup_b or {}).get("team_logo_url")
        
        # wannspieltbig.de's first_map_at consistently carries the German
        # wall-clock digits as its literal value, regardless of what offset
        # suffix accompanies it (verified live: always e.g. "+02:00", but the
        # digits are what matter - confirmed against the site's own displayed
        # match time). Discard the offset and reinterpret the naive wall-clock
        # as Europe/Berlin; this is robust even if the API's own offset were
        # ever wrong, since it never trusts that offset in the first place.
        germany_tz = pytz.timezone("Europe/Berlin")

        def _parse_wsb_local_time(raw: str) -> datetime:
            naive = datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None)
            return germany_tz.localize(naive).astimezone(timezone.utc)

        self.start_time = _parse_wsb_local_time(match_data["first_map_at"])

        # last_map_end is different: verified live, it always comes back with
        # a genuine "Z" (real UTC) suffix, and is a coarse scheduling estimate
        # (~start + 1h per best-of map), not a mislabeled local time. Trust
        # its offset directly instead of reinterpreting the digits as Berlin
        # local time - doing the latter (as a previous fix mistakenly did)
        # shifts it ~2h earlier, making it look like it ends before it starts.
        raw = match_data["last_map_end"]
        raw_end_time = datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc) if raw else None
        # Still keep a sanity floor in case the API ever sends genuinely bad
        # data - the reliable finished-detection is a match disappearing from
        # the API response (see _handle_match_finished), not this field.
        MIN_MATCH_DURATION = timedelta(minutes=15)
        self.end_time = (
            raw_end_time
            if raw_end_time and raw_end_time >= self.start_time + MIN_MATCH_DURATION
            else None
        )
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
        self.ping_message_id: Optional[int] = None  # Summary-channel ping message (CS large-role workaround)
        self.ping_text_message_id: Optional[int] = None  # Plain-text role ping preceding the CV2 ping card
    
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
        
        hltv_link = f" • 🔗 [HLTV]({self.hltv_url})" if self.game == "cs" and self.hltv_url else ""
        return (
            f"🏆  **{self.tournament_name}**\n\n"
            f"{game_emoji}  {game_name} - BO{self.bestof}\n\n"
            f"[wannspieltbig]({self.detail_url}){hltv_link}\n\n"
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
    
    # Rendering of the reminder message lives in build_reminder_view (CV2).


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
        # Display-only history of completed maps for the score message's map
        # table: [{"map": 1, "name": "Mirage", "score": "13:9", "winner": "BIG"}].
        # Not persisted — the 30s livescore sync rebuilds it from the API.
        self.map_history: List[dict] = []

    def map_name(self, map_nr: int) -> Optional[str]:
        """Map name from the stored matchmaps data, if already known."""
        idx = map_nr - 1
        if 0 <= idx < len(self.match.matchmaps):
            played = (self.match.matchmaps[idx] or {}).get("played_map")
            if played:
                return played.get("name")
        return None
        
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
        winner = self.get_winning_team()
        if winner and not any(h["map"] == self.current_map for h in self.map_history):
            self.map_history.append({
                "map": self.current_map,
                "name": self.map_name(self.current_map),
                "score": f"{self.team_a_score}:{self.team_b_score}",
                "winner": winner,
            })
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
        """Generate Discord event name with live score, e.g. 'BIG vs MIBR - 3:4 (1:0)'"""
        score = "bra71l" if (self.team_a_score, self.team_b_score) == (7, 1) else f"{self.team_a_score}:{self.team_b_score}"
        return f"{self.match.team_a} vs {self.match.team_b} - {score} ({self.team_a_maps}:{self.team_b_maps})"

CS_EMOTE = "<:cs:1416235161594499092>"
GAME_EMOJI = {"cs": CS_EMOTE, "lol": "<:lol:1416235138307854416>", "tm": "🏎️"}


EVENT_COVER_H = 640  # 1600×640 = 2.5:1 — Discord event header shows covers at ~2.5:1 (800×320)
EVENT_COVER_VERSION = 4  # bump when the cover composition changes → one-time re-upload of all scheduled events

def build_reminder_view(match: "EsportsMatch", guild_id: Optional[int], mention: Optional[str] = None,
                        versus: bool = False) -> discord.ui.LayoutView:
    """CV2 reminder message: title with live countdown, link buttons
    (Voice → Discord event / wannspieltbig). With versus=True the gallery
    shows the composed 2:1 both-logos image (attachment://versus.jpg),
    otherwise the club logo (attachment://big.png) plus the opponent's
    logo URL as separate tiles."""
    unix_ts = int(match.start_time.timestamp())
    game_emoji = GAME_EMOJI.get(match.game, "🎮")

    container = discord.ui.Container(accent_colour=discord.Colour(0xFF6B35))

    gallery = discord.ui.MediaGallery()
    if versus:
        gallery.add_item(media="attachment://versus.jpg")
    else:
        gallery.add_item(media="attachment://big.png")
        if match.team_b_logo_url:
            gallery.add_item(media=match.team_b_logo_url)
    container.add_item(gallery)

    container.add_item(discord.ui.TextDisplay(
        f"## {match.team_a} vs {match.team_b} — <t:{unix_ts}:R>\n"
        f"-# {game_emoji} {match.tournament_name} · Best of {match.bestof} · <t:{unix_ts}:t> Uhr"
    ))
    if mention:
        container.add_item(discord.ui.TextDisplay(mention))

    row = discord.ui.ActionRow()
    if match.discord_event_id and guild_id:
        row.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label="Voice",
            url=f"https://discord.com/events/{guild_id}/{match.discord_event_id}",
        ))
    row.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="wannspieltbig", url=match.detail_url))
    container.add_item(row)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def build_cs_ping_view(match: "EsportsMatch", thread_id: int, guild_id: int,
                       versus: bool = False) -> discord.ui.LayoutView:
    """CV2 summary-channel ping message: same layout as the thread reminder
    but with a single "Match Thread" link button instead of Voice+wannspieltbig.
    The role ping goes in message.content, not inside the CV2 container."""
    unix_ts = int(match.start_time.timestamp())
    game_emoji = GAME_EMOJI.get(match.game, "🎮")

    container = discord.ui.Container(accent_colour=discord.Colour(0xFF6B35))

    gallery = discord.ui.MediaGallery()
    if versus:
        gallery.add_item(media="attachment://versus.jpg")
    else:
        gallery.add_item(media="attachment://big.png")
        if match.team_b_logo_url:
            gallery.add_item(media=match.team_b_logo_url)
    container.add_item(gallery)

    container.add_item(discord.ui.TextDisplay(
        f"## {match.team_a} vs {match.team_b} — <t:{unix_ts}:R>\n"
        f"-# {game_emoji} {match.tournament_name} · Best of {match.bestof} · <t:{unix_ts}:t> Uhr"
    ))

    row = discord.ui.ActionRow()
    row.add_item(discord.ui.Button(
        style=discord.ButtonStyle.link,
        label="Match Thread",
        url=f"https://discord.com/channels/{guild_id}/{thread_id}",
    ))
    container.add_item(row)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def build_weekly_view(upcoming_matches: List["EsportsMatch"], week_start, week_end,
                      guild: Optional[discord.Guild], germany_tz,
                      extra_row: Optional[discord.ui.ActionRow] = None) -> discord.ui.LayoutView:
    """CV2 weekly summary: header section with the (square-padded) BIG logo,
    one block per day with event-linked match lines. Expects big_square.png
    to be attached to the message."""
    header = (
        f"## This Week\n"
        f"### {week_start.strftime('%B %d')} - {(week_end - timedelta(days=1)).strftime('%B %d')}\n"
        f"-# Powered by [wannspieltbig.de](https://wannspieltbig.de)"
    )

    container = discord.ui.Container(accent_colour=discord.Colour(0x00FF88))
    container.add_item(discord.ui.Section(
        discord.ui.TextDisplay(header),
        accessory=discord.ui.Thumbnail(media="attachment://big_square.png"),
    ))
    container.add_item(discord.ui.Separator())

    if not upcoming_matches:
        container.add_item(discord.ui.TextDisplay(
            "**No Matches Scheduled**\nNo matches are scheduled for this week."))
    else:
        matches_by_day: Dict[str, List[str]] = {}
        for match in upcoming_matches:
            match_time = match.start_time.astimezone(germany_tz)
            day_key = match_time.strftime("%A, %B %d")
            game_emoji = GAME_EMOJI.get(match.game, "🎮")
            label = f"{match_time.strftime('%H:%M')} - {match.team_a} vs {match.team_b}"
            if match.discord_event_id and guild:
                event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                line = f"{game_emoji} **[{label}]({event_url})**"
            else:
                line = f"{game_emoji} **{label}**"
            matches_by_day.setdefault(day_key, []).append(line)

        days = list(matches_by_day.items())
        for i, (day, lines) in enumerate(days):
            container.add_item(discord.ui.TextDisplay(f"### {day}\n" + "\n".join(lines)))
            if i < len(days) - 1:
                container.add_item(discord.ui.Separator())

    if extra_row is not None:
        container.add_item(discord.ui.Separator())
        container.add_item(extra_row)

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _score_map_table(tracker: "CSGameTracker") -> str:
    """Monospace map table: 'Map 1  Mirage   13 : 9   ✓ BIG' per best-of slot."""
    history = {h["map"]: h for h in tracker.map_history}
    names = []
    for nr in range(1, tracker.match.bestof + 1):
        names.append((history.get(nr) or {}).get("name") or tracker.map_name(nr) or "TBA")
    name_width = max([len(n) for n in names] + [7])

    rows = []
    for nr in range(1, tracker.match.bestof + 1):
        h = history.get(nr)
        if h:
            a, b = h["score"].split(":")
            score = f"{int(a):>2} : {int(b):<2}"
            status = f"✓ {h['winner']}"
        elif not tracker.is_finished and nr == tracker.current_map:
            score = f"{tracker.team_a_score:>2} : {tracker.team_b_score:<2}"
            status = "● live"
            if tracker.overtime_target > 13:
                status += f" (first to {tracker.overtime_target})"
        else:
            score = " – : – "
            status = ""
        rows.append(f"Map {nr}  {names[nr - 1]:<{name_width}}  {score}  {status}".rstrip())
    return "\n".join(rows)


def build_score_container(tracker: "CSGameTracker") -> discord.ui.Container:
    """Components-V2 rendering of the score message (cosmetic replacement for
    the old embed — same information, same update flow)."""
    m = tracker.match
    header = f"-# {CS_EMOTE} {m.tournament_name} · Best of {m.bestof} · [wannspieltbig.de]({m.detail_url})"
    if m.hltv_url:
        header += f" · [HLTV]({m.hltv_url})"

    if tracker.is_finished:
        winner = m.team_a if tracker.team_a_maps > tracker.team_b_maps else m.team_b
        title = f"## 🏆 {winner} wins!  ·  Maps {tracker.team_a_maps} : {tracker.team_b_maps}"
    else:
        title = f"## {m.team_a}  {tracker.team_a_score} : {tracker.team_b_score}  {m.team_b}"

    container = discord.ui.Container(accent_colour=discord.Colour(0x57F287))
    container.add_item(discord.ui.TextDisplay(header))
    container.add_item(discord.ui.TextDisplay(title))
    container.add_item(discord.ui.TextDisplay(f"```\n{_score_map_table(tracker)}\n```"))
    return container


class MapConfirmationView(discord.ui.LayoutView):
    """Score message with confirm/cancel buttons for map completion (CV2)"""

    def __init__(self, tracker: CSGameTracker, esports_cog, winning_team: str):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.tracker = tracker
        self.esports_cog = esports_cog
        self.winning_team = winning_team

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

        container = build_score_container(tracker)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(self.confirm_button, self.cancel_button))
        self.add_item(container)

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

            await interaction.response.edit_message(view=ScoreUpdateView(self.tracker, self.esports_cog))
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

        await interaction.response.edit_message(view=ScoreUpdateView(self.tracker, self.esports_cog))


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

                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(view=view)

                # Send confirmation as followup
                await interaction.followup.send(
                    f"✅ Score updated: {old_team_a_score}-{old_team_b_score} → {team_a_rounds}-{team_b_rounds}\n🏆 {winning_team} reached winning score!",
                    ephemeral=True
                )
            else:
                # Update the message with normal view
                view = ScoreUpdateView(self.tracker, self.esports_cog)
                await interaction.response.edit_message(view=view)

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


class ScoreUpdateView(discord.ui.LayoutView):
    """The score message itself (CV2 container) plus round-update buttons.
    When the tracker is finished, it renders the winner state without buttons."""

    def __init__(self, tracker: CSGameTracker, esports_cog):
        super().__init__(timeout=21600)  # 6 hours timeout
        self.tracker = tracker
        self.esports_cog = esports_cog

        container = build_score_container(tracker)

        if not tracker.is_finished:
            self.team_a_button = discord.ui.Button(
                label=f"{tracker.match.team_a} won round",
                style=discord.ButtonStyle.success,
                custom_id=f"team_a_{tracker.match.id}"
            )
            self.team_b_button = discord.ui.Button(
                label=f"{tracker.match.team_b} won round",
                style=discord.ButtonStyle.danger,
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

            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(self.team_a_button, self.team_b_button, self.manual_score_button))
        else:
            container.add_item(discord.ui.TextDisplay("-# Match finished"))

        self.add_item(container)

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
                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(view=view)
            else:
                await interaction.response.edit_message(view=ScoreUpdateView(self.tracker, self.esports_cog))
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
                view = MapConfirmationView(self.tracker, self.esports_cog, winning_team)
                await interaction.response.edit_message(view=view)
            else:
                await interaction.response.edit_message(view=ScoreUpdateView(self.tracker, self.esports_cog))
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
        self.ping_to_match: Dict[int, int] = {}  # Ping message ID -> match ID (CS large-role workaround)
        self.plain_ping_to_match: Dict[int, int] = {}  # Plain-text role ping message ID -> match ID
        self.event_start_failures: Dict[int, int] = {}  # event ID -> consecutive failure count
        self._first_poll_done: bool = False  # set after first successful poll post-startup
        self._versus_cache: Dict[int, Tuple[str, bytes]] = {}  # match ID -> (logo URL, composed PNG)
        self.event_not_found_count: Dict[int, int] = {}  # event ID -> consecutive NotFound count
        self._event_status_by_match: Dict[int, discord.EventStatus] = {}  # match ID -> last observed Discord event status (this poll)
        self.summary_message_id: Optional[int] = None  # Latest summary message ID
        self._event_cover_version: int = 0  # persisted; reconciled covers re-uploaded when < EVENT_COVER_VERSION

        # CS game tracking
        self.active_cs_games: Dict[int, CSGameTracker] = {}  # match ID -> tracker
        self.monitored_matches: Set[int] = set()  # Matches currently being monitored for start time
        self._pending_tracker_restore: Dict[int, dict] = {}  # Loaded from DB, applied after first API poll
        self._livescore_finished_ids: Set[int] = set()  # Match IDs finished via livescore sync; skip health checks until they leave the API
        self._whatsapp_ping_sent: Dict[int, Optional[str]] = {}  # match_id → start_time (ISO) the ping was sent for; None for legacy entries

        # German timezone for weekly summary scheduling
        self.germany_tz = pytz.timezone("Europe/Berlin")

        # Persisted data is loaded in cog_load (needs the DB connection, which
        # isn't ready yet during __init__)

        # Start polling if enabled
        if config.esports_enabled:
            self.log.info("E-Sports monitoring enabled")
        else:
            self.log.info("E-Sports monitoring disabled")

    async def _load_data(self):
        """Load persisted match and event data from Postgres"""
        try:
            data = await self.bot.db.esports.load_all()

            self.event_to_match = data["event_to_match"]
            self.reminder_to_match = data["reminder_to_match"]
            self.thread_to_match = data["thread_to_match"]
            self.ping_to_match = data.get("ping_to_match", {})
            self.plain_ping_to_match = data.get("plain_ping_to_match", {})
            self.summary_message_id = data["summary_message_id"]
            self.monitored_matches = set(data["monitored_matches"])
            self.known_match_ids = set(data["known_match_ids"])
            self._livescore_finished_ids = set(data.get("livescore_finished_ids", []))
            _wa = data.get("whatsapp_ping_sent", [])
            if isinstance(_wa, dict):
                self._whatsapp_ping_sent = {int(k): v for k, v in _wa.items()}
            else:
                self._whatsapp_ping_sent = {int(x): None for x in _wa}
            self._event_cover_version = data.get("event_cover_version", 0)
            self._pending_tracker_restore = {
                int(k): v for k, v in data["active_cs_trackers"].items()
            }

            self.log.info(f"Loaded {len(self.event_to_match)} event mappings and {len(self.monitored_matches)} monitored matches")
            if self._pending_tracker_restore:
                self.log.info(f"Pending CS tracker restore for {len(self._pending_tracker_restore)} match(es)")
        except Exception as e:
            self.log.error(f"Error loading esports data: {e}")

    async def _save_data(self):
        """Bulk-resync current match and event data to Postgres"""
        try:
            active_cs_trackers = {
                match_id: {
                    "message_id": tracker.message_id,
                    "current_map": tracker.current_map,
                    "team_a_score": tracker.team_a_score,
                    "team_b_score": tracker.team_b_score,
                    "team_a_maps": tracker.team_a_maps,
                    "team_b_maps": tracker.team_b_maps,
                    "overtime_target": tracker.overtime_target,
                    "match_maps": tracker.match_maps,
                }
                for match_id, tracker in self.active_cs_games.items()
                if not tracker.is_finished and tracker.message_id
            }

            await self.bot.db.esports.save_all(
                event_to_match=self.event_to_match,
                reminder_to_match=self.reminder_to_match,
                thread_to_match=self.thread_to_match,
                ping_to_match=self.ping_to_match,
                plain_ping_to_match=self.plain_ping_to_match,
                summary_message_id=self.summary_message_id,
                monitored_matches=self.monitored_matches,
                known_match_ids=set(self.matches.keys()),
                active_cs_trackers=active_cs_trackers,
                livescore_finished_ids=self._livescore_finished_ids,
                whatsapp_ping_sent=self._whatsapp_ping_sent,
                event_cover_version=self._event_cover_version,
            )
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
        await self._load_data()
        if config.esports_enabled:
            self.match_monitor.start()
            self.live_score_updater.start()
            self.log.info("Started e-sports monitoring tasks")

    async def cog_unload(self):
        """Called when the cog is unloaded"""
        self.match_monitor.cancel()
        self.live_score_updater.cancel()
        await self._save_data()
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
                self.event_not_found_count.pop(eid, None)
                self.log.info(f"Removed stale event_to_match entry on startup: event {eid} -> match {mid}")
            await self._save_data()

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
                await message.edit(view=ScoreUpdateView(tracker, self))
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

                # Restore ping message ID from stored mappings
                for ping_id, stored_match_id in self.ping_to_match.items():
                    if stored_match_id == match.id:
                        match.ping_message_id = ping_id
                        break

                # Restore plain-text role ping ID from stored mappings
                for ping_text_id, stored_match_id in self.plain_ping_to_match.items():
                    if stored_match_id == match.id:
                        match.ping_text_message_id = ping_text_id
                        break

                current_matches[match.id] = match
            
            # Handle new, updated, and cancelled matches
            await self._process_match_updates(current_matches)

            # On the first poll after startup, reconcile existing reminders,
            # ping cards, and thread titles against current match data.
            # Catches changes that happened while the bot was down or crashing,
            # which the normal update-detection path would miss (it needs an
            # old_match to compare against, which doesn't exist on first poll).
            if not self._first_poll_done:
                self._first_poll_done = True
                await self._reconcile_all_reminders(current_matches)
                await self._reconcile_event_covers(current_matches)

            # Check for CS matches starting soon
            await self._check_for_starting_matches()
            
            # Check for Discord events needing status updates
            await self._check_event_status_updates()

            # Clean up duplicate events (orphans from pre-fix transient-error bugs)
            await self._dedup_guild_events()

            # Check for matches needing 30-minute reminders
            await self._check_for_match_reminders()

            # Check for matches needing 45-min WhatsApp pings
            await self._check_for_whatsapp_pings()
            
            # Check for reminder messages that should be cleaned up
            await self._check_for_reminder_cleanup()
            
            self.log.debug(f"Processed {len(current_matches)} matches from API")

            now = datetime.now(timezone.utc)
            week_end = now + timedelta(days=7)
            live_grace = now - timedelta(hours=6)  # assume still running if started recently and no end_time yet

            def _is_current_or_upcoming(m: "EsportsMatch", horizon=None) -> bool:
                if m.cancelled or (horizon is not None and m.start_time > horizon):
                    return False
                if m.end_time is not None:
                    return m.end_time >= now
                return m.start_time >= live_grace

            upcoming = sorted(
                (m for m in self.matches.values() if _is_current_or_upcoming(m, week_end)),
                key=lambda m: m.start_time,
            )
            # All live/upcoming matches, sorted by start time — the dashboard
            # renders the full list with no trimming.
            all_upcoming = sorted(
                (m for m in self.matches.values() if _is_current_or_upcoming(m)),
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
                scheduled_matches=len(upcoming),
                scheduled_discord_events=len([m for m in upcoming if m.discord_event_id]),
                active_cs_trackers=len(self.active_cs_games),
                summary_message_id=self.summary_message_id,
                upcoming_matches=[
                    {
                        "match_id": m.id,
                        "teams": f"{m.team_a} vs. {m.team_b}",
                        "tournament": m.tournament_name,
                        "game": m.game,
                        "start_time": m.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end_time": m.end_time.strftime("%Y-%m-%dT%H:%M:%SZ") if m.end_time else None,
                        "bestof": m.bestof,
                        "detail_url": m.detail_url,
                    }
                    for m in upcoming
                ],
                next_matches=self._compute_next_matches(all_upcoming, now),
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

                # Keep the tracker's map metadata fresh — played_map (the map
                # name) can be filled in after tracking started, and the
                # tracker.match object is a snapshot from tracking start.
                tracker.match.matchmaps = matchmaps

                # Calculate map scores from API data
                team_a_maps = 0
                team_b_maps = 0
                current_map_idx = 0
                api_history = []  # completed maps, feeds the score message's map table

                for i, mm in enumerate(matchmaps):
                    rounds_a = mm.get("rounds_won_team_a", 0) or 0
                    rounds_b = mm.get("rounds_won_team_b", 0) or 0

                    # A map is finished only when the leader has reached the
                    # current target score: 13 in regulation, then 16, 19, 22,…
                    # in overtime. The target escalates while the trailing team
                    # keeps within one round of it (12:12 -> first to 16,
                    # 15:15 -> first to 19, …) — an intermediate OT score like
                    # 17:15 is NOT a finished map, even with a 2-round lead.
                    leader = max(rounds_a, rounds_b)
                    trailer = min(rounds_a, rounds_b)
                    target = 13
                    while trailer >= target - 1:
                        target += 3
                    map_finished = leader >= target

                    if not map_finished:
                        current_map_idx = i
                        break
                    else:
                        if rounds_a > rounds_b:
                            team_a_maps += 1
                        else:
                            team_b_maps += 1
                        api_history.append({
                            "map": i + 1,
                            "name": (mm.get("played_map") or {}).get("name"),
                            "score": f"{rounds_a}:{rounds_b}",
                            "winner": tracker.match.team_a if rounds_a > rounds_b else tracker.match.team_b,
                        })
                        current_map_idx = i + 1

                # API is the source of truth for completed maps (also restores
                # the display-only history after a bot restart)
                if api_history:
                    tracker.map_history = api_history

                scores_changed = False

                # Update map scores
                if team_a_maps != tracker.team_a_maps or team_b_maps != tracker.team_b_maps:
                    self.log.info(f"Match {match_id}: Map score updated from API - "
                                 f"{tracker.team_a_maps}-{tracker.team_b_maps} -> {team_a_maps}-{team_b_maps}")
                    tracker.team_a_maps = team_a_maps
                    tracker.team_b_maps = team_b_maps
                    status_reporter.bump_counter("esports", "score_updates_from_api")
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
                        status_reporter.bump_counter("esports", "score_updates_from_api")
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
                                # Winner rendering, no buttons (is_finished=True)
                                await message.edit(view=ScoreUpdateView(tracker, self))
                                self.log.info(f"Match {match_id} finished via API sync - "
                                             f"{tracker.match.team_a} {tracker.team_a_maps}-{tracker.team_b_maps} {tracker.match.team_b}")

                                # End the Discord event
                                await self._end_match_event(tracker.match)

                                # Remove from active games and silence health checks
                                # until the match leaves the API (the event and
                                # tracker are already gone, so "no_discord_event"
                                # and "tracking_missing" would fire every poll).
                                del self.active_cs_games[match_id]
                                self._livescore_finished_ids.add(match_id)
                            else:
                                await message.edit(view=ScoreUpdateView(tracker, self))
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
                        "map_name": t.map_name(t.current_map),
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

    def _record_match_event(self, match: "EsportsMatch", event_type: str):
        """Append a match lifecycle event for the dashboard's E-Sports timeline (see DATA_INTERFACE.md)."""
        status_reporter.record_event(
            "esports",
            "match_events",
            {
                "type": event_type,
                "match_id": match.id,
                "teams": match.event_name,
                "tournament": match.tournament_name,
                "game": match.game,
                "start_time": match.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bestof": match.bestof,
                "detail_url": match.detail_url,
            },
            max_len=200,
        )

    async def _process_match_updates(self, current_matches: Dict[int, EsportsMatch]):
        """Process match updates and manage Discord events"""

        # A match previously finished via livescore that reappears with a future
        # start_time means wannspieltbig.de reused the match_id for a new fixture
        # (e.g. "BIG vs. TBA" -> "BIG vs. magic"). Clear the stale "finished" flag
        # so the new fixture is tracked, health-checked and shown in the weekly
        # overview again instead of being silently filtered out.
        now = datetime.now(timezone.utc)
        for match_id, match in current_matches.items():
            if match_id in self._livescore_finished_ids and match.start_time > now:
                self._livescore_finished_ids.discard(match_id)
                self.log.info(
                    f"Match {match_id} ({match.event_name}) previously finished via livescore "
                    f"reappeared as an upcoming fixture — cleared stale finished flag"
                )

        # Same match-id-reuse hazard for the 45-min WhatsApp ping: the flag was
        # set for an OLD fixture's start time (e.g. "BIG vs. TBA" -> "BIG vs.
        # magic"). If wannspieltbig.de reuses the match_id for a NEW fixture with
        # a different future start_time, clear the stale "already pinged" flag so
        # the new fixture gets its own ping. Legacy entries (None, from the old
        # plain-IDs format) self-heal on the first poll: cleared whenever the
        # match is still upcoming, then re-stored with the real start time.
        for match_id, match in current_matches.items():
            if (not match.cancelled and match.start_time > now
                    and match_id in self._whatsapp_ping_sent):
                sent_for_start = self._whatsapp_ping_sent[match_id]
                if sent_for_start is None or sent_for_start != match.start_time.isoformat():
                    del self._whatsapp_ping_sent[match_id]
                    self.log.info(
                        f"Match {match_id} ({match.event_name}) previously WhatsApp-pinged "
                        f"for a different start time — cleared stale ping flag"
                    )

        # Same match-id-reuse hazard for the "monitored for tracking start" flag.
        # A match that finished via livescore keeps its monitored_matches entry
        # until it leaves the API (only _handle_match_finished clears it). If
        # wannspieltbig.de reuses the match_id for a NEW upcoming fixture, that
        # stale flag would make _check_for_starting_matches skip it forever —
        # no score tracking, no event-name score updates (Sep-2026: "BIG vs.
        # magic" never got tracked because the flag from the finished "BIG vs.
        # TBA" fixture was still set). Actively tracked matches are in
        # active_cs_games and are left untouched.
        for match_id, match in current_matches.items():
            if (not match.cancelled and match.start_time > now
                    and match_id in self.monitored_matches
                    and match_id not in self.active_cs_games):
                self.monitored_matches.discard(match_id)
                self.log.info(
                    f"Match {match_id} ({match.event_name}) flagged for monitoring "
                    f"without an active tracker — cleared stale monitored flag"
                )

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
                self._record_match_event(match, "created")
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
                    # Match details changed (time, opponent, tournament, etc.)
                    if match.reminder_message_id:
                        # Edit the existing reminder/ping messages and thread title
                        await self._edit_reminder_message(match)
                    # else: no reminder sent yet, will fire normally at 30-min mark
                    await self._update_discord_event(match)
                elif not match.cancelled and not match.discord_event_id and match.start_time > datetime.now(timezone.utc):
                    # Belt-and-suspenders: existing match lost its event (e.g., mapping cleared last cycle)
                    self.log.info(f"Existing match {match_id} ({match.event_name}) has no Discord event — recreating")
                    await self._create_discord_event(match)
        
        # Update our local cache
        self.matches = current_matches
        await self._save_data()

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

    EVENT_START_LEAD = timedelta(minutes=5)
    # Start the event 60 seconds before its scheduled_start_time so our bot
    # deterministically beats Discord's built-in auto-start; otherwise they
    # race and Discord wins when the poll lands a few seconds past the hour.
    EVENT_START_PRE_LEAD = timedelta(seconds=60)

    def _event_target_start(self, match: "EsportsMatch") -> datetime:
        """The Discord event should go live 5 minutes before the real kickoff.
        This is the nominal target used for "is it due yet?" checks — it must
        NOT be clamped to the future, otherwise the auto-start condition
        (target <= now) can never become true.
        """
        return match.start_time - self.EVENT_START_LEAD

    def _event_start_trigger(self, match: "EsportsMatch") -> datetime:
        """The instant at which our code should try to start the event.
        This is 60 seconds before the nominal target (and therefore 60 seconds
        before the event's visible scheduled_start_time on Discord), so we
        always beat Discord's own auto-start."""
        return self._event_target_start(match) - self.EVENT_START_PRE_LEAD

    def _event_api_start_time(self, match: "EsportsMatch") -> datetime:
        """The start_time to send to Discord when creating/editing an event:
        the nominal target clamped to a few seconds in the future, because
        Discord's API rejects a past/immediate start_time (relevant for
        matches discovered with less than 5 minutes' notice).
        """
        floor = datetime.now(timezone.utc) + timedelta(seconds=30)
        return max(self._event_target_start(match), floor)

    def _event_end_time(self, match: "EsportsMatch") -> datetime:
        """Generous end-time estimate: 90 min per best-of map + 90 min slack.
        This is only a Discord scheduling hint — the real finish is detected
        by the CS live-score loop or the match disappearing from the API."""
        maps = max(match.bestof, 1)
        return match.start_time + timedelta(minutes=90) * maps + timedelta(minutes=90)

    SCHEDULE_RECONCILE_TOLERANCE = timedelta(seconds=60)
    SCHEDULE_RECONCILE_MIN_LEAD = timedelta(minutes=2)

    async def _reconcile_event_schedule(self, event: discord.ScheduledEvent, match: "EsportsMatch", now: datetime):
        """Nudge a still-scheduled event's start/end back onto the current
        formula (start = kickoff − 5 min, generous end) when it has drifted —
        typically legacy events created before a formula change. No-op (and no
        API call) when already aligned, so this is free on every poll once the
        backlog is fixed. Skipped in the last couple of minutes before the
        intended start so it never races the auto-start or fights the
        now+30 s clamp.
        """
        target_start = self._event_target_start(match)  # unclamped intended start
        if target_start <= now + self.SCHEDULE_RECONCILE_MIN_LEAD:
            return
        target_end = self._event_end_time(match)

        start_off = (event.start_time is None or
                     abs(event.start_time - target_start) > self.SCHEDULE_RECONCILE_TOLERANCE)
        end_off = (event.end_time is None or
                   abs(event.end_time - target_end) > self.SCHEDULE_RECONCILE_TOLERANCE)
        if not (start_off or end_off):
            return


        try:
            await event.edit(
                start_time=self._event_api_start_time(match),
                end_time=target_end,
            )
            self.log.info(
                f"Reconciled event {event.id} schedule for match {match.id} "
                f"({match.event_name}): start->{target_start:%Y-%m-%d %H:%M}Z "
                f"end->{target_end:%H:%M}Z"
            )
        except Exception as e:
            self.log.warning(f"Could not reconcile event {event.id} for match {match.id}: {e}")

    def _match_health_issues(self, match: "EsportsMatch", now: datetime) -> List[str]:
        """Detect and log discrepancies between what should exist for an
        upcoming/live match and what actually does — surfaced to the
        dashboard's E-Sports status boxes so problems aren't silent."""
        issues = []

        # Matches already finished by the livescore sync have their
        # event/tracker cleaned up — skip them until they leave the API
        # (at which point _handle_match_finished removes them from the set).
        if match.id in self._livescore_finished_ids:
            return issues

        if not match.discord_event_id:
            issues.append("no_discord_event")

        time_to_start = (match.start_time - now).total_seconds()
        reminder_ok = bool(match.reminder_message_id or match.forum_thread_id)
        # Only flag while the match is still upcoming (0..30 min before kickoff),
        # matching the sender's window in _check_for_match_reminders. Without the
        # lower bound, matches that already started but linger in the API would be
        # flagged forever — the sender never re-reminds a started match.
        if 0 < time_to_start <= 1800 and not reminder_ok:
            issues.append("reminder_missing")

        if match.discord_event_id and now >= self._event_start_trigger(match):
            status = self._event_status_by_match.get(match.id)
            if status == discord.EventStatus.scheduled:
                issues.append("event_not_started")

        if match.game == "cs" and now >= match.start_time - timedelta(minutes=5) and match.id not in self.active_cs_games:
            still_relevant = match.end_time is None or now < match.end_time + timedelta(hours=1)
            if still_relevant:
                issues.append("tracking_missing")

        if issues:
            self.log.error(f"Match {match.id} ({match.event_name}) health issues: {', '.join(issues)}")
        return issues

    def _compute_next_matches(self, matches: List["EsportsMatch"], now: datetime) -> list:
        """Build the dashboard's live/upcoming matches status list, see DATA_INTERFACE.md."""
        result = []
        for m in matches:
            is_live = (m.start_time <= now
                       and (m.end_time is None or m.end_time >= now)
                       and m.id not in self._livescore_finished_ids)
            issues = self._match_health_issues(m, now)

            reminder_at = m.start_time - timedelta(minutes=30)
            reminder_ok = bool(m.reminder_message_id or m.forum_thread_id)

            voice_event_at = self._event_target_start(m)
            event_status = self._event_status_by_match.get(m.id)
            voice_event_ok = event_status is not None and event_status != discord.EventStatus.scheduled

            tracking_at = None
            tracking_ok = None
            if m.game == "cs":
                tracking_at = m.start_time - timedelta(minutes=5)
                tracking_ok = m.id in self.active_cs_games or (
                    m.end_time is not None and now >= m.end_time
                ) or m.id in self._livescore_finished_ids

            live_score = None
            if m.game == "cs" and m.id in self.active_cs_games:
                tracker = self.active_cs_games[m.id]
                live_score = {
                    "map": tracker.current_map,
                    "map_name": tracker.map_name(tracker.current_map),
                    "score": f"{tracker.team_a_score}-{tracker.team_b_score}",
                    "maps": f"{tracker.team_a_maps}-{tracker.team_b_maps}",
                }

            result.append({
                "match_id": m.id,
                "teams": f"{m.team_a} vs. {m.team_b}",
                "tournament": m.tournament_name,
                "game": m.game,
                "start_time": m.start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "detail_url": m.detail_url,
                "is_live": is_live,
                "cleanly_finished": m.id in self._livescore_finished_ids,
                "has_discord_event": bool(m.discord_event_id or m.id in self._livescore_finished_ids),
                "reminder_at": reminder_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reminder_ok": reminder_ok,
                "tracking_at": tracking_at.strftime("%Y-%m-%dT%H:%M:%SZ") if tracking_at else None,
                "tracking_ok": tracking_ok,
                "voice_event_at": voice_event_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "voice_event_ok": voice_event_ok,
                "live_score": live_score,
                "issues": issues,
            })
        return result

    async def _create_discord_event(self, match: EsportsMatch):
        """Create a Discord scheduled event for a match"""
        try:
            # Skip if event already tracked
            if match.discord_event_id:
                self.log.debug(f"Discord event already exists for match {match.id}: {match.discord_event_id}")
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

            # Before creating a new event, scan existing guild events for one
            # with the same name.  If found, adopt it — this heals duplicates
            # created by transient NotFound in _update_discord_event or
            # _check_event_status_updates, and prevents the problem from
            # compounding across restarts.
            # Only adopt when the event's start time is close to our match's
            # target start — otherwise it's a name collision (e.g. two
            # different "BIG vs. TBA" matches on different dates).
            try:
                target_start = self._event_api_start_time(match)
                for ev in await guild.fetch_scheduled_events():
                    if ev.name == match.event_name:
                        if ev.start_time and target_start:
                            delta = abs((ev.start_time - target_start).total_seconds())
                            if delta > 7200:  # more than 2 h apart — different match
                                continue
                        self.log.warning(
                            f"Duplicate event {ev.id} for match {match.id} "
                            f"({match.event_name}) detected — adopting instead of creating"
                        )
                        self.event_to_match[ev.id] = match.id
                        match.discord_event_id = ev.id
                        # Bring the adopted event up to current schedule/cover
                        await self._update_discord_event(match)
                        return
            except Exception as scan_exc:
                self.log.warning(
                    f"Duplicate scan for match {match.id} failed "
                    f"— skipping creation to avoid potential duplicate: {scan_exc}"
                )
                return

            # Only create events for matches that haven't started yet
            if match.start_time <= datetime.now(timezone.utc):
                self.log.debug(f"Skipping event creation for past match {match.id}")
                return
            event_start_time = self._event_api_start_time(match)
            end_time = self._event_end_time(match)
            event_cover_bytes = await self._build_event_cover_media(match)

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
                    start_time=event_start_time,
                    end_time=end_time,
                    entity_type=entity_type,
                    channel=voice_channel,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    **({"image": event_cover_bytes} if event_cover_bytes is not None else {})
                )
            else:
                event = await guild.create_scheduled_event(
                    name=match.event_name,
                    description=match.event_description,
                    start_time=event_start_time,
                    end_time=end_time,
                    entity_type=discord.EntityType.external,
                    location="wannspieltbig.de",
                    privacy_level=discord.PrivacyLevel.guild_only,
                    **({"image": event_cover_bytes} if event_cover_bytes is not None else {})
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
            genuine_not_found = False  # True when we got a real 404
            transient_failure = False  # True when we got a transient error (503, etc.)

            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(match.discord_event_id)
                    except discord.NotFound:
                        self.log.warning(f"Event {match.discord_event_id} not found in configured guild {config.esports_guild_id}")
                        genuine_not_found = True
                    except Exception as e:
                        self.log.warning(f"Error fetching event from configured guild: {e}")
                        transient_failure = True
            else:
                # Search through all guilds (original behavior)
                for g in self.bot.guilds:
                    try:
                        event = await g.fetch_scheduled_event(match.discord_event_id)
                        guild = g
                        break
                    except discord.NotFound:
                        genuine_not_found = True
                        continue
                    except Exception as e:
                        self.log.warning(f"Error fetching event from guild {g.id}: {e}")
                        transient_failure = True
                        continue

            if not event:
                # Transient API errors (503, 522, timeouts) are NOT the event
                # being gone — skip this cycle and retry next poll.
                if transient_failure and not genuine_not_found:
                    self.log.debug(
                        f"Skipping event {match.discord_event_id} for match {match.id} "
                        f"this cycle — fetch failed with transient error"
                    )
                    return

                # Only recreate after 2 consecutive NotFound to guard against
                # transient API errors (same rule as _check_event_status_updates).
                event_id = match.discord_event_id
                count = self.event_not_found_count.get(event_id, 0) + 1
                self.event_not_found_count[event_id] = count
                if count < 2:
                    self.log.warning(
                        f"Discord event {event_id} not found for match {match.id} "
                        f"(attempt {count}/2) — will verify next cycle before recreating"
                    )
                    return
                # Second consecutive NotFound — treat as genuinely gone
                self.event_not_found_count.pop(event_id, None)
                self.log.warning(
                    f"Discord event {event_id} not found for match {match.id} "
                    f"after {count} attempts — recreating"
                )
                # Remove invalid mapping
                if event_id in self.event_to_match:
                    del self.event_to_match[event_id]
                match.discord_event_id = None
                # Recreate so the match still has a Discord presence
                await self._create_discord_event(match)
                return

            # Event was found — reset NotFound counter
            self.event_not_found_count.pop(match.discord_event_id, None)
            
            # Event should go live 5 minutes before the real kickoff; the end
            # time stays relative to the real kickoff (see _event_end_time).
            event_start_time = self._event_api_start_time(match)
            end_time = self._event_end_time(match)

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

            # Refresh cover image (e.g. TBA → real opponent logo)
            event_cover_bytes = await self._build_event_cover_media(match)

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
                await self._save_data()
                await self._create_discord_event(match)
                return

            # Update the event
            cover_kwargs = {"image": event_cover_bytes} if event_cover_bytes is not None else {}
            if entity_type == discord.EntityType.voice and voice_channel:
                if can_update_start_time:
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        start_time=event_start_time,
                        end_time=end_time,
                        entity_type=entity_type,
                        channel=voice_channel,
                        **cover_kwargs
                    )
                else:
                    # Event already started - only update what Discord allows
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        end_time=end_time,
                        **cover_kwargs
                    )
            else:
                if can_update_start_time:
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        start_time=event_start_time,
                        end_time=end_time,
                        entity_type=discord.EntityType.external,
                        location="wannspieltbig.de",
                        **cover_kwargs
                    )
                else:
                    # Event already started - only update what Discord allows
                    await event.edit(
                        name=match.event_name,
                        description=match.event_description,
                        end_time=end_time,
                        **cover_kwargs
                    )

            self.log.info(f"Updated Discord event {event.id} for match {match.id}")
            
        except Exception as e:
            self.log.error(f"Error updating Discord event for match {match.id}: {e}")
    
    async def _handle_match_cancelled(self, match: EsportsMatch):
        """Handle a cancelled match by deleting its Discord event"""
        self._record_match_event(match, "cancelled")
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
        self._record_match_event(match, "finished")
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

            # Remove from livescore-finished set (safe no-op if not present)
            self._livescore_finished_ids.discard(match.id)
            
        except Exception as e:
            self.log.error(f"Error handling finished match {match.id}: {e}")

    def _build_upcoming_ephemeral_view(self, guild: Optional[discord.Guild]) -> Optional[discord.ui.LayoutView]:
        """Build a CV2 ephemeral view matching the weekly overview style for
        all matches after the current week.  Returns None when there are none."""
        now_berlin = datetime.now(timezone.utc).astimezone(self.germany_tz)
        days_since_monday = now_berlin.weekday()
        week_start = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
        week_end_utc = (week_start + timedelta(days=7)).astimezone(timezone.utc)

        future = [
            m for m in self.matches.values()
            if not m.cancelled and m.start_time >= week_end_utc
        ]
        future.sort(key=lambda m: m.start_time)

        if not future:
            return None

        n = len(future)
        container = discord.ui.Container(accent_colour=discord.Colour(0x00FF88))
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                f"## Upcoming Matches\n"
                f"-# {n} match{'es' if n != 1 else ''} after this week"
            ),
            accessory=discord.ui.Thumbnail(media="attachment://big_square.png"),
        ))
        container.add_item(discord.ui.Separator())

        matches_by_day: Dict[str, List[str]] = {}
        for match in future:
            match_time = match.start_time.astimezone(self.germany_tz)
            day_key = match_time.strftime("%A, %B %d")
            game_emoji = GAME_EMOJI.get(match.game, "🎮")
            label = f"{match_time.strftime('%H:%M')} - {match.team_a} vs {match.team_b}"
            if match.discord_event_id and guild:
                event_url = f"https://discord.com/events/{guild.id}/{match.discord_event_id}"
                line = f"{game_emoji} **[{label}]({event_url})**"
            else:
                line = f"{game_emoji} **{label}**"
            matches_by_day.setdefault(day_key, []).append(line)

        days = list(matches_by_day.items())
        for i, (day, lines) in enumerate(days):
            container.add_item(discord.ui.TextDisplay(f"### {day}\n" + "\n".join(lines)))
            if i < len(days) - 1:
                container.add_item(discord.ui.Separator())

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view

    async def _weekly_upcoming_callback(self, interaction: discord.Interaction):
        """Button callback — show all matches after the current week as ephemeral."""
        view = self._build_upcoming_ephemeral_view(interaction.guild)
        if view is None:
            await interaction.response.send_message(
                "No upcoming matches after this week.", ephemeral=True,
            )
            return
        file = discord.File("resources/big_square.png", filename="big_square.png")
        await interaction.response.send_message(file=file, view=view, ephemeral=True)

    def _is_upcoming_or_live(self, m: "EsportsMatch", now: datetime) -> bool:
        """A match is still relevant for the weekly overview when it has not
        finished yet (still upcoming or currently live)."""
        if m.cancelled or m.id in self._livescore_finished_ids:
            return False
        if m.end_time is not None:
            return m.end_time >= now
        return m.start_time >= now - timedelta(hours=6)

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
                if self._is_upcoming_or_live(match, now) and week_start_utc <= match.start_time < week_end_utc
            ]
            
            # Sort by start time
            upcoming_matches.sort(key=lambda m: m.start_time)
            
            # New posts always use the CV2 layout (see build_weekly_view)
            guild = channel.guild or (self.bot.get_guild(config.esports_guild_id) if config.esports_guild_id else None)
            btn_row = discord.ui.ActionRow()
            btn = discord.ui.Button(
                label="later",
                style=discord.ButtonStyle.secondary,
                custom_id="weekly_upcoming_btn",
            )
            btn.callback = self._weekly_upcoming_callback
            btn_row.add_item(btn)
            view = build_weekly_view(upcoming_matches, week_start, week_end, guild, self.germany_tz,
                                     extra_row=btn_row)
            file = discord.File("resources/big_square.png", filename="big_square.png")
            message = await channel.send(file=file, view=view)
            self.summary_message_id = message.id
            await self._save_data()  # Save the new message ID
            
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
                if self._is_upcoming_or_live(match, now) and week_start_utc <= match.start_time < week_end_utc
            ]
            
            # Sort by start time
            upcoming_matches.sort(key=lambda m: m.start_time)

            # Edit the existing CV2 message in place.
            message = await channel.fetch_message(self.summary_message_id)
            btn_row = discord.ui.ActionRow()
            btn = discord.ui.Button(
                label="later",
                style=discord.ButtonStyle.secondary,
                custom_id="weekly_upcoming_btn",
            )
            btn.callback = self._weekly_upcoming_callback
            btn_row.add_item(btn)
            view = build_weekly_view(upcoming_matches, week_start, week_end, channel.guild, self.germany_tz,
                                     extra_row=btn_row)
            await message.edit(view=view, attachments=[discord.File("resources/big_square.png", filename="big_square.png")])
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

            # Prepare the update data
            update_data = {
                "map_nr": tracker.current_map,
                "rounds_won_team_a": tracker.team_a_score,
                "rounds_won_team_b": tracker.team_b_score,
            }
            if played_map_name:
                update_data["played_map_name"] = played_map_name
            else:
                self.log.debug(f"No played_map_name for match {tracker.match.id}, map {tracker.current_map} — sending score update without it")
            
            # Update the score via API
            url = f"https://wannspieltbig.de/api/matchmap_update/{tracker.current_map_id}/"
            response = await http_client.put(url, json=update_data, headers=headers)
            try:
                if response.status in [200, 204]:
                    self.log.info(f"Successfully updated scores for match {tracker.match.id}, map {tracker.current_map}")
                    status_reporter.bump_counter("esports", "score_updates_to_api")
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
            await self._save_data()

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
            await self._save_data()
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
                
                # Fire as close to exactly T-5 minutes as the 1-minute poll allows
                # (same pattern as the 30-minute reminder window above).
                time_to_start = (match.start_time - now).total_seconds()
                if 240 <= time_to_start <= 300:  # 4-5 minutes
                    self.monitored_matches.add(match.id)
                    await self._start_cs_game_tracking(match)
    
    # Sentinel returned by _find_alternative_vc when both VCs are occupied.
    _VC_BLOCKED = object()

    async def _find_alternative_vc(
        self, guild: discord.Guild, current_channel_id: int, match: "EsportsMatch"
    ):
        """Check whether *current_channel_id* already has an active voice event.

        Returns
        -------
        (None, None)
            The current channel is free — no action needed.
        (discord.VoiceChannel, str)
            The current channel is occupied, but the *other* configured VC is
            free.  The tuple contains the channel object and its label
            (``"VC 1"`` / ``"VC 2"``) for updating ``block_voice_channel``.
        (_VC_BLOCKED, None)
            Both voice channels are occupied.  The caller should skip this
            cycle and retry on the next poll.
        """
        # Build the set of voice-channel IDs that currently have an active event.
        try:
            all_events = await guild.fetch_scheduled_events()
        except Exception as exc:
            self.log.warning(
                f"Could not fetch guild events for VC availability check: {exc}"
            )
            return (None, None)  # can't tell — let event.start() decide

        active_channel_ids = {
            e.channel_id
            for e in all_events
            if e.status == discord.EventStatus.active
            and e.entity_type == discord.EntityType.voice
            and e.channel_id is not None
        }

        if current_channel_id not in active_channel_ids:
            return (None, None)  # current VC is free

        # Current VC is occupied — see if the other one is free.
        vc1_id = config.esports_vc1_id
        vc2_id = config.esports_vc2_id
        if current_channel_id == vc1_id:
            other_id, other_label = vc2_id, "VC 2"
        elif current_channel_id == vc2_id:
            other_id, other_label = vc1_id, "VC 1"
        else:
            # The event is on a VC that isn't one of our two configured ones.
            self.log.warning(
                f"Event for match {match.id} is on unknown VC {current_channel_id}"
            )
            return (None, None)

        if other_id is None or other_id in active_channel_ids:
            return (self._VC_BLOCKED, None)

        other_channel = guild.get_channel(other_id)
        if other_channel is None or not isinstance(other_channel, discord.VoiceChannel):
            self.log.warning(f"Configured {other_label} ({other_id}) is not a voice channel")
            return (self._VC_BLOCKED, None)

        return (other_channel, other_label)

    async def _reconcile_event_covers(self, current_matches: dict):
        """After a cover-composition change (EVENT_COVER_VERSION bump), re-upload
        the cover image for every existing scheduled event whose match is still
        tracked.  The normal update path only refreshes the cover when match
        metadata changes, so events with stable metadata would otherwise keep
        the old rendering forever.  Runs once per version bump; no-op afterwards."""
        if self._event_cover_version >= EVENT_COVER_VERSION:
            return

        guild = None
        if config.esports_guild_id:
            guild = self.bot.get_guild(config.esports_guild_id)
        elif self.bot.guilds:
            guild = self.bot.guilds[0]
        if not guild:
            self.log.warning("Cover reconciliation skipped — no guild resolved")
            return

        updated = skipped = failed = 0
        for match in current_matches.values():
            if match.cancelled or not match.discord_event_id:
                continue
            try:
                event = await guild.fetch_scheduled_event(match.discord_event_id)
            except discord.NotFound:
                continue  # handled by the normal event-missing path
            except Exception as e:
                self.log.warning(f"Cover reconcile: fetch event {match.discord_event_id} failed: {e}")
                failed += 1
                continue
            if event.status != discord.EventStatus.scheduled:
                skipped += 1
                continue
            cover_bytes = await self._build_event_cover_media(match)
            if cover_bytes is None:
                skipped += 1
                continue
            try:
                await event.edit(image=cover_bytes)
                updated += 1
            except Exception as e:
                self.log.warning(f"Cover reconcile: edit event {event.id} for match {match.id} failed: {e}")
                failed += 1

        if failed == 0:
            self._event_cover_version = EVENT_COVER_VERSION
            await self._save_data()
        self.log.info(
            f"Event cover reconciliation (v{EVENT_COVER_VERSION}): "
            f"updated {updated}, skipped {skipped}, failed {failed}"
        )

    async def _reconcile_all_reminders(self, current_matches: dict):
        """After a restart, bring all existing reminders/pings/threads in sync
        with current match data.  Without this, changes that happened while the
        bot was down are never detected because the normal update path needs an
        old_match to compare against (which doesn't exist on the first poll)."""
        for match_id, match in current_matches.items():
            if not match.reminder_message_id:
                continue
            if match.cancelled:
                continue
            try:
                await self._edit_reminder_message(match)
            except Exception as e:
                self.log.debug(
                    f"Startup reminder reconciliation for match {match_id} "
                    f"failed (non-fatal): {e}"
                )

    async def _dedup_guild_events(self):
        """Delete duplicate Discord events with the same name and close start times.

        Called once per poll cycle.  Groups all scheduled events by name; when
        multiple events share a name and start within 2 h of each other, the
        one tracked in event_to_match is kept (or the earliest one if none are
        tracked) and the rest are deleted.  This cleans up orphans created
        before the duplicate scan and 2-strike rules were hardened.
        """
        # Find the guild
        guild = None
        if config.esports_guild_id:
            guild = self.bot.get_guild(config.esports_guild_id)
        elif self.bot.guilds:
            guild = self.bot.guilds[0]
        if not guild:
            return

        try:
            events = await guild.fetch_scheduled_events()
        except Exception as e:
            self.log.debug(f"_dedup_guild_events: could not fetch events: {e}")
            return

        # Group by event name
        by_name: dict[str, list[discord.ScheduledEvent]] = {}
        for ev in events:
            by_name.setdefault(ev.name, []).append(ev)

        for name, group in by_name.items():
            if len(group) <= 1:
                continue

            # Sort into clusters whose start times are within 2 h
            group.sort(key=lambda e: e.start_time or datetime.now(timezone.utc))
            clusters: list[list[discord.ScheduledEvent]] = []
            for ev in group:
                placed = False
                for cluster in clusters:
                    ref = cluster[0]
                    if (ev.start_time and ref.start_time and
                            abs((ev.start_time - ref.start_time).total_seconds()) <= 7200):
                        cluster.append(ev)
                        placed = True
                        break
                if not placed:
                    clusters.append([ev])

            for cluster in clusters:
                if len(cluster) <= 1:
                    continue
                # Prefer the event tracked in event_to_match; otherwise keep
                # the earliest one.
                tracked = next((e for e in cluster if e.id in self.event_to_match), None)
                keeper = tracked if tracked else cluster[0]
                for ev in cluster:
                    if ev.id == keeper.id:
                        continue
                    self.log.warning(
                        f"_dedup_guild_events: deleting duplicate event {ev.id} "
                        f"(\"{ev.name}\", {ev.start_time}) — keeping {keeper.id}"
                    )
                    try:
                        await ev.delete()
                    except Exception as del_exc:
                        self.log.warning(
                            f"_dedup_guild_events: could not delete event {ev.id}: {del_exc}"
                        )
                    # If we deleted a tracked event (shouldn't happen with the
                    # preference logic above, but be defensive), clean up.
                    if ev.id in self.event_to_match:
                        self.log.warning(
                            f"_dedup_guild_events: deleted tracked event {ev.id} — "
                            f"clearing mapping"
                        )
                        del self.event_to_match[ev.id]

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
            
            transient_failure = False  # track non-NotFound errors to avoid miscounting

            if config.esports_guild_id:
                # Use configured guild if specified
                guild = self.bot.get_guild(config.esports_guild_id)
                if guild:
                    try:
                        event = await guild.fetch_scheduled_event(event_id)
                    except discord.NotFound:
                        pass  # genuinely gone — will be counted below
                    except Exception as fetch_exc:
                        self.log.warning(
                            f"Could not fetch event {event_id} for match {match_id}: "
                            f"{type(fetch_exc).__name__}: {fetch_exc}"
                        )
                        transient_failure = True
            else:
                # Search through all guilds (original behavior)
                for g in self.bot.guilds:
                    try:
                        event = await g.fetch_scheduled_event(event_id)
                        guild = g
                        break
                    except discord.NotFound:
                        continue  # genuinely gone in this guild — try next
                    except Exception as fetch_exc:
                        self.log.warning(
                            f"Could not fetch event {event_id} from guild {g.id}: "
                            f"{type(fetch_exc).__name__}: {fetch_exc}"
                        )
                        transient_failure = True
                        continue

            # Transient API errors (503, 522, timeouts) are NOT the event being
            # gone — skip this match entirely this poll cycle rather than
            # miscounting toward the 2-strike recreate threshold.
            if transient_failure and not event:
                self.log.debug(
                    f"Skipping event {event_id} for match {match_id} this cycle "
                    f"— fetch failed with transient error, will retry next poll"
                )
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
            # Remember status as observed this poll, for the next_matches health check
            self._event_status_by_match[match_id] = event.status

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

            # Check if event should be started (60 s before its scheduled_start_time
            # to deterministically beat Discord's built-in auto-start — see
            # EVENT_START_PRE_LEAD).
            if (event.status == discord.EventStatus.scheduled and
                    self._event_start_trigger(match) <= now):

                # If this is a voice event, Discord only allows one active event
                # per voice channel.  Check availability before calling start()
                # so we don't burn failure attempts on a predictable conflict.
                if event.entity_type == discord.EntityType.voice and event.channel_id:
                    alt_ch, alt_label = await self._find_alternative_vc(
                        guild, event.channel_id, match
                    )
                    if alt_ch is self._VC_BLOCKED:
                        self.log.warning(
                            f"Cannot start event {event_id} for match {match_id} "
                            f"({match.event_name}): voice channel occupied and "
                            f"other VC also busy — will retry next poll"
                        )
                        continue
                    if alt_ch is not None:
                        self.log.info(
                            f"Switching event {event_id} for match {match_id} "
                            f"({match.event_name}) to {alt_label} (current VC occupied)"
                        )
                        await event.edit(channel=alt_ch)
                        match.block_voice_channel = alt_label

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
                        await self._save_data()
                        # Recreate the event so the match still has a Discord presence
                        await self._create_discord_event(match)

            # Not yet due to start: keep the scheduled start/end aligned with
            # the current formula, so events created by older code (or before a
            # formula change) self-correct instead of lingering with stale
            # times — e.g. legacy events whose start sat at the real kickoff
            # instead of kickoff − 5 min.
            elif event.status == discord.EventStatus.scheduled:
                await self._reconcile_event_schedule(event, match, now)

            # NOTE: events are deliberately NOT ended on a time estimate here.
            # Ending is driven exclusively by wannspieltbig: the CS live-score
            # loop ends the event on the real match finish (map score reached
            # or has_ended), and any match that leaves the API is ended via
            # _handle_match_finished. The API's last_map_end proved unreliable
            # (BIG vs Parivision ended a 3-hour estimate short while the match
            # was still on map 3), so if a finish signal is ever missed the
            # event simply lingers until it is ended manually — an accepted
            # trade-off vs. cutting a live match's event short.

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
            
            # Send the CV2 score message (layout view carries the whole content)
            message = await channel.send(view=ScoreUpdateView(tracker, self))
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

                # Fire as close to exactly T-30 minutes as the 1-minute poll allows:
                # the preferred moment is a 1-minute window just below the 30-minute
                # mark. The window is open for the whole last 30 minutes though, so a
                # match that enters the API late (or a restart during the window)
                # still gets its reminder. The health check flags reminder_missing
                # for exactly this same window (0 < time_to_start <= 1800) — already
                # started matches are never re-reminded and must not be flagged.
                time_to_start = (match.start_time - now).total_seconds()
                if 0 < time_to_start <= 1800:  # last 30 minutes, up to kickoff
                    await self._send_match_reminder(match, channel)

    async def _check_for_whatsapp_pings(self):
        """Send a small WhatsApp-reminder card 45 min before each match
        to the configured PING_WHATSAPP channel."""
        channel_id = config.ping_whatsapp_channel_id
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        now = datetime.now(timezone.utc)

        for match in self.matches.values():
            if match.cancelled or match.id in self._whatsapp_ping_sent:
                continue
            if match.start_time <= now:
                continue

            time_to_start = (match.start_time - now).total_seconds()
            # Fire in the 44–45 min window before kickoff.  The window stays
            # open for the full 45 min so a late-arriving match or a restart
            # still gets its ping.
            if 0 < time_to_start <= 2700:  # last 45 minutes
                await self._send_whatsapp_ping(match, channel)

    async def _send_whatsapp_ping(self, match: EsportsMatch, channel):
        """Post a minimal CV2 card: title line + URL button to the share page."""
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour(0x25D366))
        container.add_item(discord.ui.TextDisplay(
            f"## BIG vs. {match.team_b} in 45 min"
        ))
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="Send WhatsApp Reminder",
            url=f"{config.share_base_url}/",
        ))
        container.add_item(row)
        view.add_item(container)
        try:
            await channel.send(view=view)
            self._whatsapp_ping_sent[match.id] = match.start_time.isoformat()
            self.log.info(f"WhatsApp ping sent for match {match.id} ({match.event_name})")
        except Exception as e:
            self.log.error(f"Failed to send WhatsApp ping for match {match.id}: {e}")

    REMINDER_PING_DELAY = 60  # seconds between reminder message and role ping

    async def _build_reminder_media(self, match: EsportsMatch) -> Optional[bytes]:
        """The composed 2:1 versus image for the reminder, or None when it
        can't be built (proxy failure for a known logo). A TBA placeholder is
        generated when the opponent hasn't been announced yet. Uses the same
        game-background design as the WhatsApp share pages."""
        bo_text = f"BO{match.bestof}" if match.bestof else ""
        berlin_tz = pytz.timezone("Europe/Berlin")
        match_berlin = match.start_time.astimezone(berlin_tz)
        now_berlin = datetime.now(berlin_tz)
        if match_berlin.date() == now_berlin.date():
            time_str = f"Today {match_berlin.strftime('%H:%M')}"
        elif match_berlin.date() == now_berlin.date() + timedelta(days=1):
            time_str = f"Tomorrow {match_berlin.strftime('%H:%M')}"
        else:
            time_str = match_berlin.strftime("%d %b %H:%M")

        compose_kwargs = dict(game=match.game, tournament=match.tournament_name,
                              bo_text=bo_text, time_str=time_str)

        if not match.team_b_logo_url:
            if match.team_b == "TBA":
                return await asyncio.to_thread(compose_versus_image, b"", **compose_kwargs)
            return None

        # Cache key now includes game/tournament/bo/time since they affect the image
        cache_sig = f"{match.team_b_logo_url}|{match.game}|{match.tournament_name}|{match.bestof}|{time_str}"
        cached = self._versus_cache.get(match.id)
        if cached and cached[0] == cache_sig:
            return cached[1]

        try:
            proxy_url = (
                "https://images.weserv.nl/?url="
                + urllib.parse.quote(re.sub(r"^https?://", "", match.team_b_logo_url), safe="")
                + "&w=400"
            )
            logo_bytes = None
            response = await http_client.get(proxy_url)
            try:
                if response.status == 200:
                    logo_bytes = await response.read()
            finally:
                await response.release()
            if logo_bytes is None:
                # Proxy failed — try the raw URL directly
                self.log.debug(f"Logo proxy failed for match {match.id}, trying direct fetch: {match.team_b_logo_url}")
                response = await http_client.get(match.team_b_logo_url)
                try:
                    if response.status == 200:
                        logo_bytes = await response.read()
                finally:
                    await response.release()
            if logo_bytes is None:
                self.log.warning(f"Logo fetch failed (proxy + direct) for match {match.id}")
                return None
            versus = await asyncio.to_thread(compose_versus_image, logo_bytes, **compose_kwargs)
            self._versus_cache[match.id] = (cache_sig, versus)
            return versus
        except Exception as e:
            self.log.warning(f"Could not build versus image for match {match.id}: {e}")
            return None

    async def _build_event_cover_media(self, match: EsportsMatch) -> Optional[bytes]:
        """4:1 variant of the versus image for the Discord event cover.
        Same opponent-logo fetch + proxy as _build_reminder_media but uses
        the event-cover composition (game background, smaller logos).
        Design: only background + both team logos (no game logo, no
        tournament label, no BO/date/time)."""
        bo_text = f"BO{match.bestof}" if match.bestof else ""
        berlin_tz = pytz.timezone("Europe/Berlin")
        match_berlin = match.start_time.astimezone(berlin_tz)
        now_berlin = datetime.now(berlin_tz)
        if match_berlin.date() == now_berlin.date():
            time_str = f"Today {match_berlin.strftime('%H:%M')}"
        elif match_berlin.date() == now_berlin.date() + timedelta(days=1):
            time_str = f"Tomorrow {match_berlin.strftime('%H:%M')}"
        else:
            time_str = match_berlin.strftime("%d %b %H:%M")

        compose_kwargs = dict(game=match.game, tournament=match.tournament_name,
                              bo_text=bo_text, time_str=time_str,
                              show_tournament=False, show_game_logo=False,
                              show_info=False)

        if not match.team_b_logo_url:
            if match.team_b == "TBA":
                return await asyncio.to_thread(compose_versus_image, b"", h=EVENT_COVER_H, **compose_kwargs)
            return None
        try:
            proxy_url = (
                "https://images.weserv.nl/?url="
                + urllib.parse.quote(re.sub(r"^https?://", "", match.team_b_logo_url), safe="")
                + "&w=400"
            )
            logo_bytes = None
            response = await http_client.get(proxy_url)
            try:
                if response.status == 200:
                    logo_bytes = await response.read()
            finally:
                await response.release()
            if logo_bytes is None:
                # Proxy failed — try the raw URL directly
                self.log.debug(f"Logo proxy failed for match {match.id}, trying direct fetch: {match.team_b_logo_url}")
                response = await http_client.get(match.team_b_logo_url)
                try:
                    if response.status == 200:
                        logo_bytes = await response.read()
                finally:
                    await response.release()
            if logo_bytes is None:
                return None
            return await asyncio.to_thread(compose_versus_image, logo_bytes, h=EVENT_COVER_H, **compose_kwargs)
        except Exception as e:
            self.log.warning(f"Could not build event cover image for match {match.id}: {e}")
            return None

    async def _send_delayed_ping(self, thread: discord.Thread, mention_text: str, match_id: int,
                                  summary_channel: Optional[discord.TextChannel] = None,
                                  versus_bytes: Optional[bytes] = None):
        """Users reported missing notifications when the role ping followed the
        reminder message immediately — give Discord a moment to settle the new
        thread before pinging.

        When *summary_channel* is provided a CV2 message (same design as the
        thread reminder but with a single "Match Thread" button) is sent there,
        with the role ping in ``content`` so recipients get notified.  This
        bypasses Discord's 250-member thread role-ping limit."""
        try:
            await asyncio.sleep(self.REMINDER_PING_DELAY)
            match = self.matches.get(match_id)
            if summary_channel and match:
                # CS workaround: CV2 card + plain ping in summary channel
                # (bypasses Discord's 250-member thread role-ping limit).
                # CV2 messages cannot carry `content`, so the role ping is a
                # separate plain-text message that triggers notifications.
                guild_id = thread.guild.id
                versus = versus_bytes is not None

                # 1) Plain-text role ping — triggers push notifications
                plain_ping_msg = await summary_channel.send(
                    content=mention_text,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                match.ping_text_message_id = plain_ping_msg.id
                self.plain_ping_to_match[plain_ping_msg.id] = match_id

                # 2) CV2 card — same design as the thread reminder but with
                #    a single "Match Thread" link button
                view = build_cs_ping_view(match, thread.id, guild_id, versus=versus)

                if versus:
                    file = discord.File(io.BytesIO(versus_bytes), filename="versus.jpg")
                else:
                    file = discord.File("resources/big.png", filename="big.png")

                msg = await summary_channel.send(view=view, file=file)
                match.ping_message_id = msg.id
                self.ping_to_match[msg.id] = match_id
                await self._save_data()
            else:
                # Original behavior: ping in thread (works for ≤250 member roles)
                await thread.send(
                    content=mention_text,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
        except Exception as e:
            self.log.error(f"Failed to send delayed reminder ping for match {match_id}: {e}")

    async def _edit_reminder_message(self, match: EsportsMatch):
        """Edit existing reminder/ping messages and thread title after a match update
        (reschedule, opponent change, tournament change, etc.)"""
        try:
            guild_id = config.esports_guild_id or (self.bot.guilds[0].id if self.bot.guilds else None)
            versus_bytes = await self._build_reminder_media(match)
            view = build_reminder_view(match, guild_id, versus=versus_bytes is not None)

            message = None
            thread = None

            # Try forum thread first — also grab the thread for title renaming
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
                if versus_bytes is not None:
                    attachment = discord.File(io.BytesIO(versus_bytes), filename="versus.jpg")
                else:
                    attachment = discord.File("resources/big.png", filename="big.png")
                await message.edit(view=view, attachments=[attachment])
                self.log.info(f"Edited reminder for updated match {match.id}: {match.event_name}")

                # Also edit the ping card in the summary channel (CS workaround).
                # Must create a fresh discord.File — the BytesIO underlying the
                # first attachment was consumed by message.edit() above.
                if match.ping_message_id and config.esports_summary_channel_id:
                    ping_channel = self.bot.get_channel(config.esports_summary_channel_id)
                    if ping_channel and thread:
                        try:
                            ping_msg = await ping_channel.fetch_message(match.ping_message_id)
                            ping_view = build_cs_ping_view(
                                match, thread.id, guild_id, versus=versus_bytes is not None
                            )
                            if versus_bytes is not None:
                                ping_attachment = discord.File(
                                    io.BytesIO(versus_bytes), filename="versus.jpg"
                                )
                            else:
                                ping_attachment = discord.File(
                                    "resources/big.png", filename="big.png"
                                )
                            await ping_msg.edit(view=ping_view, attachments=[ping_attachment])
                            self.log.info(
                                f"Edited ping card for updated match {match.id}: {match.event_name}"
                            )
                        except discord.NotFound:
                            self.log.debug(
                                f"Ping card {match.ping_message_id} for match {match.id} "
                                f"not found — will be replaced on next ping"
                            )
                            match.ping_message_id = None

                # Rename the forum thread if team names changed
                if thread and isinstance(thread, discord.Thread):
                    game_name = {"cs": "CS", "tm": "TM", "lol": "LoL"}.get(
                        match.game, match.game.upper()
                    )
                    expected_title = f"{match.team_a} vs {match.team_b} – {game_name}"
                    if thread.name != expected_title:
                        try:
                            await thread.edit(name=expected_title)
                            self.log.info(
                                f"Renamed thread {thread.id} to \"{expected_title}\" "
                                f"for updated match {match.id}"
                            )
                        except Exception as rename_exc:
                            self.log.warning(
                                f"Could not rename thread {thread.id} for match {match.id}: {rename_exc}"
                            )
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

            guild_id = channel.guild.id if channel.guild else config.esports_guild_id
            versus_bytes = await self._build_reminder_media(match)
            versus = versus_bytes is not None
            view = build_reminder_view(match, guild_id, versus=versus)

            def reminder_file() -> discord.File:
                # discord.File objects are single-use — build a fresh one per send
                if versus:
                    return discord.File(io.BytesIO(versus_bytes), filename="versus.jpg")
                return discord.File("resources/big.png", filename="big.png")

            # CS matches with large ping roles (>250 members) need the ping in the
            # summary channel instead of the thread — Discord caps thread role pings.
            cs_channel = None
            if match.game == "cs" and config.esports_summary_channel_id:
                cs_channel = self.bot.get_channel(config.esports_summary_channel_id)

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
                                msg = await existing.send(view=view, file=reminder_file())
                                if mention_text:
                                    asyncio.create_task(self._send_delayed_ping(
                                        existing, mention_text, match.id,
                                        summary_channel=cs_channel,
                                        versus_bytes=versus_bytes,
                                    ))
                                match.reminder_message_id = msg.id
                                self.reminder_to_match[msg.id] = match.id
                                await self._save_data()
                                self.log.info(f"Sent rescheduled reminder in existing thread for match {match.id}: {match.event_name}")
                                return
                            else:
                                # Thread no longer accessible — clear stale tracking so a fresh thread can be created
                                self.log.warning(f"Existing thread {match.forum_thread_id} for match {match.id} not found, clearing stale tracking")
                                self._close_forum_thread(match.forum_thread_id, match.id)

                        # No existing thread — create a new one
                        game_name = {"cs": "CS", "tm": "TM", "lol": "LoL"}.get(match.game, match.game.upper())
                        thread_with_msg = await forum_channel.create_thread(
                            name=f"{match.team_a} vs {match.team_b} – {game_name}",
                            view=view,
                            file=reminder_file(),
                        )
                        forum_thread = thread_with_msg.thread
                        match.forum_thread_id = forum_thread.id
                        self.thread_to_match[forum_thread.id] = match.id
                        match.reminder_message_id = thread_with_msg.message.id
                        self.reminder_to_match[thread_with_msg.message.id] = match.id
                        # Send ping as separate, delayed message so Discord triggers proper notifications
                        if mention_text:
                            asyncio.create_task(self._send_delayed_ping(
                                forum_thread, mention_text, match.id,
                                summary_channel=cs_channel,
                                versus_bytes=versus_bytes,
                            ))
                        await self._save_data()
                        self.log.info(f"Sent 30-minute reminder (thread) for match {match.id}: {match.event_name}")
                        status_reporter.record(
                            "esports",
                            last_reminder_sent_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            last_reminder_match=match.event_name,
                        )
                        return
                    except Exception as e:
                        self.log.error(f"Error creating/reusing forum thread for match {match.id}: {e}")

            # Fallback: post to games channel if no forum channel configured.
            # CV2 messages cannot carry `content`, so the role ping moves into
            # the container as a small text line.
            view = build_reminder_view(match, guild_id, mention=mention_text or None, versus=versus)
            message = await channel.send(view=view, file=reminder_file())
            match.reminder_message_id = message.id
            self.reminder_to_match[message.id] = match.id
            await self._save_data()
            
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
                # Also clean up stale ping entries for this match_id
                stale_ping_ids = [pid for pid, mid in self.ping_to_match.items() if mid == match_id]
                for pid in stale_ping_ids:
                    del self.ping_to_match[pid]
                stale_plain_ping_ids = [pid for pid, mid in self.plain_ping_to_match.items() if mid == match_id]
                for pid in stale_plain_ping_ids:
                    del self.plain_ping_to_match[pid]
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
            if match_id:
                self._versus_cache.pop(match_id, None)

            # Delete the summary-channel ping message (CS large-role workaround)
            if match and match.ping_message_id:
                try:
                    ping_msg = await channel.fetch_message(match.ping_message_id)
                    await ping_msg.delete()
                    self.log.info(f"Deleted ping message {match.ping_message_id}")
                except discord.NotFound:
                    self.log.debug(f"Ping message {match.ping_message_id} already deleted")
                except Exception as e:
                    self.log.warning(f"Failed to delete ping message {match.ping_message_id}: {e}")
                if match.ping_message_id in self.ping_to_match:
                    del self.ping_to_match[match.ping_message_id]
                match.ping_message_id = None

            # Delete the plain-text role ping sent right before the ping card
            if match and match.ping_text_message_id:
                try:
                    plain_ping_msg = await channel.fetch_message(match.ping_text_message_id)
                    await plain_ping_msg.delete()
                    self.log.info(f"Deleted plain ping message {match.ping_text_message_id}")
                except discord.NotFound:
                    self.log.debug(f"Plain ping message {match.ping_text_message_id} already deleted")
                except Exception as e:
                    self.log.warning(f"Failed to delete plain ping message {match.ping_text_message_id}: {e}")
                if match.ping_text_message_id in self.plain_ping_to_match:
                    del self.plain_ping_to_match[match.ping_text_message_id]
                match.ping_text_message_id = None

        if reminders_to_delete:
            await self._save_data()
    
    def _close_forum_thread(self, thread_id: int, match_id: int):
        """Clean up forum thread tracking data (Discord handles archiving via inactivity setting)"""
        if thread_id in self.thread_to_match:
            del self.thread_to_match[thread_id]
        if match_id in self.matches:
            self.matches[match_id].forum_thread_id = None

    async def _cleanup_match_reminder(self, match: EsportsMatch):
        """Clean up reminder message for a specific match"""
        if match.reminder_message_id:
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

        # Delete the summary-channel ping message (CS large-role workaround)
        if match.ping_message_id:
            try:
                if config.esports_summary_channel_id:
                    ch = self.bot.get_channel(config.esports_summary_channel_id)
                    if ch:
                        msg = await ch.fetch_message(match.ping_message_id)
                        await msg.delete()
                        self.log.info(f"Deleted ping message {match.ping_message_id} for match {match.id}")
            except discord.NotFound:
                self.log.debug(f"Ping message {match.ping_message_id} already deleted")
            except Exception as e:
                self.log.warning(f"Failed to delete ping message {match.ping_message_id}: {e}")
            if match.ping_message_id in self.ping_to_match:
                del self.ping_to_match[match.ping_message_id]
            match.ping_message_id = None

        # Delete the plain-text role ping sent right before the ping card
        if match.ping_text_message_id:
            try:
                if config.esports_summary_channel_id:
                    ch = self.bot.get_channel(config.esports_summary_channel_id)
                    if ch:
                        msg = await ch.fetch_message(match.ping_text_message_id)
                        await msg.delete()
                        self.log.info(f"Deleted plain ping message {match.ping_text_message_id} for match {match.id}")
            except discord.NotFound:
                self.log.debug(f"Plain ping message {match.ping_text_message_id} already deleted")
            except Exception as e:
                self.log.warning(f"Failed to delete plain ping message {match.ping_text_message_id}: {e}")
            if match.ping_text_message_id in self.plain_ping_to_match:
                del self.plain_ping_to_match[match.ping_text_message_id]
            match.ping_text_message_id = None

        await self._save_data()
    

async def setup(bot: commands.Bot):
    if config.esports_enabled:
        await bot.add_cog(EsportsCog(bot))
    else:
        logging.getLogger("roaringbot").info("E-Sports cog disabled via configuration")
