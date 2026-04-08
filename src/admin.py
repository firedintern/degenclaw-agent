"""
Admin portal — manual position control.

Routes:
  GET  /          Dashboard (position, PnL, bot status)
  POST /close     Close current position via ACP
  POST /restart   Clear halt/cooldown so bot can trade again

Protected by ADMIN_SECRET env var (query param or header X-Admin-Secret).
"""
import os
import json
import time
import threading
import urllib.request
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

logger = logging.getLogger(__name__)

POSITION_FILE = "logs/position.json"
CONTROL_FILE  = "logs/control.json"
BOT_STATE_FILE = "logs/bot_state.json"

TRADING_PAIR = os.getenv("TRADING_PAIR", "BTC")
HL_WALLET    = "0x7e086e978fc8b2ea16532a6cc77c610d36ca0c3f"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

ACP_CMD = os.path.join(os.path.dirname(__file__), "..", "run_acp.sh")
DGCLAW_ACP_WALLET = "0xd478a8B40372db16cA8045F28C6FE07228F3781A"

app = Flask(__name__)


# ------------------------------------------------------------------ #
# Auth
# ------------------------------------------------------------------ #

def _check_auth() -> bool:
    if not ADMIN_SECRET:
        return True  # no secret configured → open (Railway private network)
    secret = request.args.get("secret") or request.headers.get("X-Admin-Secret", "")
    return secret == ADMIN_SECRET


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _hl_info(payload: dict):
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


