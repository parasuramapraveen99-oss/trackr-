"use client";

/**
 * Chart primitives the analytics screen needs and the rest of the design system did not
 * have: an arc gauge, a correlation heat map, a bubble scatter, a ranked bar series and
 * an inline distribution marker.
 *
 * All hand-drawn SVG on `var(--chart-*)` / `var(--alloc-*)` tokens, following
 * AllocationArcs and VintageLinePlot rather than reaching for recharts — every token
 * already has a dark-mode value in globals.css, so theming is free and a hardcoded hex
 * would be the one thing that breaks it.
 */

import type { CSSProperties, ReactNode } from "react";

const AXIS = "var(--chart-axis-text)";
const GRID = "var(--chart-grid)";

/* ---------------------------------------------------------------------------- Gauge */

/**
 * A single-value arc with the grade in the middle. Used for portfolio health, which is
 * the most legible — and therefore most screenshotted — number on the page.
 */
export function Gauge({
  score,
  grade,
  size = 96,
}: {
  score: number;
  grade: string;
  size?: number;
}) {
  const radius = 30;
  const circumference = Math.PI * radius;
  const filled = circumference * Math.max(0, Math.min(100, score)) / 100;
  const tone =
    score >= 65 ? "var(--positive)" : score >= 50 ? "var(--warning)" : "var(--negative)";
  return (
    <svg viewBox="0 0 80 50" style={{ width: size, height: "auto", flex: "none" }}
         role="img" aria-label={`Health ${score} of 100, grade ${grade}`}>
      <path d="M 10 40 A 30 30 0 0 1 70 40" fill="none" stroke="var(--surface-hover)"
            strokeWidth={7} strokeLinecap="round" />
      <path d="M 10 40 A 30 30 0 0 1 70 40" fill="none" stroke={tone} strokeWidth={7}
            strokeLinecap="round"
            strokeDasharray={`${filled.toFixed(1)} ${(circumference - filled).toFixed(1)}`} />
      <text x={40} y={35} textAnchor="middle" fontSize={17} fontWeight={600}
            fill="var(--text-primary)" fontFamily="Newsreader, serif">{grade}</text>
    </svg>
  );
}

/* -------------------------------------------------------------------------- HeatMap */

export interface HeatBand {
  /** Inclusive lower bound. Bands are scanned high-to-low, so order them descending. */
  from: number;
  color: string;
  label: string;
}

/** The default correlation banding. Stated as data so the legend and the cells cannot
 *  disagree — a legend that describes different thresholds from the fill is worse than
 *  no legend. */
export const CORRELATION_BANDS: HeatBand[] = [
  { from: 0.6, color: "rgb(179 38 30 / 0.75)", label: "> 0.60 — close to one position" },
  { from: 0.35, color: "rgb(232 163 61 / 0.7)", label: "0.35 – 0.60" },
  { from: 0.15, color: "rgb(232 163 61 / 0.35)", label: "0.15 – 0.35" },
  { from: -1, color: "rgb(30 122 76 / 0.4)", label: "< 0.15 — genuinely independent" },
];

export function HeatMap({
  labels,
  matrix,
  bands = CORRELATION_BANDS,
  cell = 26,
  max = 14,
}: {
  labels: string[];
  matrix: (number | null)[][];
  bands?: HeatBand[];
  cell?: number;
  max?: number;
}) {
  const shown = labels.slice(0, max);
  const gutter = 84;
  const span = shown.length * cell;
  const fill = (v: number) =>
    bands.find((b) => v >= b.from)?.color ?? bands[bands.length - 1]?.color ?? "var(--surface-hover)";

  return (
    <svg viewBox={`0 0 ${gutter + span + 6} ${span + 64}`} style={{ width: "100%", maxWidth: 640 }}>
      {shown.map((rowLabel, i) =>
        shown.map((colLabel, j) => {
          const value = matrix[i]?.[j];
          return (
            <rect
              key={`${i}-${j}`}
              x={gutter + j * cell}
              y={2 + i * cell}
              width={cell - 1.5}
              height={cell - 1.5}
              rx={2}
              fill={i === j ? "var(--surface-hover)" : value == null ? "var(--surface-hover)" : fill(value)}
            >
              <title>{`${rowLabel} / ${colLabel}: ${value == null ? "n/a" : value.toFixed(3)}`}</title>
            </rect>
          );
        }),
      )}
      {shown.map((label, i) => (
        <text key={`r${i}`} x={gutter - 6} y={2 + i * cell + 16} textAnchor="end"
              fontSize={9.5} fill="var(--text-secondary)">{label}</text>
      ))}
      {shown.map((label, j) => (
        <text key={`c${j}`} x={gutter + j * cell + 11} y={span + 12} fontSize={9.5}
              fill="var(--text-secondary)"
              transform={`rotate(55 ${gutter + j * cell + 11} ${span + 12})`}>{label}</text>
      ))}
    </svg>
  );
}

