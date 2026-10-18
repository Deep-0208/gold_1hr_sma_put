"""
═════════════════════════════════════════════════════════════
  RISK — SL / Target, Sanity Guards, P&L and Fees (SHORT only)
═════════════════════════════════════════════════════════════

Rules (identical to NIFTY 1H SMA PUT v2, measured on the gold price):
    sl_price     = signal_candle.high
    risk         = sl_price - entry_price
    target_price = entry_price - risk * rr_ratio      (1:1 by default)

Skip the trade (never invert risk) when:
    - entry_price <= 0
    - entry_price >= sl_price          (price already gapped through the SL)
    - target_price <= 0
    - risk > max_risk_dollars          (only if the cap is enabled, > 0)
"""

from dataclasses import dataclass

from config import CONFIG, log_risk


@dataclass
class RiskParams:
    """SL / target for a SHORT trade, in gold price terms ($ per 1 PAXG)."""
    entry_price: float
    sl_price: float
    target_price: float
    risk: float
    direction: str = "SHORT"
    is_valid: bool = True
    skip_reason: str = ""


def calculate_risk(entry_price: float, signal_candle_high: float) -> RiskParams:
    """SL at the signal candle high, target at 1:rr below entry."""
    rr = float(CONFIG.get("rr_ratio", 1.0))
    sl_price = round(float(signal_candle_high), 2)
    risk = round(sl_price - entry_price, 2)
    target_price = round(entry_price - risk * rr, 2)

    def _skip(reason: str) -> RiskParams:
        log_risk.warning("[RISK] ⚠️ Validation FAILED: %s", reason)
        return RiskParams(entry_price, sl_price, target_price, risk, is_valid=False, skip_reason=reason)

    if entry_price <= 0:
        return _skip(f"Invalid entry_price={entry_price}")

    if risk <= 0:
        return _skip(
            f"ENTRY_ABOVE_SL: entry=${entry_price:.2f} >= SL=${sl_price:.2f} "
            f"(risk=${risk:.2f}). Bearish thesis already invalidated."
        )

    if target_price <= 0:
        return _skip(f"Negative target=${target_price:.2f} (entry=${entry_price:.2f}, risk=${risk:.2f})")

    max_risk = float(CONFIG.get("max_risk_dollars", 0) or 0)
    if max_risk > 0 and risk > max_risk:
        return _skip(f"RISK_EXCEEDS_CAP: risk=${risk:.2f} > max_risk_dollars=${max_risk:.2f}")

    log_risk.info(
        "[RISK] 🛡️ SHORT | entry=$%.2f | SL=$%.2f | target=$%.2f | risk=$%.2f (%.2f%%) | R:R 1:%.1f",
        entry_price, sl_price, target_price, risk, risk / entry_price * 100, rr,
    )
    return RiskParams(entry_price, sl_price, target_price, risk)


def position_oz(qty: int, contract_value: float) -> float:
    """Gold exposure in PAXG/oz for `qty` contracts."""
    return round(int(qty) * float(contract_value), 6)


def calculate_gross_pnl(entry_price: float, exit_price: float, qty: int, contract_value: float) -> float:
    """SHORT P&L in USD: (entry - exit) × contracts × contract_value."""
    return round((float(entry_price) - float(exit_price)) * position_oz(qty, contract_value), 2)


@dataclass
class TradeFees:
    entry_fee: float
    exit_fee: float
    gst: float
    total: float

    def breakdown(self) -> str:
        return f"entry=${self.entry_fee:.2f} exit=${self.exit_fee:.2f} gst=${self.gst:.2f} total=${self.total:.2f}"


def calculate_fees(entry_price: float, exit_price: float, qty: int, contract_value: float) -> TradeFees:
    """
    Round-trip taker fees on notional (both legs are market orders) plus GST
    on fees. Never raises.
    """
    try:
        oz = position_oz(qty, contract_value)
        fee_pct = float(CONFIG.get("taker_fee_pct", 0.0005))
        gst_pct = float(CONFIG.get("gst_pct", 0.18))
        entry_fee = round(max(0.0, float(entry_price)) * oz * fee_pct, 4)
        exit_fee = round(max(0.0, float(exit_price)) * oz * fee_pct, 4)
        gst = round((entry_fee + exit_fee) * gst_pct, 4)
        return TradeFees(round(entry_fee, 2), round(exit_fee, 2), round(gst, 2), round(entry_fee + exit_fee + gst, 2))
    except (TypeError, ValueError):
        return TradeFees(0.0, 0.0, 0.0, 0.0)


def estimate_short_liquidation_price(entry_price: float, leverage: float, maintenance_margin_pct: float):
    """
    Isolated-margin liquidation estimate for a SHORT (Delta user guide: liquidation
    when unrealized loss = position margin - maintenance margin):
        liq = entry × (1 + 1/leverage - MM%/100)
    Ignores fees, funding and auto top-up (which would move it further away).
    Returns None if inputs are missing.
    """
    try:
        if not leverage or leverage <= 0 or maintenance_margin_pct is None:
            return None
        return round(float(entry_price) * (1 + 1 / float(leverage) - float(maintenance_margin_pct) / 100), 2)
    except (TypeError, ValueError):
        return None


def calculate_r_multiple(entry_price: float, current_price: float, risk: float) -> float:
    """SHORT R-multiple: (entry - current) / risk."""
    if not risk or risk <= 0:
        return 0.0
    return round((float(entry_price) - float(current_price)) / float(risk), 2)
