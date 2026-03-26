"""QuantAgent configuration for DegenClaw — BTC only, Claude LLM."""
import os
from dotenv import load_dotenv

load_dotenv()

QUANTAGENT_CONFIG = {
    # LLM provider — Claude for both sub-agents and decision graph
    "agent_llm_provider": "anthropic",
    "graph_llm_provider": "anthropic",

    # Models — Haiku for fast/cheap sub-agents, Sonnet for final decision
    "agent_llm_model": "claude-haiku-4-5-20251001",
    "graph_llm_model": "claude-sonnet-4-6",

    # Low temperature = consistent, deterministic signals
    "agent_llm_temperature": 0.1,
    "graph_llm_temperature": 0.1,

    # API key — read from environment (set in .env)
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),

    # Trading context
    "stock_name": "BTC",
    "time_frame": "4hour",

    # Analysis window — 40-50 bars recommended by QuantAgent paper
    "lookback_bars": 50,
}