def _get_position_from_hl():
    data = _hl_info({"type": "clearinghouseState", "user": HL_WALLET})
    if not data:
        return None
    for p in data.get("assetPositions", []):
        pos = p.get("position", {})
        if pos.get("coin") == TRADING_PAIR and float(pos.get("szi", 0)) != 0:
            return {
                "symbol":         pos.get("coin"),
                "size":           float(pos.get("szi", 0)),
                "entry_price":    float(pos.get("entryPx", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                "direction":      "LONG" if float(pos.get("szi", 0)) > 0 else "SHORT",
                "leverage":       float(pos.get("leverage", {}).get("value", 1)),
                "position_value": float(pos.get("positionValue", 0)),
            }
    return None


def _get_equity():
    data = _hl_info({"type": "clearinghouseState", "user": HL_WALLET})
    if data:
        try:
            return float(
                data.get("marginSummary", {}).get("accountValue") or
                data.get("crossMarginSummary", {}).get("accountValue") or 0
            )
        except Exception:
            pass
    return 0.0


def _load_position_file():
    try:
        if os.path.exists(POSITION_FILE):
            with open(POSITION_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _load_bot_state():
    try:
        if os.path.exists(BOT_STATE_FILE):
            with open(BOT_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_control(cmd: dict):
    Path("logs").mkdir(exist_ok=True)
    with open(CONTROL_FILE, "w") as f:
        json.dump({**cmd, "issued_at": datetime.now(timezone.utc).isoformat()}, f)


# ------------------------------------------------------------------ #
# Dashboard HTML
# ------------------------------------------------------------------ #

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DegenClaw Admin</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }
  h1 { font-size: 1.4rem; color: #58a6ff; margin-bottom: 24px; letter-spacing: 0.05em; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
  .card h2 { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; }
  .metric { font-size: 1.6rem; font-weight: 700; }
  .metric.green { color: #3fb950; }
  .metric.red { color: #f85149; }
  .metric.yellow { color: #d29922; }
  .metric.blue { color: #58a6ff; }
  .sub { font-size: 0.8rem; color: #8b949e; margin-top: 4px; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; }
  button { padding: 10px 22px; border: none; border-radius: 6px; font-size: 0.9rem; font-family: inherit; cursor: pointer; font-weight: 600; transition: opacity 0.15s; }
  button:hover { opacity: 0.8; }
  .btn-close { background: #da3633; color: #fff; }
  .btn-restart { background: #238636; color: #fff; }
  .btn-refresh { background: #21262d; color: #e6edf3; border: 1px solid #30363d; }
  .status-ok   { color: #3fb950; }
  .status-warn { color: #d29922; }
  .status-err  { color: #f85149; }
  .log { background: #010409; border: 1px solid #21262d; border-radius: 6px; padding: 16px; font-size: 0.75rem; color: #8b949e; max-height: 240px; overflow-y: auto; line-height: 1.6; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em; }
  .tag-long  { background: #1f4b21; color: #3fb950; }
  .tag-short { background: #4b1f1f; color: #f85149; }
  .tag-none  { background: #21262d; color: #8b949e; }
  .divider { border: none; border-top: 1px solid #21262d; margin: 20px 0; }
  #msg { margin-top: 16px; padding: 12px 16px; border-radius: 6px; display: none; font-size: 0.85rem; }
  #msg.ok  { background: #1f4b21; color: #3fb950; display: block; }
  #msg.err { background: #4b1f1f; color: #f85149; display: block; }
  small { color: #6e7681; font-size: 0.7rem; }
</style>
</head>
<body>
<h1>&#x1f9A0; DegenClaw Admin Portal</h1>

<div class="grid">
  <div class="card">
    <h2>Account Equity</h2>
    <div class="metric blue">${{ equity }}</div>
    <div class="sub">Live from Hyperliquid</div>
  </div>

  <div class="card">
    <h2>Open Position</h2>
    {% if position %}
      <span class="tag tag-{{ position.direction | lower }}">{{ position.direction }}</span>
      <div class="metric" style="margin-top:8px">
        {{ position.symbol }} @ ${{ "{:,.0f}".format(position.entry_price) }}
      </div>
      <div class="sub">
        Size: ${{ "{:,.2f}".format(position.position_value) }} &nbsp;|&nbsp;
        Lev: {{ position.leverage }}x
      </div>
    {% else %}
      <span class="tag tag-none">FLAT</span>
      <div class="sub" style="margin-top:8px">No open position</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Unrealized PnL</h2>
    {% if position %}
      <div class="metric {{ 'green' if position.unrealized_pnl >= 0 else 'red' }}">
        ${{ "{:+.2f}".format(position.unrealized_pnl) }}
      </div>
      <div class="sub">vs entry ${{ "{:,.0f}".format(position.entry_price) }}</div>
    {% else %}
      <div class="metric" style="color:#8b949e">—</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Bot Status</h2>
    {% if bot_state.get('halted') %}
      <div class="metric status-err">HALTED</div>
      <div class="sub">Max signal attempts reached — needs restart</div>
    {% elif bot_state.get('cooldown_until') and bot_state.get('cooldown_until') > now %}
      <div class="metric status-warn">COOLDOWN</div>
      <div class="sub">{{ bot_state.cooldown_remaining }}s remaining</div>
    {% else %}
      <div class="metric status-ok">RUNNING</div>
      <div class="sub">Monitoring every {{ check_interval }}s</div>
    {% endif %}
  </div>
</div>

{% if position %}
<div class="card" style="margin-bottom:20px">
  <h2>Position Details (from bot)</h2>
  {% if bot_pos %}
  <div style="font-size:0.8rem; color:#8b949e; line-height:2">
    Entry: <b style="color:#e6edf3">${{ "{:,.0f}".format(bot_pos.entry) }}</b> &nbsp;|&nbsp;
    Stop Loss: <b style="color:#f85149">${{ "{:,.0f}".format(bot_pos.stop_loss) }}</b> &nbsp;|&nbsp;
    Take Profit: <b style="color:#3fb950">${{ "{:,.0f}".format(bot_pos.take_profit) }}</b> &nbsp;|&nbsp;
    R:R: <b style="color:#58a6ff">{{ bot_pos.risk_reward }}</b><br>
    Opened: {{ bot_pos.timestamp[:19].replace('T',' ') }} UTC
  </div>
  {% else %}
    <div style="color:#8b949e;font-size:0.8rem">Position file not available</div>
  {% endif %}
</div>
{% endif %}

<hr class="divider">
<div class="actions">
  {% if position %}
  <button class="btn-close" onclick="doAction('/close', 'Close position now?')">
    &#x274C; Close Position
  </button>
  {% endif %}
  <button class="btn-restart" onclick="doAction('/restart', 'Resume bot trading?')">
    &#x25B6; Resume Bot
  </button>
  <button class="btn-refresh" onclick="location.reload()">
    &#x21BB; Refresh
  </button>
</div>

<div id="msg"></div>

<hr class="divider">
<div class="card">
  <h2>Recent Trades (last 5)</h2>
  <div class="log">{{ trade_log }}</div>
</div>

<div style="margin-top:16px"><small>Auto-refreshes every 30s &nbsp;|&nbsp; {{ now_str }} UTC</small></div>

<script>
  setTimeout(() => location.reload(), 30000);

  const SECRET = new URLSearchParams(location.search).get('secret') || '';

  async function doAction(path, confirm_msg) {
    if (!confirm(confirm_msg)) return;
    const params = SECRET ? '?secret=' + SECRET : '';
    const r = await fetch(path + params, { method: 'POST' });
    const data = await r.json();
    const el = document.getElementById('msg');
    if (r.ok) {
      el.className = 'ok';
      el.textContent = '✓ ' + data.message;
    } else {
      el.className = 'err';
      el.textContent = '✗ ' + (data.error || 'Unknown error');
    }
    setTimeout(() => location.reload(), 2000);
  }
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.route("/")
def dashboard():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    equity   = _get_equity()
    position = _get_position_from_hl()
    bot_pos  = _load_position_file()
    bot_state = _load_bot_state()

    # Cooldown remaining
    cooldown_until = bot_state.get("cooldown_until", 0)
    now_ts = time.time()
    bot_state["cooldown_remaining"] = max(0, int(cooldown_until - now_ts))

    # Trade log preview
    trade_log_str = "No trades yet."
    try:
        if os.path.exists("logs/trades.csv"):
            with open("logs/trades.csv") as f:
                lines = f.readlines()
            trade_log_str = "".join(lines[-6:]) if len(lines) > 1 else "No closed trades."
    except Exception:
        pass

    check_interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

    return render_template_string(
        DASHBOARD_HTML,
        equity=f"{equity:.2f}",
        position=position,
        bot_pos=bot_pos,
        bot_state=bot_state,
        now=now_ts,
        now_str=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        trade_log=trade_log_str,
        check_interval=check_interval,
    )


@app.route("/close", methods=["POST"])
def close_position():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    position = _get_position_from_hl()
    if not position:
        return jsonify({"error": "No open position to close"}), 400

    _write_control({"action": "close", "reason": "manual_portal"})
    return jsonify({"message": f"Close signal sent — bot will close {position['direction']} {position['symbol']} on next tick (≤{os.getenv('CHECK_INTERVAL_SECONDS','300')}s)"})


@app.route("/restart", methods=["POST"])
def restart_bot():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    _write_control({"action": "resume"})
    return jsonify({"message": "Resume signal sent — bot will clear halt/cooldown on next tick"})


@app.route("/status", methods=["GET"])
def status():
    """JSON status endpoint for monitoring."""
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    equity   = _get_equity()
    position = _get_position_from_hl()
    bot_state = _load_bot_state()

    return jsonify({
        "equity":    equity,
        "position":  position,
        "bot_state": bot_state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def run_admin(port: int = 8080):
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    run_admin(int(os.getenv("ADMIN_PORT", "8080")))
