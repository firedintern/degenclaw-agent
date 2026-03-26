"""
Main trading bot — QuantAgent signals → DegenClaw ACP execution.

All trades go through the Degen Claw ACP agent (ID 8654) so they are
tracked on the leaderboard. Direct Hyperliquid SDK calls are NOT counted.

Trade flow:
  1. Fetch BTC candles from Hyperliquid (read-only)
  2. Run QuantAgent 4-agent analysis → LONG/SHORT signal
  3. Execute via: acp job create "0xd478a8..." "perp_trade" --requirements '{...}'
  4. Post rationale to DegenClaw forum (Trading Signals thread)
"""
import os
import json
import time
import logging
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.bridge import QuantAgentBridge
from src.trade_tracker import log_trade, close_trade
from src.risk import check_drawdown
from config.settings import (
    TRADING_PAIR, CHECK_INTERVAL, TIMEFRAME,
    RISK_PER_TRADE, MAX_LEVERAGE,
    DGCLAW_AGENT_ID, DGCLAW_THREAD_ID,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# DegenClaw ACP agent wallet — ALL trades must go through this
DGCLAW_ACP_WALLET = "0xd478a8B40372db16cA8045F28C6FE07228F3781A"

# Path to the acp CLI (installed via npm link)
ACP_CMD = "acp"

# Path to dgclaw.sh
DGCLAW_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "openclaw", "dgclaw-skill", "scripts", "dgclaw.sh"
)
DGCLAW_ENV = os.path.join(
    os.path.dirname(__file__), "..", "openclaw", "dgclaw-skill", ".env"
)


POSITION_FILE = "logs/position.json"


