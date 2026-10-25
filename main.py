"""
═════════════════════════════════════════════════════════════
  MAIN — Orchestration Loop, Gold (PAXGUSD) 1-Hour SMA Breakdown
═════════════════════════════════════════════════════════════

State machine:
  STARTUP → CONNECT → LOAD STATE → IN-FLIGHT RECOVERY → CRASH RECOVERY
  → LOOP { day rollover | in position: monitor exits | flat: wait for
           H1 close → SMA → signal → SHORT entry }

24/7 market: no session window, no expiry. Positions carry overnight.
Graceful shutdown (SIGTERM/SIGINT) keeps an open position — crash
recovery resumes it on restart (or force-exits after >15 min downtime).
Unhandled exceptions trigger an emergency square-off (AGENTS §5.5).
"""

import argparse
import random
import signal as signal_mod
import sys
import time
from datetime import datetime, timezone, timedelta

from config import CONFIG, IST, log, log_event, now_ist, today_ist, rollover_logging, flush_all_logs
from broker import DeltaBroker
from pattern import attach_sma, detect_signal, format_signal_checklist
from risk import position_oz, calculate_r_multiple
from orders import OrderManager, trade_lock
from state import load_state, save_state, reset_daily_counters, append_candle_log
from telegram_alerts import tg


# ─────────────────────────────────────────────
# GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    log.info("[SHUTDOWN] 🛑 Signal %s received — shutting down gracefully (open position is kept).", signum)


signal_mod.signal(signal_mod.SIGINT, _signal_handler)
signal_mod.signal(signal_mod.SIGTERM, _signal_handler)


def _sleep_safe(seconds: float) -> None:
    """Sleep that wakes early on shutdown."""
    end = time.time() + seconds
    while time.time() < end and not _shutdown_requested:
        time.sleep(min(0.5, max(0.01, end - time.time())))


def _sleep_idle(seconds_left: int) -> None:
    """Adaptive flat-state polling with jitter (AGENTS §3)."""
    if seconds_left <= 120:
        _sleep_safe(CONFIG["boundary_poll_interval_s"] + random.uniform(0, 1.5))
    else:
        _sleep_safe(min(CONFIG["candle_poll_interval_s"], max(1, seconds_left - 120)) + random.uniform(0, 1.5))


def _banner(broker: DeltaBroker) -> None:
    oz = position_oz(CONFIG["contract_qty"], broker.contract_value)
    log.info("=" * 72)
    log.info("  GOLD (PAXGUSD) 1-HOUR SMA BREAKDOWN — SHORT ONLY")
    log.info("  Mode: %s | Delta Exchange perpetual | 24/7 positional", CONFIG["trading_mode"])
    log.info("  Size: %d contracts = %g oz | R:R 1:%.1f | SL = signal candle high", CONFIG["contract_qty"], oz, CONFIG["rr_ratio"])
    log.info("  Signal: red | open > SMA%d | SMA%d > SMA%d | close below both SMAs",
             CONFIG["sma_long"], CONFIG["sma_short"], CONFIG["sma_long"])
    log.info("  Max trades/day: %d | Max daily loss: %s | Max risk: %s",
             CONFIG["max_trades_per_day"],
             f"${CONFIG['max_daily_loss']}" if CONFIG["max_daily_loss"] else "OFF",
             f"${CONFIG['max_risk_dollars']}" if CONFIG["max_risk_dollars"] else "OFF")
    log.info("  Leverage: Delta default (bot never changes it) | product default %sx (IM %s%%, MM %s%%) | account: %s, auto top-up %s",
             f"{broker.default_leverage:g}" if broker.default_leverage else "?",
             broker.initial_margin_pct, broker.maintenance_margin_pct,
             f"{broker.account_leverage:g}x" if broker.account_leverage else "UNVERIFIED",
             {True: "ON", False: "OFF", None: "UNVERIFIED"}[broker.account_auto_topup])
    log.info("=" * 72)


# ─────────────────────────────────────────────
# STARTUP RECOVERY
# ─────────────────────────────────────────────

