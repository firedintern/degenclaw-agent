"""Risk management — position sizing and drawdown circuit breaker."""
import logging
from config.settings import RISK_PER_TRADE, MAX_LEVERAGE, MAX_DRAWDOWN

logger = logging.getLogger(__name__)


def calculate_position_size(
    equity: float,
    entry_price: float,
    stop_loss: float,
) -> dict:
    """
    Fixed fractional risk sizing.
    Risks RISK_PER_TRADE (default 2%) of equity per trade.
    Position size is determined by the distance to the stop-loss.

    Returns:
        dict with keys: contracts, leverage, risk_amount, notional
    """
    risk_amount   = equity * RISK_PER_TRADE
    stop_distance = abs(entry_price - stop_loss)

    if stop_distance == 0:
        return {"contracts": 0, "leverage": 0, "risk_amount": 0, "notional": 0}

    contracts = risk_amount / stop_distance
    notional  = contracts * entry_price
    leverage  = notional / equity

    if leverage > MAX_LEVERAGE:
        leverage  = MAX_LEVERAGE
        notional  = equity * leverage
        contracts = notional / entry_price

    return {
        "contracts":   round(contracts, 6),
        "leverage":    round(leverage, 2),
        "risk_amount": round(risk_amount, 2),
        "notional":    round(notional, 2),
    }


def check_drawdown(equity: float, peak: float) -> bool:
    """
    Returns True if safe to trade, False if circuit breaker triggered.
    Halts trading when drawdown from peak exceeds MAX_DRAWDOWN (10%).
    """
    if peak == 0:
        return True
    dd = (peak - equity) / peak
    if dd >= MAX_DRAWDOWN:
        logger.warning(
            f"CIRCUIT BREAKER: {dd:.1%} drawdown exceeds {MAX_DRAWDOWN:.0%} limit — trading halted"
        )
        return False
    return True
