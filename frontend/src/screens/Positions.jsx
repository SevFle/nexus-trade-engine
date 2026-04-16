import { useState } from "react";
import { StatRow } from "../components/primitives/StatRow";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { InlineStatus } from "../components/feedback/InlineStatus";

const MOCK_POSITIONS = [
  { symbol: "AAPL", side: "LONG", qty: 500, avgPrice: 178.42, current: 191.04, pnl: 6310.00, pnlPct: 3.54 },
  { symbol: "MSFT", side: "LONG", qty: 300, avgPrice: 412.80, current: 428.51, pnl: 4713.00, pnlPct: 3.80 },
  { symbol: "TSLA", side: "SHORT", qty: 150, avgPrice: 248.90, current: 238.14, pnl: 1614.00, pnlPct: 4.32 },
  { symbol: "NVDA", side: "LONG", qty: 200, avgPrice: 875.30, current: 912.45, pnl: 7430.00, pnlPct: 4.24 },
  { symbol: "AMZN", side: "LONG", qty: 400, avgPrice: 178.65, current: 186.52, pnl: 3148.00, pnlPct: 4.40 },
  { symbol: "META", side: "SHORT", qty: 100, avgPrice: 502.10, current: 518.33, pnl: -1623.00, pnlPct: -3.23 },
  { symbol: "GOOGL", side: "LONG", qty: 250, avgPrice: 155.20, current: 162.88, pnl: 1920.00, pnlPct: 4.95 },
  { symbol: "SPY", side: "LONG", qty: 1000, avgPrice: 512.40, current: 519.87, pnl: 7470.00, pnlPct: 1.46 },
];

const MOCK_ORDERS = [
  { id: "ORD-4821", time: "14:32:07", symbol: "AAPL", side: "BUY", qty: 100, price: 190.88, status: "filled" },
  { id: "ORD-4820", time: "14:28:41", symbol: "TSLA", side: "SELL", qty: 50, price: 239.10, status: "filled" },
  { id: "ORD-4819", time: "13:55:12", symbol: "NVDA", side: "BUY", qty: 50, price: 908.22, status: "filled" },
  { id: "ORD-4818", time: "11:04:33", symbol: "META", side: "SELL", qty: 25, price: 517.90, status: "cancelled" },
];

const TOTAL_EXPOSURE = MOCK_POSITIONS.reduce((sum, p) => sum + p.current * p.qty, 0);
const TOTAL_PNL = MOCK_POSITIONS.reduce((sum, p) => sum + p.pnl, 0);

