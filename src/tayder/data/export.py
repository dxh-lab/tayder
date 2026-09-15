"""Export public 15-minute candles to validated research CSV; no account access."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time

import httpx

from tayder.data.market import CoinbasePublicMarket
from tayder.research import _validate_candles


class HistoricalMarket(CoinbasePublicMarket):
    """Pace bulk public GETs and retry rate limits; never used for order POSTs."""

    def _get(self, path, params):
        for attempt in range(5):
            time.sleep(1 if attempt == 0 else min(2 ** attempt, 16))
            try:
                return super()._get(path, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt == 4:
                    raise


class ExchangeHistoricalMarket:
    """Explicit research-only source: Coinbase Exchange's public candle API.

    https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
    No credentials, order methods or implicit fallback from the live market feed.
    """

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=20, trust_env=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def candles_history(self, pair, granularity, start, end):
        if pair not in ('BTC-USD', 'ETH-USD') or granularity != 900:
            raise ValueError('Research export requires BTC-USD/ETH-USD at 15 minutes')
        rows = {}
        cursor = start
        while cursor < end:
            page_end = min(end, cursor + timedelta(seconds=300 * granularity))
            for attempt in range(5):
                time.sleep(1 if attempt == 0 else min(2 ** attempt, 16))
                response = self.client.get(f'https://api.exchange.coinbase.com/products/{pair}/candles',
                    params={'granularity': granularity, 'start': cursor.isoformat(),
                            'end': (page_end - timedelta(seconds=1)).isoformat()},
                    follow_redirects=False)
                if response.status_code != 429 or attempt == 4:
                    response.raise_for_status()
                    break
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('Invalid Exchange candle response')
            for row in data:
                if not isinstance(row, list) or len(row) != 6:
                    raise ValueError('Invalid Exchange candle row')
                candle = CoinbasePublicMarket._candle(dict(zip(
                    ('start', 'low', 'high', 'open', 'close', 'volume'), row)))
                if cursor <= candle.ts < page_end:
                    if candle.ts in rows and rows[candle.ts] != candle:
                        raise ValueError('Conflicting Exchange candles')
                    rows[candle.ts] = candle
            cursor = page_end
        return sorted(rows.values(), key=lambda c: c.ts)


def export_csv(path: Path, start: datetime, end: datetime, *, market=None, source='advanced') -> dict[str, int]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Require timezone-aware start < end")
    if start.timestamp() % 900 or end.timestamp() % 900:
        raise ValueError("Start and end must align to 15 minutes")
    if end > datetime.now(timezone.utc):
        raise ValueError("End must contain only completed candles")
    if source not in ('advanced', 'exchange'):
        raise ValueError('Unknown candle source')
    client = market or (HistoricalMarket() if source == 'advanced' else ExchangeHistoricalMarket())
    try:
        data = {pair: client.candles_history(pair, 900, start, end) for pair in ('BTC-USD', 'ETH-USD')}
    finally:
        if market is None:
            client.close()
    expected = int((end - start).total_seconds() // 900)
    for pair, rows in data.items():
        _validate_candles(pair, rows)
        if len(rows) != expected or rows[0].ts != start or rows[-1].ts.timestamp() != end.timestamp() - 900:
            raise ValueError(f"{pair}: incomplete requested coverage; no CSV written")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, newline='', delete=False) as f:
            temp = Path(f.name)
            writer = csv.writer(f)
            writer.writerow(['product_id', 'timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for pair, rows in data.items():
                writer.writerows((pair, c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume) for c in rows)
        temp.replace(path)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return {pair: len(rows) for pair, rows in data.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, help='Inclusive ISO UTC date/time, e.g. 2026-06-01T00:00:00Z')
    parser.add_argument('--end', required=True, help='Exclusive ISO UTC date/time')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', choices=('advanced', 'exchange'), default='advanced')
    args = parser.parse_args(argv)
    try:
        counts = export_csv(args.output,
            datetime.fromisoformat(args.start.replace('Z', '+00:00')),
            datetime.fromisoformat(args.end.replace('Z', '+00:00')), source=args.source)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        parser.error(str(exc))
    print(f"Saved {counts} to {args.output}")


if __name__ == '__main__':
    main()
