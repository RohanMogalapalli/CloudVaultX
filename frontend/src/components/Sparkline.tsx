interface SparklineProps {
  values: number[];
  color?: string;
  height?: number;
  max?: number;
}

/** Minimal inline SVG sparkline; no charting dependency needed. */
export default function Sparkline({ values, color = "#4ade80", height = 34, max }: SparklineProps) {
  if (values.length < 2) {
    return <div className="sparkline empty">not enough samples yet</div>;
  }

  const width = 160;
  const autoCeiling = Math.max(...values) * 1.15;
  const ceiling = max ?? (autoCeiling > 0 ? autoCeiling : 1);
  const step = width / (values.length - 1);
  const points = values
    .map((value, index) => {
      const x = index * step;
      const y = height - (Math.max(0, value) / ceiling) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      <polyline points={points} fill="none" stroke={color} strokeWidth="2" />
    </svg>
  );
}
