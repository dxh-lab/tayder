"""Discord embeds with Approve / Skip buttons + kill-switch command."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ui import Button, View

from tayder.approve.state import ApprovalError, ApprovalStore
from tayder.config import Settings
from tayder.models import Proposal, ProposalStatus

log = logging.getLogger(__name__)

ApproveCallback = Callable[[Proposal], Awaitable[None] | None]


class ProposalView(View):
    def __init__(
        self,
        proposal_id: str,
        store: ApprovalStore,
        settings: Settings,
        on_approved: ApproveCallback | None = None,
    ) -> None:
        super().__init__(timeout=float(settings.proposal_expiry_seconds))
        self.proposal_id = proposal_id
        self.store = store
        self.settings = settings
        self.on_approved = on_approved

    def _allowed(self, user: discord.abc.User) -> bool:
        if not self.settings.discord_allowlist:
            return True
        return user.id in self.settings.discord_allowlist

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve_btn(
        self, interaction: discord.Interaction, button: Button
    ) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("Not allowlisted.", ephemeral=True)
            return
        try:
            p = self.store.approve(self.proposal_id)
        except ApprovalError as e:
            await interaction.response.send_message(f"Reject: {e.code}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Approved `{p.proposal_id}`", ephemeral=True
        )
        if self.on_approved:
            result = self.on_approved(p)
            if asyncio.iscoroutine(result):
                await result
        self.stop()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_btn(
        self, interaction: discord.Interaction, button: Button
    ) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("Not allowlisted.", ephemeral=True)
            return
        try:
            p = self.store.skip(self.proposal_id)
        except ApprovalError as e:
            await interaction.response.send_message(f"Reject: {e.code}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Skipped `{p.proposal_id}`", ephemeral=True
        )
        self.stop()


def proposal_embed(p: Proposal, mode: str) -> discord.Embed:
    color = discord.Color.green() if p.side.value == "BUY" else discord.Color.red()
    emb = discord.Embed(
        title=f"{p.side.value} {p.product_id}",
        description=p.reason,
        color=color,
    )
    emb.add_field(name="Notional", value=f"${p.notional_usd:.2f}")
    emb.add_field(name="Signal", value=f"{p.signal_price:.2f}")
    emb.add_field(name="Mode", value=mode.upper())
    emb.set_footer(text=f"id={p.proposal_id}")
    return emb


class TayderBot(discord.Client):
    def __init__(
        self,
        settings: Settings,
        store: ApprovalStore,
        *,
        on_approved: ApproveCallback | None = None,
        on_kill: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.store = store
        self.on_approved = on_approved
        self.on_kill = on_kill
        self.on_resume = on_resume
        self.tree = app_commands.CommandTree(self)
        self._channel: discord.abc.Messageable | None = None

    async def setup_hook(self) -> None:
        @self.tree.command(name="kill", description="Halt all new trading (kill-switch)")
        async def kill_cmd(interaction: discord.Interaction) -> None:
            if (
                self.settings.discord_allowlist
                and interaction.user.id not in self.settings.discord_allowlist
            ):
                await interaction.response.send_message("Not allowlisted.", ephemeral=True)
                return
            if self.on_kill:
                self.on_kill()
            await interaction.response.send_message(
                "Kill-switch ON. No new proposals/trades.", ephemeral=False
            )

        @self.tree.command(name="resume", description="Clear kill-switch")
        async def resume_cmd(interaction: discord.Interaction) -> None:
            if (
                self.settings.discord_allowlist
                and interaction.user.id not in self.settings.discord_allowlist
            ):
                await interaction.response.send_message("Not allowlisted.", ephemeral=True)
                return
            if self.on_resume:
                self.on_resume()
            await interaction.response.send_message(
                "Kill-switch OFF. Trading may resume.", ephemeral=False
            )

        @self.tree.command(name="status", description="Show tayder status")
        async def status_cmd(interaction: discord.Interaction) -> None:
            killed = getattr(self.settings, "killed", False)
            await interaction.response.send_message(
                f"mode={self.settings.mode} bankroll=${self.settings.bankroll_usd} "
                f"killed={killed}",
                ephemeral=True,
            )

        if self.settings.discord_channel_id:
            guild = None
            # sync globally; for faster guild sync user can re-invite
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Discord ready as %s", self.user)
        if self.settings.discord_channel_id:
            ch = self.get_channel(self.settings.discord_channel_id)
            if ch is None:
                try:
                    ch = await self.fetch_channel(self.settings.discord_channel_id)
                except Exception:
                    log.exception("Cannot fetch Discord channel")
                    ch = None
            self._channel = ch  # type: ignore[assignment]

    async def publish_proposal(self, proposal: Proposal) -> None:
        if self._channel is None:
            log.warning("No Discord channel; proposal %s not sent", proposal.proposal_id)
            return
        view = ProposalView(
            proposal.proposal_id,
            self.store,
            self.settings,
            on_approved=self.on_approved,
        )
        await self._channel.send(
            embed=proposal_embed(proposal, self.settings.mode), view=view
        )
