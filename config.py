"""
═════════════════════════════════════════════════════════════
  CONFIGURATION — GOLD (PAXGUSD) 1-Hour SMA Breakdown Strategy
                  on Delta Exchange (Positional, SHORT only)
═════════════════════════════════════════════════════════════

Single source of truth for ALL strategy parameters.
Nothing strategy-specific is hardcoded outside this module.

Port of the NIFTY 1-Hour SMA PUT v2 strategy to Gold:
  - Same signal rules (red candle, open > SMA50, SMA20 > SMA50,
    close below both SMAs) on completed H1 candles.
  - Same risk rules (SL = signal candle high, 1:1 R:R).
  - Bearish exposure is taken by SHORTING the PAXGUSD perpetual
    (Delta Exchange has no gold options to buy a PUT on).

PAXGUSD trades 24/7 — there is no session window and no expiry.
"""

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, date, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# Enable Windows ANSI VT100 processing for rich console logging
if os.name == "nt":
    try:
        os.system("")
    except Exception:
        pass

# ─────────────────────────────────────────────
# PATH SETUP
# ─────────────────────────────────────────────
STRATEGY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = STRATEGY_DIR.parent
DOTENV_PATH = STRATEGY_DIR / ".env"
load_dotenv(DOTENV_PATH)

LOG_DIR = STRATEGY_DIR / "logs"
STATE_DIR = STRATEGY_DIR / "state"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# TIMEZONES
# ─────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30), "IST")
UTC = timezone.utc


def now_ist() -> datetime:
    """Current datetime in IST."""
    return datetime.now(IST)


def now_utc() -> datetime:
    """Current datetime in UTC."""
    return datetime.now(UTC)


def today_ist() -> date:
    """Current date in IST (trading-day key for daily counters)."""
    return now_ist().date()


# ─────────────────────────────────────────────
# STRATEGY CONFIGURATION
# ─────────────────────────────────────────────
CONFIG = {
    # ── Instrument ───────────────────────────────
    "symbol": "PAXGUSD",
    "symbol_description": "PAX Gold Perpetual / US Dollar (Delta Exchange)",
    "delta_base_url": os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange"),
    "delta_symbol": "PAXGUSD",
    "delta_resolution": "1h",

    # ── Position sizing ──────────────────────────
    # 1 Delta contract = contract_value PAXG (0.001 oz as of 2026-10-17).
    # 100 contracts = 0.1 oz gold = $0.10 P&L per $1 price move
    # (owner decision 2026-10-17: same size as gold_4hr_reversal).
    # contract_value is read live from /v2/products at startup and must
    # match expected_contract_value, otherwise the bot refuses to start.
    "contract_qty": 100,
    "expected_contract_value": 0.001,

    # ── Candle / timeframe ───────────────────────
    "candle_tf_label": "H1",
    "candle_tf_minutes": 60,

    # ── Indicators ───────────────────────────────
    "sma_short": 20,
    "sma_long": 50,
    "candle_history_bars": 300,       # SMA warmup window (~12.5 days of 24/7 H1 bars)

    # ── Risk management (same rules as NIFTY 1H SMA PUT v2) ──
    "max_trades_per_day": 5,
    "rr_ratio": 1.0,                  # Target = entry - risk * rr_ratio
    "max_risk_dollars": 0,            # Max SL distance in $ (0 = disabled), like NIFTY max_spot_risk
    "max_daily_loss": 0,              # Daily drawdown kill-switch in $ (0 = disabled)

    # ── Execution guards ─────────────────────────
    "max_spread_dollars": 1.20,       # Skip entry if bid-ask spread is wider
    "max_entry_delay_seconds": 300,   # Skip signal if bot is > 5 min late after candle close
    "downtime_exit_minutes": 15,      # Crash recovery: force exit if offline longer (AGENTS §5.4)
    # Leverage: owner decision 2026-10-17 — use Delta's default (100x on PAXGUSD); the bot
    # NEVER changes it. Startup reads the account leverage / auto top-up (read-only) and every
    # entry logs Delta's liquidation price, alerting if it sits before the SL.
    "bracket_stop_trigger_method": "last_traded_price",

    # ── Transaction costs ────────────────────────
    # taker_fee_pct is replaced at startup by the live product taker rate.
    "taker_fee_pct": 0.0005,
    "gst_pct": 0.18,                  # GST on exchange fees (Delta India)

    # ── Product type ─────────────────────────────
    "product": "POSITIONAL",          # Carry overnight / weekends (no expiry on perpetuals)

    # ── Trading mode ─────────────────────────────
    "trading_mode": os.getenv("TRADING_MODE", "PAPER").upper(),

    # ── Virtual starting capital (paper mode) ────
    "starting_capital": 100000.0,

    # ── Monitoring loop intervals ────────────────
    "candle_poll_interval_s": 30,               # Far from candle boundary
    "boundary_poll_interval_s": 5,              # Within 2 minutes of candle close
    "position_monitor_interval_s": 1.0,         # Tick poll while in position
    "position_monitor_jitter_s": 0.2,
    "broker_position_check_interval_s": 15,     # LIVE: detect bracket / manual exits
    "stale_tick_threshold_s": 10,               # Warn if ticker data is older than this
    "data_unavailable_exit_s": 120,             # Emergency exit if no price data this long
    "heartbeat_save_interval_s": 60,            # Persist last_heartbeat (AGENTS §5.6)
    "in_position_heartbeat_interval_s": 300,    # In-trade status log
    "idle_heartbeat_interval_s": 900,           # Flat status log

    # ── Logging ──────────────────────────────────
    "log_level": "INFO",
    "log_max_bytes": 10 * 1024 * 1024,
    "log_backup_count": 10,
}


