from datetime import datetime, timedelta, timezone
from dataclasses import replace
import pytest

from tayder.account import Account, Position
from tayder.config import Settings
from tayder.data.export import export_csv
from tayder.models import Candle, Fill, Side
from tayder.research import load_candles_csv


@pytest.mark.parametrize('kwargs', [
    {'mode': 'unknown'}, {'bankroll_usd': 11}, {'bankroll_usd': float('nan')},
    {'coinbase_api_base': 'https://attacker.test'}, {'mode': 'live'},
    {'mode': 'live', 'discord_allowlist': frozenset({1})},
    {'mode': 'live', 'coinbase_api_key_name': 'key', 'coinbase_api_private_key': 'pem'},
    {'max_open_positions': 2}, {'max_open_positions': True},
    {'strategy_pairs': ('BTC-USD', 'BTC-USD')}, {'strategy_pairs': ('DOGE-USD',)},
    {'slippage_bps': 10000}, {'max_book_age_seconds': float('nan')},
    {'cooldown_seconds': -1}, {'candle_granularity_seconds': 60},
    {'strategy_lookback': 1}, {'strategy_z_entry': 0}, {'strategy_z_entry': float('nan')},
])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs).validate()


def test_default_settings_valid():
    Settings().validate()


def test_load_settings_paper_defaults_fee_gate_advisory(monkeypatch, tmp_path):
    from tayder.config import load_settings
    monkeypatch.setenv('MODE', 'paper')
    monkeypatch.delenv('ENFORCE_FEE_DOMINANCE', raising=False)
    env = tmp_path / '.env'
    env.write_text('')
    settings = load_settings(str(env))
    assert settings.mode == 'paper'
    assert settings.enforce_fee_dominance is False


def test_load_settings_live_defaults_fee_gate_enforced(monkeypatch, tmp_path):
    from tayder.config import load_settings
    monkeypatch.setenv('MODE', 'live')
    monkeypatch.delenv('ENFORCE_FEE_DOMINANCE', raising=False)
    env = tmp_path / '.env'
    env.write_text('')
    settings = load_settings(str(env))
    assert settings.mode == 'live'
    assert settings.enforce_fee_dominance is True


def test_load_settings_honors_explicit_fee_gate_override(monkeypatch, tmp_path):
    from tayder.config import load_settings
    monkeypatch.setenv('MODE', 'live')
    monkeypatch.setenv('ENFORCE_FEE_DOMINANCE', 'false')
    env = tmp_path / '.env'
    env.write_text('')
    assert load_settings(str(env)).enforce_fee_dominance is False


def fill(side, size=.05, price=100, fee=.03, **kwargs):
    return Fill('proposal', 'BTC-USD', side, price, size, fee, size * price, 'paper', 'order', **kwargs)


def test_partial_sell_cost_basis_and_midnight_fee_accounting():
    account = Account(10)
    account.apply(fill(Side.BUY, filled_at=datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)))
    account.apply(fill(Side.SELL, size=.02, price=90, fee=.01,
                       filled_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    assert account.positions['BTC-USD'].size == pytest.approx(.03)
    assert account.positions['BTC-USD'].cost_usd == pytest.approx(3)
    assert account.cash_usd == pytest.approx(6.76)
    assert account.realized_by_day == pytest.approx({'2026-01-01': -.03, '2026-01-02': -.21})
    assert Account.from_dict(account.to_dict()) == account


def test_unexpected_actual_fee_overrun_is_recorded_and_halts_account():
    account = Account(5.03)
    account.apply(fill(Side.BUY, fee=.1))
    assert account.cash_usd == pytest.approx(-.07)
    assert account.killed


def test_oversell_is_rejected_without_mutation():
    account = Account(5, {'BTC-USD': Position(.05, 5)})
    before = account.to_dict()
    with pytest.raises(ValueError):
        account.apply(fill(Side.SELL, size=.1))
    assert before == account.to_dict()


def test_export_roundtrip_and_missing_coverage_does_not_replace_existing_file(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    class Market:
        gap = False
        def candles_history(self, *args):
            return [Candle(start + timedelta(minutes=i * 15), 10, 11, 9, 10, 1)
                    for i in range(3 if self.gap else 4)]
    market = Market()
    path = tmp_path / 'candles.csv'
    assert export_csv(path, start, end, market=market) == {'BTC-USD': 4, 'ETH-USD': 4}
    assert len(load_candles_csv(path)['BTC-USD']) == 4
    before = path.read_bytes()
    market.gap = True
    with pytest.raises(ValueError, match='incomplete'):
        export_csv(path, start, end, market=market)
    assert path.read_bytes() == before


def test_exchange_export_pages_filters_extra_rows_and_retries_rate_limit(tmp_path, monkeypatch):
    import httpx
    from tayder.data.export import ExchangeHistoricalMarket
    monkeypatch.setattr('tayder.data.export.time.sleep', lambda _: None)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15 * 301)
    requests = []
    def handler(request):
        requests.append(request)
        assert not request.headers.get('Authorization')
        assert request.url.host == 'api.exchange.coinbase.com'
        if len(requests) == 1:
            return httpx.Response(429)
        left = datetime.fromisoformat(request.url.params['start'])
        right = datetime.fromisoformat(request.url.params['end'])
        rows = []
        cursor = left - timedelta(minutes=15)
        while cursor <= right + timedelta(minutes=15):
            rows.append([cursor.timestamp(), 9, 11, 10, 10, 1])
            cursor += timedelta(minutes=15)
        return httpx.Response(200, json=list(reversed(rows)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        market = ExchangeHistoricalMarket(client)
        counts = export_csv(tmp_path / 'x.csv', start, end, market=market, source='exchange')
    assert counts == {'BTC-USD': 301, 'ETH-USD': 301}
    assert len(requests) == 5  # two pages per asset + one 429 retry
    assert len(load_candles_csv(tmp_path / 'x.csv')['BTC-USD']) == 301
