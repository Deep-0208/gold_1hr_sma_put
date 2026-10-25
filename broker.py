"""
═════════════════════════════════════════════════════════════
  BROKER — Delta Exchange REST API Client (PAXGUSD)
═════════════════════════════════════════════════════════════

Thin, fail-loud wrapper around Delta Exchange v2 REST API:
  - Product / contract spec resolution (contract_value verified)
  - Completed H1 candle fetching (forming bar dropped)
  - Ticker (best bid / ask from `quotes`, LTP, mark)
  - Market orders with client_order_id + fill confirmation
  - Position-level bracket SL/TP (POST /v2/orders/bracket)
  - Reduce-only position close + stale stop-order cleanup
  - Position query that distinguishes FLAT from UNKNOWN

Verified against docs.delta.exchange (2026-10-17):
  * client_order_id max length 32
  * order state: open | pending | closed | cancelled
  * GET /v2/orders/{id}, GET /v2/orders/client_order_id/{oid}
  * Position bracket = POST /v2/orders/bracket (PUT edits an ORDER's bracket)
  * Ticker best_bid / best_ask live under result["quotes"]
"""

import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests

from config import CONFIG, IST, log, log_broker, log_network, now_ist
from delta_auth import get_auth_headers


@dataclass
class TickData:
    """Live ticker snapshot."""
    bid: float
    ask: float
    ltp: float            # Last traded price (exit trigger price, like NIFTY spot LTP)
    mark: float
    spread: float
    time: datetime        # Ticker timestamp (UTC)
    funding_rate: float = 0.0


@dataclass
class PositionInfo:
    """Open Delta Exchange position on the configured product."""
    product_id: int
    symbol: str
    direction: str        # "LONG" / "SHORT"
    size: int             # absolute number of contracts
    entry_price: float
    unrealized_pnl: float
    liquidation_price: Optional[float] = None   # Delta's own figure (mark-price based)
    margin_mode: str = ""
    auto_topup: Optional[bool] = None


