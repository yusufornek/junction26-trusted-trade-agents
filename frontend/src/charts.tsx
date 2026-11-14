/**
 * Hand-written SVG charts. No charting library: every shape here is a path or a circle,
 * and a dependency would cost more bytes than the whole file while fighting the design tokens.
 * All of them read colours from CSS variables so light/dark and theme edits stay in one place.
 * Line charts measure their container and draw in real pixels: a stretched viewBox would
 * squash text and strokes at every width but one.
 */
import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";

const clamp01 = (n: number) => Math.min(1, Math.max(0, n));

type Tone = "positive" | "negative" | "warn" | "ink" | "brand" | "muted";
const TONE: Record<Tone, string> = {
  positive: "var(--color-positive)",
  negative: "var(--color-negative)",
  warn: "var(--color-warn)",
  ink: "var(--color-ink)",
  brand: "var(--color-brand)",
  // Cancelled / expired: grey, so an event that no longer counts never reads as a status colour.
  muted: "var(--color-ink-soft)",
};

/** Content width of a block element in CSS px, kept current while the layout changes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/**
 * Monotone cubic (Fritsch–Carlson, as d3's curveMonotoneX): smooth like the reference,
 * but it never overshoots — a running total must not appear to dip between two equal days.
 */
function smoothPath(pts: [number, number][]) {
  const n = pts.length;
  if (n < 2) return "";
  const s = pts.slice(1).map(([x, y], i) => (y - pts[i][1]) / (x - pts[i][0] || 1));
  const t = pts.map((_, i) => {
    if (i === 0 || i === n - 1) return 0;
    const h0 = pts[i][0] - pts[i - 1][0];
    const h1 = pts[i + 1][0] - pts[i][0];
    const p = (s[i - 1] * h1 + s[i] * h0) / (h0 + h1 || 1);
    return (Math.sign(s[i - 1]) + Math.sign(s[i])) * Math.min(Math.abs(s[i - 1]), Math.abs(s[i]), 0.5 * Math.abs(p)) || 0;
  });
  t[0] = n === 2 ? s[0] : (3 * s[0] - t[1]) / 2;
  t[n - 1] = n === 2 ? s[0] : (3 * s[n - 2] - t[n - 2]) / 2;
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    const dx = (x1 - x0) / 3;
    d += ` C ${x0 + dx} ${y0 + dx * t[i]} ${x1 - dx} ${y1 - dx * t[i + 1]} ${x1} ${y1}`;
  }
  return d;
}

/** Values → points inside the box [left, right] × [top, bottom]; zero is always on the scale. */
function scale(values: number[], left: number, right: number, top: number, bottom: number): [number, number][] {
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = values.length > 1 ? (right - left) / (values.length - 1) : 0;
  return values.map((v, i) => [left + i * step, bottom - ((v - min) / span) * (bottom - top)]);
}

/** The small trend line under a KPI number. */
export function Sparkline({
  values,
  tone = "positive",
  height = 34,
}: {
  values: number[];
  tone?: "positive" | "negative" | "ink" | "brand";
  height?: number;
}) {
  const id = useId();
  const [ref, w] = useWidth();
  const colour = TONE[tone];
  const draw = w > 0 && values.length > 1;
  // Inset by the stroke so the round caps and the peak are never cut by the box edge.
  const pts = draw ? scale(values, 1, w - 1, 3, height - 1.5) : [];
  const line = smoothPath(pts);
  return (
    <div ref={ref} className="w-full" style={{ height }}>
      {draw ? (
        <svg width={w} height={height} className="block overflow-visible" aria-hidden>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colour} stopOpacity={tone === "ink" ? 0.08 : 0.16} />
              <stop offset="100%" stopColor={colour} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={`${line} L ${w - 1} ${height} L 1 ${height} Z`} fill={`url(#${id})`} />
          <path d={line} fill="none" stroke={colour} strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : null}
    </div>
  );
}

export type Marker = { index: number; label: string; tone: "positive" | "negative" | "ink" | "warn" | "brand" | "muted" };