/* --------------------------------------------------------------------- ScatterPlot */

export interface BubblePoint {
  key: string;
  label: string;
  x: number;
  y: number;
  /** Drives radius. Portfolio weight, in percent. */
  weight: number;
  tone: string;
}

interface Placed extends BubblePoint {
  cx: number;
  cy: number;
  r: number;
  lx: number;
  ly: number;
  moved: boolean;
}

/**
 * Bubble scatter with COLLISION-AWARE LABELS.
 *
 * The reference product this screen is modelled on drops or overlaps half its labels in
 * the dense region, which makes the densest — and therefore most interesting — part of
 * the chart the least readable. Labels here are placed largest-bubble-first around a ring
 * of candidate positions, joined by a leader line whenever they move, and never dropped:
 * if no candidate is clear the last one is used anyway, because an overlapping label is
 * still recoverable and a missing one is not.
 */
export function ScatterPlot({
  points,
  xLabel,
  yLabel,
  xAvg,
  yAvg,
  xLowerIsBetter,
  yLowerIsBetter,
  neutral = false,
  format,
  height = 430,
}: {
  points: BubblePoint[];
  xLabel: string;
  yLabel: string;
  xAvg: number | null;
  yAvg: number | null;
  xLowerIsBetter: boolean;
  yLowerIsBetter: boolean;
  neutral?: boolean;
  format: (v: number, axis: "x" | "y") => string;
  height?: number;
}) {
  const W = 900;
  const pad = 62;
  if (!points.length) return null;

  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const sx = (v: number) => pad + ((v - x0) / (x1 - x0 || 1)) * (W - pad - 30);
  const sy = (v: number) => height - pad - ((v - y0) / (y1 - y0 || 1)) * (height - pad - 26);
  const maxWeight = Math.max(...points.map((p) => p.weight)) || 1;

  const items: Placed[] = points
    .map((p) => ({
      ...p,
      cx: sx(p.x),
      cy: sy(p.y),
      r: neutral ? 30 * Math.sqrt(p.weight / maxWeight) : Math.max(7, 7 + (26 * p.weight) / maxWeight),
      lx: 0,
      ly: 0,
      moved: false,
    }))
    .sort((a, b) => b.r - a.r);

  const placed: { x1: number; x2: number; y1: number; y2: number }[] = [];
  const CH = 5.35;
  const LH = 11;
  const overlaps = (a: typeof placed[0], b: typeof placed[0]) =>
    !(a.x2 < b.x1 || b.x2 < a.x1 || a.y2 < b.y1 || b.y2 < a.y1);

  for (const item of items) {
    const width = item.label.length * CH;
    const candidates: { cx: number; cy: number }[] = [];
    const ring: [number, number][] = [[0, 1], [0, -1], [1, 0.4], [-1, 0.4], [1, -0.4], [-1, -0.4]];
    for (const [dx, dy] of ring) {
      for (let step = 0; step < 5; step++) {
        const offset = item.r + 10 + step * LH;
        candidates.push({
          cx: item.cx + dx * (dx ? width / 2 + 6 : 0),
          cy: item.cy + dy * offset,
        });
      }
    }
    // Never empty: the ring above always produces candidates, and if none is clear the
    // first is used anyway -- an overlapping label is recoverable, a missing one is not.
    let chosen = candidates[0] ?? { cx: item.cx, cy: item.cy + item.r + 10 };
    for (const c of candidates) {
      const box = { x1: c.cx - width / 2 - 2, x2: c.cx + width / 2 + 2, y1: c.cy - LH + 2, y2: c.cy + 3 };
      const clash =
        placed.some((q) => overlaps(box, q)) ||
        items.some((o) => o !== item && Math.hypot(o.cx - c.cx, o.cy - c.cy) < o.r + 3);
      if (!clash) { chosen = c; break; }
    }
    item.lx = chosen.cx;
    item.ly = chosen.cy;
    item.moved = Math.hypot(item.lx - item.cx, item.ly - item.cy) > item.r + 14;
    placed.push({ x1: item.lx - width / 2 - 2, x2: item.lx + width / 2 + 2, y1: item.ly - LH + 2, y2: item.ly + 3 });
  }

  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <svg viewBox={`0 0 ${W} ${height}`} style={{ width: "100%", height: "auto" }}>
      {/* The favourable quadrant, tinted. Which corner is good depends on the lens, so it
          is derived from the direction flags rather than assumed to be top-right. */}
      {!neutral && xAvg != null && yAvg != null && (
        <rect
          x={xLowerIsBetter ? pad : sx(xAvg)}
          y={yLowerIsBetter ? sy(yAvg) : 26}
          width={Math.abs(sx(xAvg) - (xLowerIsBetter ? pad : W - 30))}
          height={Math.abs(sy(yAvg) - (yLowerIsBetter ? height - pad : 26))}
          fill="var(--positive)"
          opacity={0.05}
        />
      )}
      {xAvg != null && (
        <line x1={sx(xAvg)} x2={sx(xAvg)} y1={26} y2={height - pad} stroke={AXIS} strokeDasharray="4 4" />
      )}
      {yAvg != null && (
        <line x1={pad} x2={W - 30} y1={sy(yAvg)} y2={sy(yAvg)} stroke={AXIS} strokeDasharray="4 4" />
      )}
      {ticks.map((f) => {
        const v = x0 + (x1 - x0) * f;
        return (
          <text key={`xt${f}`} x={sx(v)} y={height - pad + 15} textAnchor="middle" fontSize={9.5} fill={AXIS}>
            {format(v, "x")}
          </text>
        );
      })}
      {ticks.map((f) => {
        const v = y0 + (y1 - y0) * f;
        return (
          <text key={`yt${f}`} x={pad - 7} y={sy(v) + 3} textAnchor="end" fontSize={9.5} fill={AXIS}>
            {format(v, "y")}
          </text>
        );
      })}
      <line x1={pad} x2={W - 30} y1={height - pad} y2={height - pad} stroke="var(--border-strong)" />
      <line x1={pad} x2={pad} y1={26} y2={height - pad} stroke="var(--border-strong)" />
      {items.map((item) => (
        <g key={item.key}>
          {item.moved && (
            <line x1={item.cx} y1={item.cy} x2={item.lx} y2={item.ly - 3}
                  stroke="var(--border-strong)" strokeWidth={0.8} />
          )}
          <circle cx={item.cx} cy={item.cy} r={item.r} fill={item.tone} opacity={0.5}
                  stroke={item.tone} strokeWidth={1.5} />
          <title>{`${item.label} — ${xLabel} ${format(item.x, "x")}, ${yLabel} ${format(item.y, "y")}, weight ${item.weight.toFixed(1)}%`}</title>
          <text x={item.lx} y={item.ly} textAnchor="middle" fontSize={9.5}
                fill="var(--text-secondary)" paintOrder="stroke" stroke="var(--surface)"
                strokeWidth={2.5} strokeLinejoin="round">{item.label}</text>
        </g>
      ))}
      <text x={W / 2} y={height - 8} textAnchor="middle" fontSize={11} fill="var(--text-secondary)">
        {xLabel} →
      </text>
      <text x={14} y={height / 2} textAnchor="middle" fontSize={11} fill="var(--text-secondary)"
            transform={`rotate(-90 14 ${height / 2})`}>{yLabel} →</text>
    </svg>
  );
}

