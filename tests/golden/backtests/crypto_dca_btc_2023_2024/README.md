# Crypto DCA BTC 2023-2024

Dollar-cost averaging strategy that buys a fixed quantity of BTC every
7 bars. Tests repeated buy orders without sells, cumulative position
tracking, and cost accumulation over an extended period.

- **Strategy**: DCAStrategy (buy 10 shares every 7 bars)
- **Data**: Synthetic BTC-like, seed=401, ~550 bars, high volatility
- **Warmup**: 5 bars (minimal)
- **Capital**: $100,000 initial
- **Note**: Uses standard equity calendar; crypto 365-day calendar
  support pending (engine issue #112)