def _recover_in_flight_order(state: dict, broker: DeltaBroker) -> None:
    """
    A crash between sending the entry order and saving the position leaves
    `in_flight_order` in state. Adopt the broker SHORT if it exists, else clear.
    """
    flight = state.get("in_flight_order")
    if not flight or state.get("in_position"):
        return
    log.warning("[RECOVERY] ⚠️ Found in-flight entry order from previous run: %s", flight)
    if CONFIG["trading_mode"] != "LIVE":
        state["in_flight_order"] = None
        save_state(state)
        return

    known, pos = broker.get_position()
    if not known:
        tg.critical("In-flight order UNKNOWN", f"Cannot read positions to resolve {flight.get('client_order_id')}. Halting.")
        raise RuntimeError("Cannot resolve in-flight entry order: broker positions unavailable.")

    if pos is not None and pos.direction == "SHORT":
        now = now_ist()
        state["in_position"] = True
        state["trades_today"] = state.get("trades_today", 0) + 1
        state["current_position"] = {
            "direction": "SHORT",
            "entry_order_id": flight.get("client_order_id", ""),
            "qty": pos.size,
            "entry_price": pos.entry_price,
            "sl_price": flight["sl_price"],
            "target_price": flight["target_price"],
            "risk": round(flight["sl_price"] - pos.entry_price, 2),
            "signal_candle_time": flight.get("signal_candle_time", ""),
            "bracket_attached": broker.place_position_bracket(flight["sl_price"], flight["target_price"]),
            "entry_time": flight.get("placed_at", now.isoformat()),
            "entry_date": now.date().isoformat(),
            "last_heartbeat": flight.get("placed_at", now.isoformat()),
        }
        log.warning("[RECOVERY] ✅ Adopted broker SHORT %d @ $%.2f from in-flight order.", pos.size, pos.entry_price)
        tg.bot_restarted("ADOPTED_IN_FLIGHT_ENTRY", f"SHORT {pos.size} @ ${pos.entry_price:.2f}")
    else:
        log.info("[RECOVERY] In-flight order never opened a position. Clearing.")
    state["in_flight_order"] = None
    save_state(state)


def _crash_recovery(state: dict, broker: DeltaBroker, order_mgr: OrderManager) -> None:
    """
    Open position on startup (AGENTS §5.3/5.4):
      LIVE + broker FLAT       → reconcile (bracket/manual exit while offline)
      downtime > 15 min        → force exit (market is 24/7, so all downtime counts)
      SL / target breached now → exit
      otherwise                → resume monitoring
    """
    pos = state.get("current_position")
    if not state.get("in_position") or not pos:
        return

    tick = broker.get_tick()
    if order_mgr.reconcile_external_exit(tick):
        tg.bot_restarted("BROKER_ALREADY_EXITED", "Position was closed on Delta while the bot was offline.")
        return

    downtime_min = 999.0
    try:
        last_hb = datetime.fromisoformat(pos.get("last_heartbeat", ""))
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=IST)
        downtime_min = max(0.0, (now_ist() - last_hb).total_seconds() / 60.0)
    except (TypeError, ValueError):
        pass

    log.warning(
        "[RECOVERY] Open SHORT %d @ $%.2f (SL $%.2f / TP $%.2f) | downtime %.1f min",
        pos["qty"], pos["entry_price"], pos["sl_price"], pos["target_price"], downtime_min,
    )

    if downtime_min > CONFIG["downtime_exit_minutes"]:
        log.warning("[RECOVERY] Downtime > %d min — emergency square-off.", CONFIG["downtime_exit_minutes"])
        order_mgr.exit_trade("CRASH_DOWNTIME_EXCEEDED", tick)
        tg.bot_restarted("FORCE_EXIT_DOWNTIME", f"Offline {downtime_min:.0f} min with an open SHORT.")
        return

    if tick:
        reason = order_mgr.check_exit_conditions(tick.ltp)
        if reason:
            order_mgr.exit_trade(reason, tick)
            tg.bot_restarted("EXITED_ON_RESTART", f"{reason} already breached (LTP ${tick.ltp:.2f}).")
            return

    order_mgr.update_heartbeat()
    log.info("[RECOVERY] ✅ Position still valid — resuming monitoring.")
    tg.bot_restarted("RESUME_MONITORING", f"SHORT {pos['qty']} @ ${pos['entry_price']:.2f}")


# ─────────────────────────────────────────────
# CANDLE EVALUATION
# ─────────────────────────────────────────────