/* ------------------------------------------------------------------------ BarSeries */

/** Ranked bars, one per holding. Sorted by the caller so rank reads without a legend. */
export function BarSeries({
  bars,
  height = 260,
}: {
  bars: { label: string; value: number; tone: string; title?: string }[];
  height?: number;
}) {
  if (!bars.length) return null;
  const W = 900;
  const left = 42;
  const floor = height - 40;
  const scale = (floor - 20) / 100;
  const slot = (W - left - 10) / bars.length;

  return (
    <svg viewBox={`0 0 ${W} ${height}`} style={{ width: "100%", height: "auto" }}>
      {[0, 35, 70, 100].map((v) => (
        <g key={v}>
          <line x1={left} x2={W - 10} y1={floor - v * scale} y2={floor - v * scale} stroke={GRID} />
          <text x={left - 6} y={floor - v * scale + 4} textAnchor="end" fontSize={10} fill={AXIS}>{v}</text>
        </g>
      ))}
      {bars.map((bar, i) => {
        const w = Math.max(3, slot - 4);
        const x = left + i * slot;
        const h = Math.max(1, bar.value * scale);
        return (
          <g key={bar.label + i}>
            <rect x={x} y={floor - h} width={w} height={h} fill={bar.tone} rx={2}>
              <title>{bar.title ?? `${bar.label} — ${bar.value.toFixed(1)}/100`}</title>
            </rect>
            <text x={x + w / 2} y={floor + 14} textAnchor="end" fontSize={9} fill={AXIS}
                  transform={`rotate(-55 ${x + w / 2} ${floor + 14})`}>{bar.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ------------------------------------------------------------------ DistributionBar */

/**
 * Where one holding sits on a metric's range, with the portfolio average ticked.
 * Turns an absolute figure in a table cell into a comparison without another column.
 */
export function DistributionBar({
  value,
  min,
  max,
  avg,
  better,
}: {
  value: number;
  min: number;
  max: number;
  avg: number | null;
  better: boolean;
}) {
  const clamp = (v: number) => Math.max(2, Math.min(98, v));
  const pos = clamp(((value - min) / (max - min || 1)) * 100);
  const avgPos = avg == null ? null : clamp(((avg - min) / (max - min || 1)) * 100);
  return (
    <div style={{
      position: "relative", height: 5, borderRadius: 3, background: "var(--surface-hover)",
      width: 74, marginLeft: "auto", marginTop: 5,
    }}>
      {avgPos != null && (
        <i style={{
          position: "absolute", top: -3, left: `${avgPos}%`, width: 1.5, height: 11,
          background: "var(--text-tertiary)",
        }} />
      )}
      <i style={{
        position: "absolute", top: -2, left: `${pos}%`, width: 3, height: 9, borderRadius: 2,
        background: better ? "var(--positive)" : "var(--negative)",
      }} />
    </div>
  );
}

/* -------------------------------------------------------------------------- MeterRow */

export function MeterRow({
  label,
  score,
  detail,
  caption,
}: {
  label: ReactNode;
  score: number | null;
  detail?: ReactNode;
  caption?: ReactNode;
}) {
  const tone =
    score == null ? "var(--ink-300)"
      : score >= 65 ? "var(--positive)"
      : score >= 40 ? "var(--warning)"
      : "var(--negative)";
  const row: CSSProperties = {
    padding: "12px 0", borderBottom: "1px solid var(--border)",
  };
  return (
    <div style={row}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 6 }}>
        <div style={{ fontSize: 13.5 }}>
          <b>{label}</b>{" "}
          {detail && <span style={{ color: "var(--text-tertiary)", fontSize: 11.5 }}>— {detail}</span>}
        </div>
        <div style={{ fontVariantNumeric: "tabular-nums", fontSize: 13 }}>
          {score == null ? <span style={{ color: "var(--text-tertiary)" }}>n/a</span> : score.toFixed(1)}
          <span style={{ color: "var(--text-tertiary)", fontSize: 11 }}>/100</span>
        </div>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: "var(--surface-hover)", overflow: "hidden" }}>
        <i style={{ display: "block", height: "100%", borderRadius: 3, background: tone,
                    width: `${score ?? 0}%` }} />
      </div>
      {caption && (
        <div style={{ display: "flex", gap: 14, fontSize: 10.5, color: "var(--text-tertiary)", marginTop: 5 }}>
          {caption}
        </div>
      )}
    </div>
  );
}
