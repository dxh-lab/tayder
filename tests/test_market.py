"""Public market contract tests: all HTTP uses httpx.MockTransport."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tayder.data.market import CoinbasePublicMarket, MarketDataError, PUBLIC_BASE

UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
BOUNDARY = int(NOW.timestamp()) - 30


def candle(start, /, **changes):
    return dict(start=str(start), open="100", high="102", low="99",
                close="101", volume="2") | changes


def book(**changes):
    return {"pricebook": dict(
        product_id="BTC-USD", time=(NOW - timedelta(seconds=2)).isoformat(),
        bids=[{"price": "100", "size": "2"}],
        asks=[{"price": "101", "size": "3"}],
    ) | changes}


@pytest.fixture
def market_factory():
    clients = []

    def make(payload=None, *, handler=None, status=200, **options):
        requests = []

        def respond(request):
            requests.append(request)
            assert request.method == "GET"
            assert request.url.host == "api.coinbase.com"
            assert "authorization" not in request.headers
            assert request.headers["cache-control"] == "no-cache"
            if handler is not None:
                return handler(request)
            return httpx.Response(status, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return CoinbasePublicMarket(client=client, **options), requests

    yield make
    for client in clients:
        client.close()


def test_candles_public_contract_sort_dedupe_and_exclude_forming(market_factory):
    rows = [candle(BOUNDARY), candle(BOUNDARY - 60), candle(BOUNDARY - 180),
            candle(BOUNDARY - 60), candle(BOUNDARY + 60), candle(BOUNDARY - 240),
            candle(BOUNDARY - 120)]
    market, requests = market_factory({"candles": rows})
    result = market.candles("BTC-USD", 60, 3, now=NOW)
    assert [c.ts.timestamp() for c in result] == [BOUNDARY - 180, BOUNDARY - 120, BOUNDARY - 60]
    assert all(c.ts.tzinfo == UTC for c in result)
    assert (result[0].open, result[0].high, result[0].low,
            result[0].close, result[0].volume) == (100, 102, 99, 101, 2)
    request, = requests
    assert str(request.url).split("?")[0] == PUBLIC_BASE + "/products/BTC-USD/candles"
    assert dict(request.url.params) == {
        "start": str(BOUNDARY - 180), "end": str(BOUNDARY - 1),
        "granularity": "ONE_MINUTE", "limit": "3",
    }


@pytest.mark.parametrize("granularity,enum", [
    (60, "ONE_MINUTE"), (300, "FIVE_MINUTE"), (900, "FIFTEEN_MINUTE"),
    (1800, "THIRTY_MINUTE"), (3600, "ONE_HOUR"), (7200, "TWO_HOUR"),
    (14400, "FOUR_HOUR"), (21600, "SIX_HOUR"), (86400, "ONE_DAY"),
])
def test_all_documented_granularities(market_factory, granularity, enum):
    market, requests = market_factory({"candles": []})
    assert market.candles("ETH-USD", granularity, 2, now=NOW) == []
    params = requests[0].url.params
    end = int(NOW.timestamp()) // granularity * granularity
    assert params["granularity"] == enum
    assert int(params["start"]) == end - 2 * granularity
    assert int(params["end"]) == end - 1


@pytest.mark.parametrize("granularity", [0, -1, 1, 120, 60.0, True, "60", None])
def test_unsupported_granularity_rejected_before_http(market_factory, granularity):
    market, requests = market_factory()
    with pytest.raises(ValueError, match="granularity"):
        market.candles("BTC-USD", granularity, now=NOW)
    assert requests == []


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "3", None])
def test_invalid_limit(market_factory, limit):
    market, requests = market_factory()
    with pytest.raises(ValueError, match="limit"):
        market.candles("BTC-USD", 60, limit, now=NOW)
    assert requests == []


def test_history_paginates_all_buckets_without_boundary_loss(market_factory):
    start = BOUNDARY - 701 * 60

    def respond(request):
        params = request.url.params
        lower, upper = int(params["start"]), int(params["end"])
        assert upper - lower < 350 * 60
        assert int(params["limit"]) <= 350
        # Simulate an inclusive end API and reverse chronological responses.
        rows = [candle(ts) for ts in range(lower, upper + 1, 60)]
        return httpx.Response(200, json={"candles": list(reversed(rows))})

    market, requests = market_factory(handler=respond)
    result = market.candles_history("BTC-USD", 60, start, BOUNDARY, now=NOW)
    assert [c.ts.timestamp() for c in result] == list(range(start, BOUNDARY, 60))
    assert [int(r.url.params["limit"]) for r in requests] == [350, 350, 1]
    assert [int(r.url.params["start"]) for r in requests] == [
        start, start + 350 * 60, start + 700 * 60,
    ]


def test_large_recent_limit_uses_history_pagination(market_factory):
    market, requests = market_factory({"candles": []})
    assert market.candles("BTC-USD", 60, 351, now=NOW) == []
    assert [int(r.url.params["limit"]) for r in requests] == [350, 1]


def test_history_continues_after_empty_and_sparse_pages(market_factory):
    start = BOUNDARY - 701 * 60
    pages = iter([
        [], [candle(start + 350 * 60), candle(start + 350 * 60), candle(start)],
        [candle(BOUNDARY - 60), candle(BOUNDARY)],
    ])
    market, requests = market_factory(handler=lambda r: httpx.Response(
        200, json={"candles": next(pages)}))
    result = market.candles_history("BTC-USD", 60, start, BOUNDARY, now=NOW)
    assert [c.ts.timestamp() for c in result] == [start + 350 * 60, BOUNDARY - 60]
    assert len(requests) == 3


def test_history_honors_past_end_partial_boundaries_and_timezone(market_factory):
    start = datetime.fromtimestamp(BOUNDARY - 179.5, timezone(timedelta(hours=3)))
    end = BOUNDARY - 30
    market, requests = market_factory({"candles": [
        candle(BOUNDARY - 180), candle(BOUNDARY - 120), candle(BOUNDARY - 60),
    ]})
    result = market.candles_history("BTC-USD", 60, start, end, now=NOW)
    assert [c.ts.timestamp() for c in result] == [BOUNDARY - 120]
    assert int(requests[0].url.params["start"]) == BOUNDARY - 120
    assert int(requests[0].url.params["end"]) == BOUNDARY - 61


@pytest.mark.parametrize("now", [BOUNDARY, BOUNDARY + 30])
def test_history_future_end_never_returns_forming_candle(market_factory, now):
    market, requests = market_factory({"candles": [
        candle(BOUNDARY - 60), candle(BOUNDARY), candle(BOUNDARY + 60),
    ]})
    result = market.candles_history("BTC-USD", 60, BOUNDARY - 60,
                                    BOUNDARY + 600, now=now)
    assert [c.ts.timestamp() for c in result] == [BOUNDARY - 60]
    assert int(requests[0].url.params["end"]) == BOUNDARY - 1


@pytest.mark.parametrize("start,end", [(BOUNDARY, BOUNDARY + 30), (BOUNDARY + 60, BOUNDARY + 120)])
def test_no_closed_buckets_requires_no_request(market_factory, start, end):
    market, requests = market_factory()
    assert market.candles_history("BTC-USD", 60, start, end, now=NOW) == []
    assert requests == []


@pytest.mark.parametrize("start,end", [
    (2, 2), (2, 1), (-1, 100), (float("nan"), 100), (0, float("inf")),
    (datetime(2020, 1, 1), NOW), (True, NOW), ("123", NOW), (0, 1e30),
])
def test_bad_history_bounds(market_factory, start, end):
    market, requests = market_factory()
    with pytest.raises(ValueError):
        market.candles_history("BTC-USD", 60, start, end, now=NOW)
    assert requests == []


@pytest.mark.parametrize("payload", [{}, {"candles": None}, {"candles": {}}, [], {"error": "failed"}])
def test_invalid_candle_envelope(market_factory, payload):
    market, _ = market_factory(payload)
    with pytest.raises(MarketDataError):
        market.candles("BTC-USD", 60, 1, now=NOW)


@pytest.mark.parametrize("changes", [
    {"start": "NaN"}, {"start": "1.5"}, {"start": "1e30"}, {"start": None},
    {"open": "nan"}, {"close": "inf"}, {"low": "0"}, {"high": "-1"},
    {"volume": "-1"}, {"volume": "inf"}, {"close": "103"}, {"low": "101"},
    {"open": True}, {"high": None},
])
def test_invalid_candle_values(market_factory, changes):
    market, _ = market_factory({"candles": [candle(BOUNDARY - 60, **changes)]})
    with pytest.raises(ValueError):
        market.candles("BTC-USD", 60, 1, now=NOW)


def test_candle_arrays_from_other_api_are_not_silently_accepted(market_factory):
    market, _ = market_factory({"candles": [[BOUNDARY - 60, 99, 102, 100, 101, 2]]})
    with pytest.raises(MarketDataError):
        market.candles("BTC-USD", 60, 1, now=NOW)


def test_zero_volume_is_valid(market_factory):
    market, _ = market_factory({"candles": [candle(BOUNDARY - 60, volume="0")]})
    assert market.candles("BTC-USD", 60, 1, now=NOW)[0].volume == 0


def test_conflicting_duplicate_candles_fail(market_factory):
    market, _ = market_factory({"candles": [
        candle(BOUNDARY - 60), candle(BOUNDARY - 60, close="100"),
    ]})
    with pytest.raises(MarketDataError, match="Conflicting"):
        market.candles("BTC-USD", 60, 1, now=NOW)


def test_misaligned_candle_timestamp_rejected(market_factory):
    market, _ = market_factory({"candles": [candle(BOUNDARY - 59)]})
    with pytest.raises(MarketDataError, match="aligned"):
        market.candles("BTC-USD", 60, 2, now=NOW)


def test_history_does_not_return_partial_success_after_http_failure(market_factory):
    start = BOUNDARY - 351 * 60

    def respond(request):
        if int(request.url.params["start"]) == start:
            return httpx.Response(200, json={"candles": [candle(start)]})
        return httpx.Response(503, json={"error": "unavailable"})

    market, requests = market_factory(handler=respond)
    with pytest.raises(httpx.HTTPStatusError):
        market.candles_history("BTC-USD", 60, start, BOUNDARY, now=NOW)
    assert len(requests) == 2


@pytest.mark.parametrize("method", ["candles", "top_of_book"])
@pytest.mark.parametrize("now", [datetime(2026, 1, 1), float("nan"), float("inf"), True])
def test_bad_explicit_now_rejected_before_http(market_factory, method, now):
    market, requests = market_factory()
    with pytest.raises(ValueError):
        getattr(market, method)("BTC-USD", now=now)
    assert requests == []


@pytest.mark.parametrize("options", [
    {"max_age_seconds": 0}, {"max_age_seconds": float("inf")},
    {"max_spread_bps": float("nan")}, {"max_spread_bps": -1},
    {"future_tolerance_seconds": -1},
])
def test_invalid_sanity_configuration(market_factory, options):
    with pytest.raises(ValueError):
        market_factory(**options)


def test_top_of_book_uses_server_timestamp_and_public_endpoint(market_factory):
    market, requests = market_factory(book())
    quote = market.top_of_book("BTC-USD", now=NOW)
    assert (quote.product_id, quote.bid, quote.ask, quote.bid_size, quote.ask_size) == (
        "BTC-USD", 100, 101, 2, 3,
    )
    assert quote.ts == NOW - timedelta(seconds=2)
    assert quote.ts.tzinfo == UTC
    assert quote.spread_bps == pytest.approx(10000 / 100.5)
    request, = requests
    assert str(request.url).split("?")[0] == PUBLIC_BASE + "/product_book"
    assert dict(request.url.params) == {"product_id": "BTC-USD", "limit": "1"}


@pytest.mark.parametrize("age,match", [(30.001, "Stale"), (3600, "Stale"), (-5.001, "future")])
def test_stale_or_future_book_rejected_even_if_received_now(market_factory, age, match):
    market, _ = market_factory(book(time=(NOW - timedelta(seconds=age)).isoformat()))
    with pytest.raises(MarketDataError, match=match):
        market.top_of_book("BTC-USD", now=NOW)


@pytest.mark.parametrize("age", [30, -5, 0])
def test_quote_freshness_boundaries(market_factory, age):
    market, _ = market_factory(book(time=(NOW - timedelta(seconds=age)).isoformat()))
    assert market.top_of_book("BTC-USD", now=NOW).ts == NOW - timedelta(seconds=age)


@pytest.mark.parametrize("timestamp", [None, "", "garbage", "2026-01-01T12:00:30", 1767268830])
def test_invalid_server_timestamp_never_uses_local_receive_time(market_factory, timestamp):
    market, _ = market_factory(book(time=timestamp))
    with pytest.raises(MarketDataError, match="timestamp"):
        market.top_of_book("BTC-USD", now=NOW)


def test_missing_server_timestamp(market_factory):
    payload = book()
    del payload["pricebook"]["time"]
    market, _ = market_factory(payload)
    with pytest.raises(MarketDataError, match="timestamp"):
        market.top_of_book("BTC-USD", now=NOW)


def test_server_timestamp_accepts_z_and_normalizes_offset(market_factory):
    for timestamp in ["2026-01-01T12:00:28.000000000Z", "2026-01-01T15:00:28+03:00"]:
        market, _ = market_factory(book(time=timestamp))
        assert market.top_of_book("BTC-USD", now=NOW).ts == NOW - timedelta(seconds=2)


@pytest.mark.parametrize("side", ["bids", "asks"])
@pytest.mark.parametrize("field", ["price", "size"])
@pytest.mark.parametrize("value", ["NaN", "inf", "-inf", "0", "-1", None, True, "bad"])
def test_book_requires_finite_positive_prices_and_sizes(market_factory, side, field, value):
    payload = book()
    payload["pricebook"][side][0][field] = value
    market, _ = market_factory(payload)
    with pytest.raises(MarketDataError):
        market.top_of_book("BTC-USD", now=NOW)


@pytest.mark.parametrize("changes", [
    {"product_id": "ETH-USD"}, {"product_id": None}, {"bids": []}, {"asks": []},
    {"bids": None}, {"asks": {}}, {"bids": [[100, 1]]}, {"asks": [{}]},
])
def test_invalid_book_structure(market_factory, changes):
    market, _ = market_factory(book(**changes))
    with pytest.raises(MarketDataError):
        market.top_of_book("BTC-USD", now=NOW)


@pytest.mark.parametrize("bid,ask,match", [
    (100, 100, "Locked"), (101, 100, "crossed"), (1, 100, "spread"),
    (1e308, 1.1e308, "Non-finite"),
])
def test_book_spread_sanity(market_factory, bid, ask, match):
    market, _ = market_factory(book(bids=[{"price": str(bid), "size": "1"}],
                                    asks=[{"price": str(ask), "size": "1"}]))
    with pytest.raises(MarketDataError, match=match):
        market.top_of_book("BTC-USD", now=NOW)


def test_quote_sanity_limits_are_configurable(market_factory):
    market, _ = market_factory(book(), max_spread_bps=50)
    with pytest.raises(MarketDataError, match="spread"):
        market.top_of_book("BTC-USD", now=NOW)
    market, _ = market_factory(book(), max_age_seconds=1)
    with pytest.raises(MarketDataError, match="Stale"):
        market.top_of_book("BTC-USD", now=NOW)


@pytest.mark.parametrize("payload", [
    {"trade_id": "12345", "price": "100", "best_bid": "99", "best_ask": "101"},
    book(bids=[], asks=[], trade_id="12345", price="100"),
    {"pricebook": None}, [],
])
def test_never_fabricates_quote_or_falls_back_to_trade_id(market_factory, payload):
    market, requests = market_factory(payload)
    with pytest.raises(MarketDataError):
        market.top_of_book("BTC-USD", now=NOW)
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/product_book")


@pytest.mark.parametrize("method", ["candles", "top_of_book"])
@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_http_errors_propagate_without_fake_data(market_factory, method, status):
    market, _ = market_factory({"error": "unavailable"}, status=status)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        getattr(market, method)("BTC-USD", now=NOW)
    assert exc.value.response.status_code == status


@pytest.mark.parametrize("method", ["candles", "top_of_book"])
def test_invalid_json(market_factory, method):
    market, _ = market_factory(handler=lambda r: httpx.Response(200, text="not JSON"))
    with pytest.raises(MarketDataError, match="JSON"):
        getattr(market, method)("BTC-USD", now=NOW)


def test_timeout_propagates(market_factory):
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)
    market, _ = market_factory(handler=timeout)
    with pytest.raises(httpx.ReadTimeout):
        market.top_of_book("BTC-USD", now=NOW)


@pytest.mark.parametrize("product", ["", "../BTC-USD", "BTC-USD?x=1", None])
def test_invalid_product_rejected_without_http(market_factory, product):
    market, requests = market_factory()
    with pytest.raises(ValueError, match="product_id"):
        market.candles(product, now=NOW)
    with pytest.raises(ValueError, match="product_id"):
        market.top_of_book(product, now=NOW)
    assert requests == []


def test_original_signatures_use_clock_when_now_omitted(market_factory, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(NOW.timestamp(), tz)
    monkeypatch.setattr("tayder.data.market.datetime", FrozenDatetime)
    market, requests = market_factory({"candles": [candle(BOUNDARY - 60)]})
    assert len(market.candles("BTC-USD", 60, 1)) == 1
    market, _ = market_factory(book())
    assert market.top_of_book("BTC-USD").ts.timestamp() == NOW.timestamp() - 2


def test_client_lifecycle():
    with CoinbasePublicMarket() as market:
        assert not market._client.is_closed
    assert market._client.is_closed
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        with CoinbasePublicMarket(client=client):
            pass
        assert not client.is_closed