# ═══════════════════════════════════════════════
# LOGGING SETUP — Multi-channel strategy logging
# ═══════════════════════════════════════════════
#
# Daily directory: logs/YYYY-MM-DD/
#   strategy.log  — Main strategy flow (INFO+)
#   debug.log     — Full debug trace (DEBUG+)
#   trades.log    — Entries, exits, P&L only
#   network.log   — Delta Exchange API & urllib3 traffic
#   candles.csv   — Completed H1 bars with SMA values
#   events.jsonl  — Machine-readable event stream
#
# Persistent cross-day ledger: logs/journal.csv

class ISTFormatter(logging.Formatter):
    """Formatter with explicit IST timestamps and aligned multiline messages."""

    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None, include_module: bool = False):
        super().__init__(fmt, datefmt=datefmt)
        self.include_module = include_module

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + " IST"

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if "\n" in s:
            lines = s.split("\n")
            first_line = lines[0]
            msg_first_line = record.getMessage().split("\n")[0]
            sep_idx = first_line.find(" | " + msg_first_line)
            indent = (" " * sep_idx + " | ") if sep_idx > 0 else (" " * 31 + " | ")
            return first_line + "\n" + "\n".join(indent + line if line.strip() else "" for line in lines[1:])
        return s


class ConsoleColorFormatter(ISTFormatter):
    """ANSI-colored formatter for interactive terminals."""

    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[1;91m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            color = self.COLORS.get(record.levelno, "")
            if color:
                return f"{color}{formatted}{self.RESET}"
        return formatted


def _make_formatter(include_module: bool = False, use_color: bool = False) -> logging.Formatter:
    if include_module:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)-5s | %(message)s"
    cls = ConsoleColorFormatter if use_color else ISTFormatter
    return cls(fmt, datefmt="%Y-%m-%d %H:%M:%S IST", include_module=include_module)


class AutoFlushRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes every record for real-time observability."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


# urllib3 logs request lines ("POST /bot<TOKEN>/sendMessage") at DEBUG — mask the Telegram bot token.
_TG_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_\-]+")


class _TelegramTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = _TG_TOKEN_RE.sub("/bot<redacted>", message)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _TG_TOKEN_RE.sub("/bot<redacted>", record.exc_text)
        return True


_TG_TOKEN_FILTER = _TelegramTokenFilter()


def _new_file_handler(path: Path, level: int, include_module: bool = True) -> AutoFlushRotatingFileHandler:
    fh = AutoFlushRotatingFileHandler(
        path,
        maxBytes=CONFIG["log_max_bytes"],
        backupCount=CONFIG["log_backup_count"],
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(_make_formatter(include_module))
    fh.addFilter(_TG_TOKEN_FILTER)
    return fh


LOGGER_NAME = "Gold1HrSMA"
_logging_lock = threading.RLock()


def _remove_file_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)


