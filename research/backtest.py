"""
═════════════════════════════════════════════════════════════
  BACKTEST — Gold 1H SMA Breakdown (SHORT) on Delta PAXGUSD
═════════════════════════════════════════════════════════════

Uses the live bot's own pattern.py (attach_sma, detect_signal) and
risk.py (calculate_risk, P&L, fees) so the rules cannot drift.

Execution model (mirrors main.py / orders.py):
  - Signal on a COMPLETED H1 bar (Delta's broker-computed 1h candles).
  - Entry at the OPEN of the next H1 bar (no lookahead, AGENTS §15 #16),
    selling at the bid (open - half spread).
  - SL = signal candle high, target = 1:1 (risk.calculate_risk).
  - Exits walked on 1-minute bars: LTP >= SL → STOP_LOSS, LTP <= target →
    TARGET_HIT, filled at the ask (+ half spread). A minute that opens beyond
    a level fills at that open (gap). SL and target in the same minute → SL
    (conservative).
  - One position at a time; signals while in a trade are ignored; max
    trades/day by IST date; entry above SL (gap) is skipped.
  - Funding payments are NOT modelled (shown as a caveat).

Usage:
  python research/backtest.py            # uses cached data in research/data/
  python research/backtest.py --refresh  # re-download from Delta
"""

import argparse
import bisect
import csv
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import CONFIG, IST  # noqa: E402

logging.getLogger("Gold1HrSMA").setLevel(logging.WARNING)  # keep the live bot's logs quiet

from pattern import attach_sma, detect_signal  # noqa: E402
from risk import calculate_risk, calculate_gross_pnl, calculate_fees, calculate_r_multiple  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
SYMBOL = CONFIG["delta_symbol"]
URL = CONFIG["delta_base_url"] + "/v2/history/candles"
MAX_BARS_PER_REQUEST = 4000          # verified 2026-10-17
STEP = {"1h": 3600, "1m": 60, "1d": 86400}


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────

def _get(resolution: str, start: int, end: int) -> list:
    for attempt in range(5):
        try:
            r = requests.get(URL, params={"symbol": SYMBOL, "resolution": resolution,
                                          "start": str(start), "end": str(end)}, timeout=30)
            j = r.json()
            if r.status_code == 200 and j.get("success", True):
                return j.get("result") or []
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Delta candles request failed: {resolution} {start}-{end}")


def download(resolution: str) -> list:
    """Every completed bar from listing day to now, oldest first."""
    now = int(time.time())
    daily = _get("1d", now - 3 * 365 * 86400, now)
    listed = min(b["time"] for b in daily)
    step = STEP[resolution]
    bars, start = {}, listed - step
    while start < now:
        end = min(start + MAX_BARS_PER_REQUEST * step, now)
        for b in _get(resolution, start, end):
            bars[int(b["time"])] = b
        start = end
        time.sleep(0.25)
    out = [{"t": t, "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
            "close": float(b["close"]), "volume": float(b.get("volume") or 0)} for t, b in sorted(bars.items())]
    return [b for b in out if b["t"] + step <= now]          # completed bars only


def load(resolution: str, refresh: bool) -> list:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{SYMBOL}_{resolution}.csv"
    if refresh or not path.exists():
        print(f"Downloading {resolution} candles from Delta...", flush=True)
        bars = download(resolution)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["t", "open", "high", "low", "close", "volume"])
            w.writeheader()
            w.writerows(bars)
    with path.open(encoding="utf-8") as f:
        return [{"t": int(r["t"]), **{k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}}
                for r in csv.DictReader(f)]


