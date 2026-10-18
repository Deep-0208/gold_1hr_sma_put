"""
═════════════════════════════════════════════════════════════
  STATE — Atomic Persistence, Trade Journal & Candle Log
═════════════════════════════════════════════════════════════

Single state_active.json file, written atomically (tmp → os.replace).
Positions carry across days (overnight, weekends). Daily counters
(trades_today, realized_pnl_today, last_signal_candle_time) reset
when the IST date changes.
"""

import csv
import json
import os
import tempfile
import threading
import time
from typing import Dict, Any, Optional

try:
    from typing import TypedDict
except ImportError:  # pragma: no cover
    from typing_extensions import TypedDict

from config import STATE_DIR, LOG_DIR, CONFIG, today_ist, log

_state_lock = threading.RLock()


class PositionState(TypedDict, total=False):
    direction: str               # Always "SHORT"
    entry_order_id: str
    qty: int                     # contracts
    entry_price: float           # actual fill price
    sl_price: float              # signal candle high
    target_price: float          # entry - risk * rr
    risk: float                  # sl - entry ($ per 1 PAXG)
    signal_candle_time: str
    bracket_attached: bool
    entry_time: str
    entry_date: str
    last_heartbeat: str


class StrategyState(TypedDict, total=False):
    date: str
    trades_today: int
    realized_pnl_today: float
    total_realized_pnl: float
    cash: float
    in_position: bool
    current_position: Optional[PositionState]
    in_flight_order: Optional[Dict[str, Any]]
    last_signal_candle_time: Optional[str]


def fresh_state() -> StrategyState:
    return StrategyState(
        date=today_ist().isoformat(),
        trades_today=0,
        realized_pnl_today=0.0,
        total_realized_pnl=0.0,
        cash=float(CONFIG["starting_capital"]),
        in_position=False,
        current_position=None,
        in_flight_order=None,
        last_signal_candle_time=None,
    )


def _state_file():
    return STATE_DIR / "state_active.json"


def reset_daily_counters(state: StrategyState) -> None:
    """Reset per-day counters; open position, cash and total P&L are preserved."""
    state["date"] = today_ist().isoformat()
    state["trades_today"] = 0
    state["realized_pnl_today"] = 0.0
    state["last_signal_candle_time"] = None


def load_state() -> StrategyState:
    """
    Load state from disk (or fresh). A corrupt state file halts the bot —
    it may describe an open position we must not forget about.
    """
    with _state_lock:
        for f in STATE_DIR.glob("state_*.tmp"):
            try:
                if time.time() - f.stat().st_mtime > 600:
                    f.unlink()
            except OSError:
                pass

        path = _state_file()
        if not path.exists():
            log.info("[STATE] 📂 No state file — fresh state, capital $%s.", f"{CONFIG['starting_capital']:,.2f}")
            return fresh_state()

        raw = None
        for attempt in range(3):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except PermissionError:
                time.sleep(0.05)
            except Exception as e:
                if attempt == 2:
                    log.critical("[STATE] 🛑 State file %s is corrupt (%s). Refusing to start.", path, e)
                    raise RuntimeError(f"Corrupt state file {path}: {e}")
                time.sleep(0.05)
        if raw is None:
            raise RuntimeError(f"State file {path} is locked / unreadable.")

        state = fresh_state()
        state.update(raw)

        if state.get("date") != today_ist().isoformat():
            log.info(
                "[STATE] 📅 Date rollover since last run (%s → %s). Daily counters reset; position preserved.",
                state.get("date"), today_ist().isoformat(),
            )
            reset_daily_counters(state)
            save_state(state)

        pos = state.get("current_position") if state.get("in_position") else None
        log.info(
            "[STATE] 📂 Restored | Trades today: %d | Position: %s | Cash: $%s | Total P&L: $%s",
            state.get("trades_today", 0),
            f"SHORT {pos.get('qty')} @ ${pos.get('entry_price', 0):.2f}" if pos else "FLAT",
            f"{state.get('cash', 0):,.2f}", f"{state.get('total_realized_pnl', 0):+,.2f}",
        )
        return state


def save_state(state: StrategyState) -> None:
    """Atomic write: temp file in the same directory, then os.replace (AGENTS §5.1)."""
    with _state_lock:
        data = json.dumps(state, indent=2, default=str)
        fd, tmp_path = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp", prefix="state_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(3):
                try:
                    os.replace(tmp_path, str(_state_file()))
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ─────────────────────────────────────────────
# TRADE JOURNAL (cross-day CSV)
# ─────────────────────────────────────────────

JOURNAL_FIELDS = [
    "date", "symbol", "direction", "qty", "oz", "entry_order_id",
    "signal_candle_time", "entry_time", "exit_time",
    "entry_price", "exit_price", "sl_price", "target_price", "risk", "liquidation_price",
    "gross_pnl", "fees", "net_pnl", "r_multiple", "exit_reason", "mode",
]


def append_trade_journal(row: Dict[str, Any]) -> None:
    """Append one closed trade to logs/journal.csv (duplicate entry_order_id guarded)."""
    with _state_lock:
        path = LOG_DIR / "journal.csv"
        new_file = not path.exists() or path.stat().st_size == 0

        oid = row.get("entry_order_id")
        if not new_file and oid:
            try:
                with path.open("r", encoding="utf-8") as rf:
                    if any(r.get("entry_order_id") == str(oid) for r in csv.DictReader(rf)):
                        log.warning("[JOURNAL] ⚠️ Trade %s already journaled — skipping duplicate.", oid)
                        return
            except Exception as e:
                log.warning("[JOURNAL] ⚠️ Could not check journal duplicates: %s", e)

        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        log.info("[JOURNAL] 📝 Trade saved to %s", path.name)


# ─────────────────────────────────────────────
# CANDLE LOG (per-day CSV, AGENTS §9)
# ─────────────────────────────────────────────

CANDLE_FIELDS = ["candle_time_ist", "candle_time_utc", "open", "high", "low", "close", "volume", "sma_20", "sma_50", "signal"]


def append_candle_log(candle: Dict[str, Any], signal: bool) -> None:
    """Append one completed bar (with SMAs) to logs/YYYY-MM-DD/candles.csv."""
    try:
        day_dir = LOG_DIR / today_ist().isoformat()
        day_dir.mkdir(exist_ok=True)
        path = day_dir / "candles.csv"
        new_file = not path.exists() or path.stat().st_size == 0

        def _r(v):
            return round(v, 2) if isinstance(v, float) else v

        with _state_lock, path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CANDLE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow({
                "candle_time_ist": candle["date"].isoformat(),
                "candle_time_utc": candle["date_utc"].isoformat() if candle.get("date_utc") else "",
                "open": candle["open"], "high": candle["high"], "low": candle["low"], "close": candle["close"],
                "volume": candle.get("volume", ""),
                "sma_20": _r(candle.get("sma_20")), "sma_50": _r(candle.get("sma_50")),
                "signal": "SHORT" if signal else "",
            })
    except Exception as e:
        log.warning("[CANDLES] ⚠️ Could not write candles.csv: %s", e)
