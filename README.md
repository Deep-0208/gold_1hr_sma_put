# 🏛️ Gold (PAXGUSD) 1-Hour SMA Breakdown — SHORT

Port of the **NIFTY 1-Hour SMA PUT v2** strategy to gold on **Delta Exchange** (PAXGUSD perpetual futures).
Delta has no gold options, so the bearish "buy PUT" leg is a **SHORT on the perpetual** — same signal, same SL, same 1:1 target.

## 📌 Rules (identical to NIFTY 1H SMA PUT v2)

On each **completed H1 candle** (forming bar always dropped), SMA 20 / SMA 50 on closes:

| # | Condition |
|---|-----------|
| 1 | Candle is **red** — `close < open` |
| 2 | **Open above SMA 50** — `open > SMA50` (between the SMAs or above both; below SMA 50 = skip) |
| 3 | **SMA 20 above SMA 50** |
| 4 | **Close below SMA 20** |
| 5 | **Close below SMA 50** |

- **Entry:** SHORT `contract_qty` contracts at market right after the candle closes.
- **SL:** signal candle **high**. **Target:** `entry − (SL − entry) × rr_ratio` (1:1).
- **Exit triggers:** last traded price `>= SL` → STOP_LOSS, `<= target` → TARGET_HIT.
- **Skips:** entry already above SL (gap), spread > $1.20, signal > 5 min stale, max trades/day (5), daily loss cap (off), risk cap (off).

## 🔁 Differences vs NIFTY (market structure, not rules)

| NIFTY 1H SMA PUT v2 | Gold 1H SMA |
|---|---|
| Buy ATM monthly PUT (NRML) | SHORT PAXGUSD perpetual (positional) |
| Entry window 10:15–15:15 IST | 24/7 — every H1 close |
| Force exit 15:15 on expiry day | No expiry |
| Spot SL/target, P&L on option premium | SL/target and P&L on the same gold price |
| KiteTicker WebSocket + REST fallback | REST ticker polling every ~1s (+ broker bracket in LIVE) |

## 📦 Sizing

`1 contract = 0.001 PAXG` (verified live at startup — bot refuses to start if Delta changes it).
Default `contract_qty = 100` → **0.1 oz** → $0.10 P&L per $1 gold move (same size as gold 4hr).
At 25x leverage that needs ≈ $17 margin per trade; at Delta's 100x default ≈ $4.20.

## 🚀 Run

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill credentials (optional in PAPER)
python main.py           # interactive
python main.py --daemon  # systemd / non-interactive
```

PAPER mode works without API keys (public market data).

## 🧪 Tests

```powershell
python test_audit_comprehensive.py
```

Offline (mocked broker) — no network or credentials needed.

## 📂 Logs

`logs/YYYY-MM-DD/` → `strategy.log`, `debug.log`, `trades.log`, `network.log`, `candles.csv`, `events.jsonl`; cross-day `logs/journal.csv`.
