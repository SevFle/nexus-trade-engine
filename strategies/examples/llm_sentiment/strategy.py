"""
LLM Sentiment Strategy — Example Plugin

Demonstrates calling external LLM APIs from a Nexus strategy.
Analyzes news sentiment to make allocation decisions.

The developer's API key is stored in the encrypted secrets vault
and passed via StrategyConfig.secrets.
"""

from __future__ import annotations

import json

import httpx
import structlog

try:
    from core.portfolio import PortfolioSnapshot
    from core.signal import Signal, SignalStrength
    from plugins.sdk import DataFeed, IStrategy, MarketState, StrategyConfig
except ImportError:
    from nexus_sdk import (
        DataFeed,
        IStrategy,
        MarketState,
        PortfolioSnapshot,
        Signal,
        SignalStrength,
        StrategyConfig,
    )

logger = structlog.get_logger()

SENTIMENT_PROMPT = (
    """Analyze the following news headlines for {symbol} """
    """and return a JSON object with:
- "score": a float from -1.0 (very bearish) to 1.0 (very bullish)
- "confidence": a float from 0.0 to 1.0
- "reasoning": a brief explanation

Headlines:
{headlines}

Respond ONLY with valid JSON, no other text."""
)


class LLMSentimentStrategy(IStrategy):
    """
    LLM-powered sentiment analysis strategy.

    Shows the full pattern:
    1. Receive news from MarketState
    2. Send to LLM for analysis
    3. Parse sentiment scores
    4. Generate cost-aware signals
    """

    def __init__(self):
        self._config: StrategyConfig | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._provider = "anthropic"
        self._model = "claude-sonnet-4-20250514"
        self._api_key = ""
        self._sentiment_threshold = 0.6
        self._max_allocation = 0.15
        self._watchlist = []
        self._cached_sentiments: dict[str, dict] = {}

    @property
    def id(self) -> str:
        return "llm-sentiment-alpha"

    @property
    def name(self) -> str:
        return "LLM Sentiment Alpha"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def author(self) -> str:
        return "Nexus Team"

    async def initialize(self, config: StrategyConfig) -> None:
        self._config = config
        params = config.params

        self._provider = params.get("llm_provider", "anthropic")
        self._model = params.get("model_name", "claude-sonnet-4-20250514")
        self._api_key = config.secrets.get("llm_api_key", "")
        self._sentiment_threshold = params.get("sentiment_threshold", 0.6)
        self._max_allocation = params.get("max_allocation_pct", 0.15)
        self._watchlist = params.get("watchlist", ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])

        if not self._api_key:
            logger.warning(
                "llm_sentiment.no_api_key",
                note="Set 'llm_api_key' in strategy secrets",
            )

        self._http_client = httpx.AsyncClient(timeout=30.0)
        logger.info("llm_sentiment.initialized", provider=self._provider, model=self._model)

    async def dispose(self) -> None:
        if self._http_client:
            await self._http_client.aclose()

    async def evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: MarketState,
        costs,
    ) -> list[Signal]:
        signals = []
        news = market.get_news(hours=24)

        if not news:
            return signals

        # Group news by symbol
        news_by_symbol: dict[str, list[str]] = {}
        for item in news:
            symbols = item.get("symbols", [])
            headline = item.get("headline", "")
            for sym in symbols:
                if sym in self._watchlist:
                    news_by_symbol.setdefault(sym, []).append(headline)

        # Analyze sentiment for each symbol with news
        for symbol, headlines in news_by_symbol.items():
            sentiment = await self._analyze_sentiment(symbol, headlines)
            if sentiment is None:
                continue

            score = sentiment.get("score", 0.0)
            confidence = sentiment.get("confidence", 0.0)
            price = market.latest(symbol)
            if price is None:
                continue

            has_position = portfolio.has_position(symbol)

            # BUY: strong positive sentiment
            if (
                score > self._sentiment_threshold
                and confidence > 0.5  # noqa: PLR2004
                and not has_position
            ):
                cost_pct = costs.estimate_pct(symbol, price, "buy")
                weight = min(self._max_allocation, confidence * self._max_allocation)

                signals.append(Signal.buy(
                    symbol=symbol,
                    strategy_id=self.id,
                    weight=weight,
                    strength=(
                        SignalStrength.STRONG
                        if confidence > 0.8  # noqa: PLR2004
                        else SignalStrength.MODERATE
                    ),
                    reason=(
                        f"LLM sentiment={score:.2f}, "
                        f"confidence={confidence:.2f}: "
                        f"{sentiment.get('reasoning', '')}"
                    ),
                    metadata={"sentiment": sentiment, "cost_pct": cost_pct},
                ))

            # SELL: strong negative sentiment on existing position
            elif score < -self._sentiment_threshold and has_position:
                signals.append(Signal.sell(
                    symbol=symbol,
                    strategy_id=self.id,
                    reason=f"Negative sentiment={score:.2f}: {sentiment.get('reasoning', '')}",
                    metadata={"sentiment": sentiment},
                ))

        return signals

    async def _analyze_sentiment(self, symbol: str, headlines: list[str]) -> dict | None:
        """Call LLM API to analyze news sentiment."""
        if not self._api_key or not self._http_client:
            return None

        prompt = SENTIMENT_PROMPT.format(
            symbol=symbol,
            headlines="\n".join(f"- {h}" for h in headlines[:10]),  # Limit to 10 headlines
        )

        try:
            if self._provider == "anthropic":
                response = await self._http_client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                data = response.json()
                text = data.get("content", [{}])[0].get("text", "{}")
                return json.loads(text)

            if self._provider == "openai":
                response = await self._http_client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 256,
                    },
                )
                data = response.json()
                text = data["choices"][0]["message"]["content"]
                return json.loads(text)

        except Exception as e:
            logger.exception("llm_sentiment.api_error", symbol=symbol, error=str(e))
            return None

    def get_config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "llm_provider": {
                    "type": "string",
                    "enum": ["anthropic", "openai"],
                    "default": "anthropic",
                },
                "model_name": {"type": "string", "default": "claude-sonnet-4-20250514"},
                "sentiment_threshold": {"type": "number", "default": 0.6},
                "max_allocation_pct": {"type": "number", "default": 0.15},
            },
        }

    def get_required_data_feeds(self):
        return [
            DataFeed(feed_type="ohlcv", symbols=self._watchlist),
            DataFeed(feed_type="news", symbols=self._watchlist),
        ]

    def get_watchlist(self) -> list[str]:
        return self._watchlist