def to_candles(h1: list) -> list:
    """Shape H1 rows like broker.get_candles() output."""
    out = []
    for b in h1:
        utc = datetime.fromtimestamp(b["t"], tz=timezone.utc)
        out.append({"date": utc.astimezone(IST), "date_utc": utc, "t": b["t"],
                    "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]})
    return out


def validate(h1: list, m1: list) -> dict:
    """Data-quality checks: gaps and 1h-vs-1m consistency."""
    h_gaps = sum(1 for a, b in zip(h1, h1[1:]) if b["t"] - a["t"] != 3600)
    m_missing = sum((b["t"] - a["t"]) // 60 - 1 for a, b in zip(m1, m1[1:]) if b["t"] - a["t"] > 60)
    by_hour = defaultdict(list)
    for b in m1:
        by_hour[b["t"] // 3600 * 3600].append(b)
    mismatched = 0
    for b in h1:
        mins = by_hour.get(b["t"])
        if mins and (abs(max(x["high"] for x in mins) - b["high"]) > 0.05 or abs(min(x["low"] for x in mins) - b["low"]) > 0.05):
            mismatched += 1
    return {"h1_bars": len(h1), "m1_bars": len(m1), "h1_gaps": h_gaps, "m1_missing_minutes": m_missing,
            "h1_vs_m1_hilo_mismatch": mismatched}


# ─────────────────────────────────────────────
# SIMULATION
# ─────────────────────────────────────────────

def walk_exit(m1, m1_t, entry_ts, sl, tp, half_spread):
    """Walk 1-minute bars from entry; return (exit_price, exit_ts, reason, ambiguous)."""
    j = bisect.bisect_left(m1_t, entry_ts)
    for b in m1[j:]:
        if b["open"] >= sl:
            return b["open"] + half_spread, b["t"], "STOP_LOSS", False
        if b["open"] <= tp:
            return b["open"] + half_spread, b["t"], "TARGET_HIT", False
        hit_sl, hit_tp = b["high"] >= sl, b["low"] <= tp
        if hit_sl:
            return sl + half_spread, b["t"], "STOP_LOSS", hit_tp
        if hit_tp:
            return tp + half_spread, b["t"], "TARGET_HIT", False
    last = m1[-1]
    return last["close"] + half_spread, last["t"], "OPEN_AT_END", False


def simulate(candles, m1, fee_pct, half_spread, qty, contract_value, max_per_day):
    CONFIG["taker_fee_pct"] = fee_pct
    m1_t = [b["t"] for b in m1]
    trades, skips, per_day = [], Counter(), Counter()
    flat_since = 0  # epoch of the last exit; bars closing at/before it were not evaluated by the bot

    for i in range(len(candles) - 1):
        bar = candles[i]
        close_ts = bar["t"] + 3600
        if close_ts <= flat_since:
            continue
        sig = detect_signal([bar])
        if sig is None:
            continue
        nxt = candles[i + 1]
        if nxt["t"] != close_ts:
            skips["DATA_GAP"] += 1
            continue
        day = datetime.fromtimestamp(close_ts, tz=timezone.utc).astimezone(IST).date()
        if per_day[day] >= max_per_day:
            skips["MAX_TRADES_PER_DAY"] += 1
            continue

        entry = round(nxt["open"] - half_spread, 2)
        rp = calculate_risk(entry, sig.sl_price)
        if not rp.is_valid:
            skips[rp.skip_reason.split(":")[0]] += 1
            continue

        per_day[day] += 1
        exit_price, exit_ts, reason, ambiguous = walk_exit(m1, m1_t, close_ts, rp.sl_price, rp.target_price, half_spread)
        exit_price = round(exit_price, 2)
        gross = calculate_gross_pnl(entry, exit_price, qty, contract_value)
        fees = calculate_fees(entry, exit_price, qty, contract_value).total
        trades.append({
            "signal_bar_ist": bar["date"].strftime("%Y-%m-%d %H:%M"),
            "entry_ist": datetime.fromtimestamp(close_ts, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "exit_ist": datetime.fromtimestamp(exit_ts, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "hold_hours": round((exit_ts - close_ts) / 3600, 2),
            "entry": entry, "sl": rp.sl_price, "target": rp.target_price, "risk": rp.risk, "exit": exit_price,
            "reason": reason, "same_minute_sl_and_target": ambiguous,
            "r_multiple": calculate_r_multiple(entry, exit_price, rp.risk),
            "gross": gross, "fees": fees, "net": round(gross - fees, 2),
        })
        flat_since = exit_ts
    return trades, skips


# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def stats(trades):
    closed = [t for t in trades if t["reason"] != "OPEN_AT_END"]
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] <= 0]
    eq, peak, max_dd, streak, max_streak = 0.0, 0.0, 0.0, 0, 0
    for t in trades:
        eq += t["net"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        streak = streak + 1 if t["net"] <= 0 else 0
        max_streak = max(max_streak, streak)
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    return {
        "trades": len(trades),
        "targets": sum(1 for t in closed if t["reason"] == "TARGET_HIT"),
        "stops": sum(1 for t in closed if t["reason"] == "STOP_LOSS"),
        "net_winners": len(wins),
        "win_rate": len(wins) / len(closed) * 100 if closed else 0.0,
        "target_rate": sum(1 for t in closed if t["reason"] == "TARGET_HIT") / len(closed) * 100 if closed else 0.0,
        "net": round(sum(t["net"] for t in trades), 2),
        "gross": round(sum(t["gross"] for t in trades), 2),
        "fees": round(sum(t["fees"] for t in trades), 2),
        "total_r": round(sum(t["r_multiple"] for t in trades), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_drawdown": round(max_dd, 2),
        "max_losing_streak": max_streak,
        "avg_hold_h": round(sum(t["hold_hours"] for t in trades) / len(trades), 1) if trades else 0.0,
        "max_hold_h": max((t["hold_hours"] for t in trades), default=0),
        "ambiguous": sum(1 for t in trades if t["same_minute_sl_and_target"]),
    }


def by_bucket(trades, key_fn, order):
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    return [(k, stats(groups[k])) for k in order if k in groups]


def risk_bucket(t):
    r = t["risk"]
    return "< $1" if r < 1 else "$1-3" if r < 3 else "$3-5" if r < 5 else "$5-10" if r < 10 else "$10-20" if r < 20 else ">= $20"


def report(quality, runs, months, buckets, first, last, qty, cv):
    oz = qty * cv
    L = []
    L.append("# Backtest — Gold (PAXGUSD) 1H SMA Breakdown, SHORT\n")
    L.append(f"Data: Delta Exchange India, {first} → {last} IST (all history since PAXGUSD listing). "
             f"Size: {qty} contracts = {oz:g} oz → $ figures are per {oz:g} oz (×10 for 1 oz).\n")
    L.append("## Data quality\n")
    for k, v in quality.items():
        L.append(f"- {k}: {v}")
    L.append("")
    L.append("## Results\n")
    L.append("| Scenario | Trades | Target hit | Win rate (net) | Total R | Gross $ | Fees $ | **Net $** | Profit factor | Max DD $ | Worst losing streak |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, s in runs:
        L.append(f"| {name} | {s['trades']} | {s['target_rate']:.1f}% | {s['win_rate']:.1f}% | {s['total_r']:+.2f} | "
                 f"{s['gross']:+.2f} | {s['fees']:.2f} | **{s['net']:+.2f}** | {s['profit_factor']} | {s['max_drawdown']:.2f} | {s['max_losing_streak']} |")
    L.append("")
    base = runs[0][1]
    L.append(f"Avg net win ${base['avg_win']:.2f} | avg net loss ${base['avg_loss']:.2f} | avg hold {base['avg_hold_h']} h "
             f"(max {base['max_hold_h']} h) | SL & target inside the same minute (counted as SL): {base['ambiguous']}\n")
    L.append("## By month (base scenario)\n")
    L.append("| Month | Trades | Win rate | Total R | Net $ |")
    L.append("|---|---|---|---|---|")
    for k, s in months:
        L.append(f"| {k} | {s['trades']} | {s['win_rate']:.0f}% | {s['total_r']:+.2f} | {s['net']:+.2f} |")
    L.append("")
    L.append("## By SL distance (base scenario)\n")
    L.append("| Risk (SL − entry) | Trades | Win rate (net) | Total R | Net $ | Fees $ |")
    L.append("|---|---|---|---|---|---|")
    for k, s in buckets:
        L.append(f"| {k} | {s['trades']} | {s['win_rate']:.0f}% | {s['total_r']:+.2f} | {s['net']:+.2f} | {s['fees']:.2f} |")
    L.append("")
    L.append("## Not modelled\n")
    L.append("- Funding payments on overnight holds (perpetual futures).")
    L.append("- Stop-order slippage beyond half the spread, except when a minute opens beyond the level (then fills at that open).")
    L.append("- Historical spreads (constant half-spread used); entry spread guard ($1.20) not applied.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download candles from Delta")
    args = ap.parse_args()

    h1 = load("1h", args.refresh)
    m1 = load("1m", args.refresh)
    quality = validate(h1, m1)
    candles = attach_sma(to_candles(h1))

    qty = int(CONFIG["contract_qty"])
    cv = float(CONFIG["expected_contract_value"])
    per_day = int(CONFIG["max_trades_per_day"])
    live_fee = 0.0001   # PAXGUSD taker_commission_rate from /v2/products (2026-10-17)

    scenarios = [
        ("Base: live fee 0.01%, spread $0.10", live_fee, 0.05),
        ("Fee 0.05% (Delta standard futures taker)", 0.0005, 0.05),
        ("Fee 0.05% + spread $0.30", 0.0005, 0.15),
    ]
    runs, base_trades, base_skips = [], None, None
    for name, fee, hs in scenarios:
        trades, skips = simulate(candles, m1, fee, hs, qty, cv, per_day)
        runs.append((name, stats(trades)))
        if base_trades is None:
            base_trades, base_skips = trades, skips

    months = by_bucket(base_trades, lambda t: t["entry_ist"][:7], sorted({t["entry_ist"][:7] for t in base_trades}))
    buckets = by_bucket(base_trades, risk_bucket, ["< $1", "$1-3", "$3-5", "$5-10", "$10-20", ">= $20"])

    first = to_candles(h1[:1])[0]["date"].strftime("%Y-%m-%d %H:%M")
    last = to_candles(h1[-1:])[0]["date"].strftime("%Y-%m-%d %H:%M")
    quality["signals_skipped"] = dict(base_skips)
    text = report(quality, runs, months, buckets, first, last, qty, cv)

    (HERE / "backtest_report.md").write_text(text, encoding="utf-8")
    with (HERE / "backtest_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(base_trades[0].keys()))
        w.writeheader()
        w.writerows(base_trades)
    print(text)
    print(f"\nSaved: {HERE / 'backtest_report.md'} and {HERE / 'backtest_trades.csv'}")


if __name__ == "__main__":
    main()
