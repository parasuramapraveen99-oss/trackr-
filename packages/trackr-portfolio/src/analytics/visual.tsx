"use client";

/**
 * The visual Analytics board: one question per tile, answered by a shape.
 *
 * Rules this file keeps (the redesign brief, 10 Oct 2026):
 *   - labels are at most three words; sentences live only in hover tooltips;
 *   - size means money, red/green mean worse/better, the index is always grey;
 *   - money never passes through `number` for a label -- geometry uses
 *     `decimalScale(...).position`, labels use the Decimal formatter;
 *   - a missing input is drawn as a gap (hatched or "—"), never as a zero.
 *
 * Statistical measures (volatility, correlation, ratios) are floats on the wire by the
 * analytics module's own contract (see ./format.ts) and are drawn as such.
 */
import {
  createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { Dec, decimalScale, parseDecimal, usePrivacy } from "@trackr/core";
import type {
  AnalyticsResponse, BenchmarkSeriesResponse, ContributionResponse, SnapshotOut,
} from "@trackr/core";

import { sampleMoneyRows, moneyPaths } from "./money-path";

export const COMPANY_LENSES = {
  valuation: { label: "Valuation", x: "P/E", y: "P/B", percent: false },
  growth: { label: "Growth", x: "Profit 1Y", y: "Sales 1Y", percent: true },
  profitability: { label: "Profitability", x: "ROE", y: "Net margin", percent: true },
  quality: { label: "Leverage", x: "Debt / equity", y: "Interest cover", percent: false },
} as const;
export type CompanyLens = keyof typeof COMPANY_LENSES;

/* ------------------------------------------------------------------ primitives --- */

const MASK = "••••";
const LAKH = new Dec(100000);
const CRORE = new Dec(10000000);

/** ₹ compact, Indian units, from a Decimal string: ₹13.4L, ₹1.86Cr, ₹42k. */
export function compactInr(value: string | Dec | null | undefined, masked = false): string {
  if (masked) return MASK;
  const d = typeof value === "string" || value == null ? parseDecimal(value as string | null) : value;
  if (d == null) return "—";
  const sign = d.isNegative() && !d.isZero() ? "−" : "";
  const a = d.abs();
  if (a.greaterThanOrEqualTo(CRORE)) return `${sign}₹${a.dividedBy(CRORE).toFixed(2)}Cr`;
  if (a.greaterThanOrEqualTo(LAKH)) return `${sign}₹${a.dividedBy(LAKH).toFixed(1)}L`;
  if (a.greaterThanOrEqualTo(1000)) return `${sign}₹${a.dividedBy(1000).toFixed(0)}k`;
  return `${sign}₹${a.toFixed(0)}`;
}

const pctF = (v: number | null | undefined, d = 1, sign = false) =>
  v == null || !Number.isFinite(v) ? "—" : `${sign && v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const ppF = (v: number | null | undefined, d = 1, sign = false) =>
  v == null || !Number.isFinite(v) ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(d)}%`;
const rate = (s: string | null | undefined) => parseDecimal(s)?.toNumber() ?? null;
const sym = (p: { symbol?: string | null; isin: string }) => p.symbol ?? p.isin;

const AXIS: React.CSSProperties = { fill: "var(--chart-axis-text)", font: "500 10.5px var(--font-label)" };
const INK: React.CSSProperties = { fill: "var(--text-primary)", font: "600 11px var(--font-label)" };
const INK2: React.CSSProperties = { fill: "var(--text-secondary)", font: "500 11px var(--font-label)" };

/** Measured width of a container, so every chart draws to its real pixels. */
export function useWidth(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(([e]) => e && setW(Math.floor(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/* tooltip: one fixed element for the whole board */
type TipApi = { show: (e: React.MouseEvent, text: ReactNode) => void; hide: () => void };
const TipContext = createContext<TipApi>({ show: () => {}, hide: () => {} });
export const useTip = () => useContext(TipContext);

export function TipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<{ x: number; y: number; text: ReactNode } | null>(null);
  const show = useCallback((e: React.MouseEvent, text: ReactNode) => {
    setTip({ x: Math.min(e.clientX + 12, window.innerWidth - 260), y: e.clientY + 14, text });
  }, []);
  const hide = useCallback(() => setTip(null), []);
  const api = useMemo(() => ({ show, hide }), [show, hide]);
  return (
    <TipContext.Provider value={api}>
      {children}
      {tip && (
        <div role="status" style={{
          position: "fixed", left: tip.x, top: tip.y, zIndex: 50, pointerEvents: "none", maxWidth: 250,
          background: "var(--ink-900)", color: "var(--text-inverse)", font: "500 11.5px/1.45 var(--font-label)",
          padding: "6px 9px", borderRadius: 6,
        }}>{tip.text}</div>
      )}
    </TipContext.Provider>
  );
}

/** Hover handlers for one mark. */
function useHover(text: () => ReactNode) {
  const { show, hide } = useTip();
  return { onMouseMove: (e: React.MouseEvent) => show(e, text()), onMouseLeave: hide };
}

function Hatch({ id }: { id: string }) {
  return (
    <defs>
      <pattern id={id} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width={2} height={6} style={{ fill: "var(--ink-300)" }} />
      </pattern>
    </defs>
  );
}

export function Tile({ label, figure, keys, span, children }: {
  label: string; figure?: ReactNode; keys?: [label: string, color: string, kind?: "line" | "dot" | "tick"][]; span: number; children: ReactNode;
}) {
  return (
    <section className={span >= 7 ? "tp-tile tp-wide" : "tp-tile"} style={{ gridColumn: `span ${span}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <span style={{ font: "600 11px var(--font-label)", letterSpacing: ".1em", textTransform: "uppercase", color: "var(--text-tertiary)" }}>{label}</span>
        {keys && (
          <span style={{ display: "flex", gap: 10, flexWrap: "wrap", font: "500 11px var(--font-label)", color: "var(--text-secondary)" }}>
            {keys.map(([text, color, kind]) => (
              <span key={text} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                <i style={{
                  display: "inline-block", background: color,
                  width: kind === "dot" ? 8 : kind === "tick" ? 3 : 14, height: kind === "dot" ? 8 : kind === "tick" ? 11 : 3,
                  borderRadius: kind === "dot" ? "50%" : 2,
                }} />{text}
              </span>
            ))}
          </span>
        )}
      </div>
      {figure != null && <div style={{ font: "500 28px/1.05 var(--font-figure)", fontVariantNumeric: "tabular-nums" }}>{figure}</div>}
      {children}
    </section>
  );
}

export const Small = ({ children }: { children: ReactNode }) => (
  <small style={{ font: "500 12px var(--font-label)", color: "var(--text-tertiary)", marginLeft: 6 }}>{children}</small>
);

/* ------------------------------------------------------------------ outcome ------ */

/** Recorded worth and cash-flow-matched index, alongside current holdings cost. */
export function MoneyPath({ snapshots, bench }: { snapshots: SnapshotOut[]; bench: BenchmarkSeriesResponse | null }) {
  const [ref, w] = useWidth();
  const { masked } = usePrivacy();
  const hover = useTip();
  const byDate = useMemo(() => new Map((bench?.points ?? []).map((p) => [p.as_of, p.value])), [bench]);
  const rows = useMemo(() => sampleMoneyRows(snapshots, byDate), [snapshots, byDate]);
  const h = 250, m = { t: 10, r: 74, b: 22, l: 52 };
  const observed = rows.filter((r): r is SnapshotOut => r != null);
  if (observed.length < 2) return <div ref={ref} style={{ height: h }} />;
  const scale = decimalScale([...observed.flatMap((r) => [r.invested, r.market_value]), ...observed.map((r) => byDate.get(r.as_of) ?? null), "0"]);
  const iw = Math.max(1, w - m.l - m.r), ih = h - m.t - m.b;
  const firstDate = Date.parse(observed[0]!.as_of), lastDate = Date.parse(observed[observed.length - 1]!.as_of);
  const X = (r: SnapshotOut) => m.l + ((Date.parse(r.as_of) - firstDate) / Math.max(1, lastDate - firstDate)) * iw;
  const Y = (v: string | null | undefined) => { const p = scale.position(v); return p == null ? null : m.t + (1 - p) * ih; };
  const paths = (get: (r: SnapshotOut) => string | null | undefined, step = false) =>
    moneyPaths(rows, X, (r) => Y(get(r)), m.t + ih, step);
  const worth = paths((r) => r.market_value);
  const last = observed[observed.length - 1]!;
  const lastIdx = byDate.get(last.as_of) ?? null;
  const years = [...new Set(observed.map((r) => r.as_of.slice(0, 4)))];
  const onMove = (e: React.MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const target = firstDate + Math.max(0, Math.min(1, (e.clientX - box.left) / box.width)) * (lastDate - firstDate);
    const r = observed.reduce((a, b) => Math.abs(Date.parse(a.as_of) - target) < Math.abs(Date.parse(b.as_of) - target) ? a : b);
    hover.show(e, <>{r.as_of}<br />Worth {compactInr(r.market_value, masked)} · Index {compactInr(byDate.get(r.as_of) ?? null, masked)} · Holdings cost {compactInr(r.invested, masked)}{r.excluded_isins?.length ? <><br />Excluded: {r.excluded_isins.join(", ")}</> : null}</>);
  };
  return (
    <div ref={ref}>
      {w > 0 && (
        <svg width={w} height={h} role="img" aria-label="Portfolio worth against the same cash flows invested in the index">
          {scale.ticks.map((t) => { const y = Y(t)!; return (
            <g key={t}><line x1={m.l} x2={w - m.r} y1={y} y2={y} style={{ stroke: "var(--chart-grid)" }} />
              <text x={m.l - 6} y={y} dy=".32em" textAnchor="end" style={AXIS}>{compactInr(t, masked)}</text></g>); })}
          {years.map((yr) => { const i = observed.findIndex((r) => r.as_of.startsWith(yr)); return i > 0 || years.length === 1
            ? <text key={yr} x={X(observed[i]!)} y={h - 6} style={AXIS}>{yr}</text> : null; })}
          <path d={worth.area} style={{ fill: "var(--chart-1-tint)" }} />
          <path d={paths((r) => r.invested, true).line} style={{ fill: "none", stroke: "var(--ink-300)", strokeWidth: 1 }} />
          <path d={paths((r) => byDate.get(r.as_of)).line} style={{ fill: "none", stroke: "var(--ink-400)", strokeWidth: 1.6, strokeDasharray: "4 3" }} />
          <path d={worth.line} style={{ fill: "none", stroke: "var(--chart-1)", strokeWidth: 2.2 }} />
          {[[last.market_value, "var(--chart-1)"], [lastIdx, "var(--ink-400)"]].map(([v, c]) => {
            const y = Y(v); return y == null ? null : (
              <g key={c}><circle cx={X(last)} cy={y} r={3.5} style={{ fill: c! }} />
                <text x={X(last) + 8} y={y} dy=".32em" style={{ ...INK, font: "600 12px var(--font-label)" }}>{compactInr(v, masked)}</text></g>);
          })}
          <rect x={m.l} y={m.t} width={iw} height={ih} style={{ fill: "transparent" }} onMouseMove={onMove} onMouseLeave={hover.hide} />
        </svg>
      )}
    </div>
  );
}

/** Gap at the end of the money path, for the tile's headline. */
export function moneyGap(snapshots: SnapshotOut[], bench: BenchmarkSeriesResponse | null): Dec | null {
  const last = snapshots[snapshots.length - 1];
  const idx = last ? bench?.points.find((p) => p.as_of === last.as_of)?.value : null;
  const a = parseDecimal(last?.market_value), b = parseDecimal(idx);
  return a && b ? a.minus(b) : null;
}

/** Recorded XIRR against the cash-flow-matched index, per window. */
export function WindowsDumbbell({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const rows = (["1y", "3y", "all"] as const).map((k) => [k, data.performance?.windows?.[k]] as const).filter(([, v]) => v && v.xirr != null);
  const vals = rows.flatMap(([, v]) => [rate(v!.xirr), rate(v!.benchmark_xirr)]).filter((v): v is number => v != null);
  const h = rows.length * 52 + 24, m = { l: 34, r: 16 };
  const lo = Math.min(0, ...vals) * 1.12, hi = Math.max(0.05, ...vals) * 1.12;
  const X = (v: number) => m.l + ((v - lo) / (hi - lo || 1)) * Math.max(1, w - m.l - m.r);
  const ticks = [lo, lo / 2, 0, hi > 0.06 ? hi : null].filter((v): v is number => v != null);
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Annual return against the index">
        <line x1={X(0)} x2={X(0)} y1={4} y2={h - 20} style={{ stroke: "var(--chart-grid)" }} />
        {ticks.map((t) => <text key={t} x={X(t)} y={h - 4} textAnchor="middle" style={AXIS}>{pctF(t, 0)}</text>)}
        {rows.map(([k, v], i) => {
          const you = rate(v!.xirr)!, idx = rate(v!.benchmark_xirr), cy = 20 + i * 52, bad = idx != null && you < idx;
          return (
            <g key={k} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${k.toUpperCase()}: you ${pctF(you)} · index ${pctF(idx)}${idx != null ? ` · gap ${((you - idx) * 100).toFixed(1)} pp` : ""}`), onMouseLeave: hover.hide }}>
              <text x={0} y={cy} dy=".32em" style={INK2}>{k === "all" ? "All" : k.toUpperCase()}</text>
              {idx != null && <line x1={X(you)} x2={X(idx)} y1={cy} y2={cy} style={{ stroke: bad ? "var(--negative)" : "var(--positive)", strokeWidth: 6, strokeLinecap: "round", opacity: 0.25 }} />}
              {idx != null && <circle cx={X(idx)} cy={cy} r={6} style={{ fill: "var(--ink-400)" }} />}
              <circle cx={X(you)} cy={cy} r={7} style={{ fill: "var(--chart-1)" }} />
              <text x={X(you)} y={cy - 13} textAnchor="middle" style={INK}>{pctF(you, 0)}</text>
              {idx != null && <text x={X(idx)} y={cy + 19} textAnchor="middle" style={AXIS}>{pctF(idx, 0)}</text>}
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/** Rupee gain per holding over the period, worst and best. */
export function GainBars({ contribution }: { contribution: ContributionResponse }) {
  const [ref, w] = useWidth();
  const { masked } = usePrivacy();
  const hover = useTip();
  const all = [...contribution.contributions].sort((a, b) => (parseDecimal(a.gain)?.comparedTo(parseDecimal(b.gain) ?? 0) ?? 0));
  const rows = all.length > 10 ? [...all.slice(0, 5), ...all.slice(-5)] : all;
  const scale = decimalScale([...rows.map((r) => r.gain), "0"]);
  const bh = 20, h = rows.length * bh + 4, m = { l: 82, r: 50 };
  const iw = Math.max(1, w - m.l - m.r);
  const X = (v: string) => m.l + (scale.position(v) ?? 0) * iw;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Gain by holding">
        {rows.map((r, i) => {
          const y = i * bh + 2, neg = parseDecimal(r.gain)?.isNegative() ?? false, x0 = X("0"), x1 = X(r.gain);
          const inside = neg && x1 - 46 < m.l;
          return (
            <g key={r.isin} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${r.symbol ?? r.isin}: ${compactInr(r.gain, masked)}`), onMouseLeave: hover.hide }}>
              <text x={m.l - 6} y={y + 7} dy=".32em" textAnchor="end" style={INK2}>{r.symbol ?? r.isin}</text>
              <rect x={Math.min(x0, x1)} y={y} width={Math.max(1, Math.abs(x1 - x0))} height={bh - 6} rx={2} style={{ fill: neg ? "var(--negative)" : "var(--positive)" }} />
              <text x={inside ? x1 + 5 : neg ? x1 - 4 : x1 + 4} y={y + 7} dy=".32em" textAnchor={inside || !neg ? "start" : "end"}
                style={inside ? { ...AXIS, fill: "#fff" } : AXIS}>{compactInr(r.gain, masked)}</text>
            </g>
          );
        })}
        <line x1={X("0")} x2={X("0")} y1={0} y2={h} style={{ stroke: "var(--ink-400)" }} />
      </svg>)}</div>
  );
}

/** Health components: width = weight, fill = score; a missing input is hatched. */
export function HealthSegments({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const comps = data.health.components;
  const gap = 6, total = Math.max(1, w - gap * (comps.length - 1));
  const short: Record<string, string> = {
    factor_composite: "Factors", concentration: "Spread", balance_sheet: "Balance sheet", benchmark_relative: "Vs index",
    // portfolio-health-v1 (advisor)
    diversification: "Co-movement", risk: "Risk", quality: "Quality", valuation: "Valuation",
  };
  let x0 = 0;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={70} role="img" aria-label="Health components">
        <Hatch id="tp-hatch-health" />
        {comps.map((c) => {
          const bw = total * c.weight, x = x0; x0 += bw + gap;
          const col = c.score == null ? "" : c.score >= 65 ? "var(--positive)" : c.score >= 35 ? "var(--warning)" : "var(--negative)";
          const why = c.score == null && c.reasons?.length ? c.reasons.map((r) => r.toLowerCase().replaceAll("_", " ")).join(" · ") : "no reliable input";
          return (
            <g key={c.key} transform={`translate(${x},0)`} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${c.label}: ${c.score == null ? why : `${Math.round(c.score)}/100`} · weight ${Math.round(c.weight * 100)}%`), onMouseLeave: hover.hide }}>
              <text y={11} style={AXIS}>{short[c.key] ?? c.label}</text>
              <rect y={18} width={bw} height={20} rx={4} style={{ fill: c.score == null ? "url(#tp-hatch-health)" : "var(--surface-sunken)" }} />
              {c.score != null && <rect y={18} width={(bw * c.score) / 100} height={20} rx={4} style={{ fill: col }} />}
              <text y={58} style={{ fill: "var(--text-primary)", font: "500 16px var(--font-figure)" }}>{c.score == null ? "—" : Math.round(c.score)}</text>
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/* ------------------------------------------------------------------ exposure ----- */

type Rect = { x: number; y: number; w: number; h: number };
/** Squarified treemap (Bruls et al.): rows that keep boxes close to square. */
function squarify<T>(items: { v: number; d: T }[], r: Rect): (Rect & { d: T })[] {
  const out: (Rect & { d: T })[] = [];
  const total = items.reduce((s, i) => s + i.v, 0);
  if (!total || r.w <= 0 || r.h <= 0) return out;
  const scaled = items.map((i) => ({ ...i, a: (i.v / total) * r.w * r.h }));
  let rect = { ...r };
  let row: typeof scaled = [];
  const worst = (rw: typeof scaled, side: number) => {
    const s = rw.reduce((t, i) => t + i.a, 0);
    const mx = Math.max(...rw.map((i) => i.a)), mn = Math.min(...rw.map((i) => i.a));
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
  };
  const lay = (rw: typeof scaled) => {
    const s = rw.reduce((t, i) => t + i.a, 0);
    if (rect.w >= rect.h) {
      const cw = s / rect.h; let y = rect.y;
      rw.forEach((i) => { const ch = i.a / cw; out.push({ x: rect.x, y, w: cw, h: ch, d: i.d }); y += ch; });
      rect = { x: rect.x + cw, y: rect.y, w: rect.w - cw, h: rect.h };
    } else {
      const ch = s / rect.w; let x = rect.x;
      rw.forEach((i) => { const cw = i.a / ch; out.push({ x, y: rect.y, w: cw, h: ch, d: i.d }); x += cw; });
      rect = { x: rect.x, y: rect.y + ch, w: rect.w, h: rect.h - ch };
    }
  };
  for (const item of scaled) {
    const side = Math.min(rect.w, rect.h);
    if (!row.length || worst([...row, item], side) <= worst(row, side)) row.push(item);
    else { lay(row); row = [item]; }
  }
  if (row.length) lay(row);
  return out;
}

const retColor = (r: number | null | undefined) =>
  r == null ? "var(--surface-sunken)"
    : `color-mix(in srgb, ${r < 0 ? "var(--negative)" : "var(--positive)"} ${Math.round(Math.min(1, Math.abs(r) / 60) * 100)}%, var(--surface-sunken))`;

/** Every holding as a box: size = money, colour = 1-year price return. */
export function HoldingsTreemap({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const { masked } = usePrivacy();
  const hover = useTip();
  const h = Math.max(240, Math.min(360, Math.round(w * 0.45)));
  const items = data.positions.filter((p) => (p.weight ?? 0) > 0.05).sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0)).map((p) => ({ v: p.weight ?? 0, d: p }));
  const boxes = squarify(items, { x: 0, y: 0, w, h });
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Holdings sized by money and coloured by one-year return">
        {boxes.map(({ x, y, w: bw, h: bh, d: p }) => {
          const r1 = p.returns?.["1y"] ?? null, strong = r1 != null && Math.abs(r1) > 30, big = bw > 46 && bh > 28;
          const ink = strong ? "#fff" : "var(--text-primary)";
          return (
            <g key={p.isin} transform={`translate(${x},${y})`} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, <>{sym(p)} · {ppF(p.weight)} of money · {compactInr(p.market_value, masked)}<br />1Y {ppF(r1, 1, true)} · unrealised {compactInr(p.unrealised, masked)}</>), onMouseLeave: hover.hide }}>
              <rect x={1} y={1} width={Math.max(0, bw - 2)} height={Math.max(0, bh - 2)} rx={3} style={{ fill: retColor(r1) }} />
              {big && <text x={8} y={18} style={{ fill: ink, font: `600 ${Math.min(15, Math.max(10, bw / 7))}px var(--font-label)` }}>{sym(p)}</text>}
              {big && bh > 46 && <text x={8} y={34} style={{ fill: ink, font: "500 11px var(--font-label)", opacity: 0.85 }}>{ppF(p.weight, 0)} · {ppF(r1, 0, true)}</text>}
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/** Cumulative share of money by holding count: how fast the book "fills". */
export function ConcentrationCurve({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const ws = data.positions.map((p) => p.weight ?? 0).sort((a, b) => b - a);
  let c = 0;
  const cum = [0, ...ws.map((v) => (c += v))];
  const h = 140, m = { l: 30, r: 10, t: 8, b: 18 }, iw = Math.max(1, w - m.l - m.r), ih = h - m.t - m.b;
  const X = (i: number) => m.l + (i / Math.max(1, ws.length)) * iw, Y = (v: number) => m.t + (1 - v / 100) * ih;
  const line = cum.map((v, i) => `${i ? "H" + X(i) + "V" : "M" + X(i) + ","}${Y(v)}`).join("");
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Cumulative share of money">
        <line x1={X(0)} y1={Y(0)} x2={X(ws.length)} y2={Y(100)} style={{ stroke: "var(--chart-grid)", strokeDasharray: "3 3" }} />
        <path d={`${line}V${Y(0)}H${X(0)}Z`} style={{ fill: "var(--chart-1-tint)" }} />
        <path d={line} style={{ fill: "none", stroke: "var(--chart-1)", strokeWidth: 2 }} />
        {[1, 3, 5].filter((k) => k <= ws.length).map((k) => (
          <g key={k}><circle cx={X(k)} cy={Y(cum[k]!)} r={3.5} style={{ fill: "var(--chart-1)" }} />
            <text x={X(k) + 6} y={Y(cum[k]!) + (k === 1 ? 10 : -5)} style={INK}>{`${k} → ${cum[k]!.toFixed(0)}%`}</text></g>
        ))}
        {[0, 50, 100].map((v) => <text key={v} x={m.l - 5} y={Y(v)} dy=".32em" textAnchor="end" style={AXIS}>{v}%</text>)}
        <text x={X(ws.length)} y={h - 2} textAnchor="end" style={AXIS}>{ws.length}</text>
      </svg>)}</div>
  );
}

const PALETTE = ["var(--chart-1)", "var(--chart-2)", "var(--chart-4)", "var(--chart-3)", "var(--chart-6)", "var(--chart-7)", "var(--chart-5)", "var(--chart-9)", "var(--chart-mono-2)"];

/** Sector and market-cap mix as 100% bars; unclassified money is hatched, not hidden. */
export function MixBars({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const sets = [["Sector", data.allocation.sector], ["Size", data.allocation.market_cap]] as const;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={sets.length * 58} role="img" aria-label="Sector and size mix">
        <Hatch id="tp-hatch-mix" />
        {sets.map(([label, rows], i) => {
          let x0 = 0; const y = i * 58 + 16;
          return (
            <g key={label}>
              <text x={0} y={y - 6} style={AXIS}>{label.toUpperCase()}</text>
              {rows.map((r, j) => {
                const bw = (w * r.pct) / 100, x = x0, un = /unclass/i.test(r.label); x0 += bw;
                return (
                  <g key={r.label} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${r.label}: ${ppF(r.pct)}`), onMouseLeave: hover.hide }}>
                    <rect x={x} y={y} width={Math.max(0, bw - 2)} height={26} rx={3} style={{ fill: un ? "url(#tp-hatch-mix)" : PALETTE[j % PALETTE.length], opacity: un ? 1 : 0.88 }} />
                    {bw > 64 && <text x={x + 6} y={y + 13} dy=".32em" style={{ fill: un ? "var(--text-primary)" : "#fff", font: "600 10.5px var(--font-label)" }}>{`${r.label.split(" ")[0]} ${r.pct.toFixed(0)}%`}</text>}
                  </g>
                );
              })}
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/** Correlation matrix ordered so names that move together form visible blocks. */
export function CorrelationMap({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const { symbols: S, matrix: M } = data.correlation;
  const n = S.length;
  const order = useMemo(() => {
    if (n < 2) return [];
    const avg = (i: number) => { const v = M[i]!.filter((x, j) => j !== i && x != null) as number[]; return v.reduce((a, b) => a + b, 0) / (v.length || 1); };
    const start = S.map((_, i) => i).sort((a, b) => avg(b) - avg(a))[0]!;
    const out = [start], left = new Set(S.map((_, i) => i)); left.delete(start);
    while (left.size) { // nearest-neighbour chain: each next name is the one most correlated with the last
      const last = out[out.length - 1]!; let best = -1, bd = Infinity;
      left.forEach((j) => { const d = 1 - (M[last]![j] ?? 0); if (d < bd) { bd = d; best = j; } });
      out.push(best); left.delete(best);
    }
    return out;
  }, [S, M, n]);
  if (n < 2) return <div ref={ref} />;
  const lab = 76, cell = Math.max(9, Math.min(18, (w - lab) / n)), size = lab + cell * n;
  const col = (v: number | null | undefined) => v == null ? "var(--surface-sunken)"
    : `color-mix(in srgb, ${v >= 0 ? "var(--chart-1)" : "var(--warning)"} ${Math.round(Math.min(1, Math.abs(v) * (v >= 0 ? 1 : 2)) * 100)}%, var(--surface))`;
  return (
    <div ref={ref} style={{ overflowX: "auto" }}>{w > 0 && (
      <svg width={size} height={size} role="img" aria-label="How holdings move together">
        {order.map((i, r) => (
          <g key={i}>
            <text x={lab - 6} y={lab + r * cell + cell / 2} dy=".32em" textAnchor="end" style={{ ...AXIS, fontSize: 9.5 }}>{S[i]}</text>
            <text transform={`translate(${lab + r * cell + cell / 2},${lab - 6}) rotate(-60)`} style={{ ...AXIS, fontSize: 9.5 }}>{S[i]}</text>
            {order.map((j, c) => (
              <rect key={j} x={lab + c * cell} y={lab + r * cell} width={cell - 1} height={cell - 1}
                style={{ fill: i === j ? "var(--chart-grid)" : col(M[i]![j]) }}
                onMouseMove={(e) => hover.show(e, `${S[i]} × ${S[j]}: ${M[i]![j] == null ? "—" : M[i]![j]!.toFixed(2)}`)} onMouseLeave={hover.hide} />
            ))}
          </g>
        ))}
      </svg>)}</div>
  );
}

/* ------------------------------------------------------------------ risk --------- */

/** The window the risk tiles describe: 1Y, or the longest fully observed fallback. */
/** All the risk tiles read: enough for the landing page's sample board to fill without
 *  inventing a whole analytics response. */
export type RiskData = Pick<AnalyticsResponse, "risk" | "risk_contributions">;

const riskWindow = (data: RiskData) => data.risk["1y"] ?? data.risk.available;

/** Histogram of daily moves (API: risk.return_histogram); the worst 5% are red. */
export function BadDayHistogram({ data }: { data: RiskData }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const win = riskWindow(data);
  const hist = win?.return_histogram;
  const h = 120, m = { l: 6, r: 6, t: 6, b: 18 };
  if (!hist || hist.counts.length === 0) return <div ref={ref} style={{ height: h }} />;
  const lo = hist.edges[0]!, hi = hist.edges[hist.edges.length - 1]!, top = Math.max(1, ...hist.counts);
  const iw = Math.max(1, w - m.l - m.r), X = (v: number) => m.l + ((v - lo) / (hi - lo || 1)) * iw, Y = (n: number) => m.t + (1 - n / top) * (h - m.t - m.b);
  const cut = win?.var_1d_95 != null ? -win.var_1d_95 : null;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Distribution of daily moves">
        {hist.counts.map((n, i) => {
          const x0 = hist.edges[i]!, x1 = hist.edges[i + 1]!, tail = cut != null && x1 <= cut;
          return (
            <rect key={i} x={X(x0) + 0.5} y={Y(n)} width={Math.max(0, X(x1) - X(x0) - 1)} height={h - m.b - Y(n)}
              style={{ fill: tail ? "var(--negative)" : "var(--ink-300)", opacity: tail ? 0.9 : 0.5 }}
              onMouseMove={(e) => hover.show(e, `${n} days between ${pctF(x0, 1)} and ${pctF(x1, 1)}`)} onMouseLeave={hover.hide} />
          );
        })}
        {cut != null && <line x1={X(cut)} x2={X(cut)} y1={m.t} y2={h - m.b} style={{ stroke: "var(--negative)", strokeDasharray: "3 2" }} />}
        {[...new Set([lo, 0, hi])].filter((t) => t >= lo && t <= hi).map((t) => <text key={t} x={X(t)} y={h - 3} textAnchor={t === lo ? "start" : t === hi ? "end" : "middle"} style={AXIS}>{pctF(t, 0, true)}</text>)}
      </svg>)}</div>
  );
}

/** Underwater curve (API: risk.drawdown_series): how far below its running peak. */
export function Underwater({ data }: { data: RiskData }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const win = riskWindow(data);
  const dd = win?.drawdown_series?.values ?? [];
  const dates = win?.drawdown_series?.dates ?? [];
  const h = 120, m = { l: 34, r: 6, t: 6, b: 18 };
  if (dd.length < 2) return <div ref={ref} style={{ height: h }} />;
  const lo = Math.min(...dd) * 1.1 || -0.01, iw = Math.max(1, w - m.l - m.r), ih = h - m.t - m.b;
  const X = (i: number) => m.l + (i / (dd.length - 1)) * iw, Y = (v: number) => m.t + (v / lo) * ih;
  const line = dd.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join("");
  const ti = dd.indexOf(Math.min(...dd));
  const marks = win?.drawdown_dates ?? {};
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Fall from peak">
        <path d={`${line}L${X(dd.length - 1)},${Y(0)}L${X(0)},${Y(0)}Z`} style={{ fill: "var(--negative-tint)" }} />
        <path d={line} style={{ fill: "none", stroke: "var(--negative)", strokeWidth: 1.4 }} />
        {[0, lo / 2, lo].map((t) => <text key={t} x={m.l - 5} y={Y(t)} dy=".32em" textAnchor="end" style={AXIS}>{pctF(t, 0)}</text>)}
        <text x={X(0)} y={h - 3} style={AXIS}>{dates[0]?.slice(0, 7)}</text>
        <text x={X(dd.length - 1)} y={h - 3} textAnchor="end" style={AXIS}>{dates[dates.length - 1]?.slice(0, 7)}</text>
        <circle cx={X(ti)} cy={Y(dd[ti]!)} r={4} style={{ fill: "var(--negative)" }}
          onMouseMove={(e) => hover.show(e, `Peak ${marks.peak ?? "—"} → trough ${marks.trough ?? "—"}${marks.recovered ? ` → back ${marks.recovered}` : ""}`)} onMouseLeave={hover.hide} />
      </svg>)}</div>
  );
}

/** Volatility bars (you vs index) and a beta needle on a 0–2 dial. */
export function SwingDial({ data }: { data: RiskData }) {
  const [ref, w] = useWidth();
  const r = data.risk["1y"] ?? data.risk.available;
  const v = r?.volatility ?? null, b = r?.bench_volatility ?? null, beta = r?.beta ?? null;
  const h = 120, bw = w * 0.55, mx = Math.max(v ?? 0, b ?? 0) * 1.1 || 1;
  const cx = bw + (w - bw) / 2, cy = 92, R = Math.max(10, Math.min(46, (w - bw) / 2 - 8));
  const betaLow = Math.min(0, Math.floor(beta ?? 0)), betaHigh = Math.max(2, Math.ceil(beta ?? 2));
  const ang = (x: number) => Math.PI + ((x - betaLow) / (betaHigh - betaLow)) * Math.PI;
  const arc = (a0: number, a1: number, r0: number, r1: number) =>
    `M${cx + r1 * Math.cos(a0)},${cy + r1 * Math.sin(a0)}A${r1},${r1} 0 0 1 ${cx + r1 * Math.cos(a1)},${cy + r1 * Math.sin(a1)}L${cx + r0 * Math.cos(a1)},${cy + r0 * Math.sin(a1)}A${r0},${r0} 0 0 0 ${cx + r0 * Math.cos(a0)},${cy + r0 * Math.sin(a0)}Z`;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Volatility and beta">
        {([["YOU", v, "var(--chart-1)"], ["INDEX", b, "var(--ink-400)"]] as const).map(([l, val, c], i) => (
          <g key={l} transform={`translate(0,${14 + i * 36})`}>
            <text y={-4} style={AXIS}>{l}</text>
            <rect width={val == null ? 0 : ((bw - 44) * val) / mx} height={14} rx={3} style={{ fill: c }} />
            <text x={val == null ? 0 : ((bw - 44) * val) / mx + 5} y={7} dy=".32em" style={INK}>{pctF(val, 0)}</text>
          </g>
        ))}
        <path d={arc(Math.PI, 2 * Math.PI, R - 8, R)} style={{ fill: "var(--surface-sunken)" }} />
        <path d={arc(ang(0.9), ang(1.1), R - 8, R)} style={{ fill: "var(--chart-grid)" }} />
        {beta != null && <line x1={cx} y1={cy} x2={cx + Math.cos(ang(beta)) * (R - 2)} y2={cy + Math.sin(ang(beta)) * (R - 2)} style={{ stroke: "var(--text-primary)", strokeWidth: 2.2, strokeLinecap: "round" }} />}
        <circle cx={cx} cy={cy} r={3.5} style={{ fill: "var(--text-primary)" }} />
        <text x={cx} y={cy + 18} textAnchor="middle" style={INK}>{beta == null ? "β —" : `β ${beta.toFixed(2)}`}</text>
        <text x={cx - R} y={cy + 12} textAnchor="middle" style={AXIS}>{betaLow}</text>
        <text x={cx + R} y={cy + 12} textAnchor="middle" style={AXIS}>{betaHigh}</text>
      </svg>)}</div>
  );
}

/** Share of money (grey, left) against share of risk (red, right), largest positions first. */
export function RiskButterfly({ data }: { data: RiskData }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const key = data.risk_contributions["1y"] ? "1y" : "available";
  const rows = [...(data.risk_contributions[key] ?? [])].sort((a, b) => b.weight_pct - a.weight_pct).slice(0, 12);
  const narrow = w < 620, bh = 22, h = rows.length * bh + 6;
  const mid = narrow ? 84 : w / 2, half = narrow ? (w - mid) / 2 - 24 : w / 2 - 90;
  const mx = Math.max(1, ...rows.map((r) => Math.max(r.weight_pct, Math.abs(r.risk_share_pct ?? 0))));
  const X = (v: number) => (Math.max(0, half) * v) / mx;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Share of money against share of risk">
        {rows.map((r, i) => {
          const y = i * bh + 3, risk = r.risk_share_pct, heavy = risk != null && risk > r.weight_pct * 1.25;
          const left = narrow ? mid : mid - 44, right = narrow ? mid + half + 24 : mid + 44;
          return (
            <g key={r.isin} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${r.symbol}: ${ppF(r.weight_pct)} of money → ${risk == null ? "—" : ppF(risk)} of risk`), onMouseLeave: hover.hide }}>
              <rect x={0} y={y - 2} width={w} height={bh} style={{ fill: "transparent" }} />
              <text x={narrow ? 0 : mid} y={y + 7} dy=".32em" textAnchor={narrow ? "start" : "middle"} style={heavy ? INK : INK2}>{r.symbol}</text>
              <rect x={narrow ? left : left - X(r.weight_pct)} y={y} width={X(r.weight_pct)} height={bh - 8} rx={2} style={{ fill: "var(--ink-300)" }} />
              {risk == null ? <line x1={right} x2={right + 20} y1={y + 6} y2={y + 6} style={{ stroke: "var(--border-strong)", strokeDasharray: "2 3" }} /> : <rect x={risk >= 0 ? right : right - X(-risk)} y={y} width={X(Math.abs(risk))} height={bh - 8} rx={2}
                style={{ fill: risk < 0 ? "var(--positive)" : "var(--negative)", opacity: heavy ? 1 : 0.6 }} />}
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/* ------------------------------------------------------------------ companies ---- */

/** Same bubble map; server-selected axes, references, exclusions and status. */
export function ValuationBubbles({ data, lens = "valuation" }: { data: AnalyticsResponse; lens?: CompanyLens }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const config = COMPANY_LENSES[lens], derived = data.derived.lenses[lens];
  const h = Math.max(240, Math.min(330, Math.round(w * 0.6))), m = { l: 42, r: 14, t: 10, b: 24 };
  const value = (p: AnalyticsResponse["positions"][number], field: string | undefined): number | null => {
    const v = field ? p[field as keyof typeof p] : null;
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  };
  const pts = data.positions.flatMap((p) => {
    if (!derived || derived.excluded?.[p.isin]) return [];
    const x = value(p, derived.x_field), y = value(p, derived.y_field);
    return x == null || y == null || (lens === "valuation" && (x <= 0 || y <= 0)) ? [] : [{ p, x, y }];
  });
  if (!pts.length) return <div ref={ref} style={{ height: h, display: "grid", placeItems: "center", color: "var(--text-tertiary)" }}>—</div>;
  const rx = derived?.x_average ?? null, ry = derived?.y_average ?? null;
  const log = lens === "valuation";
  const domain = (values: number[], reference: number | null) => {
    const all = reference != null && (!log || reference > 0) ? [...values, reference] : values;
    const lo = Math.min(...all), hi = Math.max(...all);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.15;
    return log ? [lo * 0.8, hi * 1.25] : [lo - pad, hi + pad];
  };
  const xd = domain(pts.map((p) => p.x), rx), yd = domain(pts.map((p) => p.y), ry);
  const scale = (v: number, d: number[], a: number, b: number) => {
    const f = log ? Math.log : (n: number) => n;
    return a + ((f(v) - f(d[0]!)) / (f(d[1]!) - f(d[0]!))) * (b - a);
  };
  const X = (v: number) => scale(v, xd, m.l, w - m.r), Y = (v: number) => scale(v, yd, h - m.b, m.t);
  const mw = Math.max(...pts.map(({ p }) => p.weight ?? 0)) || 1;
  const R = (wt: number) => 2.5 + Math.sqrt(wt / mw) * Math.min(32, w / 18);
  const ticks = (d: number[]) => log
    ? [0.1, 0.2, 0.5, 1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 500].filter((t) => t >= d[0]! && t <= d[1]!)
    : Array.from({ length: 4 }, (_, i) => d[0]! + (d[1]! - d[0]!) * i / 3);
  const fmt = (v: number) => config.percent ? pctF(v, 0) : v.toFixed(1);
  return <div ref={ref}>{w > 0 && <svg width={w} height={h} role="img" aria-label={`${config.label} of each company; reference is the eligible holdings mean`}>
    {rx != null && ry != null && (!log || (rx > 0 && ry > 0)) && <>
      {lens === "valuation" && <>
        <rect x={X(rx)} y={m.t} width={Math.max(0, w - m.r - X(rx))} height={Math.max(0, Y(ry) - m.t)} style={{ fill: "var(--negative-tint)", opacity: 0.75 }} />
        <rect x={m.l} y={Y(ry)} width={Math.max(0, X(rx) - m.l)} height={Math.max(0, h - m.b - Y(ry))} style={{ fill: "var(--positive-tint)", opacity: 0.75 }} />
      </>}
      <line x1={X(rx)} x2={X(rx)} y1={m.t} y2={h - m.b} style={{ stroke: "var(--ink-400)", strokeDasharray: "4 3" }} />
      <line y1={Y(ry)} y2={Y(ry)} x1={m.l} x2={w - m.r} style={{ stroke: "var(--ink-400)", strokeDasharray: "4 3" }} />
    </>}
    {ticks(xd).map((t) => <text key={`x${t}`} x={X(t)} y={h - 6} textAnchor="middle" style={AXIS}>{fmt(t)}</text>)}
    {ticks(yd).map((t) => <text key={`y${t}`} x={m.l - 6} y={Y(t)} dy=".32em" textAnchor="end" style={AXIS}>{fmt(t)}</text>)}
    <text x={w - m.r} y={h - m.b - 6} textAnchor="end" style={AXIS}>{config.x} →</text>
    <text x={m.l + 4} y={m.t + 10} style={AXIS}>↑ {config.y}</text>
    {[...pts].sort((a, b) => (b.p.weight ?? 0) - (a.p.weight ?? 0)).map(({ p, x, y }) => <circle key={p.isin} cx={X(x)} cy={Y(y)} r={R(p.weight ?? 0)}
      style={{ fill: "var(--chart-1)", fillOpacity: 0.5, stroke: "var(--chart-1)" }}
      onMouseMove={(e) => hover.show(e, `${sym(p)} · ${ppF(p.weight)}${data.coverage.basis === "priced_subset" ? " of priced subtotal" : ""} · ${config.x} ${fmt(x)} · ${config.y} ${fmt(y)}${p.fundamentals_as_of ? ` · inputs ${p.fundamentals_as_of}` : ""} · ${derived?.position_statuses[p.isin]?.replaceAll("_", " ") ?? "unrated"}`)} onMouseLeave={hover.hide} />)}
    {pts.filter(({ p }) => (p.weight ?? 0) >= 3).map(({ p, x, y }) => <text key={`l${p.isin}`} x={X(x)} y={Y(y)} dy=".32em" textAnchor="middle" style={{ ...INK, fontSize: 10, pointerEvents: "none" }}>{sym(p)}</text>)}
  </svg>}</div>;
}

/** The book's ratios as bars, with the index's published value as a tick where one exists. */
export function BookVsIndex({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const f = data.fundamentals, c = data.characteristics;
  const comparable = f.benchmark_comparison === "compatible";
  const ib: Record<string, number | null> = comparable ? f.benchmark ?? {} : {};
  const excluded = (label: string) => label === "P/E" ? f.pe.excluded_weight_pct : label === "P/B" ? f.pb.excluded_weight_pct : f.dividend_yield.excluded_weight_pct;
  const rows: [string, number | null | undefined, number | null | undefined, (v: number) => string][] = [
    ["P/E", f.pe.value, ib.pe, (v) => v.toFixed(1)],
    ["P/B", f.pb.value, ib.pb, (v) => v.toFixed(1)],
    ["Yield", f.dividend_yield.value, ib.dividend_yield, (v) => pctF(v, 1)],
    ["ROE", c.roe?.value, null, (v) => pctF(v, 0)],
    ["D/E", c.debt_to_equity?.value, null, (v) => `${v.toFixed(2)}×`],
    ["12–1", c.momentum_12_1?.value, null, (v) => pctF(v, 1, true)],
  ];
  const bh = 40, m = { l: 48, r: 54 }, iw = Math.max(1, w - m.l - m.r);
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={rows.length * bh} role="img" aria-label="Portfolio ratios against the index">
        {rows.map(([label, v, iv, fmt], i) => {
          const y = i * bh + 12;
          if (v == null) return <g key={label}><text y={y + 5} dy=".32em" style={INK2}>{label}</text><text x={w - m.r + 6} y={y + 5} dy=".32em" style={AXIS}>—</text></g>;
          const lo = Math.min(0, v, iv ?? 0) * 1.35, hi = Math.max(0, v, iv ?? 0) * 1.35 || Math.abs(lo) * 0.25;
          const X = (x: number) => m.l + ((x - lo) / (hi - lo || 1)) * iw;
          return (
            <g key={label} onMouseMove={(e) => hover.show(e, `${label}: ${fmt(v)}${["P/E", "P/B", "Yield"].includes(label) ? ` · ${excluded(label).toFixed(1)}% of priced weight excluded` : ""}`)} onMouseLeave={hover.hide}>
              <text y={y + 5} dy=".32em" style={INK2}>{label}</text>
              <rect x={m.l} y={y} width={iw} height={10} rx={5} style={{ fill: "var(--surface-sunken)" }} />
              <rect x={Math.min(X(0), X(v))} y={y} width={Math.abs(X(v) - X(0))} height={10} rx={5} style={{ fill: v < 0 ? "var(--negative)" : "var(--chart-1)", opacity: 0.85 }} />
              {iv != null && <><rect x={X(iv) - 1.5} y={y - 6} width={3} height={22} style={{ fill: "var(--ink-400)" }} />
                <text x={X(iv)} y={y + 28} textAnchor="middle" style={AXIS}>{fmt(iv)}</text></>}
              <text x={w - m.r + 6} y={y + 5} dy=".32em" style={{ fill: "var(--text-primary)", font: "500 14px var(--font-figure)" }}>{fmt(v)}</text>
            </g>
          );
        })}
      </svg>)}
      {!comparable && <div style={{ font: "500 11px var(--font-label)", color: "var(--text-tertiary)", display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span title="Reported index ratios use different aggregation, loss and coverage conventions; no comparable spread is established.">Reported index</span>
        {(["pe", "pb", "dividend_yield"] as const).map((key) => f.benchmark?.[key] != null && <span key={key}>{key === "dividend_yield" ? "Yield" : key === "pe" ? "P/E" : "P/B"} {key === "dividend_yield" ? pctF(f.benchmark[key], 1) : f.benchmark[key]!.toFixed(1)}</span>)}
        {f.benchmark_as_of && <span>{f.benchmark_as_of}</span>}
      </div>}
    </div>
  );
}

/* ------------------------------------------------------------------ trust -------- */

/** One ring per input: the gap is the money the number cannot speak for. */
export function CoverageRings({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const c = data.coverage, P = data.positions;
  const miss = (pred: (p: (typeof P)[number]) => boolean) => P.filter(pred).map(sym);
  const rings: [string, number | null, string[]][] = [
    ["Price", c.priced_pct, data.unresolved.map(sym)],
    ["P/E", c.pe_pct, miss((p) => p.pe == null)],
    ["ROE", c.roe_pct, miss((p) => p.roe == null)],
    ["D/E", c.debt_to_equity_pct, miss((p) => p.debt_to_equity == null)],
    ["Sector", c.sector_pct, miss((p) => !p.sector)],
    ["History", c.price_history_pct, []],
  ];
  const cols = w < 420 ? 3 : 6, per = Math.min(84, w / cols), R = per / 2 - 8;
  const ring = (frac: number, r: number) => {
    const a = Math.min(0.9999, Math.max(0, frac)) * 2 * Math.PI;
    return `M0,${-r}A${r},${r} 0 ${a > Math.PI ? 1 : 0} 1 ${r * Math.sin(a)},${-r * Math.cos(a)}`;
  };
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={Math.ceil(rings.length / cols) * (per + 16)} role="img" aria-label="Coverage of the priced subtotal; full-book price coverage is unknown when a value is missing">
        {rings.map(([l, v, missing], i) => (
          <g key={l} transform={`translate(${(i % cols) * per + per / 2},${Math.floor(i / cols) * (per + 16) + per / 2 - 4})`}
            onMouseMove={(e) => hover.show(e, `${l}: ${v == null ? `value coverage unknown · ${c.priced_count ?? P.length}/${c.total_count ?? P.length + data.unresolved.length} positions priced` : `${v.toFixed(1)}% of ${c.basis === "priced_subset" ? "priced subtotal" : "money"} covered`}${missing.length ? ` · missing ${missing.slice(0, 8).join(", ")}` : ""}`)} onMouseLeave={hover.hide}>
            <circle r={R - 3} style={{ fill: "none", stroke: "var(--surface-sunken)", strokeWidth: 6 }} />
            {v != null && <path d={ring(v / 100, R - 3)} style={{ fill: "none", strokeWidth: 6, strokeLinecap: "round", stroke: v >= 95 ? "var(--positive)" : v >= 75 ? "var(--warning)" : "var(--negative)" }} />}
            <text textAnchor="middle" dy=".35em" style={{ fill: "var(--text-primary)", font: "500 14px var(--font-figure)" }}>{v == null ? "—" : Math.round(v)}</text>
            <text textAnchor="middle" y={R + 12} style={AXIS}>{l}</text>
          </g>
        ))}
      </svg>)}</div>
  );
}

/** One-day moves above 15% inside the risk windows: size = money, colour = explanation. */
export function MoveTimeline({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const ev = data.price_events.filter((e) => e.weight_pct >= 0.05);
  const h = 140, m = { l: 12, r: 12, t: 14, b: 20 };
  if (!ev.length) return <div ref={ref} style={{ height: 40 }} />;
  const t = ev.map((e) => Date.parse(e.date)), t0 = Math.min(...t), t1 = Math.max(...t);
  const X = (d: string) => m.l + ((Date.parse(d) - t0) / (t1 - t0 || 1)) * Math.max(1, w - m.l - m.r);
  const Y = (c: number) => m.t + (1 - (Math.max(-40, Math.min(40, c)) + 40) / 80) * (h - m.t - m.b);
  const kind = (r: string) => /price band/i.test(r) ? "band" : /on record/i.test(r) ? "action" : "check";
  const color = { band: "var(--ink-400)", action: "var(--chart-1)", check: "var(--warning)" } as const;
  const years = [...new Set(ev.map((e) => e.date.slice(0, 4)))].sort();
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Large one-day moves">
        <line x1={m.l} x2={w - m.r} y1={Y(0)} y2={Y(0)} style={{ stroke: "var(--chart-grid)" }} />
        <text x={2} y={m.t + 4} style={AXIS}>+</text><text x={2} y={h - m.b} style={AXIS}>−</text>
        {years.map((y) => <text key={y} x={X(`${y}-07-01`)} y={h - 3} textAnchor="middle" style={AXIS}>{y}</text>)}
        {ev.map((e) => { const k = kind(e.reason); return (
          <circle key={`${e.isin}${e.date}`} cx={X(e.date)} cy={Y(e.change_pct)} r={3 + Math.sqrt(e.weight_pct / 25) * 13}
            style={{ fill: color[k], fillOpacity: 0.55, stroke: color[k] }}
            onMouseMove={(ev_) => hover.show(ev_, <>{e.symbol} {e.date}: {ppF(e.change_pct, 1, true)} · {ppF(e.weight_pct)} of money<br />{e.reason}</>)} onMouseLeave={hover.hide} />); })}
      </svg>)}</div>
  );
}

/** Sharpe (you vs index) and Sortino on one zero-centred scale; risk-free in the tooltip. */
export function RewardPerRisk({ data }: { data: RiskData }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const r = riskWindow(data);
  const rows = ([
    ["Sharpe · you", r?.sharpe, "var(--chart-1)"],
    ["Sharpe · index", r?.bench_sharpe, "var(--ink-400)"],
    ["Sortino · you", r?.sortino, "var(--chart-4)"],
  ] as const).filter(([, v]) => v != null);
  const h = 120, m = { l: 92, r: 40 };
  if (!rows.length) return <div ref={ref} style={{ height: h }} />;
  const span = Math.max(1, ...rows.map(([, v]) => Math.abs(v!))) * 1.15;
  const iw = Math.max(1, w - m.l - m.r), X = (v: number) => m.l + ((v + span) / (2 * span)) * iw;
  const tipText = `Risk-free ${pctF(r?.risk_free_rate, 2)} a year (Nifty 1D Rate Index over this window)`;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Return per unit of risk">
        <line x1={X(0)} x2={X(0)} y1={0} y2={h - 14} style={{ stroke: "var(--ink-400)" }} />
        <text x={X(0)} y={h - 2} textAnchor="middle" style={AXIS}>0</text>
        {rows.map(([label, v, color], i) => {
          const y = 8 + i * 34;
          return (
            <g key={label} {...{ onMouseMove: (e: React.MouseEvent) => hover.show(e, `${label}: ${v!.toFixed(2)} · ${tipText}`), onMouseLeave: hover.hide }}>
              <text x={0} y={y + 7} dy=".32em" style={INK2}>{label}</text>
              <rect x={Math.min(X(0), X(v!))} y={y} width={Math.abs(X(v!) - X(0))} height={14} rx={3} style={{ fill: color }} />
              <text x={v! < 0 ? X(v!) - 4 : X(v!) + 4} y={y + 7} dy=".32em" textAnchor={v! < 0 ? "end" : "start"} style={INK}>{v!.toFixed(2)}</text>
            </g>
          );
        })}
      </svg>)}</div>
  );
}

const FACTOR_ROWS: [key: string, label: string, how: string][] = [
  ["momentum", "Momentum", "12-to-1-month return divided by daily volatility"],
  ["value", "Value", "earnings yield and book-to-price, standardised against the universe"],
  ["quality", "Quality", "ROE, net margin and low leverage, standardised against the universe"],
  ["low_volatility", "Low swing", "lower one-year daily volatility ranks higher"],
];

/** Percentile within the market universe: dots = holdings (size = money), bar = book. */
export function FactorStrips({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  const bh = 46, m = { l: 92, r: 44 }, iw = Math.max(1, w - m.l - m.r);
  const X = (v: number) => m.l + (v / 100) * iw;
  const mw = Math.max(1, ...data.positions.map((p) => p.weight ?? 0));
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={FACTOR_ROWS.length * bh + 16} role="img" aria-label="Factor percentiles against the market universe">
        <Hatch id="tp-hatch-factor" />
        {[0, 50, 100].map((t) => <text key={t} x={X(t)} y={FACTOR_ROWS.length * bh + 12} textAnchor="middle" style={AXIS}>{t}</text>)}
        {FACTOR_ROWS.map(([key, label, how], i) => {
          const f = data.factors[key], y = i * bh + 14;
          const howMeasured = data.advisor ? ({
            momentum: "252-to-21-session price return; tied peer percentiles",
            value: "mean of earnings-yield and book-yield peer percentiles",
            quality: "mean of ROE, net margin and lower-leverage peer percentiles",
            low_volatility: "lower 252-session sample volatility; tied peer percentiles",
          } as Record<string, string>)[key] ?? how : how;
          return (
            <g key={key}>
              <title>{`${howMeasured} · ${f?.eligible_count ?? f?.universe_size ?? 0} eligible peers · ${f?.reasons?.join(" · ") ?? ""}`}</title>
              <text y={y + 6} dy=".32em" style={INK2}>{label}</text>
              <rect x={m.l} y={y} width={iw} height={12} rx={6} style={{ fill: f?.score != null ? "var(--surface-sunken)" : "url(#tp-hatch-factor)" }} />
              <line x1={X(50)} x2={X(50)} y1={y - 5} y2={y + 17} style={{ stroke: "var(--ink-400)" }} />
              {data.positions.map((p) => {
                const v = p.factors?.[key];
                return v == null ? null : (
                  <circle key={p.isin} cx={X(v)} cy={y + 6} r={2 + Math.sqrt((p.weight ?? 0) / mw) * 7}
                    style={{ fill: "var(--chart-1)", fillOpacity: 0.25, stroke: "var(--chart-1)", strokeOpacity: 0.5 }}
                    onMouseMove={(e) => hover.show(e, `${sym(p)} · ${ppF(p.weight)} of money · ${label} ${v.toFixed(0)}th percentile`)} onMouseLeave={hover.hide} />
                );
              })}
              {f?.score != null && <>
                <rect x={X(f.score) - 2} y={y - 7} width={4} height={26} rx={2} style={{ fill: "var(--text-primary)" }}
                  onMouseMove={(e) => hover.show(e, `Book ${label.toLowerCase()}: ${f.score!.toFixed(0)}th percentile of ${f.universe} (${f.universe_size} names) · covers ${ppF(f.coverage_pct, 0)} of money · ${howMeasured}`)} onMouseLeave={hover.hide} />
                <text x={w - m.r + 6} y={y + 6} dy=".32em" style={{ fill: "var(--text-primary)", font: "500 15px var(--font-figure)" }}>{f.score.toFixed(0)}</text>
              </>}
              {f?.score == null && <text x={w - m.r + 6} y={y + 6} dy=".32em" style={AXIS}>—</text>}
            </g>
          );
        })}
      </svg>)}</div>
  );
}

/** One withheld vendor record per row: our name over the vendor's, and the two answers. */
export function IdentityQuestions({ items, onDecide, busy }: {
  items: import("@trackr/core").IdentityReview[];
  onDecide: (item: import("@trackr/core").IdentityReview, decision: "SAME" | "DIFFERENT") => void;
  busy: string | null;
}) {
  const btn = (active: boolean, tone: "pos" | "neg"): React.CSSProperties => ({
    font: "600 11.5px var(--font-label)", padding: "5px 10px", borderRadius: 6, cursor: "pointer",
    border: `1px solid ${tone === "pos" ? "var(--positive)" : "var(--negative)"}`,
    background: active ? (tone === "pos" ? "var(--positive)" : "var(--negative)") : "transparent",
    color: active ? "#fff" : tone === "pos" ? "var(--positive)" : "var(--negative)",
  });
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it) => {
        const key = `${it.isin}|${it.ticker}|${it.vendor_name}`;
        return (
          <div key={key} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto", gap: 10, alignItems: "center",
            padding: "8px 0", borderTop: "1px solid var(--border)" }}>
            <div style={{ minWidth: 0, display: "grid", gridTemplateColumns: "auto minmax(0,1fr)", columnGap: 8, rowGap: 2,
              font: "500 12px var(--font-label)" }}>
              <span style={{ color: "var(--text-tertiary)" }}>{it.symbol ?? it.isin}</span>
              <span style={{ color: "var(--text-primary)", overflowWrap: "anywhere" }}>{it.our_name ?? "—"}</span>
              <span style={{ color: "var(--text-tertiary)" }}>{it.ticker}</span>
              <span style={{ color: "var(--text-secondary)", overflowWrap: "anywhere" }}>{it.vendor_name}</span>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button type="button" disabled={busy === key} style={btn(it.decision === "SAME", "pos")}
                aria-pressed={it.decision === "SAME"} onClick={() => onDecide(it, "SAME")}>Same</button>
              <button type="button" disabled={busy === key} style={btn(it.decision === "DIFFERENT", "neg")}
                aria-pressed={it.decision === "DIFFERENT"} onClick={() => onDecide(it, "DIFFERENT")}>Different</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
