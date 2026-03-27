"""
Trade tracker — appends every trade to logs/trades.csv for easy review.
Called automatically by bot.py after each open/close event.
"""
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

CSV_PATH = "logs/trades.csv"

HEADERS = [
    "timestamp", "symbol", "direction", "entry_price",
    "size_usd", "leverage", "stop_loss", "take_profit",
    "risk_reward", "atr", "acp_job_id", "status", "pnl_usd",
]


def log_trade(trade: dict, status: str = "OPEN", pnl_usd: float = 0.0):
    """Append a new trade row to the CSV."""
    Path("logs").mkdir(exist_ok=True)
    file_exists = os.path.exists(CSV_PATH)

    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp":   trade.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "symbol":      trade.get("symbol", "BTC"),
            "direction":   trade.get("direction", ""),
            "entry_price": trade.get("entry", trade.get("entry_price", "")),
            "size_usd":    trade.get("size_usd", ""),
            "leverage":    trade.get("leverage", ""),
            "stop_loss":   trade.get("stop_loss", ""),
            "take_profit": trade.get("take_profit", ""),
            "risk_reward": trade.get("risk_reward", ""),
            "atr":         trade.get("atr", ""),
            "acp_job_id":  trade.get("acp_job_id", ""),
            "status":      status,
            "pnl_usd":     pnl_usd,
        })


def close_trade(acp_job_id, status: str = "CLOSED", pnl_usd: float = 0.0):
    """Update the matching OPEN row to CLOSED with realized PnL."""
    if not os.path.exists(CSV_PATH):
        return

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    updated = False
    for row in reversed(rows):
        if str(row.get("acp_job_id", "")) == str(acp_job_id) and row.get("status") == "OPEN":
            row["status"]  = status
            row["pnl_usd"] = round(pnl_usd, 2)
            updated = True
            break

    if updated:
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
