"""
═════════════════════════════════════════════════════════════
  TELEGRAM ALERTS — Non-blocking, fail-safe notifications
═════════════════════════════════════════════════════════════

  - NON-BLOCKING: messages go to a bounded queue drained by a daemon thread.
  - FAIL-SAFE:    any error is logged; the trading loop never crashes or waits.
  - MarkdownV2 with automatic plain-text fallback on HTTP 400 (AGENTS §15 #4).

Setup (.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import logging
import os
import queue
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

_IST = timezone(timedelta(hours=5, minutes=30), "IST")
_log = logging.getLogger("Gold1HrSMA.telegram")

_STRATEGY = "Gold 1\\-Hour SMA Breakdown \\(SHORT\\)"


def _now_ist_str() -> str:
    return datetime.now(_IST).strftime("%d %b %Y, %H:%M:%S IST")


def _escape(text: str) -> str:
    """Escape MarkdownV2 special characters outside code spans."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _code(text) -> str:
    """Inline code span with backslash / backtick escaped."""
    return "`" + str(text).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _mode() -> str:
    return os.getenv("TRADING_MODE", "PAPER").upper()


def _badge() -> str:
    return "📝 PAPER" if _mode() != "LIVE" else "🚀 LIVE"


class TelegramAlerter:
    """Fire-and-forget Telegram sender. Silent no-op without token / chat id."""

    def __init__(self, token: str = "", chat_id: str = ""):
        self._token = token.strip()
        self._chat_id = chat_id.strip()
        self._enabled = bool(self._token and self._chat_id)
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        if self._enabled:
            threading.Thread(target=self._worker_loop, daemon=True, name="TelegramAlertWorker").start()
            _log.info("[STARTUP] 📱 Telegram notifications active.")
        else:
            _log.info("[STARTUP] 📱 Telegram notifications DISABLED (no token/chat_id in .env).")

    def _worker_loop(self) -> None:
        while True:
            msg = self._queue.get()
            if msg is None:
                break
            self._send_sync(msg)

    def _send_sync(self, text: str) -> None:
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        for parse_mode in ("MarkdownV2", None):
            try:
                payload = {"chat_id": self._chat_id, "text": text if parse_mode else text.replace("\\", "")}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    return
                if resp.status_code == 400 and parse_mode:
                    continue  # plain-text fallback
                _log.warning("[TELEGRAM] ⚠️ HTTP %d: %s", resp.status_code, resp.text[:200])
                return
            except Exception as e:
                # requests puts the full URL (bot token included) in its error text — never log it.
                _log.warning("[TELEGRAM] ⚠️ Send failed: %s", str(e).replace(self._token, "<redacted>"))
                return

    def _enqueue(self, text: str) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            _log.warning("[TELEGRAM] ⚠️ Queue full, dropping message.")

    # ─────────────────────────────────────────
    # ALERTS
    # ─────────────────────────────────────────

    def bot_started(self, qty: int, oz: float) -> None:
        self._enqueue(
            f"✅ *Bot Started* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Strategy: {_STRATEGY}\n"
            f"🪙 Symbol:   `PAXGUSD` \\(Delta Exchange\\)\n"
            f"📦 Size:     {_code(f'{qty} contracts = {oz:g} oz')}\n"
            f"🕐 Time:     {_code(_now_ist_str())}"
        )

    def signal_detected(self, candle_time, close: float, sma_20: float, sma_50: float, sl: float) -> None:
        self._enqueue(
            f"🎯 *Bearish SMA Breakdown* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕯️ H1 Candle: {_code(candle_time.strftime('%d %b %H:%M IST') if hasattr(candle_time, 'strftime') else candle_time)}\n"
            f"📉 Close:    {_code(f'${close:.2f}')}\n"
            f"〰️ SMA20:    {_code(f'${sma_20:.2f}')}\n"
            f"〰️ SMA50:    {_code(f'${sma_50:.2f}')}\n"
            f"🛑 SL \\(High\\): {_code(f'${sl:.2f}')}"
        )

    def trade_entry(self, entry: float, sl: float, tp: float, risk: float, qty: int, oz: float) -> None:
        self._enqueue(
            f"🚀 *SHORT Executed* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Symbol:  `PAXGUSD`\n"
            f"💰 Entry:   {_code(f'${entry:.2f}')}\n"
            f"🛑 SL:      {_code(f'${sl:.2f} (-${risk:.2f})')}\n"
            f"🎯 Target:  {_code(f'${tp:.2f} (+${risk:.2f})')}\n"
            f"📦 Size:    {_code(f'{qty} contracts = {oz:g} oz')}\n"
            f"🕐 Time:    {_code(_now_ist_str())}"
        )

    def trade_exit(self, entry: float, exit_price: float, net_pnl: float, reason: str) -> None:
        r = reason.upper()
        if "TARGET" in r:
            header = "🎯 *Target Hit*"
        elif "STOP" in r or "SL" in r:
            header = "🛑 *Stop Loss Hit*"
        else:
            header = f"🏁 *Force Exit \\({_escape(reason)}\\)*"
        pnl_str = f"{'+' if net_pnl >= 0 else '-'}${abs(net_pnl):,.2f}"
        self._enqueue(
            f"{header} \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Symbol: `PAXGUSD` \\(SHORT\\)\n"
            f"💰 Entry:  {_code(f'${entry:.2f}')}\n"
            f"🏁 Exit:   {_code(f'${exit_price:.2f}')}\n"
            f"{'🟢' if net_pnl >= 0 else '🔴'} Net P&L: {_code(pnl_str)}\n"
            f"📋 Reason: {_code(reason)}\n"
            f"🕐 Time:   {_code(_now_ist_str())}"
        )

    def setup_skipped(self, reason: str) -> None:
        self._enqueue(
            f"⚠️ *Setup Skipped* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Reason: {_code(reason[:300])}\n"
            f"🕐 Time:   {_code(_now_ist_str())}"
        )

    def bot_restarted(self, action: str, detail: str = "") -> None:
        self._enqueue(
            f"🔄 *Crash Recovery* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ Action: {_code(action)}\n"
            f"📋 Detail: {_code(detail[:300] or '-')}\n"
            f"🕐 Time:   {_code(_now_ist_str())}"
        )

    def critical(self, title: str, detail: str) -> None:
        """Urgent operator alert (e.g. unknown order state, manual action needed)."""
        self._enqueue(
            f"🚨 *{_escape(title)}* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 {_code(detail[:500])}\n"
            f"🕐 Time: {_code(_now_ist_str())}"
        )

    def bot_crashed(self, error: str) -> None:
        """Sent synchronously — the process is going down."""
        self._send_sync(
            f"❌ *Bot Crashed\\!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💥 Error: {_code(str(error)[:300])}\n"
            f"🕐 Time:  {_code(_now_ist_str())}\n"
            f"⚠️ Emergency square\\-off attempted\\!"
        )

    def daily_summary(self, date_str: str, trades: int, pnl: float, total_pnl: float, cash: float) -> None:
        self._enqueue(
            f"📊 *Daily Summary* \\| {_badge()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Date:      {_code(date_str)}\n"
            f"📈 Trades:    {_code(trades)}\n"
            f"{'🟢' if pnl >= 0 else '🔴'} Day P&L:   {_code(f'{pnl:+,.2f} USD')}\n"
            f"💼 Total P&L: {_code(f'{total_pnl:+,.2f} USD')}\n"
            f"💰 Equity:    {_code(f'{cash:,.2f} USD')}"
        )


tg = TelegramAlerter(
    token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
)
