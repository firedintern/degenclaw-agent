"""
Bridge between QuantAgent (signal generation) and the execution layer.

QuantAgent is a 4-agent LangGraph framework:
  - IndicatorAgent  → RSI, MACD, ROC, Stochastic, Williams %R
  - PatternAgent    → Chart formations (head & shoulders, flags, etc.)
  - TrendAgent      → Support/resistance, trendlines, channels
  - DecisionAgent   → Synthesises all three reports → LONG / SHORT + R:R

This bridge:
  1. Fetches latest OHLCV candles from Hyperliquid
  2. Calculates ATR for stop/TP sizing
  3. Runs the full QuantAgent pipeline
  4. Parses direction (LONG/SHORT) and risk-reward ratio
  5. Computes ATR-based stop-loss and take-profit levels
  6. Returns a fully-formed signal dict ready for ACP execution
"""
import sys
import re
import os
import logging
import pandas as pd
import ta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "quantagent"))

from trading_graph import TradingGraph
from config.quantagent_config import QUANTAGENT_CONFIG
from config.settings import (
    TRADING_PAIR, QUANTAGENT_LOOKBACK, QUANTAGENT_TIMEFRAME,
    TIMEFRAME, DEFAULT_STOP_ATR_MULTIPLIER,
)
from src.data_feed import HyperliquidDataFeed

logger = logging.getLogger(__name__)


class QuantAgentBridge:
    def __init__(self):
        logger.info("Initializing QuantAgent (loading LLMs)...")
        self.trading_graph = TradingGraph(config=QUANTAGENT_CONFIG)
        self.data_feed     = HyperliquidDataFeed()
        logger.info("QuantAgentBridge ready")

    def get_signal(self) -> dict | None:
        """
        Run the full QuantAgent pipeline on the latest BTC data.

        Returns:
            dict with keys: direction, risk_reward, entry_price, stop_loss,
                            take_profit, atr, rationale, raw_decision,
                            indicator_report, pattern_report, trend_report
            None if no clear LONG/SHORT signal is produced.
        """
        try:
            # 1. Fetch candles
            candles    = self.data_feed.get_candles(TRADING_PAIR, TIMEFRAME, QUANTAGENT_LOOKBACK)
            candles_df = pd.DataFrame(candles)

            # 2. ATR for stop sizing
            atr_series    = ta.volatility.average_true_range(
                candles_df["high"], candles_df["low"], candles_df["close"], window=14
            )
            current_atr   = float(atr_series.iloc[-1])
            current_price = candles[-1]["close"]

            # 3. Build QuantAgent's column-oriented input format
            kline_data = {
                "Datetime": pd.to_datetime(candles_df["timestamp"], unit="ms")
                              .dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                "Open":   candles_df["open"].tolist(),
                "High":   candles_df["high"].tolist(),
                "Low":    candles_df["low"].tolist(),
                "Close":  candles_df["close"].tolist(),
                "Volume": candles_df["volume"].tolist(),
            }

            # 4. Run QuantAgent
            initial_state = {
                "kline_data":       kline_data,
                "analysis_results": None,
                "messages":         [],
                "time_frame":       QUANTAGENT_TIMEFRAME,
                "stock_name":       TRADING_PAIR,
            }
            logger.info(f"Running QuantAgent on {TRADING_PAIR} ({TIMEFRAME}), price=${current_price:.2f}...")
            final_state = self.trading_graph.graph.invoke(initial_state)

            # 5. Parse decision
            decision_text = final_state.get("final_trade_decision", "")
            direction     = self._parse_direction(decision_text)
            risk_reward   = self._parse_risk_reward(decision_text)

            if direction is None:
                logger.warning("QuantAgent returned no clear LONG/SHORT direction — skipping")
                return None

            # 6. ATR-based stop-loss and take-profit
            stop_distance = DEFAULT_STOP_ATR_MULTIPLIER * current_atr
            tp_distance   = stop_distance * risk_reward

            if direction == "LONG":
                stop_loss   = current_price - stop_distance
                take_profit = current_price + tp_distance
            else:
                stop_loss   = current_price + stop_distance
                take_profit = current_price - tp_distance

            return {
                "direction":        direction,
                "risk_reward":      risk_reward,
                "entry_price":      round(current_price, 1),
                "stop_loss":        int(round(stop_loss, 0)),
                "take_profit":      int(round(take_profit, 0)),
                "atr":              round(current_atr, 2),
                "rationale":        self._build_rationale(direction, risk_reward, final_state),
                "raw_decision":     decision_text,
                "indicator_report": final_state.get("indicator_report", ""),
                "pattern_report":   final_state.get("pattern_report", ""),
                "trend_report":     final_state.get("trend_report", ""),
            }

        except Exception as e:
            logger.error(f"Signal generation failed: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_direction(self, decision_text: str) -> str | None:
        """Extract LONG or SHORT from QuantAgent's decision output."""
        if not decision_text:
            return None

        text_upper = decision_text.upper()

        # Check JSON "decision" field first (most reliable)
        for line in decision_text.split("\n"):
            if '"decision"' in line.lower():
                if "LONG"  in line.upper(): return "LONG"
                if "SHORT" in line.upper(): return "SHORT"

        has_long  = "LONG"  in text_upper
        has_short = "SHORT" in text_upper

        if has_long  and not has_short: return "LONG"
        if has_short and not has_long:  return "SHORT"

        # Both present — take whichever appears last (the conclusion)
        long_pos  = text_upper.rfind("LONG")
        short_pos = text_upper.rfind("SHORT")
        if long_pos  > short_pos: return "LONG"
        if short_pos > long_pos:  return "SHORT"
        return None

    def _parse_risk_reward(self, decision_text: str) -> float:
        """Extract risk-reward ratio from QuantAgent output. Default 1.5."""
        patterns = [
            r'"risk.?reward.?ratio"\s*:\s*"?([\d.]+)',
            r'risk.?reward.?ratio[:\s]+([\d.]+)',
            r'r[:/]r\s*(?:ratio)?\s*[:\s]+([\d.]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, decision_text, re.IGNORECASE)
            if match:
                rr = float(match.group(1))
                if 1.0 <= rr <= 3.0:
                    return rr
        return 1.5  # Midpoint of QuantAgent's [1.2, 1.8] range

    def _build_rationale(self, direction: str, rr: float, state: dict) -> str:
        """Format a multi-agent report for DegenClaw forum posts."""
        decision  = state.get("final_trade_decision", "N/A")
        indicator = state.get("indicator_report", "")
        pattern   = state.get("pattern_report", "")
        trend     = state.get("trend_report", "")

        return f"""## {direction} BTC — QuantAgent Multi-Agent Analysis

**Risk-Reward Ratio:** {rr}

### Decision Summary
{decision[:500] if decision else 'N/A'}

### Indicator Agent (RSI, MACD, ROC, Stochastic, Williams %R)
{indicator[:400] if indicator else 'N/A'}

### Pattern Agent (Chart formations)
{pattern[:400] if pattern else 'N/A'}

### Trend Agent (Support/Resistance, channels)
{trend[:400] if trend else 'N/A'}

---
*Powered by QuantAgent multi-agent LLM framework*
"""
