---
title: "Risk Disclaimer"
version: "1.0.0"
effective_date: "2026-04-20"
requires_acceptance: true
category: "trading"
display_order: 4
---

# Risk Disclaimer

> **NOT LEGAL ADVICE** — This document is a template and does not constitute legal advice. Operators must have qualified legal counsel review and customize these documents for their jurisdiction before deployment.

## 1. General Trading Risks

Trading in financial instruments involves substantial risk of loss and is not suitable for every investor. The value of investments may go down as well as up, and you may not get back the amount originally invested. Past performance is not indicative of future results.

{{OPERATOR_NAME}} provides a software platform for backtesting, paper trading, and live trading. The platform does not provide investment advice, and nothing in this disclaimer or on the platform should be construed as a recommendation to buy, sell, or hold any financial instrument.

## 2. Backtesting Limitations

Backtesting results are hypothetical and have inherent limitations:

- **Look-ahead bias**: Strategy logic may inadvertently use future information that would not have been available at the time of the trade, inflating performance results.
- **Survivorship bias**: Backtests may only include currently listed securities, ignoring delisted or bankrupt companies, which overstates historical returns.
- **Transaction costs**: Backtesting engines may not fully account for commissions, slippage, market impact, borrow fees, or liquidity constraints that would reduce real-world returns.
- **Fill assumptions**: Orders in backtests assume execution at specified prices, which may not be achievable in live markets due to market microstructure effects.

**Backtesting results should not be interpreted as a guarantee or prediction of future performance.**

## 3. Paper Trading vs. Live Trading

Paper (simulated) trading is provided for educational and testing purposes. There are significant differences between paper and live trading:

- Paper trading does not involve real capital and cannot replicate the psychological effects of real financial risk.
- Execution in paper trading is simulated and may not reflect real market conditions, order routing, or fill quality.
- Slippage, partial fills, and market impact are modeled approximately and may differ materially from live conditions.
- Market data feeds in paper trading may be delayed or differ from live data.

**Success in paper trading does not imply success in live trading.**

## 4. Marketplace Strategy Authorship

{{OPERATOR_NAME}} may host a marketplace where third-party authors publish trading strategies. Users should be aware that:

- Strategy authors are independent and not affiliated with {{OPERATOR_NAME}} unless explicitly stated.
- {{OPERATOR_NAME}} does not audit, verify, or guarantee the performance, safety, or correctness of marketplace strategies.
- Strategy performance metrics displayed on the platform are based on historical data and are subject to the backtesting limitations described in Section 2.
- Users assume full responsibility for evaluating and using any marketplace strategy.

## 5. Data Provider Limitations

Market data used by the platform is provided by third-party data providers. Users should understand that:

- Data may contain errors, omissions, or delays.
- Historical data may be adjusted for corporate actions (splits, dividends) using different methodologies.
- Data coverage may vary by security, exchange, and time period.
- {{OPERATOR_NAME}} is not responsible for the accuracy, completeness, or timeliness of third-party data.

## 6. Contact

For questions about this disclaimer, contact {{OPERATOR_EMAIL}}.

---

> *This document is a template and does not constitute legal advice. Operators must have qualified legal counsel review and customize these documents for their jurisdiction before deployment.*

**Effective Date**: {{EFFECTIVE_DATE}}
**Operator**: {{OPERATOR_NAME}}
**Jurisdiction**: {{JURISDICTION}}
