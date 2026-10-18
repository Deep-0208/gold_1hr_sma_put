"""
═════════════════════════════════════════════════════════════
  ORDERS — SHORT Entry / Exit Lifecycle (PAPER & LIVE)
═════════════════════════════════════════════════════════════

Identical code path for PAPER and LIVE (AGENTS §12): same guards,
same state writes, same journal rows — only the fill source differs.

Safety rules:
  - Exits are reduce_only BUY orders: they can never open a LONG.
  - Broker position UNKNOWN is never treated as FLAT.
  - in_flight_order is persisted BEFORE the entry order is sent, so a
    crash between placement and state save is recoverable on restart.
  - Leftover bracket stops are cancelled after every exit so they can
    never fire against the next trade.
"""

import threading
from typing import Optional, Tuple

from broker import DeltaBroker, TickData, make_client_order_id
from config import CONFIG, log, log_orders, log_trades, log_event, now_ist
from pattern import SetupSignal
from risk import (
    RiskParams, calculate_risk, calculate_gross_pnl, calculate_fees,
    calculate_r_multiple, position_oz, estimate_short_liquidation_price,
)
from state import save_state, append_trade_journal
from telegram_alerts import tg

# Re-entrant: exit_trade may run while the caller already holds the lock.
trade_lock = threading.RLock()


