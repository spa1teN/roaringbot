"""Feedback cog — /feedback command, CV2 subject selector, modal, DB storage."""

from __future__ import annotations

import logging
from typing import Any

import discord
from core.status_reporter import status_reporter
from discord.ext import commands

log = logging.getLogger("roaringbot.feedback")

SUBJECTS = [
    discord.SelectOption(label="🛡️ Moderation", value="moderation"),
    discord.SelectOption(label="🎮 Match-Tracking", value="match_tracking"),
    discord.SelectOption(label="🏛️ Verein", value="verein"),
    discord.SelectOption(label="💬 Sonstiges", value="other"),
]


def _build_feedback_menu(
    cog: FeedbackCog, guild_id: int, user_id: int
) -> discord.ui.LayoutView:
    """Build an ephemeral CV2 menu with subject select + submit button."""
    view = discord.ui.LayoutView(timeout=300)
    container = discord.ui.Container(accent_colour=discord.Colour(0x5865F2))

    container.add_item(discord.ui.TextDisplay(
        "## 💬 Feedback\n-# Wähle ein Thema und schreibe deine Nachricht."))
    container.add_item(discord.ui.Separator())
    view.add_item(container)

    # Select must be in a top-level ActionRow — LayoutView containers
    # do not accept type 8 (StringSelect).
    subject_select = discord.ui.Select(
        custom_id="feedback:subject",
        placeholder="Thema…",
        options=SUBJECTS,
        min_values=1, max_values=1,
    )
    select_row = discord.ui.ActionRow()
    select_row.add_item(subject_select)
    view.add_item(select_row)

    state: dict[str, str | bool] = {"subject": "other", "anonymous": False}

    toggle_row = discord.ui.ActionRow()
    anon_btn = discord.ui.Button(
        label="Anonym: Nein", emoji="👤",
        style=discord.ButtonStyle.secondary,
        custom_id="feedback:anon",
    )
    toggle_row.add_item(anon_btn)
    view.add_item(toggle_row)

    submit_row = discord.ui.ActionRow()
    submit_btn = discord.ui.Button(
        label="Nachricht schreiben", emoji="✏️",
        style=discord.ButtonStyle.primary,
        custom_id="feedback:submit",
    )
    submit_row.add_item(submit_btn)
    view.add_item(submit_row)

    async def on_anon(interaction: discord.Interaction) -> None:
        state["anonymous"] = not state["anonymous"]
        anon_btn.label = f"Anonym: {'Ja' if state['anonymous'] else 'Nein'}"
        anon_btn.emoji = "🕶️" if state['anonymous'] else "👤"
        await interaction.response.edit_message(view=view)

    async def on_subject(interaction: discord.Interaction) -> None:
        state["subject"] = str(subject_select.values[0])
        await interaction.response.defer()

    async def on_submit(interaction: discord.Interaction) -> None:
        modal = FeedbackModal(cog, guild_id, user_id, str(state["subject"]), bool(state["anonymous"]))
        await interaction.response.send_modal(modal)

    anon_btn.callback = on_anon
    subject_select.callback = on_subject
    submit_btn.callback = on_submit

    return view


class FeedbackModal(discord.ui.Modal, title="Feedback"):
    """Modal for the feedback message text. Subject is pre-filled from the menu."""

    message = discord.ui.TextInput(
        label="Deine Nachricht",
        style=discord.TextStyle.long,
        placeholder="Was möchtest du uns mitteilen?",
        required=True,
        max_length=2000,
    )

    def __init__(self, cog: FeedbackCog, guild_id: int, user_id: int, subject: str, anonymous: bool = False):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self._subject = subject
        self._anonymous = anonymous

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if self.cog.bot.db:
                await self.cog.bot.db.feedback.submit(
                    guild_id=self.guild_id,
                    user_id=0 if self._anonymous else self.user_id,
                    is_anonymous=self._anonymous,
                    subject=self._subject,
                    message=self.message.value,
                )
                await interaction.followup.send(
                    "✅ Feedback gesendet — danke!", ephemeral=True)
                await interaction.delete_original_response()
                status_reporter.bump_counter("feedback", "submissions")
                status_reporter.record(
                    "feedback",
                    last_submission_at=discord.utils.utcnow().isoformat(),
                    last_error=None,
                )
                log.info(
                    "Feedback: guild=%s user=%s subject=%s anon=%s",
                    self.guild_id, "anon" if self._anonymous else self.user_id,
                    self._subject, self._anonymous,
                )
            else:
                await interaction.followup.send(
                    "⚠️ Feedback-Speicher nicht verfügbar.", ephemeral=True)
                await interaction.delete_original_response()
                status_reporter.bump_counter("feedback", "submission_errors")
                status_reporter.record("feedback", last_error="DB not connected")
        except Exception:
            log.exception("Failed to store feedback")
            status_reporter.bump_counter("feedback", "submission_errors")
            status_reporter.record("feedback", last_error="DB insert failed")
            await interaction.followup.send(
                "❌ Fehler beim Speichern des Feedbacks.", ephemeral=True)
            await interaction.delete_original_response()
class FeedbackCog(commands.Cog):
    """Feedback submission via /feedback command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.app_commands.command(
        name="feedback", description="Feedback an die Admins senden"
    )
    async def feedback_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Feedback ist nur auf Servern verfügbar.", ephemeral=True)
            return
        view = _build_feedback_menu(self, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackCog(bot))