const AXIS_FONT = 11;
/** Rough Inter width at 11px; only used to keep labels apart and inside the edges. */
const labelWidth = (l: string) => l.length * AXIS_FONT * 0.56;

/** The large balance chart: smooth area, dotted day rail, markers on the events that mattered. */
export function AreaChart({
  values,
  labels,
  markers = [],
  height = 260,
  tone = "brand",
}: {
  values: number[];
  labels: string[];
  markers?: Marker[];
  height?: number;
  tone?: "brand" | "positive" | "ink";
}) {
  const id = useId();
  const [ref, w] = useWidth();
  const colour = TONE[tone];
  const n = values.length;

  let body: ReactNode = null;
  if (n < 2) {
    body = <div className="grid h-full place-items-center text-[13px] text-ink-muted">Not enough history yet.</div>;
  } else if (w > 0) {
    // Vertical bands, top to bottom: plot, day rail, axis labels.
    const labelY = height - 4;
    const railY = height - 30;
    const plotBottom = railY - 18;
    const widest = Math.max(...labels.map(labelWidth), 0);
    const padX = Math.min(32, Math.max(10, Math.ceil(widest / 2)));
    const pts = scale(values, padX, w - padX, 14, plotBottom);
    const line = smoothPath(pts);
    const step = (w - 2 * padX) / (n - 1);
    const dot = Math.min(4.5, Math.max(2, step * 0.3));
    // Rings shrink with the spacing so neighbouring events never overlap at phone width.
    const ringR = Math.min(4.5, Math.max(2.5, step * 0.35));
    // Label every k-th day, counted back from the newest so "today" always has one.
    const every = Math.max(1, Math.ceil((widest + 14) / step));

    const byDay = new Map<number, Marker[]>();
    for (const m of markers) {
      if (m.index < 0 || m.index >= n) continue;
      byDay.set(m.index, [...(byDay.get(m.index) ?? []), m]);
    }

    body = (
      <svg width={w} height={height} className="block" aria-hidden>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={colour} stopOpacity="0.14" />
            <stop offset="70%" stopColor={colour} stopOpacity="0.03" />
            <stop offset="100%" stopColor={colour} stopOpacity="0" />
          </linearGradient>
        </defs>

        <path d={`${line} L ${w - padX} ${plotBottom} L ${padX} ${plotBottom} Z`} fill={`url(#${id})`} />
        <path d={line} fill="none" stroke={colour} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />

        {/* Dotted day rail, exactly as many dots as there are buckets — a plain track once they would touch. */}
        {step < 6 ? (
          <line x1={padX} y1={railY} x2={w - padX} y2={railY} stroke="var(--color-line)" strokeWidth={dot * 2} strokeLinecap="round" />
        ) : (
          pts.map(([x], i) => <circle key={i} cx={x} cy={railY} r={dot} fill="var(--color-line)" />)
        )}

        {labels.map((l, i) => {
          if (i >= n || (n - 1 - i) % every !== 0) return null;
          const half = labelWidth(l) / 2;
          const x = Math.min(w - half, Math.max(half, pts[i][0]));
          return (
            <text key={l + i} x={x} y={labelY} textAnchor="middle" fontSize={AXIS_FONT} fill="var(--color-ink-muted)">
              {l}
            </text>
          );
        })}

        {[...byDay].map(([i, day]) => {
          const [x, y] = pts[i];
          // One dot per event, stacked up from the rail, so a busy day still shows every order.
          const pitch = dot * 2 + 2;
          const stackTop = railY - (day.length - 1) * pitch - dot;
          const ring = new Set(day.map((m) => m.tone)).size === 1 ? TONE[day[0].tone] : colour;
          return (
            <g key={i}>
              <title>{day.map((m) => m.label).join("\n")}</title>
              {stackTop - 4 > y + ringR + 4 ? (
                <line x1={x} y1={y + ringR + 4} x2={x} y2={stackTop - 4} stroke={ring} strokeOpacity="0.35" strokeWidth="1.5" strokeLinecap="round" />
              ) : null}
              {/* White halo under the fill keeps a stack readable where it crosses the line. */}
              {day.map((m, j) => (
                <circle
                  key={m.label + j}
                  cx={x}
                  cy={railY - j * pitch}
                  r={dot}
                  fill={TONE[m.tone]}
                  stroke="var(--color-surface)"
                  strokeWidth="3"
                  paintOrder="stroke"
                />
              ))}
              <circle cx={x} cy={y} r={ringR} fill="var(--color-surface)" stroke={ring} strokeWidth={ringR < 4 ? 1.5 : 2} />
            </g>
          );
        })}
      </svg>
    );
  }

  return (
    <div ref={ref} className="w-full" style={{ height }}>
      {body}
    </div>
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
  tone?: "positive" | "warn" | "negative" | "brand";
  children?: ReactNode;
}) {
  const stroke = Math.max(3, Math.round(size / 11));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = clamp01(value);
  return (
    <span className="relative inline-grid shrink-0 place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--color-line)" strokeWidth={stroke} />
        {/* A zero-length dash still paints its round cap; draw nothing for 0%. */}
        {v > 0 ? (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={TONE[tone]}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={c * (1 - v)}
          />
        ) : null}
      </svg>
      {children ? (
        <span className="num absolute font-semibold leading-none" style={{ fontSize: Math.max(10, Math.round(size * 0.25)) }}>
          {children}
        </span>
      ) : null}
    </span>
  );
}

