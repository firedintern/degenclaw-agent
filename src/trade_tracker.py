"""
Trade tracker — writes every trade to logs/trades.csv for easy spreadsheet viewing.
Called automatically by bot.py after each trade.
"""
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

CSV_PATH = "logs/trades.csv"

HEADERS = [
    "timestamp", "symbol", "direction", "entry_price",
    "size_usd", "leverage", "stop_loss", "take_profit",
    "risk_reward", "atr", "acp_job_id", "status", "pnl_usd"
]


def log_trade(trade: dict, status: str = "OPEN", pnl_usd: float = 0.0):
    """Append a trade row to the CSV."""
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


def close_trade(acp_job_id, status: str = "CLOSED", pnl_usd: float = 0.0, close_price: float = 0.0):
    """Update the most recent OPEN row matching acp_job_id to CLOSED with PnL."""
    if not os.path.exists(CSV_PATH):
        return

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    updated = False
    for row in reversed(rows):
        if str(row.get("acp_job_id", "")) == str(acp_job_id) and row.get("status") == "OPEN":
            row["status"] = status
            row["pnl_usd"] = round(pnl_usd, 2)
            if close_price:
                row["close_price"] = close_price
            updated = True
            break

    if updated:
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)


def print_summary():
    """Print a summary of all trades to console."""
    if not os.path.exists(CSV_PATH):
        print("No trades yet.")
        return

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("No trades yet.")
        return

    print(f"\n{'='*70}")
    print(f"{'TRADE LOG':^70}")
    print(f"{'='*70}")
    print(f"{'#':<4} {'Time':<12} {'Dir':<6} {'Entry':>10} {'Size':>8} {'SL':>10} {'TP':>10} {'Status':<10}")
    print(f"{'-'*70}")

    for i, row in enumerate(rows, 1):
        ts = row["timestamp"][:16].replace("T", " ")
        print(
            f"{i:<4} {ts:<12} {row['direction']:<6} "
            f"${float(row['entry_price'] or 0):>9,.1f} "
            f"${float(row['size_usd'] or 0):>7,.2f} "
            f"${float(row['stop_loss'] or 0):>9,.1f} "
            f"${float(row['take_profit'] or 0):>9,.1f} "
            f"{row['status']:<10}"
        )

    print(f"{'='*70}")
    print(f"Total trades: {len(rows)}")


if __name__ == "__main__":
    print_summary()
