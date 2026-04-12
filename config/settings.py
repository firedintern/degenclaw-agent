"""All tuneable parameters in one place."""
import os
from dotenv import load_dotenv

load_dotenv()

# Trading
TRADING_PAIR = os.getenv("TRADING_PAIR", "BTC")
TIMEFRAME = os.getenv("TIMEFRAME", "4h")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min default

# Risk management
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.10"))       # 10% of equity
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))   # BTC only = 1
MAX_DRAWDOWN = 0.30                                                 # 30% circuit breaker

# QuantAgent risk-reward mapping
# Paper uses fixed stop-loss ρ = 0.0005 (0.05%) with r ∈ [1.2, 1.8]
# For Hyperliquid perps we scale this to ATR-based stops
DEFAULT_STOP_ATR_MULTIPLIER = 0.5    # Stop at 0.5x ATR (tighter stops = bigger position size)
MIN_RISK_REWARD = 1.2                # From QuantAgent's range
MAX_RISK_REWARD = 1.8                # From QuantAgent's range

# Execution
SLIPPAGE_BPS = 10
ORDER_TYPE = "market"

# QuantAgent
QUANTAGENT_LOOKBACK = 50             # Number of candles to feed QuantAgent
QUANTAGENT_TIMEFRAME = "4hour"       # Text label for QuantAgent prompts

# DegenClaw forum
DGCLAW_AGENT_ID = os.getenv("DGCLAW_AGENT_ID", "")
DGCLAW_THREAD_ID = os.getenv("DGCLAW_THREAD_ID", "")
