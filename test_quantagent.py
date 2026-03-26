"""
Standalone QuantAgent verification script.
Run this to confirm the 4-agent LLM pipeline works before connecting to Hyperliquid.

Usage:
    python test_quantagent.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "quantagent"))

from dotenv import load_dotenv
load_dotenv()

from trading_graph import TradingGraph
from config.quantagent_config import QUANTAGENT_CONFIG
import yfinance as yf

print("Fetching BTC data via yfinance...")
btc = yf.download("BTC-USD", period="30d", interval="1h", progress=False)
# Flatten multi-level columns (yfinance >= 0.2.x returns MultiIndex)
if isinstance(btc.columns, __import__("pandas").MultiIndex):
    btc.columns = btc.columns.get_level_values(0)
btc = btc.reset_index()

# Take last 50 candles (reset_index puts the datetime into a "Datetime" column)
btc = btc.tail(50)

# QuantAgent expects a column-oriented dict with capitalized keys + Datetime
kline_data = {
    "Datetime": btc["Datetime"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
    "Open":     btc["Open"].tolist(),
    "High":     btc["High"].tolist(),
    "Low":      btc["Low"].tolist(),
    "Close":    btc["Close"].tolist(),
    "Volume":   btc["Volume"].tolist(),
}
print(f"Using {len(kline_data['Close'])} candles. Last close: ${kline_data['Close'][-1]:.2f}")

print("\nInitializing QuantAgent with Claude (Haiku agents + Sonnet decision)...")
trading_graph = TradingGraph(config=QUANTAGENT_CONFIG)

initial_state = {
    "kline_data": kline_data,
    "analysis_results": None,
    "messages": [],
    "time_frame": QUANTAGENT_CONFIG["time_frame"],
    "stock_name": QUANTAGENT_CONFIG["stock_name"],
}

print("Running 4-agent analysis (this takes ~30-60s)...\n")
final_state = trading_graph.graph.invoke(initial_state)

print("=" * 60)
print("FINAL DECISION:")
print(final_state.get("final_trade_decision", "No decision"))
print("=" * 60)
print("\nINDICATOR REPORT:")
print(final_state.get("indicator_report", "N/A"))
print("\nPATTERN REPORT:")
print(final_state.get("pattern_report", "N/A"))
print("\nTREND REPORT:")
print(final_state.get("trend_report", "N/A"))
