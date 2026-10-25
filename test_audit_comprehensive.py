"""
═════════════════════════════════════════════════════════════
  TEST AUDIT — Gold 1-Hour SMA Breakdown (SHORT)
═════════════════════════════════════════════════════════════

Fully offline: the Delta broker is faked / its HTTP layer mocked.
State, journal and events are redirected to a temp directory and
Telegram is muted, so running tests never touches real files or chats.

  python test_audit_comprehensive.py
"""

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from config import CONFIG, IST, now_ist
import state as state_mod
import telegram_alerts
from broker import DeltaBroker, TickData, PositionInfo, OrderResult
from pattern import attach_sma, detect_signal, format_signal_checklist
from risk import (calculate_risk, calculate_gross_pnl, calculate_fees, calculate_r_multiple, position_oz,
                  estimate_short_liquidation_price)
from orders import OrderManager
import main as main_mod

telegram_alerts.tg._enabled = False  # never send real messages from tests

_TMP = Path(tempfile.mkdtemp(prefix="gold1h_test_"))
state_mod.STATE_DIR = _TMP
state_mod.LOG_DIR = _TMP
config.LOG_DIR = _TMP


def _candle(o, h, l, c, sma_20=None, sma_50=None, dt=None):
    dt = dt or datetime(2026, 10, 16, 14, 30, tzinfo=IST)
    return {"open": o, "high": h, "low": l, "close": c, "sma_20": sma_20, "sma_50": sma_50,
            "date": dt, "date_utc": dt.astimezone(timezone.utc)}


def _fresh_state():
    s = state_mod.fresh_state()
    return s


def _tick(bid=4149.90, ask=4150.10, ltp=None):
    return TickData(bid=bid, ask=ask, ltp=ltp if ltp is not None else bid, mark=bid,
                    spread=round(ask - bid, 2), time=datetime.now(timezone.utc))


def _signal_candles(close_dt_utc: datetime):
    """59 rising bars (SMA20 > SMA50) + one bearish breakdown bar whose bar-open is close_dt_utc - 1h."""
    base = close_dt_utc - timedelta(hours=60)
    candles = []
    for i in range(59):
        c = 4100 + i * 2
        dt = base + timedelta(hours=i)
        candles.append({"open": c + 1, "high": c + 3, "low": c - 3, "close": c,
                        "date": dt.astimezone(IST), "date_utc": dt, "volume": 1})
    last_open = close_dt_utc - timedelta(hours=1)
    candles.append({"open": 4210.0, "high": 4215.0, "low": 4145.0, "close": 4150.0,
                    "date": last_open.astimezone(IST), "date_utc": last_open, "volume": 1})
    return candles


class FakeBroker:
    """In-memory Delta broker: fills market orders at bid/ask and tracks one position."""

    contract_value = 0.001
    default_leverage = 100.0          # PAXGUSD product default (verified 2026-10-17)
    maintenance_margin_pct = 0.5
    account_leverage = None
    account_auto_topup = None

    def __init__(self):
        self.tick = _tick()
        self.position = None
        self.position_known = True
        self.orders = []
        self.brackets = []
        self.cancels = 0
        self.order_override = "UNSET"
        self.candles = []

    def get_tick(self):
        return self.tick

    def get_position(self):
        if not self.position_known:
            return False, None
        return True, self.position

    def place_market_order(self, side, size, client_order_id, reduce_only=False, confirm_timeout_s=8.0):
        self.orders.append({"side": side, "size": size, "coid": client_order_id, "reduce_only": reduce_only})
        if self.order_override != "UNSET":
            return self.order_override
        price = self.tick.bid if side == "sell" else self.tick.ask
        self.position = (PositionInfo(1, "PAXGUSD", "SHORT", size, price, 0.0, liquidation_price=4999.0)
                         if side == "sell" else None)
        return OrderResult(len(self.orders), client_order_id, "closed", size, price)

    def place_position_bracket(self, sl, tp):
        self.brackets.append((sl, tp))
        return True

    def cancel_stop_orders(self):
        self.cancels += 1
        return True

    def get_candles(self, count, include_forming=False):
        return self.candles[-count:]

    @staticmethod
    def get_candle_status():
        return DeltaBroker.get_candle_status()


