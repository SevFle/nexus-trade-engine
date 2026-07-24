import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  PositionsTable,
  formatCurrency,
  formatSignedCurrency,
  formatQuantity,
  type Position,
} from "../PositionsTable";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const samplePositions: Position[] = [
  {
    symbol: "AAPL",
    quantity: 100,
    avg_cost: 150.25,
    market_value: 18500.5,
    unrealized_pnl: 3475.0, // gain -> positive tone
  },
  {
    symbol: "MSFT",
    quantity: 50,
    avg_cost: 300.0,
    market_value: 15500.0,
    unrealized_pnl: -500.0, // loss -> negative tone
  },
  {
    symbol: "TSLA",
    quantity: 12.5,
    avg_cost: 800.0,
    market_value: 10000.0,
    unrealized_pnl: 0.0, // breakeven -> neutral tone
  },
];

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

describe("PositionsTable — empty state", () => {
  it("renders the empty state when given an empty array", () => {
    render(<PositionsTable positions={[]} />);
    expect(screen.getByTestId("positions-empty")).toBeInTheDocument();
    // The table itself should not be rendered.
    expect(screen.queryByTestId("positions-table")).not.toBeInTheDocument();
    // ...but the empty state lives under an accessible region label.
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });

  it("does not render any position rows when empty", () => {
    render(<PositionsTable positions={[]} />);
    expect(screen.queryAllByTestId("position-row")).toHaveLength(0);
  });

  it("exposes an accessible empty-state region", () => {
    render(<PositionsTable positions={[]} />);
    expect(
      screen.getByRole("region", { name: "Open positions" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("No positions")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Basic render
// ---------------------------------------------------------------------------

describe("PositionsTable — basic render", () => {
  it("renders a row for each position", () => {
    render(<PositionsTable positions={samplePositions} />);
    expect(screen.getAllByTestId("position-row")).toHaveLength(3);
  });

  it("renders the table with an accessible label", () => {
    render(<PositionsTable positions={samplePositions} />);
    expect(
      screen.getByRole("region", { name: "Open positions" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("positions-table")).toBeInTheDocument();
  });

  it("renders each symbol", () => {
    render(<PositionsTable positions={samplePositions} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
  });

  it("renders a sortable header for every column", () => {
    render(<PositionsTable positions={samplePositions} />);
    expect(screen.getByTestId("th-symbol")).toBeInTheDocument();
    expect(screen.getByTestId("th-quantity")).toBeInTheDocument();
    expect(screen.getByTestId("th-avg_cost")).toBeInTheDocument();
    expect(screen.getByTestId("th-market_value")).toBeInTheDocument();
    expect(screen.getByTestId("th-unrealized_pnl")).toBeInTheDocument();
  });

  it("exposes screen-reader sort buttons for each column", () => {
    render(<PositionsTable positions={samplePositions} />);
    for (const label of [
      "Sort by Symbol",
      "Sort by Quantity",
      "Sort by Avg Cost",
      "Sort by Market Value",
      "Sort by Unrealized P&L",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });
});

// ---------------------------------------------------------------------------
// P&L coloring
// ---------------------------------------------------------------------------

describe("PositionsTable — P&L coloring", () => {
  it("applies the positive (success) tone to a gain", () => {
    render(<PositionsTable positions={samplePositions} />);
    const pnlCell = screen.getByTestId("pnl-AAPL");
    const span = pnlCell.querySelector("span");
    expect(span).not.toBeNull();
    expect(span!.className).toContain("text-nx-success");
    expect(span!.className).not.toContain("text-nx-accent");
  });

  it("applies the negative (accent) tone to a loss", () => {
    render(<PositionsTable positions={samplePositions} />);
    const pnlCell = screen.getByTestId("pnl-MSFT");
    const span = pnlCell.querySelector("span");
    expect(span).not.toBeNull();
    expect(span!.className).toContain("text-nx-accent");
    expect(span!.className).not.toContain("text-nx-success");
  });

  it("applies a neutral tone at breakeven (0 P&L)", () => {
    render(<PositionsTable positions={samplePositions} />);
    const pnlCell = screen.getByTestId("pnl-TSLA");
    const span = pnlCell.querySelector("span");
    expect(span).not.toBeNull();
    // Neutral => neither success nor accent; primary text colour only.
    expect(span!.className).toContain("text-nx-text-primary");
    expect(span!.className).not.toContain("text-nx-success");
    expect(span!.className).not.toContain("text-nx-accent");
  });

  it("renders the P&L value with an explicit sign", () => {
    render(<PositionsTable positions={samplePositions} />);
    // Positive gain carries a "+", the loss carries a "-". signDisplay
    // "always" also prefixes zero, so breakeven renders as "+$0.00".
    expect(screen.getByText("+$3,475.00")).toBeInTheDocument();
    expect(screen.getByText("-$500.00")).toBeInTheDocument();
    expect(screen.getByText("+$0.00")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Number formatting
// ---------------------------------------------------------------------------

describe("PositionsTable — number formatting", () => {
  it("formats currency values with Intl USD grouping", () => {
    render(<PositionsTable positions={samplePositions} />);
    // Avg cost: 150.25 -> $150.25
    expect(screen.getByText("$150.25")).toBeInTheDocument();
    // Market value: 18500.5 -> $18,500.50 (grouped thousands, 2 decimals)
    expect(screen.getByText("$18,500.50")).toBeInTheDocument();
  });

  it("formats quantities without a currency symbol", () => {
    render(<PositionsTable positions={samplePositions} />);
    // Whole-share quantity renders without decimals/currency.
    expect(screen.getByText("100")).toBeInTheDocument();
    // Fractional quantity renders up to 4 decimals, trimmed.
    expect(screen.getByText("12.5")).toBeInTheDocument();
  });

  it("formatCurrency formats and groups thousands with a dollar sign", () => {
    expect(formatCurrency(1234.5)).toBe("$1,234.50");
    expect(formatCurrency(0)).toBe("$0.00");
    expect(formatCurrency(1000000)).toBe("$1,000,000.00");
  });

  it("formatCurrency falls back to an em-dash for non-finite input", () => {
    expect(formatCurrency(Number.NaN)).toBe("—");
    expect(formatCurrency(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatCurrency(Number.NEGATIVE_INFINITY)).toBe("—");
  });

  it("formatSignedCurrency always carries an explicit sign", () => {
    expect(formatSignedCurrency(100)).toBe("+$100.00");
    expect(formatSignedCurrency(-100)).toBe("-$100.00");
    // signDisplay "always" prefixes every value, including zero, so 0
    // renders as "+$0.00" (use signDisplay "exceptZero" to omit it).
    expect(formatSignedCurrency(0)).toBe("+$0.00");
  });

  it("formatSignedCurrency falls back to an em-dash for non-finite input", () => {
    expect(formatSignedCurrency(Number.NaN)).toBe("—");
  });

  it("formatQuantity formats integers and fractions without a symbol", () => {
    expect(formatQuantity(100)).toBe("100");
    expect(formatQuantity(12.5)).toBe("12.5");
    expect(formatQuantity(12.34567)).toBe("12.3457"); // rounds to 4 dp
    expect(formatQuantity(0)).toBe("0");
  });

  it("formatQuantity falls back to an em-dash for non-finite input", () => {
    expect(formatQuantity(Number.NaN)).toBe("—");
    expect(formatQuantity(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

describe("PositionsTable — sorting", () => {
  it("defaults to sorting by market value descending", () => {
    render(<PositionsTable positions={samplePositions} />);
    const rows = screen.getAllByTestId("position-row");
    expect(rows[0]).toHaveAttribute("data-symbol", "AAPL"); // 18500.5
    expect(rows[1]).toHaveAttribute("data-symbol", "MSFT"); // 15500
    expect(rows[2]).toHaveAttribute("data-symbol", "TSLA"); // 10000

    // aria-sort reflects the active column + direction.
    expect(screen.getByTestId("th-market_value")).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("sorts numerically by avg_cost ascending when the header is toggled", () => {
    render(<PositionsTable positions={samplePositions} />);
    // First click on avg_cost: switches column, defaults to desc.
    fireEvent.click(screen.getByRole("button", { name: "Sort by Avg Cost" }));
    expect(screen.getByTestId("th-avg_cost")).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    let rows = screen.getAllByTestId("position-row");
    expect(rows.map((r) => r.getAttribute("data-symbol"))).toEqual([
      "TSLA",
      "MSFT",
      "AAPL",
    ]);

    // Second click flips to ascending.
    fireEvent.click(screen.getByRole("button", { name: "Sort by Avg Cost" }));
    expect(screen.getByTestId("th-avg_cost")).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    rows = screen.getAllByTestId("position-row");
    expect(rows.map((r) => r.getAttribute("data-symbol"))).toEqual([
      "AAPL",
      "MSFT",
      "TSLA",
    ]);
  });

  it("sorts alphabetically by symbol ascending on first click", () => {
    render(<PositionsTable positions={samplePositions} />);
    fireEvent.click(screen.getByRole("button", { name: "Sort by Symbol" }));
    expect(screen.getByTestId("th-symbol")).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    const rows = screen.getAllByTestId("position-row");
    expect(rows.map((r) => r.getAttribute("data-symbol"))).toEqual([
      "AAPL",
      "MSFT",
      "TSLA",
    ]);
  });

  it("sorts by unrealized P&L and re-orders rows", () => {
    render(<PositionsTable positions={samplePositions} />);
    // Default for numeric column is descending: biggest gain first.
    fireEvent.click(
      screen.getByRole("button", { name: "Sort by Unrealized P&L" }),
    );
    expect(screen.getByTestId("th-unrealized_pnl")).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(
      screen.getAllByTestId("position-row").map((r) =>
        r.getAttribute("data-symbol"),
      ),
    ).toEqual(["AAPL", "TSLA", "MSFT"]); // 3475, 0, -500
  });

  it("does not mutate the input positions array", () => {
    const input: Position[] = [
      { symbol: "B", quantity: 2, avg_cost: 2, market_value: 2, unrealized_pnl: 0 },
      { symbol: "A", quantity: 1, avg_cost: 1, market_value: 1, unrealized_pnl: 0 },
    ];
    const snapshot = input.map((p) => p.symbol);
    render(<PositionsTable positions={input} />);
    fireEvent.click(screen.getByRole("button", { name: "Sort by Symbol" }));
    // The original array order is preserved even after sorting in the table.
    expect(input.map((p) => p.symbol)).toEqual(snapshot);
  });
});
