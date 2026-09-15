"""Live contracts exercised via MockTransport and ephemeral signing keys only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from tayder.config import Settings
from tayder.execute.jwt_auth import build_jwt, validate_api_base
from tayder.execute.live import (
    LiveCoinbaseExecutor, MissingCredentialsExecutor, OrderAmbiguous, OrderRejected,
)
from tayder.models import Proposal, Side


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture(scope="module")
def credentials():
    key = ec.generate_private_key(ec.SECP256R1())
    return Settings(coinbase_api_key_name="organizations/test/apiKeys/ephemeral",
                    coinbase_api_private_key=pem(key)), key.public_key()


@pytest.fixture
def proposal():
    return Proposal("BTC-USD", Side.BUY, 5, "test", 99999, proposal_id="proposal-123")


@pytest.fixture
def product():
    return {
        "product_id": "BTC-USD", "product_type": "SPOT", "status": "online",
        "base_currency_id": "BTC", "quote_currency_id": "USD",
        "base_increment": "0.00000001", "quote_increment": "0.01",
        "base_min_size": "0.00001", "base_max_size": "100",
        "quote_min_size": "1", "quote_max_size": "1000000",
        "is_disabled": False, "trading_disabled": False, "cancel_only": False,
        "limit_only": False, "post_only": False, "auction_mode": False, "view_only": False,
    }


@pytest.fixture
def order(proposal):
    return {
        "order_id": "exchange-456", "client_order_id": proposal.proposal_id,
        "product_id": proposal.product_id, "side": proposal.side.value,
        "product_type": "SPOT", "status": "FILLED", "settled": True,
        "filled_size": "0.0001", "average_filled_price": "48000",
        "filled_value": "4.8", "total_fees": "0.0288",
        "last_fill_time": "2026-09-14T12:34:56.123Z",
    }


@pytest.fixture
def exchange(credentials, product):
    """Verify JWT signatures/URI on every mock request; no real transport exists."""
    clients = []

    def make(handler, *, permissions=None, metadata=None, follow_redirects=False):
        requests = []
        nonces = set()

        def dispatch(request):
            requests.append(request)
            assert request.url.scheme == "https"
            assert request.url.host == "api.coinbase.com"
            token = request.headers["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(token, credentials[1], algorithms=["ES256"])
            assert claims["uri"] == f"{request.method} api.coinbase.com{request.url.path}"
            assert claims["exp"] - claims["nbf"] == 120
            nonce = jwt.get_unverified_header(token)["nonce"]
            assert nonce not in nonces
            nonces.add(nonce)
            if request.url.path.endswith("/products/BTC-USD"):
                assert request.url.params["get_tradability_status"] == "true"
                return httpx.Response(200, json=product if metadata is None else metadata)
            if request.url.path.endswith("/key_permissions"):
                data = {"can_view": True, "can_trade": True, "can_transfer": False}
                return httpx.Response(200, json=data if permissions is None else permissions)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(dispatch), follow_redirects=follow_redirects)
        clients.append(client)
        return LiveCoinbaseExecutor(credentials[0], client), requests

    yield make
    for client in clients:
        client.close()


def accepted(request):
    assert request.method == "POST"
    assert request.url.path == "/api/v3/brokerage/orders"
    body = json.loads(request.content)
    return httpx.Response(200, json={"success": True, "success_response": {
        "order_id": "exchange-456", "client_order_id": body["client_order_id"],
        "product_id": body["product_id"], "side": body["side"],
    }})


def test_submit_is_deterministic_and_does_not_claim_a_fill(exchange, proposal):
    executor, requests = exchange(accepted)
    assert executor.submit(proposal, 5.019) == "exchange-456"
    assert executor.submit(proposal, 5.019) == "exchange-456"
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert json.loads(posts[0].content) == {
        "client_order_id": proposal.proposal_id, "product_id": "BTC-USD", "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": "5.01"}},
    }
    assert not any("historical" in r.url.path for r in requests)
    assert proposal.meta == {}


def test_prepare_sell_uses_owned_size_without_mutating_and_submit_uses_meta(exchange, proposal):
    executor, requests = exchange(accepted)
    proposal.side = Side.SELL
    sizing = executor.prepare(proposal, 9999, "0.000123459")
    assert sizing == {"base_size": "0.00012345"}
    assert proposal.meta == {}
    assert all(r.method == "GET" for r in requests)
    proposal.meta.update(sizing)
    assert executor.submit(proposal, 9999) == "exchange-456"
    assert json.loads(requests[-1].content)["order_configuration"] == {"market_market_ioc": sizing}


@pytest.mark.parametrize("amount", [None, 0, -1, float("nan"), float("inf"), "1e999", True])
def test_invalid_sell_size_never_requests_network(exchange, proposal, amount):
    executor, requests = exchange(accepted)
    proposal.side = Side.SELL
    proposal.meta["base_size"] = amount
    with pytest.raises(ValueError):
        executor.submit(proposal, 5)
    assert requests == []


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), "1e-999", True])
def test_invalid_buy_size_never_requests_network(exchange, proposal, amount):
    executor, requests = exchange(accepted)
    with pytest.raises(ValueError):
        executor.submit(proposal, amount)
    assert requests == []


def test_non_power_of_ten_increment_and_minimum_after_rounding(exchange, proposal, product):
    product.update(quote_increment="0.05", quote_min_size="1.03", quote_max_size="10")
    executor, requests = exchange(accepted)
    assert executor.prepare(proposal, 5.09) == {"quote_size": "5.05"}
    for amount in (1.04, 10.1):
        with pytest.raises(ValueError, match="size_out_of_bounds"):
            executor.submit(proposal, amount)
    assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize("field,value", [
    ("product_type", "FUTURE"), ("product_type", "UNKNOWN_PRODUCT_TYPE"),
    ("product_id", "ETH-USD"), ("quote_currency_id", "USDC"), ("status", "offline"),
    ("is_disabled", True), ("trading_disabled", True), ("cancel_only", True),
    ("limit_only", True), ("post_only", True), ("auction_mode", True), ("view_only", True),
    ("trading_disabled", None), ("base_increment", "0"), ("quote_increment", "NaN"),
    ("base_min_size", "0"), ("quote_max_size", "0.5"),
])
def test_product_restrictions_prevent_submission(exchange, proposal, product, field, value):
    product[field] = value
    executor, requests = exchange(accepted)
    with pytest.raises(ValueError):
        executor.submit(proposal, 5)
    assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize("permissions", [
    {}, {"can_view": True, "can_trade": True},
    {"can_view": False, "can_trade": True, "can_transfer": False},
    {"can_view": True, "can_trade": False, "can_transfer": False},
    {"can_view": True, "can_trade": True, "can_transfer": True},
    {"can_view": True, "can_trade": True, "can_transfer": "false"},
])
def test_permissions_fail_closed_before_post(exchange, proposal, permissions):
    executor, requests = exchange(accepted, permissions=permissions)
    with pytest.raises(ValueError, match="view_trade_without_transfer"):
        executor.submit(proposal, 5)
    assert all(r.method == "GET" for r in requests)


def test_explicit_create_rejection(exchange, proposal):
    executor, _ = exchange(lambda r: httpx.Response(200, json={
        "success": False, "error_response": {"error": "INSUFFICIENT_FUND"},
    }))
    with pytest.raises(OrderRejected, match="INSUFFICIENT_FUND"):
        executor.submit(proposal, 5)


@pytest.mark.parametrize("data", [
    {}, {"success": True}, {"success": True, "success_response": {}},
    {"success": "true", "success_response": {"order_id": "exchange-456"}},
    {"success": False}, {"success": False, "error_response": {}},
    {"success": False, "error_response": {"error": "UNKNOWN_FAILURE_REASON"}},
    {"success": False, "error_response": {"error": "DUPLICATE_CLIENT_ORDER_ID"}},
    {"success": False, "order_id": "exchange-456", "error_response": {"error": "REJECTED"}},
    {"success": True, "success_response": {"order_id": "exchange-456", "client_order_id": "other"}},
])
def test_uncertain_create_response_never_fabricates_id_or_fill(exchange, proposal, data):
    executor, requests = exchange(lambda r: httpx.Response(200, json=data))
    with pytest.raises(OrderAmbiguous):
        executor.submit(proposal, 5)
    assert sum(r.method == "POST" for r in requests) == 1


def test_lost_submit_response_recovers_by_paginated_lookup_then_actual_fill(exchange, proposal, order):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout("simulated lost response", request=request)
        if request.url.path.endswith("/batch"):
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"orders": [], "has_next": True, "cursor": "next+/="})
            assert request.url.params["cursor"] == "next+/="
            return httpx.Response(200, json={"orders": [order], "has_next": False})
        return httpx.Response(200, json={"order": order})

    executor, _ = exchange(handler)
    with pytest.raises(OrderAmbiguous, match="transport"):
        executor.submit(proposal, 5)
    order_id = executor.find_order(proposal.proposal_id)
    fill = executor.reconcile(proposal, order_id)
    assert len(posts) == 1
    assert fill.price == 48000  # Different from the proposal's 99999 signal.
    assert fill.size == 0.0001
    assert fill.notional_usd == 4.8  # Different from requested stake.
    assert fill.fee_usd == 0.0288
    assert fill.filled_at == datetime(2026, 9, 14, 12, 34, 56, 123000, tzinfo=timezone.utc)
    assert fill.mode == "live" and fill.order_id == "exchange-456"


@pytest.mark.parametrize("status", ["PENDING", "OPEN", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED", "PARTIALLY_FILLED"])
def test_open_orders_with_partial_execution_do_not_return_final_fill(exchange, proposal, order, status):
    order["status"] = status
    executor, _ = exchange(lambda r: httpx.Response(200, json={"order": order}))
    assert executor.reconcile(proposal, order["order_id"]) is None


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "EXPIRED", "FAILED", "REJECTED"])
def test_terminal_partial_sell_is_recorded_but_zero_execution_is_rejected(exchange, proposal, order, status):
    proposal.side = Side.SELL
    order.update(side="SELL", status=status)
    executor, _ = exchange(lambda r: httpx.Response(200, json={"order": order}))
    fill = executor.reconcile(proposal, order["order_id"])
    assert fill.side == Side.SELL and fill.size == 0.0001 and fill.fee_usd == 0.0288
    order.update(filled_size="0", filled_value="0", total_fees="0")
    with pytest.raises(OrderRejected):
        executor.reconcile(proposal, order["order_id"])


def test_settlement_pending_and_execute_wrapper_preserve_order_id(exchange, proposal, order):
    order["settled"] = False
    executor, _ = exchange(lambda r: accepted(r) if r.method == "POST"
                           else httpx.Response(200, json={"order": order}))
    assert executor.reconcile(proposal, order["order_id"]) is None
    with pytest.raises(OrderAmbiguous, match="pending") as error:
        executor.execute(proposal, 5)
    assert error.value.order_id == order["order_id"]


@pytest.mark.parametrize("field,value", [
    ("client_order_id", "other"), ("order_id", "other"), ("product_id", "ETH-USD"),
    ("side", "SELL"), ("product_type", "FUTURE"), ("status", "UNKNOWN_ORDER_STATUS"),
    ("filled_size", "NaN"), ("filled_size", "0"), ("average_filled_price", None),
    ("filled_value", "999"), ("filled_value", "Infinity"), ("total_fees", None),
    ("total_fees", "-1"), ("settled", None), ("last_fill_time", None),
    ("last_fill_time", "2026-09-14T12:34:56"),
])
def test_invalid_execution_data_stays_ambiguous(exchange, proposal, order, field, value):
    order[field] = value
    executor, _ = exchange(lambda r: httpx.Response(200, json={"order": order}))
    with pytest.raises(OrderAmbiguous) as error:
        executor.reconcile(proposal, "exchange-456")
    assert error.value.order_id == "exchange-456"


@pytest.mark.parametrize("response", [
    httpx.Response(404), httpx.Response(429), httpx.Response(500),
    httpx.Response(200, content=b"not json"), httpx.Response(200, json=[]),
    httpx.Response(200, json={}),
])
def test_lookup_or_reconcile_errors_are_not_absence_or_rejection(exchange, proposal, response):
    executor, _ = exchange(lambda r: response)
    with pytest.raises(OrderAmbiguous):
        executor.find_order(proposal.proposal_id)
    with pytest.raises(OrderAmbiguous):
        executor.reconcile(proposal, "exchange-456")


def test_find_order_consumes_all_pages_even_after_match(exchange, proposal, order):
    def handler(request):
        assert "order_status" not in request.url.params
        assert "start_date" not in request.url.params
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"orders": [order], "has_next": True, "cursor": "2"})
        return httpx.Response(200, json={"orders": [], "has_next": False})

    executor, requests = exchange(handler)
    assert executor.find_order(proposal.proposal_id) == order["order_id"]
    assert len(requests) == 2
    assert executor.find_order("absent") is None
    assert len(requests) == 4


@pytest.mark.parametrize("page", [
    {"orders": [], "has_next": True},
    {"orders": [], "has_next": True, "cursor": "loop"},
    {"orders": [], "has_next": "false"},
    {"orders": [], "has_next": False, "proof_token_required": True},
    {"orders": [{}], "has_next": False},
    {"orders": ["invalid"], "has_next": False},
])
def test_incomplete_or_cyclic_pagination_does_not_claim_absence(exchange, proposal, page):
    executor, requests = exchange(lambda r: httpx.Response(200, json=page))
    with pytest.raises(OrderAmbiguous):
        executor.find_order(proposal.proposal_id)
    assert len(requests) <= 2


def test_conflicting_order_ids_are_ambiguous(exchange, proposal, order):
    executor, _ = exchange(lambda r: httpx.Response(200, json={
        "orders": [order, {**order, "order_id": "another"}], "has_next": False,
    }))
    with pytest.raises(OrderAmbiguous, match="multiple_matching"):
        executor.find_order(proposal.proposal_id)


def account(uuid, currency, value, **kwargs):
    return {"uuid": uuid, "currency": currency, "active": True, "ready": True,
            "available_balance": {"currency": currency, "value": value},
            "hold": {"currency": currency, "value": "99999"}, **kwargs}


def test_balances_use_available_units_paginate_and_do_not_double_count(exchange):
    usd = account("usd-1", "USD", "5.25")

    def handler(request):
        assert request.url.path.endswith("/accounts")
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"accounts": [usd], "has_next": True, "cursor": "2"})
        return httpx.Response(200, json={"accounts": [
            usd, account("usd-2", "USD", "2.50"), account("btc", "BTC", "0.002"),
            account("inactive", "USD", "100", active=False),
            account("unready", "USD", "100", ready=False),
            account("deleted", "USD", "100", deleted_at="2025-01-01T00:00:00Z"),
        ], "has_next": False})

    executor, requests = exchange(handler)
    assert executor.balances() == {"USD": 7.75, "BTC": 0.002}
    assert len(requests) == 2


@pytest.mark.parametrize("rows", [
    [account("usd", "USD", "NaN")],
    [account("usd", "USD", "-1")],
    [account("usd", "USD", "5", available_balance={"currency": "BTC", "value": "5"})],
    [account("usd", "USD", "5"), account("usd", "USD", "6")],
    [{}],
])
def test_bad_balances_are_errors_not_zeros(exchange, rows):
    executor, _ = exchange(lambda r: httpx.Response(200, json={"accounts": rows, "has_next": False}))
    with pytest.raises(OrderAmbiguous):
        executor.balances()


@pytest.mark.parametrize("base", [
    "http://api.coinbase.com", "https://api.coinbase.com.evil.test", "https://evil.test",
    "https://api.coinbase.com@evil.test", "https://user:pass@api.coinbase.com",
    "https://api.coinbase.com:443", "https://api.coinbase.com:8443", "https://api.coinbase.com/path",
    "https://api.coinbase.com?x=1", "https://api.coinbase.com#fragment",
    "https://api.coinbase.com//", " https://api.coinbase.com", "https://api.coinbase.com\n",
    "https://API.COINBASE.COM", "https://api.coinbase.com.", "https://api-sandbox.coinbase.com",
])
def test_exact_origin_required_before_client_construction(credentials, monkeypatch, base):
    def forbidden_client(*args, **kwargs):
        pytest.fail("Client created before origin was validated")

    monkeypatch.setattr(httpx, "Client", forbidden_client)
    with pytest.raises(ValueError, match="https_api_coinbase_com"):
        LiveCoinbaseExecutor(replace(credentials[0], coinbase_api_base=base))


def test_canonical_base():
    assert validate_api_base("https://api.coinbase.com") == "https://api.coinbase.com"
    assert validate_api_base("https://api.coinbase.com/") == "https://api.coinbase.com"


@pytest.mark.parametrize("location", ["https://evil.test/steal", "https://api.coinbase.com/replay"])
def test_post_redirects_never_follow_even_with_injected_client_policy(exchange, proposal, location):
    executor, requests = exchange(lambda r: httpx.Response(307, headers={"Location": location}),
                                 follow_redirects=True)
    with pytest.raises(OrderAmbiguous, match="http_307"):
        executor.submit(proposal, 5)
    assert sum(r.method == "POST" for r in requests) == 1


@pytest.mark.parametrize("kwargs", [
    {"host": "evil.test"}, {"ttl_seconds": 121}, {"ttl_seconds": 0}, {"ttl_seconds": True},
    {"path": "/api/v3/brokerage/accounts?cursor=2"}, {"path": "//evil.test"},
    {"path": "/api/v3/brokerage/../orders"}, {"method": "GET\nPOST"},
])
def test_jwt_rejects_unsafe_binding_and_expiry(credentials, kwargs):
    params = {"method": "GET", "path": "/api/v3/brokerage/accounts", **kwargs}
    with pytest.raises(ValueError):
        build_jwt(credentials[0].coinbase_api_key_name, credentials[0].private_key_pem(), **params)


@pytest.mark.parametrize("key", [ec.generate_private_key(ec.SECP384R1()), ed25519.Ed25519PrivateKey.generate()])
def test_jwt_requires_p256_key(key):
    with pytest.raises(ValueError, match="es256_p256"):
        build_jwt("test", pem(key), "GET", "/api/v3/brokerage/accounts")


def test_missing_credentials_split_interface_fails_without_network(proposal):
    executor = MissingCredentialsExecutor()
    for method, args in (("submit", (proposal, 5)), ("execute", (proposal, 5)),
                         ("reconcile", (proposal, "id")), ("find_order", (proposal.proposal_id,)),
                         ("balances", ()), ("verify_permissions", ()),
                         ("prepare", (proposal, 5)), ("validate_product", ("BTC-USD",))):
        with pytest.raises(ValueError, match="credentials_missing"):
            getattr(executor, method)(*args)
    executor.close()