class _ModeMixin:
    mode = "PAPER"

    def setUp(self):
        self._saved = {k: CONFIG[k] for k in ("trading_mode", "max_trades_per_day", "max_daily_loss",
                                               "max_risk_dollars", "taker_fee_pct", "contract_qty", "max_spread_dollars")}
        CONFIG["trading_mode"] = self.mode
        CONFIG["taker_fee_pct"] = 0.0005
        CONFIG["contract_qty"] = 1000
        for f in _TMP.glob("*"):
            if f.is_file():
                f.unlink()

    def tearDown(self):
        CONFIG.update(self._saved)


# ═══════════════════════════════════════════════
# SIGNAL — parity with NIFTY 1H SMA PUT v2 tests
# ═══════════════════════════════════════════════

class TestSignal(unittest.TestCase):

    def test_valid_signal_open_between_smas(self):
        sig = detect_signal([_candle(4165, 4175, 4080, 4100, sma_20=4180, sma_50=4150)])
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, "BEARISH_SMA_BREAKDOWN")
        self.assertEqual(sig.sl_price, 4175)

    def test_valid_signal_open_above_both(self):
        self.assertIsNotNone(detect_signal([_candle(4200, 4220, 4080, 4100, sma_20=4180, sma_50=4150)]))

    def test_green_candle_rejected(self):
        self.assertIsNone(detect_signal([_candle(4165, 4220, 4080, 4170, sma_20=4180, sma_50=4150)]))

    def test_close_above_sma20_rejected(self):
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4190, sma_20=4180, sma_50=4150)]))

    def test_close_above_sma50_rejected(self):
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4160, sma_20=4180, sma_50=4150)]))

    def test_open_below_sma50_rejected(self):
        self.assertIsNone(detect_signal([_candle(4140, 4145, 4080, 4100, sma_20=4180, sma_50=4150)]))

    def test_sma20_below_sma50_rejected(self):
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4100, sma_20=4150, sma_50=4180)]))

    def test_missing_and_nan_sma_rejected(self):
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4100, sma_20=None, sma_50=4180)]))
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4100, sma_20=4150, sma_50=None)]))
        self.assertIsNone(detect_signal([_candle(4200, 4220, 4080, 4100, sma_20=float("nan"), sma_50=4180)]))

    def test_empty_rejected(self):
        self.assertIsNone(detect_signal([]))

    def test_only_latest_candle_evaluated(self):
        good = _candle(4200, 4220, 4080, 4100, sma_20=4180, sma_50=4150)
        bad = _candle(4140, 4145, 4080, 4100, sma_20=4180, sma_50=4150)
        self.assertIsNone(detect_signal([good, bad]))

    def test_checklist_text(self):
        line = format_signal_checklist(_candle(4200, 4220, 4080, 4100, sma_20=4180, sma_50=4150))
        self.assertEqual(line.count("✅"), 5)


class TestSMA(unittest.TestCase):

    def test_warmup_and_values(self):
        candles = [{"close": float(c)} for c in range(1, 61)]
        out = attach_sma(candles)
        self.assertIsNone(out[18]["sma_20"])
        self.assertEqual(out[19]["sma_20"], sum(range(1, 21)) / 20)
        self.assertIsNone(out[48]["sma_50"])
        self.assertEqual(out[49]["sma_50"], sum(range(1, 51)) / 50)
        self.assertEqual(out[59]["sma_20"], sum(range(41, 61)) / 20)
        self.assertNotIn("sma_20", candles[0], "attach_sma must not mutate its input")

    def test_signal_fixture_is_valid(self):
        out = attach_sma(_signal_candles(datetime.now(timezone.utc)))
        self.assertIsNotNone(detect_signal(out))


# ═══════════════════════════════════════════════
# RISK, P&L, FEES
# ═══════════════════════════════════════════════

