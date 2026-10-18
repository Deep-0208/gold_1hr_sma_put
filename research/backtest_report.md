# Backtest — Gold (PAXGUSD) 1H SMA Breakdown, SHORT

Data: Delta Exchange India, 2026-02-19 18:30 → 2026-10-17 19:30 IST (all history since PAXGUSD listing). Size: 100 contracts = 0.1 oz → $ figures are per 0.1 oz (×10 for 1 oz).

## Data quality

- h1_bars: 5762
- m1_bars: 345613
- h1_gaps: 0
- m1_missing_minutes: 59
- h1_vs_m1_hilo_mismatch: 28
- signals_skipped: {'ENTRY_ABOVE_SL': 1}

## Results

| Scenario | Trades | Target hit | Win rate (net) | Total R | Gross $ | Fees $ | **Net $** | Profit factor | Max DD $ | Worst losing streak |
|---|---|---|---|---|---|---|---|---|---|---|
| Base: live fee 0.01%, spread $0.10 | 154 | 52.3% | 51.6% | +4.87 | -0.39 | 16.29 | **-16.68** | 0.87 | 21.43 | 5 |
| Fee 0.05% (Delta standard futures taker) | 154 | 52.3% | 42.5% | +4.87 | -0.39 | 81.19 | **-81.58** | 0.51 | 83.61 | 9 |
| Fee 0.05% + spread $0.30 | 154 | 51.6% | 42.5% | +0.30 | -2.02 | 81.19 | **-83.21** | 0.5 | 85.22 | 9 |

Avg net win $1.43 | avg net loss $-1.75 | avg hold 3.7 h (max 58.83 h) | SL & target inside the same minute (counted as SL): 0

## By month (base scenario)

| Month | Trades | Win rate | Total R | Net $ |
|---|---|---|---|---|
| 2026-02 | 3 | 0% | -3.00 | -5.94 |
| 2026-03 | 22 | 55% | +1.94 | -10.11 |
| 2026-04 | 25 | 56% | +4.58 | +7.65 |
| 2026-05 | 19 | 53% | +0.89 | -1.13 |
| 2026-06 | 11 | 55% | +1.04 | +2.00 |
| 2026-07 | 14 | 57% | +1.80 | -5.47 |
| 2026-08 | 18 | 39% | -4.35 | -0.74 |
| 2026-09 | 30 | 60% | +5.64 | +2.39 |
| 2026-10 | 12 | 36% | -3.67 | -5.33 |

## By SL distance (base scenario)

| Risk (SL − entry) | Trades | Win rate (net) | Total R | Net $ | Fees $ |
|---|---|---|---|---|---|
| < $1 | 2 | 0% | -0.12 | -0.23 | 0.22 |
| $1-3 | 20 | 53% | -0.41 | -2.30 | 2.08 |
| $3-5 | 7 | 57% | +0.71 | -0.43 | 0.72 |
| $5-10 | 31 | 52% | +0.69 | -2.08 | 3.25 |
| $10-20 | 51 | 59% | +9.00 | +9.47 | 5.39 |
| >= $20 | 43 | 44% | -5.00 | -21.11 | 4.63 |

## Not modelled

- Funding payments on overnight holds (perpetual futures).
- Stop-order slippage beyond half the spread, except when a minute opens beyond the level (then fills at that open).
- Historical spreads (constant half-spread used); entry spread guard ($1.20) not applied.