def _evaluate_closed_candle(broker: DeltaBroker, order_mgr: OrderManager, state: dict, closed_bar_ts: int) -> bool:
    """
    Fetch history, compute SMAs, evaluate the just-closed H1 bar and enter if valid.
    Returns False if Delta has not published the bar yet (caller retries).
    """
    bar_s = CONFIG["candle_tf_minutes"] * 60
    candles = broker.get_candles(CONFIG["candle_history_bars"])
    if not candles or int(candles[-1]["date_utc"].timestamp()) != closed_bar_ts:
        log.debug("[DATA] Closed bar %s not yet available (latest=%s).", closed_bar_ts,
                  candles[-1]["date_utc"] if candles else None)
        return False

    candles = attach_sma(candles)
    latest = candles[-1]
    signal = detect_signal(candles)

    close_ist = latest["date"] + timedelta(seconds=bar_s)
    log.info("═" * 72)
    log.info(
        "[CANDLE CLOSED] 📊 H1 %s → %s IST | O=$%.2f H=$%.2f L=$%.2f C=$%.2f | %s",
        latest["date"].strftime("%Y-%m-%d %H:%M"), close_ist.strftime("%H:%M"),
        latest["open"], latest["high"], latest["low"], latest["close"],
        "🔴 RED" if latest["close"] < latest["open"] else ("🟢 GREEN" if latest["close"] > latest["open"] else "⚪ DOJI"),
    )
    log.info(format_signal_checklist(latest))
    log.info("═" * 72)
    append_candle_log(latest, signal is not None)
    log_event("CANDLE_CLOSED", {
        "time": latest["date"].isoformat(), "open": latest["open"], "high": latest["high"],
        "low": latest["low"], "close": latest["close"],
        "sma_20": latest.get("sma_20"), "sma_50": latest.get("sma_50"), "signal": signal is not None,
    })

    if signal is None:
        return True

    candle_key = str(latest["date"])
    if state.get("last_signal_candle_time") == candle_key:
        log.info("[SIGNAL] Duplicate signal candle %s — already handled.", candle_key)
        return True

    delay = (datetime.now(timezone.utc) - (latest["date_utc"] + timedelta(seconds=bar_s))).total_seconds()
    if delay > CONFIG["max_entry_delay_seconds"]:
        log.warning("[SIGNAL] ⚠️ Signal is %.0fs old (> %ds). Not chasing — skipped.", delay, CONFIG["max_entry_delay_seconds"])
        tg.setup_skipped(f"STALE_SIGNAL: {delay:.0f}s after candle close")
    else:
        tg.signal_detected(latest["date"], latest["close"], signal.sma_20, signal.sma_50, signal.sl_price)
        log_event("SIGNAL_DETECTED", {"candle": candle_key, "close": latest["close"], "sl": signal.sl_price})
        ok, reason = order_mgr.can_enter()
        if ok:
            order_mgr.enter_trade(signal, broker.get_tick())
        else:
            log.info("[SIGNAL] Entry blocked: %s", reason)

    state["last_signal_candle_time"] = candle_key
    save_state(state)
    return True


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_strategy() -> None:
    parser = argparse.ArgumentParser(description="Gold 1-Hour SMA Breakdown (SHORT) on Delta Exchange")
    parser.add_argument("--daemon", action="store_true", help="Non-interactive mode (systemd): skip the LIVE prompt")
    args = parser.parse_args()

    broker = DeltaBroker()
    if not broker.connect():
        log.critical("[STARTUP] ❌ Cannot connect to Delta Exchange. Exiting.")
        tg.bot_crashed("STARTUP: Delta Exchange connection / product validation failed")
        sys.exit(1)

    _banner(broker)

    if CONFIG["trading_mode"] == "LIVE":
        if not args.daemon and sys.stdin.isatty():
            if input("⚠️  LIVE trading with real money. Type 'LIVE' to confirm: ").strip().upper() != "LIVE":
                log.info("[STARTUP] LIVE mode not confirmed. Exiting.")
                sys.exit(0)
        log.warning("[STARTUP] 🔴 LIVE TRADING ACTIVE — real orders will be placed.")
    else:
        log.info("[STARTUP] 📝 PAPER trading — no real orders.")

    state = load_state()
    order_mgr = OrderManager(broker, state)

    _recover_in_flight_order(state, broker)
    _crash_recovery(state, broker, order_mgr)
    tg.bot_started(CONFIG["contract_qty"], order_mgr.oz)

    # Started mid-bar: don't evaluate a bar that closed long ago
    bar_s = CONFIG["candle_tf_minutes"] * 60
    status = broker.get_candle_status()
    last_evaluated = status["last_closed_bar_time"] if status["seconds_left"] < bar_s - CONFIG["max_entry_delay_seconds"] else None

    current_date = today_ist().isoformat()
    last_data_time = time.time()
    last_hb_save = last_pos_log = last_idle_log = last_broker_check = 0.0

    try:
        while not _shutdown_requested:
            # ── Day rollover (IST) ──
            new_date = today_ist().isoformat()
            if new_date != current_date:
                tg.daily_summary(current_date, state.get("trades_today", 0), state.get("realized_pnl_today", 0.0),
                                 state.get("total_realized_pnl", 0.0), state.get("cash", 0.0))
                rollover_logging(new_date)
                reset_daily_counters(state)
                save_state(state)
                log.info("[DAY ROLLOVER] 📅 %s → %s | daily counters reset, position preserved.", current_date, new_date)
                current_date = new_date

            # ── In position: monitor exits ──
            if state.get("in_position") and state.get("current_position"):
                now_ts = time.time()
                tick = broker.get_tick()

                if CONFIG["trading_mode"] == "LIVE" and now_ts - last_broker_check >= CONFIG["broker_position_check_interval_s"]:
                    last_broker_check = now_ts
                    if order_mgr.reconcile_external_exit(tick):
                        last_evaluated = broker.get_candle_status()["last_closed_bar_time"]
                        continue

                if tick:
                    last_data_time = now_ts
                    tick_age = (datetime.now(timezone.utc) - tick.time).total_seconds()
                    if tick_age > CONFIG["stale_tick_threshold_s"]:
                        log.warning("[MONITOR] ⚠️ Ticker data is %.0fs old.", tick_age)
                    with trade_lock:
                        reason = order_mgr.check_exit_conditions(tick.ltp)
                        if reason and order_mgr.exit_trade(reason, tick):
                            # Bars that closed during the trade are not re-evaluated as fresh signals
                            last_evaluated = broker.get_candle_status()["last_closed_bar_time"]
                            continue
                elif now_ts - last_data_time > CONFIG["data_unavailable_exit_s"]:
                    log.critical("[MONITOR] 🛑 No price data for %.0fs — emergency exit.", now_ts - last_data_time)
                    pos = state["current_position"]
                    if order_mgr.exit_trade("DATA_UNAVAILABLE", None, fallback_price=pos["entry_price"]):
                        last_evaluated = broker.get_candle_status()["last_closed_bar_time"]
                        continue

                if now_ts - last_hb_save >= CONFIG["heartbeat_save_interval_s"]:
                    last_hb_save = now_ts
                    order_mgr.update_heartbeat()

                if tick and now_ts - last_pos_log >= CONFIG["in_position_heartbeat_interval_s"]:
                    last_pos_log = now_ts
                    pos = state["current_position"]
                    log.info(
                        "[IN POSITION] 💓 SHORT %d @ $%.2f | LTP $%.2f (bid $%.2f / ask $%.2f) | SL $%.2f | TP $%.2f | %.2fR | funding %.4f%%",
                        pos["qty"], pos["entry_price"], tick.ltp, tick.bid, tick.ask, pos["sl_price"], pos["target_price"],
                        calculate_r_multiple(pos["entry_price"], tick.ask, pos["risk"]), tick.funding_rate * 100,
                    )

                _sleep_safe(CONFIG["position_monitor_interval_s"] + random.uniform(0, CONFIG["position_monitor_jitter_s"]))
                continue

            # ── Flat: wait for the next completed H1 bar ──
            status = broker.get_candle_status()
            closed_ts = status["last_closed_bar_time"]

            if last_evaluated != closed_ts:
                since_close = bar_s - status["seconds_left"]
                if since_close < 3:
                    _sleep_safe(3 - since_close)  # let Delta finalize the bar
                if _evaluate_closed_candle(broker, order_mgr, state, closed_ts):
                    last_evaluated = closed_ts
                elif since_close > CONFIG["max_entry_delay_seconds"]:
                    log.warning("[DATA] ⚠️ Bar %s still missing %.0fs after close — giving up on it.", closed_ts, since_close)
                    last_evaluated = closed_ts
                else:
                    _sleep_safe(5 + random.uniform(0, 1.5))
                    continue

            if time.time() - last_idle_log >= CONFIG["idle_heartbeat_interval_s"]:
                last_idle_log = time.time()
                tick = broker.get_tick()
                log.info(
                    "[HEARTBEAT] ⏳ FLAT | LTP %s | next H1 close %s IST (%dm) | trades today %d/%d | day P&L $%+.2f | %s",
                    f"${tick.ltp:.2f}" if tick else "N/A", status["expected_close_ist"].strftime("%H:%M"),
                    status["seconds_left"] // 60, state.get("trades_today", 0), CONFIG["max_trades_per_day"],
                    state.get("realized_pnl_today", 0.0), CONFIG["trading_mode"],
                )

            _sleep_idle(broker.get_candle_status()["seconds_left"])

    except Exception as e:
        log.critical("[CRASH] 🚨 Unhandled exception: %s", e, exc_info=True)
        tg.bot_crashed(str(e))
        if state.get("in_position") and state.get("current_position"):
            log.warning("[CRASH GUARD] ⚠️ Emergency square-off of open SHORT...")
            try:
                order_mgr.exit_trade("CRASH_SQUAREOFF", broker.get_tick(), fallback_price=state["current_position"]["entry_price"])
            except Exception as exit_err:
                log.critical("[CRASH GUARD] ❌ Emergency exit FAILED: %s", exit_err)
        raise
    finally:
        broker.disconnect()
        log.info("[SHUTDOWN] ✅ Stopped. Position: %s", "OPEN (kept)" if state.get("in_position") else "FLAT")
        flush_all_logs()


if __name__ == "__main__":
    run_strategy()
