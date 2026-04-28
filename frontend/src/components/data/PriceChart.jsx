import React from "react";
import {
  Bar,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const UP_COLOR = "var(--success, #16a34a)";
const DOWN_COLOR = "var(--accent, #ef4444)";
const LINE_COLOR = "var(--text-display, #e5e7eb)";

function Candle(props) {
  // Recharts gives us pixel-space x/y/width/height for the rendered Bar.
  // The Bar's dataKey returns [low, high] so y is the pixel of `high` and
  // height spans the wick. Use the original OHLC values from payload to
  // place the body correctly within those bounds.
  const { x, y, width, height, payload } = props;
  if (!payload) return null;
  const { open, close, high, low } = payload;
  if (open == null || close == null || high == null || low == null) return null;

  const priceRange = high - low;
  if (priceRange <= 0) {
    // Doji-only bar — render a thin tick.
    const cx = x + width / 2;
    return (
      <line
        x1={cx - width / 2}
        y1={y}
        x2={cx + width / 2}
        y2={y}
        stroke={LINE_COLOR}
        strokeWidth={1}
      />
    );
  }

  const pxPerPrice = height / priceRange;
  const cx = x + width / 2;
  const isUp = close >= open;
  const color = isUp ? UP_COLOR : DOWN_COLOR;

  const bodyTopPrice = Math.max(open, close);
  const bodyBottomPrice = Math.min(open, close);
  const bodyTopPx = y + (high - bodyTopPrice) * pxPerPrice;
  const bodyBottomPx = y + (high - bodyBottomPrice) * pxPerPrice;
  const bodyHeight = Math.max(1, bodyBottomPx - bodyTopPx);

  // Wick: vertical line spanning low→high (i.e. the full bar y range).
  return (
    <g>
      <line
        x1={cx}
        y1={y}
        x2={cx}
        y2={y + height}
        stroke={color}
        strokeWidth={1}
      />
      <rect
        x={x}
        y={bodyTopPx}
        width={width}
        height={bodyHeight}
        fill={color}
      />
    </g>
  );
}

function formatTimestamp(value) {
  if (!value) return "";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return value;
  }
}

function ChartTooltip({ active, payload }) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="bg-nx-surface border border-nx-border rounded-md p-sm text-caption font-mono text-nx-text-primary">
      <div className="text-nx-text-secondary mb-xs">{formatTimestamp(row.timestamp)}</div>
      <div className="grid grid-cols-2 gap-x-md gap-y-xs tabular-nums">
        <span className="text-nx-text-disabled">O</span>
        <span>{Number(row.open).toFixed(2)}</span>
        <span className="text-nx-text-disabled">H</span>
        <span>{Number(row.high).toFixed(2)}</span>
        <span className="text-nx-text-disabled">L</span>
        <span>{Number(row.low).toFixed(2)}</span>
        <span className="text-nx-text-disabled">C</span>
        <span>{Number(row.close).toFixed(2)}</span>
        <span className="text-nx-text-disabled">V</span>
        <span>{Number(row.volume).toLocaleString()}</span>
      </div>
    </div>
  );
}

export function PriceChart({ bars, mode = "line", height = 380 }) {
  // Recharts' Bar with dataKey returning [low, high] makes the rendered Bar
  // span the full vertical price range — that's the wick. The custom shape
  // then paints the OHLC body on top. Memoise the row mapping so toggling
  // line/candle doesn't churn references in tooltips.
  const data = React.useMemo(() => bars || [], [bars]);

  if (!data.length) {
    return (
      <div
        className="flex items-center justify-center text-nx-text-disabled font-mono text-label uppercase border border-nx-border rounded-2xl"
        style={{ height }}
      >
        No data
      </div>
    );
  }

  const wickRange = (d) => [d.low, d.high];

  return (
    <div style={{ width: "100%" }}>
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 16, right: 24, left: 0, bottom: 8 }}
          >
            <XAxis
              dataKey="timestamp"
              tickFormatter={formatTimestamp}
              stroke="var(--text-disabled, #6b7280)"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              minTickGap={48}
            />
            <YAxis
              domain={["dataMin", "dataMax"]}
              stroke="var(--text-disabled, #6b7280)"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              orientation="right"
              width={64}
              tickFormatter={(v) => Number(v).toFixed(2)}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--border, #374151)" }} />
            {mode === "line" ? (
              <Line
                type="monotone"
                dataKey="close"
                stroke={LINE_COLOR}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            ) : (
              <Bar dataKey={wickRange} shape={<Candle />} isAnimationActive={false} />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div style={{ height: 80 }}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 4, right: 24, left: 0, bottom: 8 }}
          >
            <XAxis dataKey="timestamp" hide />
            <YAxis hide domain={[0, "dataMax"]} />
            <Tooltip content={<ChartTooltip />} cursor={false} />
            <Bar dataKey="volume" isAnimationActive={false}>
              {data.map((row, i) => {
                const prev = i > 0 ? data[i - 1] : row;
                const up = row.close >= prev.close;
                return (
                  <Cell
                    key={`v-${row.timestamp}`}
                    fill={up ? UP_COLOR : DOWN_COLOR}
                    fillOpacity={0.55}
                  />
                );
              })}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