export default function Positions() {
  const [showOrders, setShowOrders] = useState(false);

  const pnlStatus = (val) => (val > 0 ? "success" : val < 0 ? "error" : "neutral");

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-7xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            POSITIONS & ORDERS
          </span>
          <h1 className="text-display-md font-display text-nx-text-display">
            OPEN POSITIONS
          </h1>
        </header>

        <section className="mb-2xl">
          <div className="grid grid-cols-3 gap-md">
            <div className="bg-nx-surface border border-nx-border rounded-2xl p-lg">
              <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
                TOTAL EXPOSURE
              </span>
              <span className="text-heading font-display text-nx-text-display tabular-nums">
                ${TOTAL_EXPOSURE.toLocaleString("en-US", { minimumFractionDigits: 2 })}
              </span>
            </div>
            <div className="bg-nx-surface border border-nx-border rounded-2xl p-lg">
              <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
                POSITION COUNT
              </span>
              <span className="text-heading font-display text-nx-text-display tabular-nums">
                {MOCK_POSITIONS.length}
              </span>
            </div>
            <div className="bg-nx-surface border border-nx-border rounded-2xl p-lg">
              <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
                UNREALIZED P&L
              </span>
              <span className={`text-heading font-display tabular-nums ${TOTAL_PNL >= 0 ? "text-nx-success" : "text-nx-accent"}`}>
                {TOTAL_PNL >= 0 ? "+" : ""}${TOTAL_PNL.toLocaleString("en-US", { minimumFractionDigits: 2 })}
              </span>
            </div>
          </div>
        </section>

        <section className="mb-2xl">
          <div className="border border-nx-border rounded-2xl overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-nx-border">
                  {["SYMBOL", "SIDE", "QTY", "AVG PRICE", "CURRENT", "P&L ($)", "P&L (%)"].map(
                    (h) => (
                      <th
                        key={h}
                        className={`text-label font-mono uppercase text-nx-text-secondary py-md px-md ${
                          h === "QTY" || h === "AVG PRICE" || h === "CURRENT" || h === "P&L ($)" || h === "P&L (%)"
                            ? "text-right"
                            : "text-left"
                        }`}
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {MOCK_POSITIONS.map((pos) => (
                  <tr key={pos.symbol} className="border-b border-nx-border last:border-b-0">
                    <td className="text-body-sm font-body text-nx-text-primary py-md px-md">
                      {pos.symbol}
                    </td>
                    <td className="py-md px-md">
                      <span
                        className={`text-label font-mono uppercase ${
                          pos.side === "LONG" ? "text-nx-success" : "text-nx-accent"
                        }`}
                      >
                        {pos.side}
                      </span>
                    </td>
                    <td className="text-body-sm font-mono text-nx-text-primary tabular-nums py-md px-md text-right">
                      {pos.qty.toLocaleString()}
                    </td>
                    <td className="text-body-sm font-mono text-nx-text-primary tabular-nums py-md px-md text-right">
                      ${pos.avgPrice.toFixed(2)}
                    </td>
                    <td className="text-body-sm font-mono text-nx-text-primary tabular-nums py-md px-md text-right">
                      ${pos.current.toFixed(2)}
                    </td>
                    <td
                      className={`text-body-sm font-mono tabular-nums py-md px-md text-right ${
                        pos.pnl >= 0 ? "text-nx-success" : "text-nx-accent"
                      }`}
                    >
                      {pos.pnl >= 0 ? "+" : ""}${pos.pnl.toLocaleString("en-US", { minimumFractionDigits: 2 })}
                    </td>
                    <td
                      className={`text-body-sm font-mono tabular-nums py-md px-md text-right ${
                        pos.pnl >= 0 ? "text-nx-success" : "text-nx-accent"
                      }`}
                    >
                      {pos.pnlPct >= 0 ? "+" : ""}{pos.pnlPct.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <button
            type="button"
            onClick={() => setShowOrders(!showOrders)}
            className="flex items-center gap-sm text-label font-mono uppercase text-nx-text-secondary hover:text-nx-text-primary transition-colors mb-md"
          >
            <span>{showOrders ? "-" : "+"}</span>
            <span>ORDER HISTORY</span>
            <span className="text-nx-text-disabled">({MOCK_ORDERS.length})</span>
          </button>
          {showOrders && (
            <div className="border border-nx-border rounded-2xl overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-nx-border">
                    {["ORDER ID", "TIME", "SYMBOL", "SIDE", "QTY", "PRICE", "STATUS"].map(
                      (h) => (
                        <th
                          key={h}
                          className={`text-label font-mono uppercase text-nx-text-secondary py-md px-md text-left`}
                        >
                          {h}
                        </th>
                      )
                    )}
                  </tr>
                </thead>
                <tbody>
                  {MOCK_ORDERS.map((order) => (
                    <tr key={order.id} className="border-b border-nx-border last:border-b-0">
                      <td className="text-body-sm font-mono text-nx-text-disabled py-md px-md">
                        {order.id}
                      </td>
                      <td className="text-body-sm font-mono text-nx-text-secondary tabular-nums py-md px-md">
                        {order.time}
                      </td>
                      <td className="text-body-sm font-body text-nx-text-primary py-md px-md">
                        {order.symbol}
                      </td>
                      <td className="py-md px-md">
                        <span
                          className={`text-label font-mono uppercase ${
                            order.side === "BUY" ? "text-nx-success" : "text-nx-accent"
                          }`}
                        >
                          {order.side}
                        </span>
                      </td>
                      <td className="text-body-sm font-mono text-nx-text-primary tabular-nums py-md px-md">
                        {order.qty}
                      </td>
                      <td className="text-body-sm font-mono text-nx-text-primary tabular-nums py-md px-md">
                        ${order.price.toFixed(2)}
                      </td>
                      <td className="py-md px-md">
                        <StatusBadge
                          status={order.status === "filled" ? "ok" : "warning"}
                        >
                          {order.status.toUpperCase()}
                        </StatusBadge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
