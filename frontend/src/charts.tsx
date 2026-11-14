/**
 * Hand-written SVG charts. No charting library: every shape here is a path or a circle,
 * and a dependency would cost more bytes than the whole file while fighting the design tokens.
 * All of them read colours from CSS variables so light/dark and theme edits stay in one place.
 */
import { useId } from "react";

const clamp01 = (n: number) => Math.min(1, Math.max(0, n));

/** Catmull-Rom → cubic Bézier: the reference's curves are smooth, not polylines. */
function smoothPath(pts: [number, number][]) {
  if (pts.length < 2) return "";
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] ?? p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${c1x} ${c1y} ${c2x} ${c2y} ${p2[0]} ${p2[1]}`;
  }
  return d;
}

function scale(values: number[], w: number, h: number, pad = 2): [number, number][] {
  if (!values.length) return [];
  const max = Math.max(...values);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  return values.map((v, i) => [i * step, h - pad - ((v - min) / span) * (h - pad * 2)]);
}

/** The small trend line under a KPI number. */
export function Sparkline({
  values,
  tone = "positive",
  height = 34,
}: {
  values: number[];
  tone?: "positive" | "negative" | "ink";
  height?: number;
}) {
  const id = useId();
  const w = 160;
  const stroke =
    tone === "positive" ? "var(--color-positive)" : tone === "negative" ? "var(--color-negative)" : "var(--color-ink)";
  if (values.length < 2) return <div style={{ height }} />;
  const pts = scale(values, w, height, 3);
  const line = smoothPath(pts);
  return (
    <svg viewBox={`0 0 ${w} ${height}`} height={height} className="w-full" preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line} L ${w} ${height} L 0 ${height} Z`} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke={stroke} strokeWidth="2" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export type Marker = { index: number; label: string; tone: "positive" | "negative" | "ink" };

/** The large balance chart: smooth area, dotted day rail, markers on the events that mattered. */
export function AreaChart({
  values,
  labels,
  markers = [],
  height = 260,
}: {
  values: number[];
  labels: string[];
  markers?: Marker[];
  height?: number;
}) {
  const id = useId();
  const w = 1000;
  const chartH = height - 46;
  if (values.length < 2) {
    return <div className="grid h-[200px] place-items-center text-[13px] text-ink-soft">Not enough history yet.</div>;
  }
  const pts = scale(values, w, chartH, 10);
  const line = smoothPath(pts);
  const every = Math.max(1, Math.round(labels.length / 12));

  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="w-full" style={{ height }} aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-positive)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--color-positive)" stopOpacity="0" />
        </linearGradient>
      </defs>

      <path d={`${line} L ${w} ${chartH} L 0 ${chartH} Z`} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke="var(--color-positive)" strokeWidth="2.5" strokeLinecap="round" />

      {/* Dotted day rail, exactly as many dots as there are buckets. */}
      {pts.map(([x], i) => (
        <circle key={i} cx={x} cy={chartH + 16} r="3" fill="var(--color-line)" />
      ))}

      {labels.map((l, i) =>
        i % every === 0 ? (
          <text key={l + i} x={pts[i][0]} y={chartH + 38} textAnchor="middle" className="fill-ink-soft" fontSize="11">
            {l}
          </text>
        ) : null,
      )}

      {markers.map((m) => {
        const p = pts[m.index];
        if (!p) return null;
        const fill =
          m.tone === "positive" ? "var(--color-positive)" : m.tone === "negative" ? "var(--color-negative)" : "var(--color-ink)";
        return (
          <g key={m.label + m.index}>
            <line x1={p[0]} y1={p[1]} x2={p[0]} y2={chartH} stroke={fill} strokeOpacity="0.15" strokeWidth="1" />
            <circle cx={p[0]} cy={p[1]} r="5" fill="var(--color-surface)" stroke={fill} strokeWidth="2.5" />
            <circle cx={p[0]} cy={chartH + 16} r="4" fill={fill} />
          </g>
        );
      })}
    </svg>
  );
}

/** The progress ring next to a percentage, as on the reference's margin / health tiles. */
export function Ring({
  value,
  size = 44,
  tone = "positive",
  children,
}: {
  value: number;
  size?: number;
  tone?: "positive" | "warn" | "negative";
  children?: React.ReactNode;
}) {
  const stroke = 4;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const colour =
    tone === "positive" ? "var(--color-positive)" : tone === "warn" ? "var(--color-warn)" : "var(--color-negative)";
  return (
    <span className="relative inline-grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--color-line)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={colour}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - clamp01(value))}
        />
      </svg>
      {children ? <span className="num absolute text-[11px] font-semibold">{children}</span> : null}
    </span>
  );
}

/** Rounded columns, the reference's "last 3 months" block. */
export function Bars({
  data,
  height = 150,
}: {
  data: { label: string; value: number; caption?: string }[];
  height?: number;
}) {
  const max = Math.max(...data.map((d) => Math.abs(d.value)), 1);
  return (
    <div className="flex items-end gap-3" style={{ height }}>
      {data.map((d) => {
        const h = Math.max(6, (Math.abs(d.value) / max) * (height - 46));
        const positive = d.value >= 0;
        return (
          <div key={d.label} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-2">
            <div
              className={`w-full rounded-[12px] ${
                positive
                  ? "bg-gradient-to-b from-positive to-positive/55"
                  : "bg-chip"
              }`}
              style={{ height: h }}
              title={`${d.label}: ${d.caption ?? d.value}`}
            />
            <div className="text-center">
              <div className="text-[11px] text-ink-soft">{d.label}</div>
              <div className="num text-[12px] font-medium">{d.caption ?? d.value}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function Progress({ value, tone = "ink" }: { value: number; tone?: "ink" | "positive" | "warn" }) {
  const colour = tone === "positive" ? "bg-positive" : tone === "warn" ? "bg-warn" : "bg-ink";
  return (
    <span className="block h-1.5 w-full overflow-hidden rounded-full bg-chip">
      <span className={`block h-full rounded-full ${colour}`} style={{ width: `${clamp01(value) * 100}%` }} />
    </span>
  );
}

const AVATAR_TONES = [
  "bg-[#1f2937] text-white",
  "bg-positive-soft text-positive",
  "bg-warn-soft text-warn",
  "bg-chip text-ink",
  "bg-negative-soft text-negative",
];

export function Avatar({ name, size = 32 }: { name: string; size?: number }) {
  const initials = name
    .replace(/^(agent|human|system):/, "")
    .split(/[\s\-_.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join("");
  // Stable colour per name, so the same agent keeps its swatch across pages.
  const tone = AVATAR_TONES[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % AVATAR_TONES.length];
  return (
    <span
      className={`grid shrink-0 place-items-center rounded-full font-semibold ${tone}`}
      style={{ width: size, height: size, fontSize: size * 0.36 }}
      title={name}
    >
      {initials || "?"}
    </span>
  );
}