class TestRisk(_ModeMixin, unittest.TestCase):

    def test_sl_high_target_one_to_one(self):
        rp = calculate_risk(4150.0, 4170.0)
        self.assertTrue(rp.is_valid)
        self.assertEqual((rp.sl_price, rp.risk, rp.target_price), (4170.0, 20.0, 4130.0))

    def test_entry_above_sl_skipped(self):
        rp = calculate_risk(4171.0, 4170.0)
        self.assertFalse(rp.is_valid)
        self.assertIn("ENTRY_ABOVE_SL", rp.skip_reason)
        self.assertFalse(calculate_risk(4170.0, 4170.0).is_valid)

    def test_risk_cap(self):
        CONFIG["max_risk_dollars"] = 15
        self.assertIn("RISK_EXCEEDS_CAP", calculate_risk(4150.0, 4170.0).skip_reason)
        CONFIG["max_risk_dollars"] = 0
        self.assertTrue(calculate_risk(4150.0, 4170.0).is_valid)

    def test_pnl_uses_contract_value(self):
        self.assertEqual(position_oz(1000, 0.001), 1.0)
        self.assertEqual(calculate_gross_pnl(4150.0, 4130.0, 1000, 0.001), 20.0)
        self.assertEqual(calculate_gross_pnl(4150.0, 4170.0, 1000, 0.001), -20.0)
        self.assertEqual(calculate_gross_pnl(4150.0, 4130.0, 1, 0.001), 0.02)

    def test_default_size_is_point_one_oz(self):
        # Owner decision 2026-10-17: 100 contracts = 0.1 oz, same as gold_4hr
        self.assertEqual(self._saved["contract_qty"], 100)
        self.assertEqual(position_oz(self._saved["contract_qty"], 0.001), 0.1)
        self.assertEqual(calculate_gross_pnl(4150.0, 4130.0, 100, 0.001), 2.0)

    def test_fees(self):
        fees = calculate_fees(4150.0, 4130.0, 1000, 0.001)
        self.assertAlmostEqual(fees.total, 4.89, places=2)
        self.assertEqual(calculate_fees("x", 1, 1, 0.001).total, 0.0)

    def test_liquidation_estimate(self):
        # Delta guide: liquidation when loss = IM% - MM%  →  short liq = entry × (1 + 1/lev - MM%)
        self.assertEqual(estimate_short_liquidation_price(4160.0, 100, 0.5), 4180.8)   # 0.5% away
        self.assertEqual(estimate_short_liquidation_price(4160.0, 20, 0.5), 4347.2)    # 4.5% away
        self.assertIsNone(estimate_short_liquidation_price(4160.0, None, 0.5))
        self.assertIsNone(estimate_short_liquidation_price(4160.0, 100, None))

    def test_r_multiple(self):
        self.assertEqual(calculate_r_multiple(4150.0, 4130.0, 20.0), 1.0)
        self.assertEqual(calculate_r_multiple(4150.0, 4160.0, 20.0), -0.5)
        self.assertEqual(calculate_r_multiple(4150.0, 4130.0, 0), 0.0)


# ═══════════════════════════════════════════════
# ORDERS — PAPER
# ═══════════════════════════════════════════════

def _sig(high=4215.0):
    return detect_signal([_candle(4210, high, 4145, 4150, sma_20=4200, sma_50=4180)])


