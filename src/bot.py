"""
DegenClaw trading bot — QuantAgent signals → ACP execution.

All trades are routed through the DegenClaw ACP agent so they are tracked
on the competition leaderboard. Direct Hyperliquid SDK calls are NOT counted.

Trade flow:
  1. Fetch BTC candles from Hyperliquid (read-only)
  2. Run QuantAgent 4-agent analysis → LONG / SHORT signal
  3. Execute via:  acp job create <wallet> perp_trade --requirements '{...}'
  4. Approve payment (poll for paymentRequestData, then acp job pay)
  5. Post rationale to DegenClaw forum (Trading Signals thread)
  6. Monitor price every tick — close via ACP when TP/SL is hit
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
    HL_SUBACCOUNT_ADDRESS,
    DGCLAW_AGENT_ID, DGCLAW_THREAD_ID,
)

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# DegenClaw ACP agent wallet — all competition trades must target this address
DGCLAW_ACP_WALLET = "0xd478a8B40372db16cA8045F28C6FE07228F3781A"

# ACP CLI wrapper — uses local node_modules/tsx, works without npm link
ACP_CMD = os.path.join(os.path.dirname(__file__), "..", "run_acp.sh")

# dgclaw.sh for forum posts
DGCLAW_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "openclaw", "dgclaw-skill", "scripts", "dgclaw.sh"
)
DGCLAW_ENV = os.path.join(
    os.path.dirname(__file__), "..", "openclaw", "dgclaw-skill", ".env"
)

POSITION_FILE = "logs/position.json"


class DegenClawBot:
    # After a position closes, wait this many seconds before requesting a new signal.
    POST_CLOSE_COOLDOWN = int(os.getenv("POST_CLOSE_COOLDOWN_SECONDS", "3600"))  # 1 hour

    # Max signal attempts before requiring manual restart
    MAX_SIGNAL_ATTEMPTS = int(os.getenv("MAX_SIGNAL_ATTEMPTS", "5"))

    # Trading hours (UTC/GMT) — only open new positions within this window
    TRADING_HOUR_START = 7   # 07:00 GMT
    TRADING_HOUR_END   = 23  # 23:00 GMT

    def __init__(self):
        Path("logs").mkdir(exist_ok=True)
        self.bridge       = QuantAgentBridge()
        self.trade_log:   list[dict] = []
        self.peak_equity: float      = 0.0
        self.in_position: bool       = False
        self.current_trade: dict | None = self._load_position()
        self._api_backoff: int       = 0          # consecutive API failures
        self._api_backoff_until: float = 0.0      # timestamp: skip signal calls until this
        self._close_cooldown_until: float = 0.0   # timestamp: cooldown after position close
        self._signal_attempts: int   = 0          # consecutive no-signal attempts
        self._halted: bool           = False       # True = needs manual restart
        if self.current_trade:
            self.in_position = True
            logger.info(
                f"Resuming position: {self.current_trade['direction']} "
                f"@ ${self.current_trade['entry']} | "
                f"SL={self.current_trade['stop_loss']} TP={self.current_trade['take_profit']}"
            )
        logger.info("DegenClaw bot initialised — QuantAgent signals → ACP execution")

    # ------------------------------------------------------------------ #
    # Position persistence
    # ------------------------------------------------------------------ #

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

    def _reconstruct_position(self) -> dict | None:
        """
        Rebuild position state from Hyperliquid when position.json is lost on restart.
        Called on the first tick (after API rate limits have settled).
        """
        try:
            data = self._hl_info({"type": "clearinghouseState", "user": HL_SUBACCOUNT_ADDRESS})
            if not data:
                return None
            for p in data.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == TRADING_PAIR and float(pos.get("szi", 0)) != 0:
                    szi       = float(pos["szi"])
                    entry     = float(pos["entryPx"])
                    direction = "SHORT" if szi < 0 else "LONG"
                    atr       = float(self._get_current_atr())
                    stop_dist = int(round(1.5 * atr))
                    tp_dist   = int(round(stop_dist * 1.5))
                    if direction == "SHORT":
                        sl = int(round(entry + stop_dist))
                        tp = int(round(entry - tp_dist))
                    else:
                        sl = int(round(entry - stop_dist))
                        tp = int(round(entry + tp_dist))
                    return {
                        "timestamp":    datetime.now(timezone.utc).isoformat(),
                        "symbol":       TRADING_PAIR,
                        "direction":    direction,
                        "entry":        entry,
                        "size_usd":     abs(float(pos.get("positionValue", 0))),
                        "leverage":     1,
                        "stop_loss":    sl,
                        "take_profit":  tp,
                        "risk_reward":  1.5,
                        "atr":          atr,
                        "acp_job_id":   "reconstructed",
                        "entry_equity": self._get_equity(),
                    }
        except Exception as e:
            logger.warning(f"Position reconstruction failed: {e}")
        return None

    def _get_current_atr(self) -> float:
        """Fetch latest 4h ATR via direct HL API (avoids SDK 429 on startup)."""
        try:
            import pandas as pd
            import ta
            import time as _time
            interval_ms = 14_400_000
            end_ms      = int(_time.time() * 1000)
            start_ms    = end_ms - interval_ms * 25
            payload     = {"type": "candleSnapshot", "req": {
                "coin": TRADING_PAIR, "interval": "4h",
                "startTime": start_ms, "endTime": end_ms,
            }}
            data = self._hl_info(payload)
            if not data:
                return 1100.0
            df = pd.DataFrame([{
                "high": float(b["h"]), "low": float(b["l"]), "close": float(b["c"])
            } for b in data])
            atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
            return float(atr.iloc[-1])
        except Exception:
            return 1100.0

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

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

    def _tick(self):
        equity = self._get_equity()
        if equity > 0:
            self.peak_equity = max(self.peak_equity, equity)
            logger.info(f"Equity: ${equity:.2f} | Peak: ${self.peak_equity:.2f}")
            if not check_drawdown(equity, self.peak_equity):
                return

        has_position = self._has_open_position()

        if has_position:
            if not self.current_trade:
                # position.json lost on restart — reconstruct now (API is warmed up)
                self.current_trade = self._reconstruct_position()
                if self.current_trade:
                    self.in_position = True
                    self._save_position(self.current_trade)
                    logger.info(
                        f"Reconstructed position: {self.current_trade['direction']} "
                        f"@ ${self.current_trade['entry']} | "
                        f"SL={self.current_trade['stop_loss']} TP={self.current_trade['take_profit']}"
                    )
            if self.current_trade:
                self._monitor_exit(equity)
            else:
                logger.info(f"Already in a {TRADING_PAIR} position — monitoring")
            return

        # Position just closed — record PnL and clear state
        if self.in_position and self.current_trade:
            self._on_position_closed(equity)
            self._close_cooldown_until = time.time() + self.POST_CLOSE_COOLDOWN
            self._signal_attempts = 0
            self._halted = False
            logger.info(f"Post-close cooldown: skipping signals for {self.POST_CLOSE_COOLDOWN}s")

        self.in_position = False

        # Bot halted after too many failed attempts — needs manual restart
        if self._halted:
            logger.warning("Bot HALTED — max signal attempts reached. Restart to resume.")
            return

        # Only open new trades during trading hours (07:00–23:00 GMT)
        utc_hour = datetime.now(timezone.utc).hour
        if not (self.TRADING_HOUR_START <= utc_hour < self.TRADING_HOUR_END):
            logger.info(f"Outside trading hours ({self.TRADING_HOUR_START}:00–{self.TRADING_HOUR_END}:00 GMT), current={utc_hour}:00 — skipping signal")
            return

        # Respect cooldowns before spending API credits
        now = time.time()
        if now < self._close_cooldown_until:
            remaining = int(self._close_cooldown_until - now)
            logger.info(f"Post-close cooldown active — {remaining}s remaining, skipping signal")
            return
        if now < self._api_backoff_until:
            remaining = int(self._api_backoff_until - now)
            logger.info(f"API backoff active — {remaining}s remaining, skipping signal")
            return

        # Ask QuantAgent for a signal
        try:
            signal = self.bridge.get_signal()
        except Exception as e:
            # Billing/auth or other fatal API errors — exponential backoff
            self._api_backoff = min(self._api_backoff + 1, 8)
            wait = min(CHECK_INTERVAL * (2 ** self._api_backoff), 7200)  # max 2 hours
            self._api_backoff_until = time.time() + wait
            logger.error(
                f"API error (backoff #{self._api_backoff}, next retry in {wait}s): {e}"
            )
            return

        # Successful API call — reset backoff
        self._api_backoff = 0
        self._api_backoff_until = 0.0

        if signal is None:
            self._signal_attempts += 1
            logger.info(
                f"No actionable signal from QuantAgent this tick "
                f"(attempt {self._signal_attempts}/{self.MAX_SIGNAL_ATTEMPTS})"
            )
            if self._signal_attempts >= self.MAX_SIGNAL_ATTEMPTS:
                self._halted = True
                logger.warning(
                    f"HALTED: {self.MAX_SIGNAL_ATTEMPTS} consecutive signal attempts with no result. "
                    f"Restart the bot to resume trading."
                )
            return

        # Got a signal — reset attempt counter
        self._signal_attempts = 0

        logger.info(
            f"SIGNAL: {signal['direction']} {TRADING_PAIR} | "
            f"R:R {signal['risk_reward']} | "
            f"Entry ~${signal['entry_price']:.2f} | "
            f"SL=${signal['stop_loss']:.2f} | TP=${signal['take_profit']:.2f}"
        )
        self._execute(signal, equity if equity > 0 else 100.0)

    # ------------------------------------------------------------------ #
    # Position monitoring
    # ------------------------------------------------------------------ #

    def _monitor_exit(self, equity: float):
        """Check price against TP/SL each tick and close via ACP if hit."""
        trade     = self.current_trade
        direction = trade["direction"]
        sl        = float(trade["stop_loss"])
        tp        = float(trade["take_profit"])

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

        hit_tp = (direction == "LONG"  and current_price >= tp) or \
                 (direction == "SHORT" and current_price <= tp)
        hit_sl = (direction == "LONG"  and current_price <= sl) or \
                 (direction == "SHORT" and current_price >= sl)

        if hit_tp:
            logger.info(f"TP hit at ${current_price:.0f} — closing via ACP")
            self._close_via_acp(reason="TP")
        elif hit_sl:
            logger.info(f"SL hit at ${current_price:.0f} — closing via ACP")
            self._close_via_acp(reason="SL")

    def _close_via_acp(self, reason: str = "manual"):
        """Send ACP perp_trade close order and update CSV with realized PnL."""
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

        # Brief settle window then record realized PnL
        time.sleep(3)
        if self.current_trade:
            pnl = self._get_realized_pnl(self.current_trade)
            logger.info(f"Realized PnL after {reason}: ${pnl:.2f}")
            close_trade(
                acp_job_id=self.current_trade.get("acp_job_id", ""),
                status="CLOSED",
                pnl_usd=pnl,
            )
            self.current_trade = None
            self.in_position   = False
            self._save_position(None)
            self._close_cooldown_until = time.time() + self.POST_CLOSE_COOLDOWN
            self._signal_attempts = 0
            self._halted = False
            logger.info(f"Post-close cooldown: skipping signals for {self.POST_CLOSE_COOLDOWN}s")

    def _on_position_closed(self, equity: float):
        """Update CSV when position closes naturally (TP/SL filled on HL)."""
        trade = self.current_trade
        pnl   = self._get_realized_pnl(trade)
        logger.info(f"Position closed — realized PnL: ${pnl:.2f}")
        close_trade(
            acp_job_id=trade.get("acp_job_id", ""),
            status="CLOSED",
            pnl_usd=pnl,
        )
        self.current_trade = None
        self._save_position(None)

    def _get_realized_pnl(self, trade: dict) -> float:
        """Fetch actual realized PnL from Hyperliquid fills since trade entry."""
        try:
            ts_str = trade.get("timestamp", "")
            try:
                dt       = datetime.fromisoformat(ts_str)
                start_ms = int(dt.timestamp() * 1000)
            except Exception:
                start_ms = int(time.time() * 1000) - 3_600_000

            data = self._hl_info({
                "type":      "userFillsByTime",
                "user":      HL_SUBACCOUNT_ADDRESS,
                "startTime": start_ms,
            })
            if not data:
                raise ValueError("No fills data returned")

            pnl = sum(
                float(f.get("closedPnl", 0))
                for f in data
                if f.get("coin") == TRADING_PAIR
            )
            return round(pnl, 2)
        except Exception as e:
            logger.warning(f"Could not fetch realized PnL from HL: {e}")
            entry_equity   = float(trade.get("entry_equity", 0))
            current_equity = self._get_equity()
            return round(current_equity - entry_equity, 2) if entry_equity else 0.0

    def _get_unrealized_pnl(self) -> float:
        data = self._hl_info({"type": "clearinghouseState", "user": HL_SUBACCOUNT_ADDRESS})
        if data:
            for p in data.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == TRADING_PAIR:
                    return float(pos.get("unrealizedPnl", 0))
        return 0.0

    # ------------------------------------------------------------------ #
    # Trade execution
    # ------------------------------------------------------------------ #

    def _execute(self, signal: dict, equity: float):
        try:
            side         = "long" if signal["direction"] == "LONG" else "short"
            stop_distance = abs(signal["entry_price"] - signal["stop_loss"])
            if stop_distance == 0:
                logger.warning("Stop distance is zero — skipping")
                return

            risk_amount = equity * RISK_PER_TRADE
            size_usd    = risk_amount / stop_distance * signal["entry_price"]
            size_usd    = min(size_usd, equity * MAX_LEVERAGE, equity * 0.9)
            size_usd    = round(size_usd, 2)
            leverage    = max(min(round(size_usd / equity, 1), MAX_LEVERAGE), 1)

            requirements = {
                "action":     "open",
                "pair":       TRADING_PAIR,
                "side":       side,
                "size":       str(size_usd),
                "leverage":   leverage,
                "takeProfit": str(signal["take_profit"]),
                "stopLoss":   str(signal["stop_loss"]),
            }

            logger.info(f"Placing ACP perp_trade job: {requirements}")
            result = self._acp_job("perp_trade", requirements)
            if result is None:
                logger.error("ACP job creation failed")
                return

            job_id = (
                result.get("data", {}).get("jobId")
                or result.get("jobId")
                or result.get("id")
            )
            logger.info(f"ACP job created: {job_id}")
            if job_id:
                self._approve_payment(job_id)

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
            self.in_position   = True
            self.current_trade = trade
            self._save_position(trade)

            logger.info(
                f"EXECUTED: {signal['direction']} {TRADING_PAIR} "
                f"${size_usd} ({leverage}x) | "
                f"SL=${signal['stop_loss']:.2f} | TP=${signal['take_profit']:.2f}"
            )

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
        """
        Poll until the provider sets paymentRequestData (NEGOTIATION phase),
        then approve payment. Phase stays NEGOTIATION until we pay — NOT TRANSACTION.
        """
        acp_cwd = os.path.join(os.path.dirname(__file__), "..", "openclaw", "openclaw-acp")

        for attempt in range(15):  # up to 90s
            time.sleep(6)
            status_raw = self._run(
                [ACP_CMD, "job", "status", str(job_id), "--json"], cwd=acp_cwd
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

            payment_data = status.get("paymentRequestData")
            if payment_data:
                logger.info(f"Job {job_id} payment ready (phase={phase}), approving...")
                break
            logger.info(f"Job {job_id} phase={phase}, waiting for payment memo ({attempt+1}/15)...")
        else:
            logger.warning(f"Job {job_id} never received payment memo — skipping")
            return

        result = self._run(
            [ACP_CMD, "job", "pay", str(job_id),
             "--accept", "true", "--content", "Approved", "--json"],
            cwd=acp_cwd,
        )
        if result:
            logger.info(f"Payment approved for job {job_id}")
        else:
            logger.error(f"Payment approval failed for job {job_id}")

    # ------------------------------------------------------------------ #
    # Hyperliquid read-only helpers
    # ------------------------------------------------------------------ #

    def _hl_info(self, payload: dict) -> dict | None:
        """Query Hyperliquid info endpoint (read-only, no auth required)."""
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
        data = self._hl_info({"type": "clearinghouseState", "user": HL_SUBACCOUNT_ADDRESS})
        if data:
            try:
                equity = float(
                    data.get("marginSummary", {}).get("accountValue") or
                    data.get("crossMarginSummary", {}).get("accountValue") or 0
                )
                if equity > 0:
                    return equity
            except Exception:
                pass
        return 0.0

    def _has_open_position(self) -> bool:
        """Check for an open BTC position directly on Hyperliquid."""
        data = self._hl_info({"type": "clearinghouseState", "user": HL_SUBACCOUNT_ADDRESS})
        if data:
            for p in data.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") == TRADING_PAIR and float(pos.get("szi", 0)) != 0:
                    return True
            return False
        return self.in_position  # fallback to local state

    # ------------------------------------------------------------------ #
    # Forum post
    # ------------------------------------------------------------------ #

    def _post_to_forum(self, trade: dict):
        """Post QuantAgent rationale to the DegenClaw Trading Signals thread."""
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

    def _run(self, cmd: list, cwd: str = None, timeout: int = 30) -> str | None:
        """Run a subprocess and return stdout, or None on failure."""
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


if __name__ == "__main__":
    bot = DegenClawBot()
    bot.run()
