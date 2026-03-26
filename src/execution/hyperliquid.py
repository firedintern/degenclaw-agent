"""Hyperliquid SDK wrapper for order execution."""
import os
import logging
import eth_account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self):
        secret_key = os.getenv("HL_PRIVATE_KEY")
        if not secret_key:
            raise ValueError("HL_PRIVATE_KEY not set in .env")

        self.account = eth_account.Account.from_key(secret_key)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(self.account, constants.MAINNET_API_URL)
        self.address = self.account.address
        self._meta_cache = None
        logger.info(f"Connected to Hyperliquid as {self.address}")

    # ------------------------------------------------------------------ #
    # Account state
    # ------------------------------------------------------------------ #

    def get_equity(self) -> float:
        state = self.info.user_state(self.address)
        return float(state.get("marginSummary", {}).get("accountValue", 0))

    def get_positions(self) -> list[dict]:
        state = self.info.user_state(self.address)
        positions = []
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            size = float(p.get("szi", 0))
            if size != 0:
                positions.append({
                    "symbol":         p.get("coin"),
                    "size":           size,
                    "entry_price":    float(p.get("entryPx", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    "direction":      "long" if size > 0 else "short",
                })
        return positions

    # ------------------------------------------------------------------ #
    # Leverage
    # ------------------------------------------------------------------ #

    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.exchange.update_leverage(leverage, symbol, is_cross=True)
            logger.info(f"Leverage set to {leverage}x for {symbol}")
        except Exception as e:
            logger.error(f"set_leverage error: {e}")

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def market_open(self, symbol: str, is_buy: bool, size: float,
                    slippage_bps: int = 10) -> dict:
        """Place an IOC limit order that behaves like a market order."""
        mid = self._mid_price(symbol)
        slippage = mid * (slippage_bps / 10_000)
        limit = mid + slippage if is_buy else mid - slippage
        limit = self._round_price(symbol, limit)
        size = self._round_size(symbol, size)

        result = self.exchange.order(
            symbol, is_buy, size, limit,
            {"limit": {"tif": "Ioc"}}
        )
        logger.info(f"{'BUY' if is_buy else 'SELL'} {size} {symbol} @ ~{limit}")
        return result

    def market_close(self, symbol: str) -> dict:
        """Close an existing position."""
        for pos in self.get_positions():
            if pos["symbol"] == symbol:
                is_buy = pos["direction"] == "short"  # reverse direction to close
                return self.market_open(symbol, is_buy, abs(pos["size"]))
        logger.warning(f"No open position for {symbol} to close")
        return {}

    def place_tp_sl(self, symbol: str, is_long: bool, tp: float, sl: float):
        """Place take-profit and stop-loss trigger orders."""
        tp = self._round_price(symbol, tp)
        sl = self._round_price(symbol, sl)

        # Take profit
        self.exchange.order(
            symbol, not is_long, 0, tp,
            {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}},
            reduce_only=True,
        )
        # Stop loss
        self.exchange.order(
            symbol, not is_long, 0, sl,
            {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        logger.info(f"{symbol} TP={tp} SL={sl}")

    def cancel_triggers(self, symbol: str):
        """Cancel all open trigger orders for a symbol."""
        for order in self.info.open_orders(self.address):
            if order.get("coin") == symbol:
                try:
                    self.exchange.cancel(symbol, order["oid"])
                except Exception as e:
                    logger.error(f"cancel_triggers error: {e}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _mid_price(self, symbol: str) -> float:
        book = self.info.l2_snapshot(symbol)
        bid = float(book["levels"][0][0]["px"])
        ask = float(book["levels"][1][0]["px"])
        return (bid + ask) / 2

    def _get_asset_meta(self, symbol: str) -> dict:
        if self._meta_cache is None:
            self._meta_cache = self.info.meta()
        for asset in self._meta_cache["universe"]:
            if asset["name"] == symbol:
                return asset
        return {}

    def _round_price(self, symbol: str, price: float) -> float:
        asset = self._get_asset_meta(symbol)
        decimals = max(asset.get("szDecimals", 2), 1)
        return round(price, decimals)

    def _round_size(self, symbol: str, size: float) -> float:
        asset = self._get_asset_meta(symbol)
        decimals = asset.get("szDecimals", 4)
        return round(size, decimals)
