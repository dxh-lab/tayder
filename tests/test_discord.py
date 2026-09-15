from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tayder.approve.state import ApprovalStore
from tayder.config import Settings
from tayder.models import Proposal, ProposalStatus as S, Side
from tayder.notify.discord_bot import ProposalView, TayderBot


def proposal(store):
    return store.register(Proposal('BTC-USD', Side.BUY, 5, 'test', 100))


def interaction(uid=1):
    return SimpleNamespace(user=SimpleNamespace(id=uid),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


@pytest.mark.asyncio
async def test_unauthorized_click_does_not_approve_or_execute():
    store = ApprovalStore()
    p = proposal(store)
    callback = AsyncMock()
    view = ProposalView(p.proposal_id, store, Settings(discord_allowlist=frozenset({1})), callback)
    i = interaction(2)
    await view.approve_btn.callback(i)
    assert p.status == S.PENDING
    callback.assert_not_called()
    i.response.send_message.assert_awaited_once_with('Not allowlisted.', ephemeral=True)


@pytest.mark.asyncio
async def test_persistent_button_ids_survive_new_view_and_report_execution_result():
    store = ApprovalStore()
    p = proposal(store)
    async def execute(approved):
        store.mark_executed(approved.proposal_id)
    view = ProposalView(p.proposal_id, store, Settings(), execute)
    restored = ProposalView(p.proposal_id, store, Settings(), execute)
    assert view.is_persistent()
    assert [b.custom_id for b in view.children] == [b.custom_id for b in restored.children]
    i = interaction()
    await restored.approve_btn.callback(i)
    assert p.status == S.EXECUTED
    assert i.followup.send.await_args_list[-1].args == ('Status: executed',)


@pytest.mark.asyncio
async def test_publish_persists_message_id_and_setup_restores_view(monkeypatch):
    store = ApprovalStore()
    p = proposal(store)
    settings = Settings()
    bot = TayderBot(settings, store)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=12345)))
    bot._channel = channel
    await bot.publish_proposal(p, 9.97)
    assert p.meta['discord_message_id'] == 12345
    restored = TayderBot(settings, store)
    views = []
    monkeypatch.setattr(restored, 'add_view', lambda view, **kwargs: views.append((view, kwargs)))
    await restored.setup_hook()
    assert views[0][1]['message_id'] == 12345
    assert views[0][0].proposal_id == p.proposal_id
    await bot.close()
    await restored.close()


@pytest.mark.asyncio
async def test_status_uses_runtime_kill_state_and_kill_commands_enforce_allowlist():
    store = ApprovalStore()
    calls = []
    bot = TayderBot(Settings(discord_allowlist=frozenset({1})), store,
                    on_kill=lambda: calls.append('kill'), status_provider=lambda: 'killed=True')
    await bot.setup_hook()
    rejected = interaction(2)
    await bot.tree.get_command('kill').callback(rejected)
    assert calls == []
    allowed = interaction()
    await bot.tree.get_command('kill').callback(allowed)
    assert calls == ['kill']
    i = interaction()
    await bot.tree.get_command('status').callback(i)
    i.followup.send.assert_awaited_once_with('killed=True', ephemeral=True)
    await bot.close()