def rollover_logging(new_date_str: str) -> None:
    """
    Re-bind all file handlers to logs/<new_date>/ without restarting the
    process (AGENTS §9 — 24/7 positional engines).
    """
    with _logging_lock:
        clean_date_str = Path(str(new_date_str)).name
        daily_log_dir = LOG_DIR / clean_date_str
        daily_log_dir.mkdir(exist_ok=True)

        root_logger = logging.getLogger(LOGGER_NAME)
        trade_logger = logging.getLogger(f"{LOGGER_NAME}.trades")
        network_logger = logging.getLogger(f"{LOGGER_NAME}.network")
        urllib3_logger = logging.getLogger("urllib3")

        for lg in (root_logger, trade_logger, network_logger, urllib3_logger):
            _remove_file_handlers(lg)

        root_logger.addHandler(_new_file_handler(daily_log_dir / "strategy.log", logging.INFO))
        root_logger.addHandler(_new_file_handler(daily_log_dir / "debug.log", logging.DEBUG))
        trade_logger.addHandler(_new_file_handler(daily_log_dir / "trades.log", logging.INFO, include_module=False))

        network_fh = _new_file_handler(daily_log_dir / "network.log", logging.DEBUG)
        network_logger.addHandler(network_fh)
        urllib3_logger.setLevel(logging.DEBUG)
        urllib3_logger.propagate = False
        urllib3_logger.addHandler(network_fh)

        root_logger.info("[STARTUP] 📂 Logging bound to trading date %s (Folder: %s)", clean_date_str, daily_log_dir)


def flush_all_logs() -> None:
    """Flush every file handler in the logging hierarchy."""
    with _logging_lock:
        for name in (LOGGER_NAME, f"{LOGGER_NAME}.trades", f"{LOGGER_NAME}.network", "urllib3"):
            for h in list(logging.getLogger(name).handlers):
                try:
                    h.flush()
                except Exception:
                    pass


def install_exception_hooks() -> None:
    """Record any unhandled exception in any thread to strategy.log / debug.log."""
    root_log = logging.getLogger(LOGGER_NAME)

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root_log.critical("[CRASH] 🚨 Uncaught system exception:", exc_info=(exc_type, exc_value, exc_tb))
        flush_all_logs()

    def _thread_hook(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        root_log.critical(
            "[CRASH] 🚨 Uncaught exception in thread '%s':", args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        flush_all_logs()

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


def log_event(event_type: str, data: Dict[str, Any]) -> None:
    """Append one structured event to logs/YYYY-MM-DD/events.jsonl."""
    daily_log_dir = LOG_DIR / today_ist().isoformat()
    daily_log_dir.mkdir(exist_ok=True)
    record = {
        "timestamp": now_ist().isoformat(),
        "event": event_type,
        "symbol": CONFIG.get("symbol", "PAXGUSD"),
        **data,
    }
    with _logging_lock:
        try:
            with (daily_log_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            logging.getLogger(LOGGER_NAME).warning("[LOGGING] ⚠️ Failed to write events.jsonl: %s", e)


def setup_logging() -> logging.Logger:
    """
    Logger hierarchy:
      Gold1HrSMA          — root strategy logger
      Gold1HrSMA.pattern  — signal evaluation
      Gold1HrSMA.risk     — SL / target / cost math
      Gold1HrSMA.orders   — order lifecycle
      Gold1HrSMA.broker   — Delta Exchange client
      Gold1HrSMA.trades   — trade-only log (no propagation)
      Gold1HrSMA.network  — raw API traffic (no propagation)
    """
    root_logger = logging.getLogger(LOGGER_NAME)
    if root_logger.handlers:
        return root_logger

    root_logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, CONFIG["log_level"], logging.INFO))
    ch.setFormatter(_make_formatter(include_module=False, use_color=True))
    root_logger.addHandler(ch)

    logging.getLogger(f"{LOGGER_NAME}.trades").propagate = False
    logging.getLogger(f"{LOGGER_NAME}.network").propagate = False

    rollover_logging(today_ist().isoformat())
    install_exception_hooks()
    return root_logger


# ── Module-level loggers ─────────────────────────
log = setup_logging()
log_pattern = logging.getLogger(f"{LOGGER_NAME}.pattern")
log_risk = logging.getLogger(f"{LOGGER_NAME}.risk")
log_orders = logging.getLogger(f"{LOGGER_NAME}.orders")
log_broker = logging.getLogger(f"{LOGGER_NAME}.broker")
log_trades = logging.getLogger(f"{LOGGER_NAME}.trades")
log_network = logging.getLogger(f"{LOGGER_NAME}.network")
