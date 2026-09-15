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
        super().__init__(timeout=None)
        self.proposal_id = proposal_id
        self.store = store
        self.settings = settings
        self.on_approved = on_approved
        self.approve_btn.custom_id = f"tayder:approve:{proposal_id}"
        self.skip_btn.custom_id = f"tayder:skip:{proposal_id}"

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
            await interaction.response.defer(ephemeral=True)
            p = await asyncio.to_thread(self.store.approve, self.proposal_id)
        except ApprovalError as e:
            await interaction.followup.send(f"Reject: {e.code}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Approved `{p.proposal_id}`", ephemeral=True
        )
        if self.on_approved:
            result = self.on_approved(p)
            if asyncio.iscoroutine(result):
                await result
        current = self.store.get(self.proposal_id)
        if current:
            await interaction.followup.send(
                f"Status: {current.status.value}"
                + (f" ({current.meta['failure_reason']})" if current.meta.get("failure_reason") else ""),
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_btn(
        self, interaction: discord.Interaction, button: Button
    ) -> None:
        if not self._allowed(interaction.user):
            await interaction.response.send_message("Not allowlisted.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
            p = await asyncio.to_thread(self.store.skip, self.proposal_id)
        except ApprovalError as e:
            await interaction.followup.send(f"Reject: {e.code}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Skipped `{p.proposal_id}`", ephemeral=True
        )
        self.stop()


def risk_summary(notional_usd: float, signal_price: float, account_balance_usd: float) -> str:
    """Stake as a share of strategy equity (cash plus marked holdings)."""
    if account_balance_usd > 0:
        pct = notional_usd / account_balance_usd * 100.0
    else:
        pct = 0.0
    return (
        f"Proposing ${notional_usd:.2f} @ {signal_price:.2f}. "
        f"Strategy equity: ${account_balance_usd:.2f} (stake {pct:.0f}%)"
    )


def proposal_embed(
    p: Proposal, mode: str, account_balance_usd: float
) -> discord.Embed:
    color = discord.Color.green() if p.side.value == "BUY" else discord.Color.red()
    summary = risk_summary(p.notional_usd, p.signal_price, account_balance_usd)
    emb = discord.Embed(
        title=f"{p.side.value} {p.product_id}",
        description=f"{summary}\n\n{p.reason}",
        color=color,
    )
    emb.add_field(name="Stake", value=f"${p.notional_usd:.2f}")
    emb.add_field(name="Price", value=f"{p.signal_price:.2f}")
    pct = (
        p.notional_usd / account_balance_usd * 100.0
        if account_balance_usd > 0
        else 0.0
    )
    emb.add_field(
        name="Strategy equity / stake share",
        value=f"${account_balance_usd:.2f} ({pct:.0f}%)",
    )
    emb.add_field(name="Mode", value=mode.upper())
    emb.add_field(name="Signal", value="Distance to mean; unvalidated hypothesis", inline=False)
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
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.settings = settings
        self.store = store
        self.on_approved = on_approved
        self.on_kill = on_kill
        self.on_resume = on_resume
        self.status_provider = status_provider
        self.tree = app_commands.CommandTree(self)
        self._channel: discord.abc.Messageable | None = None

    async def setup_hook(self) -> None:
        self.store.expire_due()
        for p in self.store.active():
            if p.status == ProposalStatus.PENDING and p.meta.get("discord_message_id"):
                self.add_view(ProposalView(p.proposal_id, self.store, self.settings, self.on_approved),
                              message_id=int(p.meta["discord_message_id"]))
        @self.tree.command(name="kill", description="Halt all new trading (kill-switch)")
        async def kill_cmd(interaction: discord.Interaction) -> None:
            if (
                self.settings.discord_allowlist
                and interaction.user.id not in self.settings.discord_allowlist
            ):
                await interaction.response.send_message("Not allowlisted.", ephemeral=True)
                return
            await interaction.response.defer()
            if self.on_kill:
                await asyncio.to_thread(self.on_kill)
            await interaction.followup.send(
                "Kill-switch ON. Pending approvals canceled. Already-submitted orders still reconcile.", ephemeral=False
            )

        @self.tree.command(name="resume", description="Clear kill-switch")
        async def resume_cmd(interaction: discord.Interaction) -> None:
            if (
                self.settings.discord_allowlist
                and interaction.user.id not in self.settings.discord_allowlist
            ):
                await interaction.response.send_message("Not allowlisted.", ephemeral=True)
                return
            await interaction.response.defer()
            if self.on_resume:
                await asyncio.to_thread(self.on_resume)
            await interaction.followup.send(
                "Kill-switch OFF. Trading may resume.", ephemeral=False
            )

        @self.tree.command(name="status", description="Show tayder status")
        async def status_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            status = (await asyncio.to_thread(self.status_provider) if self.status_provider
                      else f"mode={self.settings.mode}")
            await interaction.followup.send(
                status,
                ephemeral=True,
            )

        if self.settings.discord_channel_id:
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

    async def publish_proposal(
        self, proposal: Proposal, account_balance_usd: float | None = None
    ) -> None:
        if self._channel is None:
            log.warning("No Discord channel; proposal %s not sent", proposal.proposal_id)
            return
        balance = (
            float(account_balance_usd)
            if account_balance_usd is not None
            else float(self.settings.bankroll_usd)
        )
        view = ProposalView(
            proposal.proposal_id,
            self.store,
            self.settings,
            on_approved=self.on_approved,
        )
        message = await self._channel.send(
            embed=proposal_embed(proposal, self.settings.mode, balance),
            view=view,
        )
        # Persist the stable IDs so existing buttons work after restart.
        try:
            await asyncio.to_thread(self.store.transition, proposal.proposal_id,
                (ProposalStatus.PENDING,), ProposalStatus.PENDING,
                discord_message_id=message.id)
        except ApprovalError:
            pass  # It may have been approved/killed while Discord was sending.