@dataclass
class OrderResult:
    """Outcome of a market order after fill confirmation."""
    order_id: Optional[int]
    client_order_id: str
    state: str            # closed | open | pending | cancelled | UNKNOWN
    filled_qty: int
    avg_price: Optional[float]


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """float() that tolerates None / '' / garbage (Delta returns numbers as strings)."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def make_client_order_id(prefix: str) -> str:
    """Delta client_order_id (max 32 chars), e.g. g1sE20261017143005123456."""
    return f"{prefix}{now_ist().strftime('%Y%m%d%H%M%S%f')}"[:32]


class DeltaBroker:
    """Delta Exchange REST client for one perpetual product."""

    _MIN_CALL_INTERVAL = 0.22  # ~4.5 req/s, under Delta's per-IP limits

    def __init__(self):
        self._connected = False
        self._symbol = CONFIG.get("delta_symbol", "PAXGUSD")
        self._base_url = CONFIG.get("delta_base_url", "https://api.india.delta.exchange")
        self._api_key = os.getenv("DELTA_API_KEY", "")
        self._api_secret = os.getenv("DELTA_API_SECRET", "")
        self._fetch_count = 0
        self._last_api_call_ts = 0.0

        # Resolved in connect()
        self.product_id: Optional[int] = None
        self.contract_value: float = float(CONFIG.get("expected_contract_value", 0.001))
        self.tick_size: float = 0.01
        # Margin spec (percent values, as Delta publishes them: 1 = 1%)
        self.default_leverage: Optional[float] = None
        self.initial_margin_pct: Optional[float] = None
        self.maintenance_margin_pct: Optional[float] = None
        # Account settings — read at connect() when credentials allow (never changed by the bot)
        self.account_leverage: Optional[float] = None
        self.account_auto_topup: Optional[bool] = None

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Gold1HrSMA/1.0",
        })

    # ─────────────────────────────────────────
    # LOW-LEVEL HTTP
    # ─────────────────────────────────────────

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_api_call_ts
        if elapsed < self._MIN_CALL_INTERVAL:
            time.sleep(self._MIN_CALL_INTERVAL - elapsed + random.uniform(0, 0.05))
        self._last_api_call_ts = time.time()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        auth: bool = False,
    ) -> Tuple[Optional[Dict], Optional[int]]:
        """
        Perform one HTTP call.

        Returns (data, status):
          (dict, 200)   success
          (None, 4xx)   definitive API rejection / not found
          (None, None)  network failure — outcome UNKNOWN
        """
        self._rate_limit()
        self._fetch_count += 1
        m = method.upper()
        url = self._base_url + path
        body_str = json.dumps(body) if body else ""
        headers = None
        if auth:
            query_string = "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
            headers = get_auth_headers(
                api_key=self._api_key,
                api_secret=self._api_secret,
                method=m,
                path=path,
                query_string=query_string,
                body=body_str,
            )

        t0 = time.time()
        try:
            resp = self._session.request(
                m, url, params=params, data=body_str or None, headers=headers, timeout=15,
            )
        except requests.exceptions.RequestException as e:
            log_network.error("[NETWORK] ❌ %s %s failed after %.0fms: %s", m, path, (time.time() - t0) * 1000, e)
            log_broker.error("[BROKER] ❌ Network error on %s %s: %s", m, path, e)
            return None, None

        latency_ms = (time.time() - t0) * 1000
        log_network.debug("[NETWORK] %s %s | %d | %.0fms | params=%s", m, path, resp.status_code, latency_ms, params)

        try:
            data = resp.json()
        except ValueError:
            data = None

        if resp.status_code >= 400 or not isinstance(data, dict) or data.get("success") is False:
            err = data.get("error") if isinstance(data, dict) else resp.text[:300]
            log_network.error("[NETWORK] ❌ %s %s | HTTP %d | %s", m, path, resp.status_code, err)
            if resp.status_code != 404:
                log_broker.error("[BROKER] ❌ API error on %s %s (HTTP %d): %s", m, path, resp.status_code, err)
            # 5xx is ambiguous for writes — report as UNKNOWN
            return None, (None if resp.status_code >= 500 else resp.status_code)

        return data, resp.status_code

    # ─────────────────────────────────────────
    # CONNECTION & PRODUCT SPEC
    # ─────────────────────────────────────────

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def connect(self) -> bool:
        """
        Resolve product spec, verify contract_value, and (if credentials exist)
        validate them via the wallet endpoint. LIVE mode requires valid credentials.
        """
        is_live = CONFIG.get("trading_mode", "PAPER") == "LIVE"

        if not self._resolve_product():
            return False

        balance = None
        if self.has_credentials:
            data, _ = self._request("GET", "/v2/wallet/balances", auth=True)
            if data is not None:
                for asset in data.get("result", []):
                    if asset.get("asset_symbol") in ("USD", "USDT"):
                        balance = _to_float(asset.get("balance"), 0.0)
                        break
                log_broker.info("[BROKER] ✅ Delta Exchange API credentials validated.")
                self.read_account_margin_settings()

        if balance is None:
            if is_live:
                log_broker.error(
                    "[BROKER] ❌ LIVE mode needs valid DELTA_API_KEY / DELTA_API_SECRET "
                    "(and a whitelisted IP). Refusing to start."
                )
                return False
            log_broker.warning("[BROKER] ⚠️ No authenticated wallet — PAPER mode on public market data.")

        self._connected = True
        log.info(
            "[BROKER] ✅ Connected | Mode: %s | %s (product_id=%d) | 1 contract = %s PAXG | "
            "Tick: $%.2f | Taker fee: %.4f%% | Balance: %s",
            CONFIG.get("trading_mode"), self._symbol, self.product_id, self.contract_value,
            self.tick_size, CONFIG["taker_fee_pct"] * 100,
            f"${balance:,.2f}" if balance is not None else "N/A (paper)",
        )
        return True

    def _resolve_product(self) -> bool:
        data, _ = self._request("GET", f"/v2/products/{self._symbol}")
        if data is None:
            log_broker.error("[BROKER] ❌ Could not fetch product spec for %s.", self._symbol)
            return False

        prod = data.get("result") or {}
        if prod.get("contract_type") != "perpetual_futures" or prod.get("state") != "live":
            log_broker.error(
                "[BROKER] ❌ %s is not a live perpetual (type=%s, state=%s).",
                self._symbol, prod.get("contract_type"), prod.get("state"),
            )
            return False

        self.product_id = int(prod["id"])
        self.contract_value = _to_float(prod.get("contract_value"), 0.0)
        self.tick_size = _to_float(prod.get("tick_size"), 0.01)

        # Fail loudly if the contract size changed — position sizing and P&L depend on it (AGENTS §6.1)
        expected = float(CONFIG["expected_contract_value"])
        if abs(self.contract_value - expected) > 1e-12:
            log_broker.critical(
                "[BROKER] 🛑 CONTRACT VALUE MISMATCH: Delta says 1 contract = %s PAXG, "
                "config expects %s. Update CONFIG['expected_contract_value'] and "
                "CONFIG['contract_qty'] before trading.",
                self.contract_value, expected,
            )
            return False

        taker = _to_float(prod.get("taker_commission_rate"))
        if taker is not None and taker >= 0:
            CONFIG["taker_fee_pct"] = taker

        self.default_leverage = _to_float(prod.get("default_leverage"))
        self.initial_margin_pct = _to_float(prod.get("initial_margin"))
        self.maintenance_margin_pct = _to_float(prod.get("maintenance_margin"))

        log_broker.info(
            "[BROKER] ✅ Product resolved: %s → id=%d | contract_value=%s | tick=%s | taker=%s | "
            "default_leverage=%sx | IM=%s%% | MM=%s%%",
            self._symbol, self.product_id, self.contract_value, self.tick_size, taker,
            self.default_leverage, self.initial_margin_pct, self.maintenance_margin_pct,
        )
        return True

    def read_account_margin_settings(self) -> None:
        """
        Read (never change) the account's order leverage for this product and its
        default auto top-up flag. Values stay None if the API refuses (e.g. IP not
        whitelisted) — callers must treat None as UNVERIFIED, not as a default.
        """
        data, _ = self._request("GET", f"/v2/products/{self.product_id}/orders/leverage", auth=True)
        if data is not None:
            self.account_leverage = _to_float((data.get("result") or {}).get("leverage"))
        data, _ = self._request("GET", "/v2/users/trading_preferences", auth=True)
        if data is not None:
            flag = (data.get("result") or {}).get("default_auto_topup")
            self.account_auto_topup = flag if isinstance(flag, bool) else None

        log.info(
            "[BROKER] ⚙️ Account margin settings (read-only, bot never changes them): leverage=%s | auto top-up=%s",
            f"{self.account_leverage:g}x" if self.account_leverage else "UNVERIFIED",
            {True: "ON", False: "OFF", None: "UNVERIFIED"}[self.account_auto_topup],
        )

    def disconnect(self) -> None:
        if self._connected:
            self._session.close()
            self._connected = False
            log_broker.info("[BROKER] 🔌 Delta Exchange session closed.")

    # ─────────────────────────────────────────
    # CANDLES
    # ─────────────────────────────────────────

    def get_candles(self, count: int, include_forming: bool = False) -> List[Dict[str, Any]]:
        """
        Return the last `count` H1 candles (oldest first), timestamps aware
        in both IST (`date`) and UTC (`date_utc`). The forming bar is dropped
        unless include_forming=True (AGENTS §4.2). Returns [] on failure.
        """
        bar_s = int(CONFIG["candle_tf_minutes"]) * 60
        now_ts = int(time.time())
        start_ts = now_ts - (count + 3) * bar_s

        data, _ = self._request("GET", "/v2/history/candles", params={
            "symbol": self._symbol,
            "resolution": CONFIG["delta_resolution"],
            "start": str(start_ts),
            "end": str(now_ts),
        })
        if data is None:
            log_broker.error("[BROKER] ❌ Failed to fetch candles.")
            return []

        by_time: Dict[int, Dict[str, Any]] = {}
        for bar in data.get("result", []) or []:
            if not isinstance(bar, dict):
                continue
            ts = int(_to_float(bar.get("time"), 0) or 0)
            o, h, l, c = (_to_float(bar.get(k)) for k in ("open", "high", "low", "close"))
            if not ts or None in (o, h, l, c):
                continue
            utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            by_time[ts] = {
                "date": utc_dt.astimezone(IST),
                "date_utc": utc_dt,
                "open": o, "high": h, "low": l, "close": c,
                "volume": _to_float(bar.get("volume"), 0.0),
            }

        candles = [by_time[ts] for ts in sorted(by_time)]

        if not include_forming and candles:
            if candles[-1]["date_utc"] + timedelta(seconds=bar_s) > datetime.now(timezone.utc):
                candles = candles[:-1]

        candles = candles[-count:]
        if candles:
            log_broker.debug(
                "[BROKER] %d candles (API call #%d) | latest %s IST C=$%.2f",
                len(candles), self._fetch_count, candles[-1]["date"].strftime("%Y-%m-%d %H:%M"), candles[-1]["close"],
            )
        return candles

    @staticmethod
    def get_candle_status() -> Dict[str, Any]:
        """Candle boundary math from the UTC clock (H1 bars start on the hour UTC)."""
        bar_s = int(CONFIG["candle_tf_minutes"]) * 60
        now_ts = int(time.time())
        forming_open = (now_ts // bar_s) * bar_s
        close_ts = forming_open + bar_s
        return {
            "forming_open_time": forming_open,
            "last_closed_bar_time": forming_open - bar_s,
            "seconds_left": max(0, close_ts - now_ts),
            "expected_close_ist": datetime.fromtimestamp(close_ts, tz=timezone.utc).astimezone(IST),
        }

    # ─────────────────────────────────────────
    # TICKER
    # ─────────────────────────────────────────

    def get_tick(self) -> Optional[TickData]:
        """Best bid / ask (from `quotes`), LTP, and mark price. None on failure."""
        data, _ = self._request("GET", f"/v2/tickers/{self._symbol}")
        if data is None:
            return None

        t = data.get("result") or {}
        quotes = t.get("quotes") or {}
        mark = _to_float(t.get("mark_price"), 0.0)
        ltp = _to_float(t.get("close"), 0.0) or mark
        bid = _to_float(quotes.get("best_bid"), 0.0) or mark
        ask = _to_float(quotes.get("best_ask"), 0.0) or mark
        if ask < bid:
            ask = bid
        if ltp <= 0 or bid <= 0:
            log_broker.error("[BROKER] ❌ Ticker has no usable price: %s", {k: t.get(k) for k in ("close", "mark_price", "quotes")})
            return None

        ts = _to_float(t.get("timestamp"))
        try:
            tick_time = datetime.fromtimestamp(ts / 1e6, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        except (OverflowError, OSError, ValueError):
            tick_time = datetime.now(timezone.utc)

        return TickData(
            bid=bid, ask=ask, ltp=ltp, mark=mark,
            spread=round(ask - bid, 2), time=tick_time,
            funding_rate=_to_float(t.get("funding_rate"), 0.0),
        )

    # ─────────────────────────────────────────
    # POSITIONS
    # ─────────────────────────────────────────

    def get_position(self) -> Tuple[bool, Optional[PositionInfo]]:
        """
        Returns (known, position):
          (True, PositionInfo) — open position on this product
          (True, None)         — broker confirms FLAT
          (False, None)        — lookup failed, state UNKNOWN (never treat as flat)
        """
        # GET /v2/positions is the real-time endpoint; /v2/positions/margined can lag
        # up to 10 s (Delta docs), which would read a just-filled entry as FLAT.
        data, _ = self._request("GET", "/v2/positions", params={"product_id": self.product_id}, auth=True)
        if data is None:
            return False, None

        result = data.get("result") or {}
        for pos in (result if isinstance(result, list) else [result]):
            if pos.get("product_id", self.product_id) != self.product_id:
                continue
            size = int(_to_float(pos.get("size"), 0) or 0)
            if size == 0:
                continue
            return True, PositionInfo(
                product_id=self.product_id,
                symbol=self._symbol,
                direction="LONG" if size > 0 else "SHORT",
                size=abs(size),
                entry_price=_to_float(pos.get("entry_price"), 0.0),
                unrealized_pnl=_to_float(pos.get("unrealized_pnl"), 0.0),
                liquidation_price=_to_float(pos.get("liquidation_price")),
                margin_mode=str(pos.get("margin_mode") or ""),
                auto_topup=pos.get("auto_topup") if isinstance(pos.get("auto_topup"), bool) else None,
            )
        return True, None

    # ─────────────────────────────────────────
    # ORDERS
    # ─────────────────────────────────────────

    def get_order(self, order_id: Optional[int] = None, client_order_id: str = "") -> Tuple[bool, Optional[Dict]]:
        """
        Look up one order. Returns (known, order):
          (True, dict)  found
          (True, None)  broker says it does not exist
          (False, None) lookup failed — UNKNOWN
        """
        path = f"/v2/orders/{order_id}" if order_id else f"/v2/orders/client_order_id/{client_order_id}"
        data, status = self._request("GET", path, auth=True)
        if data is not None:
            return True, data.get("result")
        if status == 404:
            return True, None
        return False, None

    def place_market_order(
        self,
        side: str,
        size: int,
        client_order_id: str,
        reduce_only: bool = False,
        confirm_timeout_s: float = 8.0,
    ) -> Optional[OrderResult]:
        """
        Place a market order and confirm the fill from broker state.

        Placement != fill: the response is re-checked via GET /v2/orders/{id}
        until the order is closed/cancelled. If the POST itself fails
        ambiguously, the order is looked up by client_order_id.

        Returns:
          OrderResult        — broker answered (filled_qty may be 0 if rejected/cancelled)
          None               — outcome UNKNOWN (network failure and lookup failed)
        """
        body = {
            "product_id": self.product_id,
            "size": int(size),
            "side": side,
            "order_type": "market_order",
            "client_order_id": client_order_id,
        }
        if reduce_only:
            body["reduce_only"] = True

        log_broker.info("[BROKER] 📤 %s %d contracts (reduce_only=%s, coid=%s)", side.upper(), size, reduce_only, client_order_id)
        data, status = self._request("POST", "/v2/orders", body=body, auth=True)

        if data is not None:
            order = data.get("result") or {}
        elif status is not None:
            # Definitive 4xx rejection — the order does not exist
            log_broker.error("[BROKER] ❌ Order rejected (HTTP %s).", status)
            return OrderResult(None, client_order_id, "REJECTED", 0, None)
        else:
            known, order = self.get_order(client_order_id=client_order_id)
            if not known:
                log_broker.critical("[BROKER] 🛑 Order outcome UNKNOWN (coid=%s).", client_order_id)
                return None
            if order is None:
                log_broker.warning("[BROKER] Order never reached Delta (coid=%s).", client_order_id)
                return OrderResult(None, client_order_id, "NOT_PLACED", 0, None)
            log_broker.warning("[BROKER] Order found by client_order_id despite POST failure (id=%s).", order.get("id"))

        order_id = order.get("id")
        deadline = time.time() + confirm_timeout_s
        while order.get("state") in ("open", "pending") and time.time() < deadline and order_id:
            time.sleep(random.uniform(0.3, 0.6))
            known, latest = self.get_order(order_id=order_id)
            if known and latest:
                order = latest

        state = str(order.get("state") or "UNKNOWN")
        unfilled = int(_to_float(order.get("unfilled_size"), size) or 0)
        filled = max(0, int(_to_float(order.get("size"), size) or size) - unfilled)
        avg = _to_float(order.get("average_fill_price"))

        log_broker.info(
            "[BROKER] %s Order %s state=%s filled=%d/%d avg=%s",
            "✅" if filled else "⚠️", order_id, state, filled, size, f"${avg:.2f}" if avg else "N/A",
        )
        return OrderResult(int(order_id) if order_id else None, client_order_id, state, filled, avg)

    def place_position_bracket(self, sl_price: float, tp_price: float) -> bool:
        """
        Attach broker-side SL/TP to the open position (safety net if the bot dies).
        Python-side monitoring remains the primary exit mechanism.
        """
        body = {
            "product_id": self.product_id,
            "stop_loss_order": {"order_type": "market_order", "stop_price": f"{sl_price:.2f}"},
            "take_profit_order": {"order_type": "market_order", "stop_price": f"{tp_price:.2f}"},
            "bracket_stop_trigger_method": CONFIG.get("bracket_stop_trigger_method", "last_traded_price"),
        }
        data, _ = self._request("POST", "/v2/orders/bracket", body=body, auth=True)
        if data is None:
            log_broker.warning(
                "[BROKER] ⚠️ Bracket SL=$%.2f / TP=$%.2f NOT attached. Python-side monitoring only.",
                sl_price, tp_price,
            )
            return False
        log_broker.info("[BROKER] ✅ Position bracket attached: SL=$%.2f | TP=$%.2f", sl_price, tp_price)
        return True

    def cancel_stop_orders(self) -> bool:
        """
        Cancel leftover stop / reduce-only orders on this product so a stale
        bracket from a closed trade can never fire against the next one.
        """
        body = {
            "product_id": self.product_id,
            "cancel_limit_orders": False,
            "cancel_stop_orders": True,
            "cancel_reduce_only_orders": True,
        }
        data, _ = self._request("DELETE", "/v2/orders/all", body=body, auth=True)
        if data is None:
            log_broker.warning("[BROKER] ⚠️ Could not cancel leftover stop orders on %s.", self._symbol)
            return False
        log_broker.info("[BROKER] 🧹 Leftover stop / reduce-only orders cancelled.")
        return True