class DegenClawBot:
    def __init__(self):
        Path("logs").mkdir(exist_ok=True)
        self.bridge = QuantAgentBridge()
        self.trade_log: list[dict] = []
        self.peak_equity: float = 0.0
        self.in_position: bool = False
        self.current_trade: dict | None = self._load_position()
        if self.current_trade:
            self.in_position = True
            logger.info(
                f"Resuming position: {self.current_trade['direction']} "
                f"@ ${self.current_trade['entry']} | "
                f"SL={self.current_trade['stop_loss']} TP={self.current_trade['take_profit']}"
            )
        logger.info("DegenClaw bot initialised — QuantAgent signals → ACP execution")

    def _load_position(self) -> dict | None:
        try:
            if os.path.exists(POSITION_FILE):
                with open(POSITION_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _save_position(self, trade: dict | None):
        if trade is None:
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
        else:
            with open(POSITION_FILE, "w") as f:
                json.dump(trade, f, indent=2)

    def run(self):
        logger.info(
            f"Starting bot — {TRADING_PAIR} on {TIMEFRAME}, "
            f"checking every {CHECK_INTERVAL}s"
        )
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(CHECK_INTERVAL)

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    def _tick(self):
        equity = self._get_equity()
        if equity > 0:
            self.peak_equity = max(self.peak_equity, equity)
            logger.info(f"Equity: ${equity:.2f} | Peak: ${self.peak_equity:.2f}")
            if not check_drawdown(equity, self.peak_equity):
                return

        has_position = self._has_open_position()

        if has_position:
            # Monitor exit conditions for the current trade
            if self.current_trade:
                self._monitor_exit(equity)
            else:
                logger.info(f"Already in a {TRADING_PAIR} position — monitoring")
            return

        # Position just closed — update CSV and clear state
        if self.in_position and self.current_trade:
            self._on_position_closed(equity)

        self.in_position = False

        # Get QuantAgent signal
        signal = self.bridge.get_signal()
        if signal is None:
            logger.info("No actionable signal from QuantAgent this tick")
            return

        logger.info(
            f"SIGNAL: {signal['direction']} {TRADING_PAIR} | "
            f"R:R {signal['risk_reward']} | "
            f"Entry ~${signal['entry_price']:.2f} | "
            f"SL=${signal['stop_loss']:.2f} | TP=${signal['take_profit']:.2f}"
        )

        self._execute(signal, equity if equity > 0 else 100.0)

    def _monitor_exit(self, equity: float):
        """Check price against TP/SL and close via ACP if hit."""
        trade = self.current_trade
        direction = trade["direction"]
        sl = float(trade["stop_loss"])
        tp = float(trade["take_profit"])

        price_data = self._hl_info({"type": "allMids"})
        if not price_data:
            logger.info(f"Monitoring {direction} | SL={sl} TP={tp} | price unavailable")
            return
        current_price = float(price_data.get("BTC", 0) or price_data.get(TRADING_PAIR, 0))
        if current_price == 0:
            return

        unrealized = self._get_unrealized_pnl()
        logger.info(
            f"Monitoring {direction} BTC @ ${current_price:.0f} | "
            f"Entry={trade['entry']} SL={sl} TP={tp} | PnL=${unrealized:.2f}"
        )

        hit_tp = (direction == "LONG" and current_price >= tp) or \
                 (direction == "SHORT" and current_price <= tp)
        hit_sl = (direction == "LONG" and current_price <= sl) or \
                 (direction == "SHORT" and current_price >= sl)

        if hit_tp:
            logger.info(f"TP hit at ${current_price:.0f} — closing position via ACP")
            self._close_via_acp(reason="TP")
        elif hit_sl:
            logger.info(f"SL hit at ${current_price:.0f} — closing position via ACP")
            self._close_via_acp(reason="SL")

    def _close_via_acp(self, reason: str = "manual"):
        """Send ACP perp_trade close order."""
        requirements = {"action": "close", "pair": TRADING_PAIR}
        logger.info(f"Sending ACP close order ({reason}): {requirements}")
        result = self._acp_job("perp_trade", requirements)
        if result is None:
            logger.error("ACP close job creation failed")
            return
        job_id = result.get("data", {}).get("jobId") or result.get("jobId") or result.get("id")
        logger.info(f"ACP close job created: {job_id}")
        if job_id:
            self._approve_payment(job_id)

    def _get_unrealized_pnl(self) -> float:
        hl_wallet = "0x7e086e978fc8b2ea16532a6cc77c610d36ca0c3f"
        data = self._hl_info({"type": "clearinghouseState", "user": hl_wallet})
        if data:
            for p in data.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == TRADING_PAIR:
                    return float(pos.get("unrealizedPnl", 0))
        return 0.0

    def _on_position_closed(self, equity: float):
        """Update CSV when position closes naturally (TP/SL filled on HL)."""
        trade = self.current_trade
        pnl = self._get_realized_pnl_from_equity(equity)
        logger.info(f"Position closed — estimated PnL: ${pnl:.2f}")
        close_trade(
            acp_job_id=trade.get("acp_job_id", ""),
            status="CLOSED",
            pnl_usd=pnl,
        )
        self.current_trade = None
        self._save_position(None)

    def _get_realized_pnl_from_equity(self, current_equity: float) -> float:
        """Estimate realized PnL as equity change from entry equity."""
        entry_equity = float(self.current_trade.get("entry_equity", current_equity))
        return round(current_equity - entry_equity, 2)

    # ------------------------------------------------------------------ #
    # ACP trade execution
    # ------------------------------------------------------------------ #

    def _execute(self, signal: dict, equity: float):
        try:
            is_long = signal["direction"] == "LONG"
            side = "long" if is_long else "short"

            # Position size: risk 2% of equity, never exceed 90% of balance
            risk_amount = equity * RISK_PER_TRADE
            stop_distance = abs(signal["entry_price"] - signal["stop_loss"])
            if stop_distance == 0:
                logger.warning("Stop distance is zero — skipping")
                return

            size_usd = risk_amount / stop_distance * signal["entry_price"]
            size_usd = min(size_usd, equity * MAX_LEVERAGE, equity * 0.9)
            size_usd = round(size_usd, 2)

            leverage = min(round(size_usd / equity, 1), MAX_LEVERAGE)
            leverage = max(leverage, 1)

            requirements = {
                "action":    "open",
                "pair":      TRADING_PAIR,
                "side":      side,
                "size":      str(size_usd),
                "leverage":  leverage,
                "takeProfit": str(signal["take_profit"]),
                "stopLoss":   str(signal["stop_loss"]),
            }

            logger.info(f"Placing ACP perp_trade job: {requirements}")

            result = self._acp_job("perp_trade", requirements)

            if result is None:
                logger.error("ACP job creation failed")
                return

            # Response format: {"message": "...", "data": {"jobId": 123}}
            job_id = (
                result.get("data", {}).get("jobId")
                or result.get("jobId")
                or result.get("id")
            )
            logger.info(f"ACP job created: {job_id}")

            # Always approve payment — DegenClaw jobs require it
            if job_id:
                self._approve_payment(job_id)

            # Log trade
            trade = {
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "symbol":       TRADING_PAIR,
                "direction":    signal["direction"],
                "entry":        signal["entry_price"],
                "size_usd":     size_usd,
                "leverage":     leverage,
                "stop_loss":    signal["stop_loss"],
                "take_profit":  signal["take_profit"],
                "risk_reward":  signal["risk_reward"],
                "atr":          signal["atr"],
                "acp_job_id":   job_id,
                "rationale":    signal["rationale"],
                "entry_equity": equity,
            }
            self.trade_log.append(trade)
            self._save_log()
            log_trade(trade, status="OPEN")
            self.in_position = True
            self.current_trade = trade
            self._save_position(trade)

            logger.info(
                f"EXECUTED: {signal['direction']} {TRADING_PAIR} "
                f"${size_usd} ({leverage}x) | "
                f"SL=${signal['stop_loss']:.2f} | TP=${signal['take_profit']:.2f}"
            )

            # Post rationale to forum
            if DGCLAW_AGENT_ID and DGCLAW_THREAD_ID:
                self._post_to_forum(trade)

        except Exception as e:
            logger.error(f"Execution failed: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    # ACP helpers
    # ------------------------------------------------------------------ #

    def _acp_job(self, offering: str, requirements: dict) -> dict | None:
        """Create an ACP job against the DegenClaw agent."""
        cmd = [
            ACP_CMD, "job", "create",
            DGCLAW_ACP_WALLET, offering,
            "--requirements", json.dumps(requirements),
            "--json",
        ]
        result = self._run(cmd, cwd=os.path.join(
            os.path.dirname(__file__), "..", "openclaw", "openclaw-acp"
        ))
        if result is None:
            return None
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            logger.error(f"ACP job response not JSON: {result}")
            return None

    def _approve_payment(self, job_id):
        """Poll until provider sets paymentRequestData (NEGOTIATION phase), then pay."""
        acp_cwd = os.path.join(os.path.dirname(__file__), "..", "openclaw", "openclaw-acp")

        # Provider typically responds within 5-15s — poll up to 90s
        for attempt in range(15):
            time.sleep(6)
            status_raw = self._run(
                [ACP_CMD, "job", "status", str(job_id), "--json"],
                cwd=acp_cwd,
            )
            if not status_raw:
                continue
            try:
                status = json.loads(status_raw)
            except json.JSONDecodeError:
                continue

            phase = status.get("phase", "")
            if phase in ("COMPLETED", "REJECTED", "EXPIRED"):
                logger.info(f"Job {job_id} already terminal: {phase}")
                return

            # Payment is ready when provider has set paymentRequestData
            # (phase stays NEGOTIATION until we pay — NOT TRANSACTION)
            payment_data = status.get("paymentRequestData")
            if payment_data:
                logger.info(f"Job {job_id} payment ready (phase={phase}), approving...")
                break
            logger.info(f"Job {job_id} in phase {phase}, waiting for payment memo (attempt {attempt+1}/15)...")
        else:
            logger.warning(f"Job {job_id} never got payment memo — skipping payment")
            return

        cmd = [ACP_CMD, "job", "pay", str(job_id),
                "--accept", "true", "--content", "Approved", "--json"]
        result = self._run(cmd, cwd=acp_cwd)
        if result:
            logger.info(f"Payment approved for job {job_id}")
        else:
            logger.error(f"Payment approval failed for job {job_id}")

    def _hl_info(self, payload: dict) -> dict | None:
        """Query Hyperliquid info API directly (read-only, no auth needed)."""
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning(f"HL info query failed: {e}")
            return None

    def _get_equity(self) -> float:
        """Query account balance directly from Hyperliquid."""
        hl_wallet = "0x7e086e978fc8b2ea16532a6cc77c610d36ca0c3f"
        data = self._hl_info({"type": "clearinghouseState", "user": hl_wallet})
        if data:
            try:
                equity = float(data.get("marginSummary", {}).get("accountValue") or
                               data.get("crossMarginSummary", {}).get("accountValue") or 0)
                if equity > 0:
                    return equity
            except Exception:
                pass
        return 18.99  # fallback

    def _has_open_position(self) -> bool:
        """Check if there's an open BTC position directly from Hyperliquid."""
        hl_wallet = "0x7e086e978fc8b2ea16532a6cc77c610d36ca0c3f"
        data = self._hl_info({"type": "clearinghouseState", "user": hl_wallet})
        if data:
            positions = data.get("assetPositions", [])
            for p in positions:
                pos = p.get("position", {})
                if pos.get("coin") == TRADING_PAIR and float(pos.get("szi", 0)) != 0:
                    return True
            return False
        return self.in_position  # fallback to local state

    def _run(self, cmd: list, cwd: str = None, timeout: int = 30) -> str | None:
        """Run a shell command and return stdout."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=cwd,
            )
            if result.returncode != 0:
                logger.error(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out: {' '.join(cmd)}")
            return None
        except Exception as e:
            logger.error(f"Command error: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Forum post
    # ------------------------------------------------------------------ #

    def _post_to_forum(self, trade: dict):
        """Post QuantAgent rationale to the Trading Signals thread."""
        try:
            title   = f"{trade['direction']} {trade['symbol']} — {trade['timestamp'][:10]}"
            content = trade["rationale"][:2000]
            cmd = [
                "bash", DGCLAW_SCRIPT,
                "--env", DGCLAW_ENV,
                "create-post",
                DGCLAW_AGENT_ID, DGCLAW_THREAD_ID,
                title, content,
            ]
            result = self._run(cmd, timeout=30)
            if result:
                logger.info("Forum post published to Trading Signals thread")
        except Exception as e:
            logger.error(f"Forum post failed: {e}")

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _save_log(self):
        with open("logs/trades.json", "w") as f:
            json.dump(self.trade_log, f, indent=2)


if __name__ == "__main__":
    bot = DegenClawBot()
    bot.run()
