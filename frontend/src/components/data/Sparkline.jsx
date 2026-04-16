import { Line, LineChart, ResponsiveContainer } from "recharts";

export function Sparkline({ data, color = "var(--text-display)", height = 32, className }) {
  if (!data || data.length === 0) return null;

  return (
    <div className={className} style={{ height, width: "100%" }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <Line
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
