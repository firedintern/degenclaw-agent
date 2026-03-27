"""QuantAgent LLM configuration — Claude Haiku for all agents (fast + cost-efficient)."""
import os
from dotenv import load_dotenv

load_dotenv()

QUANTAGENT_CONFIG = {
    # LLM provider
    "agent_llm_provider": "anthropic",
    "graph_llm_provider": "anthropic",

    # claude-haiku-4-5 for all agents — fast, cheap, consistent signals at temperature 0.1
    "agent_llm_model": "claude-haiku-4-5-20251001",
    "graph_llm_model": "claude-haiku-4-5-20251001",

    # Low temperature = deterministic, consistent trading signals
    "agent_llm_temperature": 0.1,
    "graph_llm_temperature": 0.1,

    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),

    # Trading context passed to QuantAgent
    "stock_name": "BTC",
    "time_frame": "4hour",

    # 40-50 bars recommended by QuantAgent paper
    "lookback_bars": 50,
}