/** Rounded columns, the reference's "last 3 months" block. */
export function Bars({
  data,
  height = 150,
  tone = "positive",
}: {
  data: { label: string; value: number; caption?: string }[];
  height?: number;
  tone?: "positive" | "brand";
}) {
  const max = Math.max(...data.map((d) => Math.abs(d.value)), 1);
  const fill = tone === "brand" ? "bg-gradient-to-b from-brand/75 to-brand" : "bg-gradient-to-b from-positive/60 to-positive";
  return (
    <div className="flex items-end gap-3" style={{ height }}>
      {data.map((d) => {
        // Zero is a flat stub, not a short positive bar; anything else is tall enough to read as a pill.
        const h = d.value === 0 ? 4 : Math.max(10, (Math.abs(d.value) / max) * (height - 46));
        return (
          <div key={d.label} className="flex min-w-0 flex-1 flex-col items-center justify-end gap-2">
            <div
              className={`w-full max-w-16 ${d.value > 0 ? fill : "bg-chip"}`}
              style={{ height: h, borderRadius: Math.min(14, h / 2) }}
              title={`${d.label}: ${d.caption ?? d.value}`}
            />
            <div className="max-w-full text-center leading-tight">
              <div className="truncate text-[11px] text-ink-muted">{d.label}</div>
              <div className="num mt-0.5 text-[12px] font-semibold">{d.caption ?? d.value}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function Progress({ value, tone = "brand" }: { value: number; tone?: "ink" | "positive" | "warn" | "brand" }) {
  const colour = { positive: "bg-positive", warn: "bg-warn", ink: "bg-ink", brand: "bg-brand" }[tone];
  const v = clamp01(value);
  return (
    <span
      className="block h-1.5 w-full overflow-hidden rounded-full bg-chip"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(v * 100)}
    >
      <span className={`block h-full rounded-full ${colour}`} style={{ width: `${v * 100}%` }} />
    </span>
  );
}

// Blue-led and status-free: an avatar's colour must never read as confirmed / pending / rejected.
const AVATAR_TONES = [
  "bg-brand text-white",
  "bg-brand-soft text-brand",
  "bg-aimo-soft text-aimo",
  "bg-aimo text-white",
  "bg-chip text-ink-muted",
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
      className={`grid shrink-0 select-none place-items-center rounded-full font-semibold leading-none tracking-[-0.02em] ${tone}`}
      style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.36)) }}
      title={name}
    >
      {initials || "?"}
    </span>
  );
}