class TestOrdersPaper(_ModeMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.state = _fresh_state()
        self.om = OrderManager(self.broker, self.state)

    def test_entry_records_short(self):
        self.assertTrue(self.om.enter_trade(_sig(), self.broker.tick))
        pos = self.state["current_position"]
        self.assertTrue(self.state["in_position"])
        self.assertEqual(self.state["trades_today"], 1)
        self.assertEqual(pos["direction"], "SHORT")
        self.assertEqual(pos["entry_price"], 4149.90)
        self.assertEqual(pos["sl_price"], 4215.0)
        self.assertEqual(pos["risk"], 65.10)
        self.assertEqual(pos["target_price"], 4084.80)
        self.assertEqual(self.broker.orders, [], "PAPER must not send orders")

    def test_stop_loss_exit_and_journal(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertIsNone(self.om.check_exit_conditions(4214.99))
        self.assertEqual(self.om.check_exit_conditions(4215.0), "STOP_LOSS")
        self.broker.tick = _tick(bid=4215.0, ask=4215.2)
        self.assertTrue(self.om.exit_trade("STOP_LOSS", self.broker.tick))
        self.assertFalse(self.state["in_position"])
        self.assertAlmostEqual(self.state["realized_pnl_today"], -65.30 - calculate_fees(4149.9, 4215.2, 1000, 0.001).total, places=2)
        journal = (_TMP / "journal.csv").read_text(encoding="utf-8")
        self.assertIn("STOP_LOSS", journal)

    def test_target_exit(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertEqual(self.om.check_exit_conditions(4084.80), "TARGET_HIT")
        self.assertTrue(self.om.exit_trade("TARGET_HIT", _tick(bid=4084.6, ask=4084.8)))
        self.assertGreater(self.state["realized_pnl_today"], 0)

    def test_double_exit_ignored(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertTrue(self.om.exit_trade("STOP_LOSS", self.broker.tick))
        self.assertFalse(self.om.exit_trade("STOP_LOSS", self.broker.tick))

    def test_guards(self):
        self.assertFalse(self.om.enter_trade(_sig(high=4149.0), self.broker.tick), "gap through SL must skip")
        wide = _tick(bid=4148.0, ask=4150.0)
        self.assertFalse(self.om.enter_trade(_sig(), wide), "wide spread must skip")
        self.assertFalse(self.om.enter_trade(_sig(), None), "no tick must skip")

        self.state["trades_today"] = 5
        self.assertFalse(self.om.can_enter()[0])
        self.state["trades_today"] = 0

        CONFIG["max_daily_loss"] = 100
        self.state["realized_pnl_today"] = 150.0   # a PROFIT must never trip the loss kill-switch
        self.assertTrue(self.om.can_enter()[0])
        self.state["realized_pnl_today"] = -100.0
        self.assertFalse(self.om.can_enter()[0])

    def test_already_in_position(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertFalse(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertEqual(self.state["trades_today"], 1)

    def test_liquidation_before_sl_is_reported_not_blocked(self):
        # SL $65 away (1.6%) > 0.5% liquidation distance at Delta's default 100x
        with patch("orders.tg") as tg_mock:
            self.assertTrue(self.om.enter_trade(_sig(high=4215.0), self.broker.tick), "default leverage: trade still taken")
        pos = self.state["current_position"]
        self.assertEqual(pos["liquidation_price"], 4170.65)
        self.assertEqual(pos["liquidation_source"], "ESTIMATE@100x")
        tg_mock.critical.assert_called_once()

    def test_liquidation_beyond_sl_no_alert(self):
        with patch("orders.tg") as tg_mock:
            self.om.enter_trade(_sig(high=4160.0), self.broker.tick)   # SL $10 away, liq $20.75 away
        self.assertGreater(self.state["current_position"]["liquidation_price"], 4160.0)
        tg_mock.critical.assert_not_called()

    def test_account_leverage_preferred_over_product_default(self):
        self.broker.account_leverage = 20.0
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertEqual(self.state["current_position"]["liquidation_source"], "ESTIMATE@20x")


# ═══════════════════════════════════════════════
# ORDERS — LIVE (fake broker)
# ═══════════════════════════════════════════════

class TestOrdersLive(_ModeMixin, unittest.TestCase):
    mode = "LIVE"

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.state = _fresh_state()
        self.om = OrderManager(self.broker, self.state)

    def test_entry_sends_sell_with_bracket(self):
        self.assertTrue(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertEqual(self.broker.orders[0]["side"], "sell")
        self.assertEqual(self.broker.orders[0]["size"], 1000)
        self.assertFalse(self.broker.orders[0]["reduce_only"])
        self.assertLessEqual(len(self.broker.orders[0]["coid"]), 32)
        self.assertEqual(self.broker.brackets, [(4215.0, 4084.8)])
        self.assertGreaterEqual(self.broker.cancels, 1, "stale stops cleared before entry")
        self.assertTrue(self.state["current_position"]["bracket_attached"])
        self.assertIsNone(self.state["in_flight_order"])

    def test_entry_blocked_when_positions_unknown_or_existing(self):
        self.broker.position_known = False
        self.assertFalse(self.om.enter_trade(_sig(), self.broker.tick))
        self.broker.position_known = True
        self.broker.position = PositionInfo(1, "PAXGUSD", "LONG", 5, 4100.0, 0.0)
        self.assertFalse(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertEqual(self.broker.orders, [])

    def test_unfilled_entry_clears_in_flight(self):
        self.broker.order_override = OrderResult(None, "x", "REJECTED", 0, None)
        self.assertFalse(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertFalse(self.state["in_position"])
        self.assertIsNone(self.state["in_flight_order"])
        self.assertEqual(self.state["trades_today"], 0)

    def test_unknown_entry_adopts_broker_short(self):
        def filled_but_unknown(side, size, client_order_id, reduce_only=False, confirm_timeout_s=8.0):
            self.broker.position = PositionInfo(1, "PAXGUSD", "SHORT", 1000, 4149.5, 0.0)
            return None  # network died after the order reached Delta
        self.broker.place_market_order = filled_but_unknown
        self.assertTrue(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertEqual(self.state["current_position"]["entry_price"], 4149.5)

    def test_unknown_entry_unknown_broker_keeps_in_flight(self):
        self.broker.order_override = None
        orig = self.broker.get_position
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return (True, None) if calls["n"] == 1 else (False, None)  # known before order, unknown after
        self.broker.get_position = flaky
        self.assertFalse(self.om.enter_trade(_sig(), self.broker.tick))
        self.assertIsNotNone(self.state["in_flight_order"], "unknown outcome must survive for restart recovery")
        self.broker.get_position = orig

    def test_live_uses_deltas_own_liquidation_price(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        pos = self.state["current_position"]
        self.assertEqual((pos["liquidation_price"], pos["liquidation_source"]), (4999.0, "DELTA"))

    def test_exit_is_reduce_only_buy(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.assertTrue(self.om.exit_trade("STOP_LOSS", _tick(bid=4215.0, ask=4215.2)))
        exit_order = self.broker.orders[-1]
        self.assertEqual((exit_order["side"], exit_order["reduce_only"], exit_order["size"]), ("buy", True, 1000))
        self.assertFalse(self.state["in_position"])

    def test_exit_when_broker_already_flat_sends_nothing(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.broker.position = None
        n = len(self.broker.orders)
        self.assertTrue(self.om.exit_trade("STOP_LOSS", self.broker.tick))
        self.assertEqual(len(self.broker.orders), n, "no order when broker is flat (naked-flip prevention)")

    def test_exit_failure_keeps_position(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.broker.order_override = OrderResult(None, "x", "REJECTED", 0, None)
        self.assertFalse(self.om.exit_trade("STOP_LOSS", self.broker.tick))
        self.assertTrue(self.state["in_position"], "failed exit must leave the position tracked")

    def test_reconcile_external_bracket_sl(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.broker.position = None
        self.assertTrue(self.om.reconcile_external_exit(_tick(bid=4216.0, ask=4216.2)))
        self.assertFalse(self.state["in_position"])
        self.assertIn("BROKER_SL_HIT", (_TMP / "journal.csv").read_text(encoding="utf-8"))

    def test_reconcile_noop_when_unknown(self):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.broker.position_known = False
        self.assertFalse(self.om.reconcile_external_exit(self.broker.tick))
        self.assertTrue(self.state["in_position"], "UNKNOWN must never be treated as flat")


# ═══════════════════════════════════════════════
# BROKER PARSING (HTTP mocked)
# ═══════════════════════════════════════════════

class TestBrokerParsing(_ModeMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.b = DeltaBroker()
        self.b.product_id = 123006
        self.responses = {}

    def _fake_request(self, method, path, params=None, body=None, auth=False):
        key = (method, path)
        resp = self.responses.get(key)
        if callable(resp):
            return resp(body)
        return resp if resp is not None else (None, 404)

    def test_tick_reads_quotes(self):
        self.responses[("GET", "/v2/tickers/PAXGUSD")] = ({"success": True, "result": {
            "close": "4161.5", "mark_price": "4161.44", "timestamp": str(int(time.time() * 1e6)),
            "funding_rate": "0.01", "quotes": {"best_bid": "4161.4", "best_ask": "4161.6"}}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            t = self.b.get_tick()
        self.assertEqual((t.bid, t.ask, t.ltp), (4161.4, 4161.6, 4161.5))
        self.assertAlmostEqual(t.spread, 0.2)

    def test_candles_sorted_and_forming_dropped(self):
        hour = (int(time.time()) // 3600) * 3600
        bars = [{"time": hour - k * 3600, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3} for k in range(3)]
        self.responses[("GET", "/v2/history/candles")] = ({"success": True, "result": bars}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            out = self.b.get_candles(10)
        self.assertEqual([int(c["date_utc"].timestamp()) for c in out], [hour - 7200, hour - 3600])
        self.assertEqual(out[-1]["date"].tzinfo, IST)

    def test_position_flat_vs_unknown(self):
        # GET /v2/positions?product_id= (real-time) returns ONE object; size 0 = flat
        self.responses[("GET", "/v2/positions")] = ({"success": True, "result": {
            "size": -1000, "entry_price": "4150.5", "unrealized_pnl": "1.2"}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request) as req:
            known, pos = self.b.get_position()
        self.assertTrue(known)
        self.assertEqual((pos.direction, pos.size, pos.entry_price), ("SHORT", 1000, 4150.5))
        self.assertEqual(req.call_args.kwargs.get("params"), {"product_id": 123006})
        self.assertTrue(req.call_args.kwargs.get("auth"))

        self.responses[("GET", "/v2/positions")] = ({"success": True, "result": {"size": 0, "entry_price": None}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertEqual(self.b.get_position(), (True, None))

        # a list-shaped answer is still filtered to this product
        self.responses[("GET", "/v2/positions")] = ({"success": True, "result": [
            {"product_id": 999, "size": -5, "entry_price": "1"}]}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertEqual(self.b.get_position(), (True, None))

        self.responses[("GET", "/v2/positions")] = (None, None)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertEqual(self.b.get_position(), (False, None))

    def test_ambiguous_post_resolved_by_client_order_id(self):
        self.responses[("POST", "/v2/orders")] = (None, None)
        self.responses[("GET", "/v2/orders/client_order_id/abc")] = ({"success": True, "result": {
            "id": 77, "state": "closed", "size": 1000, "unfilled_size": 0, "average_fill_price": "4150.20"}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            res = self.b.place_market_order("sell", 1000, "abc")
        self.assertEqual((res.order_id, res.filled_qty, res.avg_price), (77, 1000, 4150.2))

    def test_ambiguous_post_never_received(self):
        self.responses[("POST", "/v2/orders")] = (None, None)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            res = self.b.place_market_order("sell", 1000, "abc")
        self.assertEqual((res.state, res.filled_qty), ("NOT_PLACED", 0))

    def test_open_order_polled_until_closed_with_partial_fill(self):
        self.responses[("POST", "/v2/orders")] = ({"success": True, "result": {
            "id": 5, "state": "open", "size": 1000, "unfilled_size": 1000, "average_fill_price": None}}, 200)
        self.responses[("GET", "/v2/orders/5")] = ({"success": True, "result": {
            "id": 5, "state": "cancelled", "size": 1000, "unfilled_size": 400, "average_fill_price": "4150"}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            res = self.b.place_market_order("sell", 1000, "abc")
        self.assertEqual((res.state, res.filled_qty, res.avg_price), ("cancelled", 600, 4150.0))

    def test_rejected_post(self):
        self.responses[("POST", "/v2/orders")] = (None, 400)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            res = self.b.place_market_order("sell", 1000, "abc")
        self.assertEqual(res.filled_qty, 0)

    def test_bracket_payload(self):
        seen = {}

        def capture(body):
            seen.update(body)
            return {"success": True}, 200
        self.responses[("POST", "/v2/orders/bracket")] = capture
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertTrue(self.b.place_position_bracket(4215.0, 4084.8))
        self.assertEqual(seen["stop_loss_order"], {"order_type": "market_order", "stop_price": "4215.00"})
        self.assertEqual(seen["take_profit_order"], {"order_type": "market_order", "stop_price": "4084.80"})

    def test_contract_value_mismatch_refuses_start(self):
        self.responses[("GET", "/v2/products/PAXGUSD")] = ({"success": True, "result": {
            "id": 123006, "contract_type": "perpetual_futures", "state": "live",
            "contract_value": "0.01", "tick_size": "0.01", "taker_commission_rate": "0.0001"}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertFalse(self.b.connect())

    def test_product_ok_sets_fee(self):
        self.responses[("GET", "/v2/products/PAXGUSD")] = ({"success": True, "result": {
            "id": 123006, "contract_type": "perpetual_futures", "state": "live",
            "contract_value": "0.001", "tick_size": "0.01", "taker_commission_rate": "0.0001"}}, 200)
        self.b._api_key = self.b._api_secret = ""
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertTrue(self.b.connect())
        self.assertEqual(CONFIG["taker_fee_pct"], 0.0001)

    def test_live_requires_credentials(self):
        CONFIG["trading_mode"] = "LIVE"
        self.responses[("GET", "/v2/products/PAXGUSD")] = ({"success": True, "result": {
            "id": 123006, "contract_type": "perpetual_futures", "state": "live",
            "contract_value": "0.001", "tick_size": "0.01"}}, 200)
        self.b._api_key = self.b._api_secret = ""
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertFalse(self.b.connect())

    def test_margin_spec_and_account_settings(self):
        self.responses[("GET", "/v2/products/PAXGUSD")] = ({"success": True, "result": {
            "id": 123006, "contract_type": "perpetual_futures", "state": "live", "contract_value": "0.001",
            "tick_size": "0.01", "default_leverage": "100.000000000000000000",
            "initial_margin": 1, "maintenance_margin": 0.5}}, 200)
        self.responses[("GET", "/v2/products/123006/orders/leverage")] = ({"success": True, "result": {
            "leverage": 100, "order_margin": "0", "product_id": 123006}}, 200)
        self.responses[("GET", "/v2/users/trading_preferences")] = ({"success": True, "result": {
            "default_auto_topup": True}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.assertTrue(self.b._resolve_product())
            self.b.read_account_margin_settings()
        self.assertEqual((self.b.default_leverage, self.b.initial_margin_pct, self.b.maintenance_margin_pct), (100.0, 1.0, 0.5))
        self.assertEqual((self.b.account_leverage, self.b.account_auto_topup), (100.0, True))

    def test_account_settings_unverified_on_401(self):
        self.responses[("GET", "/v2/products/123006/orders/leverage")] = (None, 401)
        self.responses[("GET", "/v2/users/trading_preferences")] = (None, 401)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            self.b.read_account_margin_settings()
        self.assertIsNone(self.b.account_leverage, "refused call must stay UNVERIFIED, never assumed")
        self.assertIsNone(self.b.account_auto_topup)

    def test_position_liquidation_fields(self):
        self.responses[("GET", "/v2/positions")] = ({"success": True, "result": {
            "product_id": 123006, "size": -1000, "entry_price": "4150", "liquidation_price": "4170.75",
            "margin_mode": "isolated", "auto_topup": False}}, 200)
        with patch.object(self.b, "_request", side_effect=self._fake_request):
            _, pos = self.b.get_position()
        self.assertEqual((pos.liquidation_price, pos.margin_mode, pos.auto_topup), (4170.75, "isolated", False))

    def test_candle_status_math(self):
        st = DeltaBroker.get_candle_status()
        self.assertEqual(st["forming_open_time"] % 3600, 0)
        self.assertEqual(st["forming_open_time"] - st["last_closed_bar_time"], 3600)
        self.assertTrue(0 <= st["seconds_left"] <= 3600)


# ═══════════════════════════════════════════════
# MAIN — candle evaluation & recovery
# ═══════════════════════════════════════════════

class TestMainFlow(_ModeMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.state = _fresh_state()
        self.om = OrderManager(self.broker, self.state)

    def _closed_bar(self, seconds_since_close):
        close_dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_since_close)
        self.broker.candles = _signal_candles(close_dt)
        return int(self.broker.candles[-1]["date_utc"].timestamp())

    def test_fresh_signal_enters(self):
        ts = self._closed_bar(10)
        self.assertTrue(main_mod._evaluate_closed_candle(self.broker, self.om, self.state, ts))
        self.assertTrue(self.state["in_position"])
        self.assertEqual(self.state["current_position"]["sl_price"], 4215.0)
        candles_csv = (_TMP / now_ist().date().isoformat() / "candles.csv").read_text(encoding="utf-8")
        self.assertIn("SHORT", candles_csv)

    def test_duplicate_signal_ignored(self):
        ts = self._closed_bar(10)
        main_mod._evaluate_closed_candle(self.broker, self.om, self.state, ts)
        self.om.exit_trade("TEST", self.broker.tick)
        main_mod._evaluate_closed_candle(self.broker, self.om, self.state, ts)
        self.assertEqual(self.state["trades_today"], 1)

    def test_stale_signal_skipped(self):
        ts = self._closed_bar(CONFIG["max_entry_delay_seconds"] + 60)
        self.assertTrue(main_mod._evaluate_closed_candle(self.broker, self.om, self.state, ts))
        self.assertFalse(self.state["in_position"])

    def test_bar_not_published_yet(self):
        ts = self._closed_bar(10)
        self.assertFalse(main_mod._evaluate_closed_candle(self.broker, self.om, self.state, ts + 3600))

    def _open_position(self, minutes_ago):
        self.om.enter_trade(_sig(), self.broker.tick)
        self.state["current_position"]["last_heartbeat"] = (now_ist() - timedelta(minutes=minutes_ago)).isoformat()

    def test_recovery_downtime_exceeded_exits(self):
        self._open_position(30)
        main_mod._crash_recovery(self.state, self.broker, self.om)
        self.assertFalse(self.state["in_position"])
        self.assertIn("CRASH_DOWNTIME_EXCEEDED", (_TMP / "journal.csv").read_text(encoding="utf-8"))

    def test_recovery_short_downtime_resumes(self):
        self._open_position(5)
        main_mod._crash_recovery(self.state, self.broker, self.om)
        self.assertTrue(self.state["in_position"])

    def test_recovery_sl_already_breached(self):
        self._open_position(5)
        self.broker.tick = _tick(bid=4220.0, ask=4220.2)
        main_mod._crash_recovery(self.state, self.broker, self.om)
        self.assertFalse(self.state["in_position"])

    def test_in_flight_adoption_live(self):
        CONFIG["trading_mode"] = "LIVE"
        self.state["in_flight_order"] = {"client_order_id": "g1sE1", "sl_price": 4215.0, "target_price": 4084.8,
                                         "qty": 1000, "placed_at": now_ist().isoformat(), "signal_candle_time": "x"}
        self.broker.position = PositionInfo(1, "PAXGUSD", "SHORT", 1000, 4150.0, 0.0)
        main_mod._recover_in_flight_order(self.state, self.broker)
        self.assertTrue(self.state["in_position"])
        self.assertEqual(self.state["current_position"]["entry_price"], 4150.0)
        self.assertIsNone(self.state["in_flight_order"])

    def test_in_flight_cleared_when_broker_flat(self):
        CONFIG["trading_mode"] = "LIVE"
        self.state["in_flight_order"] = {"client_order_id": "g1sE1", "sl_price": 4215.0, "target_price": 4084.8}
        main_mod._recover_in_flight_order(self.state, self.broker)
        self.assertFalse(self.state["in_position"])
        self.assertIsNone(self.state["in_flight_order"])


# ═══════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════

class TestState(_ModeMixin, unittest.TestCase):

    def test_atomic_roundtrip_and_rollover(self):
        s = _fresh_state()
        s["trades_today"] = 3
        s["date"] = "2000-01-01"
        s["in_position"] = True
        s["current_position"] = {"direction": "SHORT", "qty": 1000, "entry_price": 4150.0}
        state_mod.save_state(s)
        self.assertEqual(list(_TMP.glob("state_*.tmp")), [])
        loaded = state_mod.load_state()
        self.assertEqual(loaded["trades_today"], 0, "daily counters reset on new date")
        self.assertTrue(loaded["in_position"], "position preserved across days")

    def test_corrupt_state_halts(self):
        (_TMP / "state_active.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            state_mod.load_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
