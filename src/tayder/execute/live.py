"""Coinbase Advanced Trade spot execution with durable-intent friendly APIs.

Persist proposal_id and sizing BEFORE submit; persist the returned order_id
before reconcile. After ambiguous submission, find_order uses that proposal_id.
An absent listing is not proof that submission failed.

Contracts: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api
(/orders/create-order, /orders/get-order, /orders/list-orders,
 /products/get-product, /accounts/list-accounts, /data-api/get-api-key-permissions).
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any, Iterator, Protocol

import httpx

from tayder.config import Settings
from tayder.execute.jwt_auth import auth_headers, validate_api_base
from tayder.models import Fill, Proposal, Side

PREFIX = "/api/v3/brokerage"
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "FAILED", "REJECTED"})
PENDING_STATUSES = frozenset({"PENDING", "OPEN", "QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED", "PARTIALLY_FILLED"})


class OrderRejected(RuntimeError):
    """Definitive exchange rejection or terminal order with no execution."""


class OrderAmbiguous(RuntimeError):
    """Keep the persisted intent unresolved; retry lookup/reconciliation.

    order_id is provided when known. Never infer rejection from this exception.
    """

    def __init__(self, message: str, *, order_id: str | None = None) -> None:
        super().__init__(message)
        self.order_id = order_id


class Executor(Protocol):
    def execute(self, proposal: Proposal, stake_usd: float) -> Fill: ...


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid_{name}") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"invalid_{name}")
    # Fill and risk models use floats; reject overflow/underflow at that boundary.
    if not math.isfinite(float(number)) or (number != 0 and float(number) == 0):
        raise ValueError(f"invalid_{name}")
    return number


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"invalid_{name}")
    return value


class LiveCoinbaseExecutor:
    """USD-quoted SPOT market IOC orders; no automatic submission retries."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._base = validate_api_base(settings.coinbase_api_base)
        self._settings = settings
        self._client = client or httpx.Client(timeout=20.0, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = auth_headers(
            self._settings.coinbase_api_key_name,
            self._settings.private_key_pem(),
            method,
            path,
        )
        try:
            # Override even an injected client's redirect policy. Never forward
            # credentials or replay an order through a redirect.
            response = self._client.request(
                method, self._base + path, headers=headers,
                follow_redirects=False, **kwargs,
            )
        except httpx.RequestError:
            raise OrderAmbiguous("coinbase_transport_error") from None
        # HTTP errors alone cannot establish whether an earlier submission with
        # this client_order_id executed (including 404, 409 and 5xx).
        if not response.is_success:
            raise OrderAmbiguous(f"coinbase_http_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise OrderAmbiguous("coinbase_invalid_json") from None
        if not isinstance(data, dict):
            raise OrderAmbiguous("coinbase_invalid_response")
        return data

    def verify_permissions(self) -> dict[str, Any]:
        """Require view + trade and explicitly disabled transfer permission.

        Fail closed if the endpoint is unavailable or omits any permission.
        Called on every submission; reconciliation only needs view permission.
        """
        data = self._request("GET", f"{PREFIX}/key_permissions")
        if (data.get("can_view") is not True or data.get("can_trade") is not True
                or data.get("can_transfer") is not False):
            raise ValueError("live_requires_view_trade_without_transfer")
        return data

    def validate_product(self, product_id: str) -> dict[str, Any]:
        """Fetch current metadata and require a tradable USD spot market.

        Returns Coinbase metadata, including base/quote increments and limits.
        USD is required because Fill has USD amounts and no FX conversion.
        """
        _identifier(product_id, "product_id")
        data = self._request("GET", f"{PREFIX}/products/{product_id}",
                             params={"get_tradability_status": "true"})
        if data.get("product_id") != product_id or data.get("product_type") != "SPOT":
            raise ValueError("live_requires_spot_product")
        if (data.get("quote_currency_id") != "USD"
                or product_id != f"{data.get('base_currency_id')}-USD"):
            raise ValueError("live_requires_usd_quote")
        if str(data.get("status", "")).lower() != "online":
            raise ValueError("product_not_online")
        for flag in ("is_disabled", "trading_disabled", "cancel_only", "limit_only",
                     "post_only", "auction_mode"):
            if data.get(flag) is not False:
                raise ValueError(f"product_not_market_tradable:{flag}")
        # view_only is optional in the schema, requested explicitly above.
        if "view_only" in data and data["view_only"] is not False:
            raise ValueError("product_not_market_tradable:view_only")
        for unit in ("base", "quote"):
            increment = _decimal(data.get(f"{unit}_increment"), f"{unit}_increment", positive=True)
            minimum = _decimal(data.get(f"{unit}_min_size"), f"{unit}_min_size", positive=True)
            maximum = _decimal(data.get(f"{unit}_max_size"), f"{unit}_max_size", positive=True)
            if minimum > maximum or increment > maximum:
                raise ValueError(f"invalid_{unit}_limits")
        return data

    @staticmethod
    def _size(value: Any, unit: str, product: dict[str, Any]) -> str:
        amount = _decimal(value, f"{unit}_size", positive=True)
        increment = _decimal(product[f"{unit}_increment"], "increment", positive=True)
        with localcontext() as context:
            context.prec = 80
            # quantize alone is wrong for increments such as 0.05.
            rounded = (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        if (rounded <= 0 or rounded < _decimal(product[f"{unit}_min_size"], "minimum")
                or rounded > _decimal(product[f"{unit}_max_size"], "maximum")):
            raise ValueError(f"{unit}_size_out_of_bounds")
        return format(rounded, "f")

    def prepare(
        self, proposal: Proposal, stake_usd: float, base_size: Any = None,
    ) -> dict[str, str]:
        """Validate and return rounded IOC sizing, without POST or mutation.

        For SELL, base_size can be supplied from the journal's owned inventory;
        otherwise proposal.meta['base_size'] is required. Persist the returned
        base_size in proposal.meta before submit. Balance/ownership policy belongs
        to the worker; this helper never substitutes the entire account balance.
        """
        _identifier(proposal.proposal_id, "proposal_id")
        _identifier(proposal.product_id, "product_id")
        if not isinstance(proposal.side, Side):
            raise ValueError("invalid_side")
        unit = "quote" if proposal.side == Side.BUY else "base"
        amount = stake_usd if proposal.side == Side.BUY else (
            base_size if base_size is not None else proposal.meta.get("base_size")
        )
        _decimal(amount, f"{unit}_size", positive=True)
        product = self.validate_product(proposal.product_id)
        return {f"{unit}_size": self._size(amount, unit, product)}

    def submit(self, proposal: Proposal, stake_usd: float, *, before_submit=None) -> str:
        """Submit once using proposal_id as client_order_id; return exchange ID.

        Local validation raises ValueError before POST. OrderRejected is a
        definitive create-order rejection. All uncertain responses raise
        OrderAmbiguous. This method never constructs a Fill or mutates proposal.
        """
        sizing = self.prepare(proposal, stake_usd)
        self.verify_permissions()
        body = {
            "client_order_id": proposal.proposal_id,
            "product_id": proposal.product_id,
            "side": proposal.side.value,
            "order_configuration": {"market_market_ioc": sizing},
        }
        if before_submit is not None:
            before_submit()
        data = self._request("POST", f"{PREFIX}/orders", json=body)
        success = data.get("success_response")
        if data.get("success") is True and isinstance(success, dict):
            try:
                order_id = _identifier(success.get("order_id"), "order_id")
            except ValueError:
                raise OrderAmbiguous("coinbase_missing_order_id") from None
            for key, expected in (("client_order_id", proposal.proposal_id),
                                  ("product_id", proposal.product_id), ("side", proposal.side.value)):
                if key in success and success[key] != expected:
                    raise OrderAmbiguous("coinbase_submission_identity_mismatch", order_id=order_id)
            return order_id
        error = data.get("error_response")
        if (data.get("success") is False and isinstance(error, dict)
                and isinstance(error.get("error"), str) and error["error"]
                and not success and not data.get("order_id")):
            # Unknown/internal/duplicate failures can refer to an earlier accepted
            # request. Preserve the intent until lookup resolves it.
            code = error["error"]
            if any(word in code.upper() for word in ("UNKNOWN", "INTERNAL", "DUPLICATE", "TIMEOUT")):
                raise OrderAmbiguous("coinbase_create_uncertain")
            raise OrderRejected(f"coinbase_order_rejected:{code}")
        raise OrderAmbiguous("coinbase_create_uncertain")

    def reconcile(self, proposal: Proposal, order_id: str) -> Fill | None:
        """Return actual cumulative execution only after terminal settlement.

        Pending/partially-filled open orders return None. Terminal canceled,
        expired or failed orders with positive execution return that partial
        Fill; zero execution raises OrderRejected. Missing/inconsistent execution
        data is ambiguous, never replaced by the signal price or estimated fees.
        """
        _identifier(order_id, "order_id")
        try:
            data = self._request("GET", f"{PREFIX}/orders/historical/{order_id}")
            order = data.get("order")
            if not isinstance(order, dict):
                raise OrderAmbiguous("coinbase_missing_order")
            for key, expected in (("order_id", order_id), ("client_order_id", proposal.proposal_id),
                                  ("product_id", proposal.product_id), ("side", proposal.side.value)):
                if order.get(key) != expected:
                    raise OrderAmbiguous("coinbase_order_identity_mismatch")
            if order.get("product_type") != "SPOT" or not proposal.product_id.endswith("-USD"):
                raise OrderAmbiguous("coinbase_order_not_usd_spot")
            status = order.get("status")
            if status in PENDING_STATUSES:
                return None
            if status not in TERMINAL_STATUSES:
                raise OrderAmbiguous("coinbase_unknown_order_status")
            size = _decimal(order.get("filled_size"), "filled_size")
            if size == 0:
                # Do not discard a contradictory positive value or fee.
                value = _decimal(order.get("filled_value"), "filled_value")
                fee = _decimal(order.get("total_fees"), "total_fees")
                if status == "FILLED" or value != 0 or fee != 0:
                    raise OrderAmbiguous("coinbase_inconsistent_zero_fill")
                raise OrderRejected(f"coinbase_order_{status.lower()}")
            if order.get("settled") is False:
                return None
            if order.get("settled") is not True:
                raise OrderAmbiguous("coinbase_missing_settlement")
            price = _decimal(order.get("average_filled_price"), "average_filled_price", positive=True)
            value = _decimal(order.get("filled_value"), "filled_value", positive=True)
            fee = _decimal(order.get("total_fees"), "total_fees")
            # Exchange averages may be rounded; filled_value is authoritative.
            if not math.isclose(float(size * price), float(value), rel_tol=1e-5, abs_tol=1e-8):
                raise OrderAmbiguous("coinbase_inconsistent_fill_value")
            filled_at = datetime.fromisoformat(str(order.get("last_fill_time")).replace("Z", "+00:00"))
            if filled_at.tzinfo is None:
                raise ValueError("invalid_last_fill_time")
            return Fill(
                proposal_id=proposal.proposal_id, product_id=proposal.product_id,
                side=proposal.side, price=float(price), size=float(size),
                fee_usd=float(fee), notional_usd=float(value), mode="live",
                order_id=order_id, filled_at=filled_at.astimezone(timezone.utc),
            )
        except OrderAmbiguous as exc:
            exc.order_id = order_id
            raise
        except (ValueError, InvalidOperation):
            raise OrderAmbiguous("coinbase_invalid_execution_data", order_id=order_id) from None

    def _pages(self, path: str, field: str) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 100}
        cursors: set[str] = set()
        while True:
            data = self._request("GET", path, params=params)
            rows = data.get(field)
            if (not isinstance(rows, list) or type(data.get("has_next")) is not bool
                    or data.get("proof_token_required") is True):
                raise OrderAmbiguous("coinbase_incomplete_listing")
            for row in rows:
                if not isinstance(row, dict):
                    raise OrderAmbiguous("coinbase_invalid_listing_entry")
                yield row
            if not data["has_next"]:
                return
            cursor = data.get("cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise OrderAmbiguous("coinbase_invalid_pagination")
            cursors.add(cursor)
            params["cursor"] = cursor

    def find_order(self, proposal_id: str) -> str | None:
        """Search every orders page by client_order_id, without status filters.

        None means absent from the complete listing, not definitive rejection:
        order visibility can lag submission. Errors/incomplete pagination raise.
        """
        _identifier(proposal_id, "proposal_id")
        found: str | None = None
        for order in self._pages(f"{PREFIX}/orders/historical/batch", "orders"):
            if not isinstance(order.get("client_order_id"), str):
                raise OrderAmbiguous("coinbase_missing_client_order_id")
            if order["client_order_id"] != proposal_id:
                continue
            try:
                order_id = _identifier(order.get("order_id"), "order_id")
            except ValueError:
                raise OrderAmbiguous("coinbase_missing_order_id") from None
            if found is not None and found != order_id:
                raise OrderAmbiguous("coinbase_multiple_matching_orders")
            found = order_id
        return found

    def account_balances(self) -> dict[str, float]:
        """Available (not held) balances by currency across all account pages.

        Active, ready accounts only; repeat UUIDs are counted once. These are
        currency units, not USD valuations. Errors never become zero balances.
        """
        balances: dict[str, Decimal] = {}
        seen: dict[str, dict[str, Any]] = {}
        try:
            for account in self._pages(f"{PREFIX}/accounts", "accounts"):
                account_id = _identifier(account.get("uuid"), "account_id")
                if account_id in seen:
                    if seen[account_id] != account:
                        raise OrderAmbiguous("coinbase_account_changed_during_pagination")
                    continue
                seen[account_id] = account
                if account.get("active") is False or account.get("ready") is False or account.get("deleted_at"):
                    continue
                if account.get("active") is not True or account.get("ready") is not True:
                    raise ValueError("invalid_account_state")
                currency = _identifier(account.get("currency"), "account_currency")
                available = account.get("available_balance")
                if not isinstance(available, dict) or available.get("currency") != currency:
                    raise ValueError("invalid_balance_currency")
                balance = _decimal(available.get("value"), "available_balance")
                balances[currency] = balances.get(currency, Decimal(0)) + balance
            return {currency: float(_decimal(value, "balance")) for currency, value in balances.items()}
        except (ValueError, InvalidOperation):
            raise OrderAmbiguous("coinbase_invalid_account_data") from None

    def balances(self) -> dict[str, float]:
        """Worker-facing alias for paginated available account balances."""
        return self.account_balances()

    def execute(self, proposal: Proposal, stake_usd: float) -> Fill:
        """One-shot convenience only; durable workers must use the split APIs."""
        order_id = self.submit(proposal, stake_usd)
        fill = self.reconcile(proposal, order_id)
        if fill is None:
            raise OrderAmbiguous("coinbase_order_pending", order_id=order_id)
        return fill


class MissingCredentialsExecutor:
    """Fail before network when LIVE credentials are absent."""

    def close(self) -> None:
        pass

    def execute(self, proposal: Proposal, stake_usd: float) -> Fill:
        raise ValueError("live_credentials_missing")

    def submit(self, proposal: Proposal, stake_usd: float) -> str:
        raise ValueError("live_credentials_missing")

    def reconcile(self, proposal: Proposal, order_id: str) -> Fill | None:
        raise ValueError("live_credentials_missing")

    def find_order(self, proposal_id: str) -> str | None:
        raise ValueError("live_credentials_missing")

    def verify_permissions(self) -> dict[str, Any]:
        raise ValueError("live_credentials_missing")

    def validate_product(self, product_id: str) -> dict[str, Any]:
        raise ValueError("live_credentials_missing")

    def account_balances(self) -> dict[str, float]:
        raise ValueError("live_credentials_missing")

    def balances(self) -> dict[str, float]:
        raise ValueError("live_credentials_missing")

    def prepare(
        self, proposal: Proposal, stake_usd: float, base_size: Any = None,
    ) -> dict[str, str]:
        raise ValueError("live_credentials_missing")
