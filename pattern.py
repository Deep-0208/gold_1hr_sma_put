"""
═════════════════════════════════════════════════════════════
  PATTERN — 1-Hour Bearish SMA Breakdown (Gold H1)
═════════════════════════════════════════════════════════════

Pure functions only: candles in → signal or None out.
Rules are IDENTICAL to NIFTY 1-Hour SMA PUT v2 (pattern.py there).
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from config import CONFIG, log_pattern


@dataclass
class SetupSignal:
    """Confirmed bearish SMA breakdown on one completed H1 candle."""
    signal_type: str          # Always "BEARISH_SMA_BREAKDOWN"
    signal_candle: Dict       # The full H1 candle that triggered
    sma_20: float
    sma_50: float
    sl_price: float           # Signal candle High


def attach_sma(candles: List[Dict]) -> List[Dict]:
    """
    Return copies of `candles` with sma_20 / sma_50 (simple mean of closes,
    completed bars only). Bars without enough history get None.
    """
    short_n = int(CONFIG["sma_short"])
    long_n = int(CONFIG["sma_long"])
    closes = [c["close"] for c in candles]
    out = []
    for i, candle in enumerate(candles):
        c = dict(candle)
        c["sma_20"] = sum(closes[i - short_n + 1:i + 1]) / short_n if i >= short_n - 1 else None
        c["sma_50"] = sum(closes[i - long_n + 1:i + 1]) / long_n if i >= long_n - 1 else None
        out.append(c)
    return out


def detect_signal(completed_candles: List[Dict]) -> Optional[SetupSignal]:
    """
    Evaluate the latest completed H1 candle for a bearish SMA breakdown.

    Signal rules (v2 specification):
        1. Candle is RED: close < open
        2. Candle OPEN is above SMA 20: open > sma_20
           (open may sit between the SMAs OR above both; open below SMA 20 = skip)
        3. SMA 20 is below SMA 50: sma_20 < sma_50 (bearish moving average alignment)
        4. Candle CLOSE is below SMA 20: close < sma_20
        5. Candle CLOSE is below SMA 50: close < sma_50

    All conditions on the SAME completed candle.

    Returns SetupSignal if valid, None otherwise.
    """
    if not completed_candles:
        log_pattern.debug("No completed candles to evaluate.")
        return None

    candle = completed_candles[-1]
    candle_time = candle["date"]

    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]
    sma_20 = candle.get("sma_20")
    sma_50 = candle.get("sma_50")

    log_pattern.debug(
        "🔍 Evaluating H1 candle %s: O=%.2f H=%.2f L=%.2f C=%.2f | SMA20=%s SMA50=%s",
        candle_time, o, h, l, c,
        f"{sma_20:.2f}" if sma_20 is not None else "None",
        f"{sma_50:.2f}" if sma_50 is not None else "None",
    )

    # Guard: reject if SMA values are missing (warmup period)
    if sma_20 is None or sma_50 is None:
        log_pattern.debug("❌ REJECTED: SMA warmup incomplete (sma_20=%s, sma_50=%s)", sma_20, sma_50)
        return None

    # Guard: reject NaN
    if math.isnan(sma_20) or math.isnan(sma_50):
        log_pattern.debug("❌ REJECTED: SMA contains NaN.")
        return None

    # Condition 1: Red candle
    is_red = c < o
    log_pattern.debug("  [1/5] Red candle check: close=%.2f < open=%.2f -> %s", c, o, is_red)

    # Condition 2: Open above SMA 20 (between the SMAs OR above both)
    open_above_sma20 = o > sma_20
    log_pattern.debug("  [2/5] Open above SMA20 check: open=%.2f > sma_20=%.2f -> %s", o, sma_20, open_above_sma20)

    # Condition 3: SMA 20 below SMA 50 (bearish moving average alignment)
    sma20_below_sma50 = sma_20 < sma_50
    log_pattern.debug("  [3/5] SMA20 below SMA50 check: sma_20=%.2f < sma_50=%.2f -> %s", sma_20, sma_50, sma20_below_sma50)

    # Condition 4: Close below SMA 20
    below_sma20 = c < sma_20
    log_pattern.debug("  [4/5] Below SMA20 check: close=%.2f < sma_20=%.2f -> %s", c, sma_20, below_sma20)

    # Condition 5: Close below SMA 50
    below_sma50 = c < sma_50
    log_pattern.debug("  [5/5] Below SMA50 check: close=%.2f < sma_50=%.2f -> %s", c, sma_50, below_sma50)

    if is_red and open_above_sma20 and sma20_below_sma50 and below_sma20 and below_sma50:
        signal = SetupSignal(
            signal_type="BEARISH_SMA_BREAKDOWN",
            signal_candle=candle,
            sma_20=sma_20,
            sma_50=sma_50,
            sl_price=h,  # Signal candle High = SL
        )
        log_pattern.info(
            "🎯 BEARISH BREAKDOWN SIGNAL DETECTED on H1 candle %s: "
            "Open=%.2f > SMA20=%.2f | SMA20 < SMA50 (%.2f < %.2f) | Close=%.2f < both SMAs | SL (High)=%.2f",
            candle_time, o, sma_20, sma_20, sma_50, c, h,
        )
        return signal

    log_pattern.debug(
        "No signal on H1 candle %s (red=%s, open_above_sma20=%s, sma20_below_sma50=%s, "
        "below_sma20=%s, below_sma50=%s)",
        candle_time, is_red, open_above_sma20, sma20_below_sma50, below_sma20, below_sma50,
    )
    return None


def format_signal_checklist(candle: Dict) -> str:
    """One-line human checklist of the 5 rules for strategy.log (every completed bar)."""
    s20, s50 = candle.get("sma_20"), candle.get("sma_50")
    if s20 is None or s50 is None:
        return "[PATTERN] ⏳ SMA warmup incomplete — no evaluation."
    o, c = candle["open"], candle["close"]

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    return (
        f"[PATTERN] {mark(c < o)} red | {mark(o > s20)} open>SMA20 | {mark(s20 < s50)} SMA20<SMA50 | "
        f"{mark(c < s20)} close<SMA20 | {mark(c < s50)} close<SMA50 "
        f"(O=${o:.2f} C=${c:.2f} SMA20=${s20:.2f} SMA50=${s50:.2f})"
    )