class OrderManager:
    """Owns every mutation of position state."""

    def __init__(self, broker: DeltaBroker, state: dict):
        self.broker = broker
        self.state = state

    @property
    def mode(self) -> str:
        return CONFIG["trading_mode"]

    @property
    def oz(self) -> float:
        return position_oz(CONFIG["contract_qty"], self.broker.contract_value)

    # ─────────────────────────────────────────
    # PRE-TRADE GUARDS
    # ─────────────────────────────────────────

    def can_enter(self) -> Tuple[bool, str]:
        """No-trade guard conditions (AGENTS §6.6)."""
        if self.state.get("in_position"):
            return False, "ALREADY_IN_POSITION"
        max_trades = int(CONFIG.get("max_trades_per_day", 0) or 0)
        if max_trades > 0 and self.state.get("trades_today", 0) >= max_trades:
            return False, f"MAX_TRADES_REACHED ({self.state.get('trades_today')}/{max_trades})"
        max_loss = float(CONFIG.get("max_daily_loss", 0) or 0)
        if max_loss > 0 and self.state.get("realized_pnl_today", 0.0) <= -max_loss:
            return False, f"DAILY_LOSS_LIMIT (${self.state.get('realized_pnl_today'):.2f} <= -${max_loss:.2f})"
        return True, ""

    # ─────────────────────────────────────────
    # ENTRY
    # ─────────────────────────────────────────

    def enter_trade(self, signal: SetupSignal, tick: Optional[TickData]) -> bool:
        """SHORT `contract_qty` PAXGUSD at market. Returns True if a position was opened."""
        with trade_lock:
            ok, reason = self.can_enter()
            if not ok:
                log_orders.info("[ENTRY] Skipped: %s", reason)
                return False

            if tick is None:
                return self._skip("PRICE_UNAVAILABLE: no ticker data at entry")

            max_spread = float(CONFIG.get("max_spread_dollars", 1.20))
            if tick.spread > max_spread:
                return self._skip(f"SPREAD_TOO_WIDE: ${tick.spread:.2f} > ${max_spread:.2f} (bid ${tick.bid:.2f} / ask ${tick.ask:.2f})")

            # Pre-trade risk at the price we expect to sell at (best bid)
            pre = calculate_risk(tick.bid, signal.sl_price)
            if not pre.is_valid:
                return self._skip(pre.skip_reason)

            qty = int(CONFIG["contract_qty"])
            candle_time = str(signal.signal_candle["date"])

            if self.mode == "LIVE":
                fill = self._live_entry(qty, pre, candle_time)
                if fill is None:
                    return False
                order_id, filled_qty, fill_price = fill
            else:
                order_id, filled_qty, fill_price = f"PAPER_{int(now_ist().timestamp())}", qty, tick.bid
                log_orders.info("[ENTRY] ✅ PAPER SELL FILLED: %d contracts @ $%.2f", qty, fill_price)

            # SL stays at the signal candle high; target re-anchored on the actual fill (exact 1:1)
            post = calculate_risk(fill_price, signal.sl_price)
            rp = post if post.is_valid else pre
            if not post.is_valid:
                log_orders.warning(
                    "[ENTRY] ⚠️ Fill $%.2f slipped through risk checks (%s). Keeping pre-trade target $%.2f; "
                    "SL monitoring will act on the next tick.", fill_price, post.skip_reason, pre.target_price,
                )

            now = now_ist()
            self.state["in_position"] = True
            self.state["in_flight_order"] = None
            self.state["trades_today"] = self.state.get("trades_today", 0) + 1
            self.state["current_position"] = {
                "direction": "SHORT",
                "entry_order_id": str(order_id),
                "qty": int(filled_qty),
                "entry_price": round(fill_price, 2),
                "sl_price": rp.sl_price,
                "target_price": rp.target_price,
                "risk": round(rp.sl_price - fill_price, 2),
                "signal_candle_time": candle_time,
                "bracket_attached": False,
                "entry_time": now.isoformat(),
                "entry_date": now.date().isoformat(),
                "last_heartbeat": now.isoformat(),
            }
            save_state(self.state)

            if self.mode == "LIVE":
                self.state["current_position"]["bracket_attached"] = self.broker.place_position_bracket(rp.sl_price, rp.target_price)
                save_state(self.state)

            self._check_liquidation(self.state["current_position"])
            pos = self.state["current_position"]
            oz = position_oz(pos["qty"], self.broker.contract_value)
            log.info(
                "[ENTRY] 🟢 %s SHORT %d contracts (%.3f oz) @ $%.2f | SL $%.2f | Target $%.2f | Risk $%.2f/oz ($%.2f)",
                self.mode, pos["qty"], oz, pos["entry_price"], pos["sl_price"], pos["target_price"],
                pos["risk"], pos["risk"] * oz,
            )
            log_trades.info(
                "ENTRY SHORT | qty=%d (%.3f oz) | price=$%.2f | SL=$%.2f | TP=$%.2f | risk=$%.2f | order=%s | mode=%s",
                pos["qty"], oz, pos["entry_price"], pos["sl_price"], pos["target_price"], pos["risk"], order_id, self.mode,
            )
            log_event("TRADE_ENTRY", dict(pos, mode=self.mode))
            tg.trade_entry(pos["entry_price"], pos["sl_price"], pos["target_price"], pos["risk"], pos["qty"], oz)
            return True

    def _live_entry(self, qty: int, pre: RiskParams, candle_time: str) -> Optional[Tuple[str, int, float]]:
        """Send the LIVE sell. Returns (order_id, filled_qty, fill_price) or None if no position opened."""
        known, existing = self.broker.get_position()
        if not known:
            self._skip("BROKER_STATE_UNKNOWN: positions lookup failed before entry")
            return None
        if existing is not None:
            tg.critical("Unexpected broker position", f"{existing.direction} {existing.size} contracts already open on PAXGUSD")
            self._skip(f"UNEXPECTED_BROKER_POSITION: {existing.direction} {existing.size} contracts")
            return None

        self.broker.cancel_stop_orders()  # clean slate: no stale bracket may survive into this trade

        coid = make_client_order_id("g1sE")
        self.state["in_flight_order"] = {
            "client_order_id": coid, "side": "sell", "qty": qty,
            "sl_price": pre.sl_price, "target_price": pre.target_price,
            "signal_candle_time": candle_time, "placed_at": now_ist().isoformat(),
        }
        save_state(self.state)

        res = self.broker.place_market_order("sell", qty, coid)

        if res is None:
            # Outcome unknown — the broker position is the source of truth
            known, pos = self.broker.get_position()
            if known and pos is not None and pos.direction == "SHORT":
                log_orders.warning("[ENTRY] Order outcome unknown but broker shows SHORT %d — adopting it.", pos.size)
                return coid, pos.size, pos.entry_price
            if known:
                self.state["in_flight_order"] = None
                save_state(self.state)
                self._skip("ENTRY_NOT_FILLED: order outcome unknown and broker is flat")
                return None
            tg.critical("Entry order state UNKNOWN", f"coid={coid}. Check Delta Exchange manually.")
            log_orders.critical("[ENTRY] 🛑 Entry outcome UNKNOWN (coid=%s). in_flight_order kept for restart recovery.", coid)
            return None

        if res.filled_qty <= 0:
            self.state["in_flight_order"] = None
            save_state(self.state)
            self._skip(f"ENTRY_NOT_FILLED: state={res.state}")
            return None

        if res.filled_qty < qty:
            log_orders.warning("[ENTRY] ⚠️ PARTIAL FILL: %d/%d contracts. Managing the filled size.", res.filled_qty, qty)

        fill_price = res.avg_price
        if not fill_price:
            known, pos = self.broker.get_position()
            fill_price = pos.entry_price if (known and pos) else pre.entry_price
        return str(res.order_id or coid), res.filled_qty, fill_price

    def _check_liquidation(self, pos: dict) -> None:
        """
        Record where Delta would liquidate this SHORT and alert if that is before the SL.
        Report only — the bot keeps Delta's default/account leverage and never alters the trade.
        LIVE uses Delta's own position.liquidation_price; PAPER (or if Delta omits it) uses the
        isolated-margin estimate at the account leverage, else the product default leverage.
        """
        liq, source = None, "UNVERIFIED"
        if self.mode == "LIVE":
            known, live = self.broker.get_position()
            if known and live is not None and live.liquidation_price:
                liq, source = round(live.liquidation_price, 2), "DELTA"
        if liq is None:
            lev = getattr(self.broker, "account_leverage", None) or getattr(self.broker, "default_leverage", None)
            liq = estimate_short_liquidation_price(pos["entry_price"], lev, getattr(self.broker, "maintenance_margin_pct", None))
            if liq is not None:
                source = f"ESTIMATE@{lev:g}x"

        pos["liquidation_price"] = liq
        pos["liquidation_source"] = source
        save_state(self.state)

        if liq is None:
            log.warning("[MARGIN] ⚠️ Liquidation price UNVERIFIED (no leverage / margin data).")
            return

        topup = {True: "ON", False: "OFF", None: "UNVERIFIED"}[getattr(self.broker, "account_auto_topup", None)]
        gap = round(liq - pos["sl_price"], 2)
        if liq <= pos["sl_price"]:
            msg = (f"Liquidation ${liq:.2f} ({source}) is ${-gap:.2f} BEFORE SL ${pos['sl_price']:.2f}. "
                   f"Delta may liquidate before the stop loss. Auto top-up: {topup}.")
            log.critical("[MARGIN] 🛑 %s", msg)
            log_event("LIQUIDATION_BEFORE_SL", {"liquidation_price": liq, "sl_price": pos["sl_price"], "source": source, "auto_topup": topup})
            tg.critical("Liquidation before SL", msg)
        else:
            log.info("[MARGIN] ✅ Liquidation $%.2f (%s) is $%.2f beyond SL $%.2f | auto top-up: %s",
                     liq, source, gap, pos["sl_price"], topup)

    def _skip(self, reason: str) -> bool:
        log.warning("[ENTRY] ⚠️ Setup skipped: %s", reason)
        log_event("SETUP_SKIPPED", {"reason": reason})
        tg.setup_skipped(reason)
        return False

    # ─────────────────────────────────────────
    # EXIT CONDITIONS
    # ─────────────────────────────────────────

    def check_exit_conditions(self, price: float) -> Optional[str]:
        """SHORT exits on LTP: SL first (price >= signal high), then target."""
        pos = self.state.get("current_position")
        if not self.state.get("in_position") or not pos:
            return None
        if price >= pos["sl_price"]:
            log_orders.info("[EXIT] 🛑 SL HIT: LTP $%.2f >= SL $%.2f", price, pos["sl_price"])
            return "STOP_LOSS"
        if price <= pos["target_price"]:
            log_orders.info("[EXIT] 🎯 TARGET HIT: LTP $%.2f <= Target $%.2f", price, pos["target_price"])
            return "TARGET_HIT"
        return None

    # ─────────────────────────────────────────
    # EXIT
    # ─────────────────────────────────────────

    def exit_trade(self, reason: str, tick: Optional[TickData], fallback_price: Optional[float] = None) -> bool:
        """
        Close the SHORT. Returns True once the position is closed (and journaled).
        On failure the position stays open in state and is retried by the main loop.
        """
        with trade_lock:
            pos = self.state.get("current_position")
            if not self.state.get("in_position") or not pos:
                log_orders.warning("[EXIT] Exit (%s) requested but not in position — ignored.", reason)
                return False

            est_price = tick.ask if tick else (fallback_price or pos["entry_price"])

            if self.mode == "LIVE":
                result = self._live_exit(pos, reason, est_price)
                if result is None:
                    return False
                exit_price, reason = result
            else:
                exit_price = est_price
                log_orders.info("[EXIT] ✅ PAPER BUY FILLED: %d contracts @ $%.2f", pos["qty"], exit_price)

            self._finalize_exit(pos, exit_price, reason)
            return True

    def _live_exit(self, pos: dict, reason: str, est_price: float) -> Optional[Tuple[float, str]]:
        """Reduce-only buy until the broker is flat. Returns (exit_price, reason) or None on failure."""
        known, live = self.broker.get_position()
        if known and live is None:
            log_orders.warning("[EXIT] Broker already FLAT — position was closed externally.")
            self.broker.cancel_stop_orders()
            return est_price, f"{reason}_BROKER_FLAT"

        remaining = live.size if (known and live) else int(pos["qty"])
        filled_value, filled_total = 0.0, 0
        for attempt in range(1, 4):
            res = None
            if remaining > 0:
                res = self.broker.place_market_order("buy", remaining, make_client_order_id("g1sX"), reduce_only=True)
            if res is not None and res.filled_qty > 0:
                filled_total += res.filled_qty
                filled_value += (res.avg_price or est_price) * res.filled_qty
                remaining -= res.filled_qty

            known, live = self.broker.get_position()
            if known and live is None:
                self.broker.cancel_stop_orders()
                exit_price = round(filled_value / filled_total, 2) if filled_total else est_price
                return exit_price, reason
            if known and live is not None:
                remaining = live.size
            log_orders.warning("[EXIT] ⚠️ Close attempt %d/3 incomplete (remaining=%s, known=%s).", attempt, remaining, known)

        log_orders.critical("[EXIT] 🛑 Could not confirm close of SHORT %s after 3 attempts. Will retry.", pos["qty"])
        tg.critical("EXIT FAILED", f"SHORT {pos['qty']} contracts still open after 3 close attempts ({reason}). Bot keeps retrying.")
        return None

    def _finalize_exit(self, pos: dict, exit_price: float, reason: str) -> None:
        """P&L, fees, state, journal, logs, alert — identical for PAPER and LIVE."""
        qty, entry = int(pos["qty"]), float(pos["entry_price"])
        cv = self.broker.contract_value
        gross = calculate_gross_pnl(entry, exit_price, qty, cv)
        fees = calculate_fees(entry, exit_price, qty, cv)
        net = round(gross - fees.total, 2)
        r_mult = calculate_r_multiple(entry, exit_price, pos.get("risk", 0))
        now = now_ist()

        self.state["in_position"] = False
        self.state["current_position"] = None
        self.state["realized_pnl_today"] = round(self.state.get("realized_pnl_today", 0.0) + net, 2)
        self.state["total_realized_pnl"] = round(self.state.get("total_realized_pnl", 0.0) + net, 2)
        self.state["cash"] = round(self.state.get("cash", CONFIG["starting_capital"]) + net, 2)
        save_state(self.state)

        append_trade_journal({
            "date": pos.get("entry_date", ""), "symbol": CONFIG["symbol"], "direction": "SHORT",
            "qty": qty, "oz": position_oz(qty, cv), "entry_order_id": pos.get("entry_order_id", ""),
            "signal_candle_time": pos.get("signal_candle_time", ""),
            "entry_time": pos.get("entry_time", ""), "exit_time": now.isoformat(),
            "entry_price": entry, "exit_price": round(exit_price, 2),
            "sl_price": pos.get("sl_price"), "target_price": pos.get("target_price"), "risk": pos.get("risk"),
            "liquidation_price": pos.get("liquidation_price"),
            "gross_pnl": gross, "fees": fees.total, "net_pnl": net, "r_multiple": r_mult,
            "exit_reason": reason, "mode": self.mode,
        })

        icon = "🟢" if net >= 0 else "🔴"
        log.info(
            "[EXIT] %s %s SHORT closed (%s) | $%.2f → $%.2f | %.2fR | Gross $%+.2f | Fees $%.2f | Net $%+.2f | Day $%+.2f",
            icon, self.mode, reason, entry, exit_price, r_mult, gross, fees.total, net, self.state["realized_pnl_today"],
        )
        log_orders.info("[EXIT] FEES | %s", fees.breakdown())
        log_trades.info(
            "EXIT SHORT (%s) | qty=%d | entry=$%.2f | exit=$%.2f | gross=$%+.2f | fees=$%.2f | net=$%+.2f | mode=%s",
            reason, qty, entry, exit_price, gross, fees.total, net, self.mode,
        )
        log_event("TRADE_EXIT", {"reason": reason, "entry": entry, "exit": exit_price, "gross": gross, "fees": fees.total, "net": net, "r": r_mult})
        tg.trade_exit(entry, exit_price, net, reason)

    # ─────────────────────────────────────────
    # EXTERNAL EXIT (bracket / manual / liquidation) — LIVE only
    # ─────────────────────────────────────────

    def reconcile_external_exit(self, tick: Optional[TickData]) -> bool:
        """
        If the broker is FLAT while state says SHORT, the bracket (or a manual
        close) closed it. Book it at the bracket level that was hit, else LTP.
        Returns True if a reconciliation happened.
        """
        with trade_lock:
            pos = self.state.get("current_position")
            if self.mode != "LIVE" or not self.state.get("in_position") or not pos:
                return False
            known, live = self.broker.get_position()
            if not known or live is not None:
                return False

            ltp = tick.ltp if tick else pos["entry_price"]
            if ltp >= pos["sl_price"] - 0.5:
                exit_price, reason = pos["sl_price"], "BROKER_SL_HIT"
            elif ltp <= pos["target_price"] + 0.5:
                exit_price, reason = pos["target_price"], "BROKER_TP_HIT"
            else:
                exit_price, reason = ltp, "EXTERNAL_EXIT"

            log.warning("[SYNC] 🔔 Broker is FLAT — position closed externally (%s, est. $%.2f).", reason, exit_price)
            self.broker.cancel_stop_orders()
            self._finalize_exit(pos, exit_price, reason)
            return True

    def update_heartbeat(self) -> None:
        """Persist last_heartbeat for downtime calculation (AGENTS §5.6)."""
        pos = self.state.get("current_position")
        if self.state.get("in_position") and pos:
            pos["last_heartbeat"] = now_ist().isoformat()
            save_state(self.state)
