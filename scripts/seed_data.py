"""
Seed sample market data for development and testing.

Downloads historical OHLCV data for common symbols and stores
it in the database for backtest use.
"""

import asyncio
import sys
from datetime import datetime

import yfinance as yf
import pandas as pd
from sqlalchemy import text

# Add engine to path
sys.path.insert(0, "../engine")
from config import get_settings
from db.session import engine as db_engine


DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "NVDA", "TSLA", "JPM", "V", "JNJ",
    "SPY", "QQQ", "IWM", "TLT", "GLD",
]

DEFAULT_PERIOD = "5y"
DEFAULT_INTERVAL = "1d"


async def seed_ohlcv(symbols: list[str] = None, period: str = DEFAULT_PERIOD):
    """Download and store OHLCV data."""
    symbols = symbols or DEFAULT_SYMBOLS
    print(f"Seeding OHLCV data for {len(symbols)} symbols, period={period}")

    for symbol in symbols:
        try:
            print(f"  Downloading {symbol}...", end=" ")
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=DEFAULT_INTERVAL)

            if df.empty:
                print("NO DATA")
                continue

            # Insert into database
            async with db_engine.begin() as conn:
                for idx, row in df.iterrows():
                    await conn.execute(
                        text("""
                            INSERT INTO ohlcv_bars (symbol, timestamp, interval, open, high, low, close, volume)
                            VALUES (:symbol, :ts, :interval, :open, :high, :low, :close, :volume)
                            ON CONFLICT DO NOTHING
                        """),
                        {
                            "symbol": symbol,
                            "ts": idx.to_pydatetime(),
                            "interval": DEFAULT_INTERVAL,
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": int(row["Volume"]),
                        },
                    )

            print(f"{len(df)} bars")
        except Exception as e:
            print(f"ERROR: {e}")

    print("Done!")


if __name__ == "__main__":
    asyncio.run(seed_ohlcv())
