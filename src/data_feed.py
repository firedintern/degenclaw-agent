"""Fetch OHLCV data from Hyperliquid for QuantAgent consumption."""
import time
import pandas as pd
import ta
from hyperliquid.info import Info
from hyperliquid.utils import constants


_INTERVAL_MS = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}

_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}


class HyperliquidDataFeed:
    def __init__(self):
        # Retry with backoff — Hyperliquid returns 429 if hit too quickly after restart
        for attempt in range(5):
            try:
                self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
                return
            except Exception as e:
                if attempt == 4:
                    raise
                wait = 15 * (attempt + 1)
                import logging
                logging.getLogger(__name__).warning(
                    f"HL init failed ({e}), retrying in {wait}s (attempt {attempt+1}/5)"
                )
                time.sleep(wait)

    def get_candles(self, symbol: str, interval: str, count: int = 50) -> list[dict]:
        """
        Fetch OHLCV candles in QuantAgent's expected format.

        Args:
            symbol:   Coin name, e.g. "BTC"
            interval: Candle interval, e.g. "4h"
            count:    Number of candles to return

        Returns:
            list of dicts with keys: timestamp, open, high, low, close, volume
        """
        hl_interval  = _INTERVAL_MAP.get(interval, interval)
        interval_ms  = _INTERVAL_MS.get(interval, 14_400_000)

        end_time   = int(time.time() * 1000)
        start_time = end_time - interval_ms * (count + 5)  # +5 for partial candle buffer

        raw = self.info.candles_snapshot(symbol, hl_interval, start_time, end_time)

        candles = [{
            "timestamp": int(bar["t"]),
            "open":      float(bar["o"]),
            "high":      float(bar["h"]),
            "low":       float(bar["l"]),
            "close":     float(bar["c"]),
            "volume":    float(bar["v"]),
        } for bar in raw]

        return candles[-count:]

    def get_candles_df(self, symbol: str, interval: str, count: int = 50) -> pd.DataFrame:
        """Get candles as a DataFrame with ATR column."""
        candles = self.get_candles(symbol, interval, count)
        df = pd.DataFrame(candles)
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=14
        )
        return df

    def get_mid_price(self, symbol: str) -> float:
        """Get current mid price from the L2 orderbook."""
        book = self.info.l2_snapshot(symbol)
        bid  = float(book["levels"][0][0]["px"])
        ask  = float(book["levels"][1][0]["px"])
        return (bid + ask) / 2
