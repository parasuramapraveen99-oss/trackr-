"use client";

/**
 * The Health sub-tab, drawn in the Risk board's vocabulary: one question per tile, answered
 * by a shape. Every mark is placed from the advisor report (portfolio-health-v1, its
 * evidence, capital states, ledger KPIs); labels are at most three words and every method
 * note, exact value and reason lives in the hover tooltip on the mark it describes.
 * A withheld figure is a hatched mark with its named reason -- never an average, never
 * renormalised away.
 *
 * Geometry identity used by HealthScale: the stacked points bar ends at
 * sum(known contributions) = score_bounds.lower, and the hatched tail ends at
 * lower + sum(missing weights) x 100 = score_bounds.upper. So the bar IS the bound.
 */

import { useId, type ReactNode } from "react";
import { parseDecimal, sumDecimals, usePrivacy } from "@trackr/core";
import type { AdvisorCapitalStates, AdvisorHealthComponentRaw, AnalyticsResponse, MilestoneMetric } from "@trackr/core";
import { MissingVisual } from "./AdvisorVisuals";
import { RiskButterfly, Small, compactInr, useTip, useWidth } from "./visual";
import { pct, ratio } from "./format";

const AXIS: React.CSSProperties = { fill: "var(--chart-axis-text)", font: "500 10.5px var(--font-label)" };
const INK: React.CSSProperties = { fill: "var(--text-primary)", font: "600 11px var(--font-label)" };
const INK2: React.CSSProperties = { fill: "var(--text-secondary)", font: "500 11px var(--font-label)" };
const FIG: React.CSSProperties = { fill: "var(--text-primary)", font: "500 15px var(--font-figure)" };

export const PART_COLOR: Record<string, string> = {
  concentration: "var(--chart-1)", diversification: "var(--chart-2)", risk: "var(--chart-4)",
  quality: "var(--chart-3)", valuation: "var(--chart-6)",
};

/** Band floors, descending, as the backend sends them; this copy only draws an axis before data. */
const DEFAULT_BANDS: [string, number][] = [["A", 80], ["B", 65], ["C", 50], ["D", 35], ["E", 20], ["F", 0]];

const symbolOf = (data: AnalyticsResponse, isin: string) => data.positions.find((p) => p.isin === isin)?.symbol ?? isin;
const weightOf = (data: AnalyticsResponse, isin: string) => data.positions.find((p) => p.isin === isin)?.weight ?? 0;
const num = (value: unknown): number | null => {
  if (value == null) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
};

/**
 * Plain-English reason for a withheld figure, for tooltips. Codes carrying an ISIN are
 * named by symbol; an unmapped code is shown as itself, so a new reason is never hidden.
 */
export function explain(reason: string, data: AnalyticsResponse): string {
  const [code = reason, ...rest] = reason.split(":");
  const who = rest[0] ? symbolOf(data, rest[0]) : "";
  const map: Record<string, string> = {
    MISSING_SECTOR_CLASSIFICATION: "some holdings have no sector",
    MISSING_SECTOR_ALLOCATION: "no sector allocation",
    MISSING_COMPLETE_252_INTERVAL_WINDOW: "needs 252 unbroken daily returns",
    INCOMPLETE_252_INTERVAL_WINDOW: "the last 252 sessions are incomplete",
    INCOMPLETE_WINDOW_HISTORY: "a session in the last year has no portfolio snapshot",
    INSUFFICIENT_CALENDAR_COVERAGE: "not enough market history",
    INCOMPLETE_QUALITY_COVERAGE: "company financials missing for some holdings",
    CONFLICTING_STALE_OR_INVALID_QUALITY_INPUT: "company financials stale or conflicting",
    VALUE_FACTOR_UNAVAILABLE: "no dated valuation ranks",
    PARTIAL_VALUATION_COVERAGE: "valuation ranks cover only part of the book",
    INSUFFICIENT_ELIGIBLE_PEERS: "fewer than 30 ranked peers",
    MISSING_UNIVERSE: "no peer universe",
    UNKNOWN_VALUE_DENOMINATOR: "some holdings have no price",
    UNRECONCILED_SEGMENTS: "some holdings do not reconcile yet",
    INCOMPLETE_ASSET_HISTORY: `price history incomplete${who ? ` for ${who}` : ""}`,
    MISSING_HEALTH_PROFILE: `no bank / non-financial classification${who ? ` for ${who}` : ""}`,
    MISSING_QUALITY_INPUT: `${rest[1]?.replaceAll("_", " ") ?? "a ratio"} missing${who ? ` for ${who}` : ""}`,
    INVALID_BANK_RATIO: `bank ratio out of range${who ? ` for ${who}` : ""}`,
    PAIR_CORRELATION_UNAVAILABLE: "a pair of holdings has no measurable correlation",
    HISTORY_BASIS_MISMATCH: "price histories are on different bases",
    MISSING_HEALTH_COMPONENT: "a part is missing",
    MISSING_COMPONENT_SCORES: "needs all five parts",
    MISSING_HEALTH_SCORE: "needs all five parts",
    INCOMPLETE_HEALTH_RECOVERY_WINDOW: "needs the full one-year window",
    FEATURE_DISABLED: "switched off on this server",
    MISSING_UPSTREAM_KPI: "not supplied by the ledger engine",
    UNRECONCILED_SEGMENTS_EXCLUDED: "unreconciled holdings left out",
    PROFILE_NOT_YET_CLASSIFIED: "not yet classified by the exchange",
    LOOKAHEAD_INPUT: "classified after this date",
    HISTORICAL_PROFILE_NOT_RECORDED: "no classification recorded for this date",
    UNSUPPORTED_FINANCIAL: `NBFC / insurer: no v1 test${rest[0] ? ` (${rest[0]})` : ""}`,
  };
  return map[code] ?? reason.toLowerCase().replaceAll("_", " ");
}
export const explainAll = (reasons: string[] | undefined, data: AnalyticsResponse) =>
  [...new Set((reasons ?? []).filter((r) => !r.startsWith("MISSING_COMPONENT:")).map((r) => explain(r, data)))].join(" · ");

export function rawComponents(data: AnalyticsResponse): AdvisorHealthComponentRaw[] {
  const health = data.advisor?.sections.overview.health as unknown as { components?: AdvisorHealthComponentRaw[] } | undefined;
  return health?.components ?? [];
}

function Hatch({ id }: { id: string }) {
  return <defs><pattern id={id} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width={2} height={6} style={{ fill: "var(--ink-300)" }} /></pattern></defs>;
}
const useHatch = () => useId().replaceAll(":", "");

/** A withheld tile body: the hatched gap, its reason on hover. */
function Withheld({ reason, height }: { reason: string; height: number }) {
  return <MissingVisual height={height} reason={`Withheld · ${reason}`} />;
}

/** Faint grade bands behind a 0-100 axis, shared by the scale and sensitivity tiles. */
function Bands({ bands, X, y, h }: { bands: [string, number][]; X: (v: number) => number; y: number; h: number }) {
  return <>{bands.map(([grade, floor], i) => {
    const top = i === 0 ? 100 : bands[i - 1]![1];
    return <g key={grade}>
      <rect x={X(floor)} y={y} width={Math.max(0, X(top) - X(floor) - 1)} height={h} rx={2} style={{ fill: i % 2 ? "var(--surface-sunken)" : "var(--surface-hover)" }} />
      <text x={(X(floor) + X(top)) / 2} y={y + h / 2} dy=".32em" textAnchor="middle" style={{ ...AXIS, font: "600 10.5px var(--font-label)" }}>{grade}</text>
    </g>;
  })}</>;
}

/* ------------------------------------------------------------- grade scale ------- */

/** Grade bands on a 0-100 axis, points earned stacked on it, missing parts hatched. */
export function HealthScale({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const hatch = useHatch();
  const h = data.health;
  const bands = h.bands.length ? h.bands : DEFAULT_BANDS;
  const m = { l: 6, r: 6 }, iw = Math.max(1, w - m.l - m.r);
  const X = (v: number) => m.l + (Math.max(0, Math.min(100, v)) / 100) * iw;
  const exact = num(h.score_exact);
  const parts = [...h.components.filter((c) => c.points != null), ...h.components.filter((c) => c.points == null)];
  let x0 = 0;
  const limits = (h.limitations ?? []).map((l) => l.toLowerCase().replaceAll("_", " ")).join(" · ");
  return <div ref={ref}>{w > 0 && <svg width={w} height={124} role="img" aria-label="Health score on the grade scale">
    <Hatch id={hatch} />
    <Bands bands={bands} X={X} y={4} h={22} />
    {[0, 20, 35, 50, 65, 80, 100].map((t) => <text key={t} x={X(t)} y={40} textAnchor={t === 0 ? "start" : t === 100 ? "end" : "middle"} style={AXIS}>{t}</text>)}
    {parts.map((c) => {
      const width = c.points != null ? c.points : c.weight * 100, x = x0;
      x0 += width;
      const text = c.points != null
        ? `${c.label}: ${c.points.toFixed(2)} points = score ${c.score?.toFixed(1)} × weight ${Math.round(c.weight * 100)}%`
        : `${c.label}: up to ${Math.round(c.weight * 100)} points unknown · ${explainAll(c.reasons, data)}`;
      return <rect key={c.key} x={X(x)} y={50} width={Math.max(0, X(x + width) - X(x) - 1.5)} height={26} rx={3}
        style={{ fill: c.points != null ? PART_COLOR[c.key] ?? "var(--chart-1)" : `url(#${hatch})` }}
        onMouseMove={(e) => tip.show(e, text)} onMouseLeave={tip.hide} />;
    })}
    <rect x={X(0)} y={50} width={iw} height={26} rx={3} style={{ fill: "none", stroke: "var(--border-strong)", pointerEvents: "none" }} />
    {exact != null && <g onMouseMove={(e) => tip.show(e, `Score ${exact.toFixed(4)} → ${h.score} (half-up) → grade ${h.grade} · uncalibrated house rubric · ${limits}`)} onMouseLeave={tip.hide}>
      <line x1={X(exact)} x2={X(exact)} y1={44} y2={84} style={{ stroke: "var(--text-primary)", strokeWidth: 2 }} />
      <circle cx={X(exact)} cy={44} r={4} style={{ fill: "var(--text-primary)" }} />
      <text x={X(exact)} y={104} textAnchor="middle" style={FIG}>{exact.toFixed(1)}</text>
    </g>}
    {exact == null && h.score_bounds && <g onMouseMove={(e) => tip.show(e, `Missing-information range, not a forecast: measured parts give ${h.score_bounds!.lower.toFixed(2)}; withheld parts could add up to ${(h.score_bounds!.upper - h.score_bounds!.lower).toFixed(0)} more · ${explainAll(h.reasons, data)}`)} onMouseLeave={tip.hide}>
      <path d={`M${X(h.score_bounds.lower)},84 v6 H${X(h.score_bounds.upper)} v-6`} style={{ fill: "none", stroke: "var(--text-secondary)", strokeWidth: 1.5 }} />
      <text x={X(h.score_bounds.lower)} y={108} textAnchor="middle" style={FIG}>{h.score_bounds.lower.toFixed(0)}</text>
      <text x={X(h.score_bounds.upper)} y={108} textAnchor="middle" style={FIG}>{h.score_bounds.upper.toFixed(0)}</text>
    </g>}
  </svg>}</div>;
}

/* ------------------------------------------------------------- evidence ring ----- */

/** Parts measured (ring, arc = weight) and validation stages (track). */
export function HealthMaturity({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const hatch = useHatch();
  const h = data.health;
  const v = h.evidence?.validation ?? {};
  const R = 48, r0 = 33, cx = 54, cy = 60;
  const measured = h.components.filter((c) => c.score != null).length;
  let a = -Math.PI / 2;
  const arc = (a0: number, a1: number) => {
    const large = a1 - a0 > Math.PI ? 1 : 0;
    return `M${cx + R * Math.cos(a0)},${cy + R * Math.sin(a0)}A${R},${R} 0 ${large} 1 ${cx + R * Math.cos(a1)},${cy + R * Math.sin(a1)}L${cx + r0 * Math.cos(a1)},${cy + r0 * Math.sin(a1)}A${r0},${r0} 0 ${large} 0 ${cx + r0 * Math.cos(a0)},${cy + r0 * Math.sin(a0)}Z`;
  };
  const stages = [["Calibration", v.grade_calibration], ["Indian data", v.indian_equity_mf_validation], ["Expert review", v.independent_human_review]] as const;
  const tx = 128, ty = 24, step = 36;
  return <div ref={ref}>{w > 0 && <svg width={w} height={122} role="img" aria-label="Parts measured and validation stages">
    <Hatch id={hatch} />
    {h.components.map((c) => {
      const a0 = a, a1 = a + c.weight * 2 * Math.PI;
      a = a1;
      return <path key={c.key} d={arc(a0 + 0.03, a1 - 0.03)} style={{ fill: c.score != null ? PART_COLOR[c.key] : `url(#${hatch})` }}
        onMouseMove={(e) => tip.show(e, `${c.label} · ${Math.round(c.weight * 100)}% · ${c.score != null ? `measured ${c.score.toFixed(1)}` : explainAll(c.reasons, data)}`)} onMouseLeave={tip.hide} />;
    })}
    <text x={cx} y={cy - 3} textAnchor="middle" style={{ ...FIG, font: "500 20px var(--font-figure)" }}>{measured}/{h.components.length}</text>
    <text x={cx} y={cy + 14} textAnchor="middle" style={AXIS}>measured</text>
    {w > tx + 70 && <>
      <line x1={tx + 6} x2={tx + 6} y1={ty} y2={ty + step * 2} style={{ stroke: "var(--border-strong)", strokeWidth: 2 }} />
      {stages.map(([label, state], i) => {
        const done = state === "passed";
        return <g key={label} onMouseMove={(e) => tip.show(e, `${label}: ${state ?? "pending"}`)} onMouseLeave={tip.hide}>
          <rect x={tx} y={ty + i * step - 12} width={w - tx} height={24} style={{ fill: "transparent" }} />
          <circle cx={tx + 6} cy={ty + i * step} r={6} style={{ fill: done ? "var(--positive)" : "var(--surface)", stroke: done ? "var(--positive)" : "var(--warning)", strokeWidth: 2 }} />
          <text x={tx + 20} y={ty + i * step} dy=".32em" style={INK2}>{label}</text>
          <text x={w - 4} y={ty + i * step} dy=".32em" textAnchor="end" style={{ ...AXIS, fill: done ? "var(--positive)" : "var(--warning)" }}>{state ?? "pending"}</text>
        </g>;
      })}
    </>}
  </svg>}</div>;
}

/* ------------------------------------------------------------- five parts -------- */

type Dot = { v: number; r: number; tip: string };

/** The marks inside each part's strip: anchor sub-scores, or holdings for quality/valuation. */
function partDots(key: string, ev: Record<string, number | null>, raw: Record<string, unknown>, data: AnalyticsResponse): Dot[] {
  const sub = (name: string, label: string, value: string): Dot[] => ev[`${name}_score`] == null ? [] : [{ v: ev[`${name}_score`]!, r: 4.5, tip: `${label} ${value} → ${ev[`${name}_score`]!.toFixed(0)}` }];
  const mw = Math.max(1, ...data.positions.map((p) => p.weight ?? 0));
  const size = (isin: string) => 2 + Math.sqrt(weightOf(data, isin) / mw) * 6;
  switch (key) {
    case "concentration":
      return [
        ...sub("effective_holdings", "Effective holdings", ratio(ev.effective_holdings, 1)),
        ...sub("largest_holding", `Largest (${raw.largest_holding_isin ? symbolOf(data, String(raw.largest_holding_isin)) : "—"})`, pct(ev.largest_holding_weight, 1)),
        ...sub("effective_sectors", `Effective sectors of ${ev.sector_count ?? "—"}`, ratio(ev.effective_sectors, 1)),
      ];
    case "risk":
      return [
        ...sub("volatility", "Volatility", pct(ev.volatility, 1)),
        ...sub("drawdown", "Deepest fall", pct(ev.max_drawdown_magnitude, 1)),
        ...sub("var95", "1-day VaR 95%", pct(ev.var_1d_95, 2)),
      ];
    case "quality": {
      const holdings = (raw.holdings ?? {}) as Record<string, { profile: string; score: string; subscores: Record<string, string> }>;
      return Object.entries(holdings).map(([isin, q]) => ({
        v: Number(q.score), r: size(isin),
        tip: `${symbolOf(data, isin)} · ${q.profile.replace("_", "-")} · ${Number(q.score).toFixed(1)} · ${Object.entries(q.subscores).map(([k, s]) => `${k.replaceAll("_", " ")} ${Number(s).toFixed(0)}`).join(" · ")}`,
      }));
    }
    case "valuation":
      return data.positions.filter((p) => p.factors?.value != null).map((p) => ({ v: p.factors.value!, r: size(p.isin), tip: `${p.symbol ?? p.isin} · value rank ${p.factors.value!.toFixed(0)} of 100` }));
    default:
      return [];
  }
}

/** One strip per part: bar = part score, dots = what it was built from, right = points. */
export function HealthParts({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const hatch = useHatch();
  const raw = new Map(rawComponents(data).map((c) => [c.key, c.evidence]));
  const narrow = w < 520;
  const rowH = 46, m = { l: narrow ? 108 : 124, r: 56 }, iw = Math.max(1, w - m.l - m.r);
  const X = (v: number) => m.l + (Math.max(0, Math.min(100, v)) / 100) * iw;
  const comps = data.health.components;
  return <div ref={ref}>{w > 0 && <svg width={w} height={comps.length * rowH + 16} role="img" aria-label="Score of each health part">
    <Hatch id={hatch} />
    {[0, 50, 100].map((t) => <g key={t}>
      <line x1={X(t)} x2={X(t)} y1={2} y2={comps.length * rowH} style={{ stroke: "var(--chart-grid)", strokeDasharray: t === 50 ? "2 3" : undefined }} />
      <text x={X(t)} y={comps.length * rowH + 12} textAnchor="middle" style={AXIS}>{t}</text>
    </g>)}
    {comps.map((c, i) => {
      const y = i * rowH + 14, ev = c.evidence, dots = partDots(c.key, ev, raw.get(c.key) ?? {}, data);
      const corr = c.key === "diversification" && ev.weighted_pair_correlation != null ? ` · weighted pair correlation ${ratio(ev.weighted_pair_correlation, 2)} over ${ev.pair_count} pairs, ${ev.observations} sessions` : "";
      const head = c.score == null
        ? `${c.label}: withheld · ${explainAll(c.reasons, data)}`
        : `${c.label}: ${c.score.toFixed(1)} of 100 × weight ${Math.round(c.weight * 100)}% = ${c.points?.toFixed(2)} points${corr}${c.status === "provisional" ? ` · provisional: ${explainAll(c.reasons, data)}` : ""}`;
      return <g key={c.key}>
        <rect x={0} y={y - 14} width={w} height={rowH - 2} style={{ fill: "transparent" }} onMouseMove={(e) => tip.show(e, head)} onMouseLeave={tip.hide} />
        <circle cx={5} cy={y} r={4} style={{ fill: PART_COLOR[c.key], pointerEvents: "none" }} />
        <text x={15} y={y} dy=".32em" style={{ ...INK2, pointerEvents: "none" }}>{narrow ? c.label.split(" ").pop() : c.label}</text>
        <text x={15} y={y + 16} style={{ ...AXIS, pointerEvents: "none" }}>{Math.round(c.weight * 100)}%</text>
        <rect x={m.l} y={y - 7} width={iw} height={14} rx={7} style={{ fill: c.score == null ? `url(#${hatch})` : "var(--surface-sunken)", pointerEvents: "none" }} />
        {c.score != null && <rect x={m.l} y={y - 7} width={Math.max(0, X(c.score) - m.l)} height={14} rx={7} style={{ fill: PART_COLOR[c.key], opacity: 0.85, pointerEvents: "none" }} />}
        {dots.map((d, j) => <circle key={j} cx={X(d.v)} cy={y} r={d.r}
          style={{ fill: "var(--surface)", stroke: "var(--text-primary)", strokeWidth: 1.5, fillOpacity: 0.9 }}
          onMouseMove={(e) => tip.show(e, d.tip)} onMouseLeave={tip.hide} />)}
        <text x={w - m.r + 10} y={y} dy=".32em" style={{ ...(c.points == null ? AXIS : FIG), pointerEvents: "none" }}>{c.points == null ? "—" : `+${c.points.toFixed(1)}`}</text>
      </g>;
    })}
  </svg>}</div>;
}

/* ------------------------------------------------------------- money vs risk ----- */

/** Euler shares of volatility at today's weights over the health window. */
export function RiskShares({ data }: { data: AnalyticsResponse }) {
  const cov = data.health.evidence?.covariance;
  const v = cov?.value;
  if (!v) return <Withheld height={220} reason={explainAll(cov?.reasons ?? ["MISSING_COMPLETE_252_INTERVAL_WINDOW"], data)} />;
  const rows = Object.entries(v.risk_shares).map(([isin, r]) => ({
    isin, symbol: symbolOf(data, isin), weight_pct: weightOf(data, isin),
    risk_share_pct: r.risk_share == null ? null : r.risk_share * 100,
  }));
  return <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    <RiskButterfly data={{ risk: {}, risk_contributions: { "1y": rows } }} />
    <ShrinkageStrip data={data} />
  </div>;
}

/** Portfolio volatility under the sample estimator and three fixed shrinkage strengths. */
function ShrinkageStrip({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const v = data.health.evidence?.covariance?.value;
  if (!v) return null;
  const points = [["0", v.annual_volatility], ...Object.entries(v.shrinkage_sensitivity).map(([a, x]) => [a, x?.annual_volatility ?? null])] as [string, number | null][];
  const vals = points.map(([, x]) => x).filter((x): x is number => x != null);
  if (!vals.length) return null;
  const lo = Math.min(...vals) * 0.97, hi = Math.max(...vals) * 1.03;
  const m = { l: 104, r: 18 }, X = (x: number) => m.l + ((x - lo) / (hi - lo || 1)) * Math.max(1, w - m.l - m.r);
  return <div ref={ref}>{w > 0 && <svg width={w} height={40} role="img" aria-label="Volatility under shrinkage">
    <text x={0} y={14} dy=".32em" style={INK2}>Shrinkage check</text>
    <line x1={m.l} x2={w - m.r} y1={14} y2={14} style={{ stroke: "var(--chart-grid)" }} />
    {points.map(([alpha, x]) => x == null ? null : <g key={alpha} onMouseMove={(e) => tip.show(e, `${alpha === "0" ? "Sample covariance" : `Shrink ${Number(alpha) * 100}% toward equal variances (not Ledoit–Wolf)`}: portfolio volatility ${pct(x, 2)} a year · ${v.observations} sessions ${v.period?.start ?? ""} – ${v.period?.end ?? ""} · today's weights on past prices`)} onMouseLeave={tip.hide}>
      <circle cx={X(x)} cy={14} r={alpha === "0" ? 6 : 4.5} style={{ fill: alpha === "0" ? "var(--text-primary)" : "var(--surface)", stroke: "var(--text-primary)", strokeWidth: 1.5 }} />
      <text x={X(x)} y={35} textAnchor="middle" style={AXIS}>{alpha === "0" ? pct(x, 1) : `α${alpha}`}</text>
    </g>)}
  </svg>}</div>;
}

/* ------------------------------------------------------------- recovery ---------- */

/** Spells below a previous high: the open one hatched, longest completed ones as bars. */
export function Recovery({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const hatch = useHatch();
  const rec = data.health.evidence?.recovery;
  const v = rec?.value;
  if (!v) return <Withheld height={140} reason={explainAll(rec?.reasons ?? ["INCOMPLETE_HEALTH_RECOVERY_WINDOW"], data)} />;
  const completed = [...v.completed_recovery_sessions].sort((a, b) => b - a);
  const spells = [...(v.current_underwater_sessions ? [{ n: v.current_underwater_sessions, open: true }] : []), ...completed.slice(0, 6).map((n) => ({ n, open: false }))];
  const rest = completed.slice(6);
  const longest = Math.max(1, ...spells.map((s) => s.n), ...rest);
  const bh = 14, gap = 6, iw = Math.max(1, w - 34);
  const X = (n: number) => Math.max(2, (iw * n) / longest);
  const window = `${v.period?.start ?? ""} – ${v.period?.end ?? ""}`;
  const h = spells.length * (bh + gap) + (rest.length ? 26 : 4);
  if (!spells.length && !rest.length) return <div ref={ref}><MissingVisual height={80} reason={`Never below a previous high inside ${window}`} /></div>;
  return <div ref={ref}>{w > 0 && <svg width={w} height={h} role="img" aria-label="Sessions spent below a previous high">
    <Hatch id={hatch} />
    {spells.map((s, i) => <g key={i} onMouseMove={(e) => tip.show(e, s.open ? `${s.n} sessions and counting · ${pct(v.terminal_drawdown, 1)} below the high · censored at ${v.period?.end ?? "the window end"}, no end date invented` : `${s.n} sessions from a high back to a new high · inside ${window}`)} onMouseLeave={tip.hide}>
      <rect x={0} y={i * (bh + gap)} width={X(s.n)} height={bh} rx={3} style={{ fill: s.open ? `url(#${hatch})` : "var(--chart-1)", stroke: s.open ? "var(--negative)" : "none" }} />
      <text x={X(s.n) + 5} y={i * (bh + gap) + bh / 2} dy=".32em" style={s.open ? { ...INK, fill: "var(--negative)" } : AXIS}>{s.n}</text>
    </g>)}
    {rest.length > 0 && <g onMouseMove={(e) => tip.show(e, `${rest.length} shorter recoveries: ${rest.join(", ")} sessions`)} onMouseLeave={tip.hide}>
      <rect x={0} y={h - 20} width={w} height={20} style={{ fill: "transparent" }} />
      <line x1={0} x2={iw} y1={h - 10} y2={h - 10} style={{ stroke: "var(--chart-grid)" }} />
      {rest.map((n, i) => <circle key={i} cx={X(n)} cy={h - 10} r={3} style={{ fill: "var(--chart-1)", fillOpacity: 0.5 }} />)}
    </g>}
  </svg>}</div>;
}

/** Tile figure for the recovery tile: longest spell, and how far below the high it ends. */
export function RecoveryFigure({ data }: { data: AnalyticsResponse }) {
  const v = data.health.evidence?.recovery?.value;
  if (!v) return null;
  return <>{v.longest_underwater_sessions}<Small>sessions · now {pct(v.terminal_drawdown, 0)}</Small></>;
}

/* ------------------------------------------------------------- capital state ----- */

const STATES: [key: string, label: string, color: string][] = [
  ["STRONG", "Strong", "var(--positive)"],
  ["WEAKENING", "Weakening", "var(--warning)"],
  ["CAUTION", "Caution", "var(--negative)"],
  ["RECOVERY", "Recovery", "var(--chart-1)"],
  ["NEUTRAL", "Neutral", "var(--ink-300)"],
  ["UNKNOWN", "Unknown", ""],
];
const STATE_RULE: Record<string, string> = {
  STRONG: "in profit, price above its 50-session average",
  WEAKENING: "in profit, price below its 50-session average",
  CAUTION: "in loss, price below its 50-session average",
  RECOVERY: "in loss, price above its 50-session average",
  NEUTRAL: "exactly break-even or exactly on the average",
  UNKNOWN: "not enough data",
};

/** Money share by state on top; below, each holding placed by profit and trend. */
export function CapitalStates({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const { masked } = usePrivacy();
  const hatch = useHatch();
  const metric = data.advisor?.sections.overview.capital_state as unknown as MilestoneMetric<AdvisorCapitalStates> | undefined;
  const value = metric?.value;
  if (!value || Object.keys(data.capital_state).length === 0) {
    return <Withheld height={220} reason={explainAll(data.health.reasons?.includes("FEATURE_DISABLED") ? ["FEATURE_DISABLED"] : metric?.reasons, data) || "no complete valuation"} />;
  }
  // Reconciliation is checked in Decimal, on the money strings themselves.
  const sum = sumDecimals(Object.values(data.capital_state));
  const total = parseDecimal(data.derived.capital_state_total);
  const reconciles = total != null && sum != null && sum.equals(total);
  const pctOf = (key: string) => data.derived.capital_state_pct[key] ?? 0;
  const color = (state: string) => STATES.find(([k]) => k === state)?.[2] || `url(#${hatch})`;
  const points = Object.entries(value.states).flatMap(([isin, s]) => {
    const e = s.evidence, cost = parseDecimal(data.positions.find((p) => p.isin === isin)?.invested), gain = parseDecimal(e.unrealised ?? null);
    const last = parseDecimal(e.latest_level ?? null), avg = parseDecimal(e.moving_average_50 ?? null);
    if (!cost || cost.isZero() || !gain || !last || !avg || avg.isZero()) return [];
    // Geometry only: both ratios are dimensionless and computed in Decimal first.
    return [{ isin, state: s.state, x: gain.div(cost).toNumber(), y: last.div(avg).minus(1).toNumber(), e }];
  });
  const unknown = Object.entries(value.states).filter(([, s]) => s.state === "UNKNOWN");
  const barH = 26, top = barH + 12, h = 236;
  const mx = Math.max(0.05, ...points.map((p) => Math.abs(p.x))) * 1.15, my = Math.max(0.02, ...points.map((p) => Math.abs(p.y))) * 1.15;
  const m = { l: 2, r: 2, b: 16 }, cw = Math.max(1, w - m.l - m.r), ch = h - top - m.b;
  const X = (x: number) => m.l + ((x + mx) / (2 * mx)) * cw, Y = (y: number) => top + (1 - (y + my) / (2 * my)) * ch;
  const mw = Math.max(1, ...data.positions.map((p) => p.weight ?? 0));
  let x0 = 0;
  const quad: [string, number, number, "start" | "end"][] = [["STRONG", w - m.r - 6, top + 14, "end"], ["RECOVERY", m.l + 6, top + 14, "start"], ["CAUTION", m.l + 6, top + ch - 8, "start"], ["WEAKENING", w - m.r - 6, top + ch - 8, "end"]];
  return <div ref={ref}>{w > 0 && <svg width={w} height={h + (unknown.length ? 24 : 0)} role="img" aria-label="Holdings by profit and price trend">
    <Hatch id={hatch} />
    {STATES.map(([key, label]) => {
      const share = pctOf(key), x = x0;
      x0 += share;
      if (share <= 0) return null;
      return <g key={key} onMouseMove={(e) => tip.show(e, `${label} · ${STATE_RULE[key]} · ${compactInr(data.capital_state[key], masked)} (${share.toFixed(1)}%)${reconciles ? ` · buckets add up to the full ${compactInr(data.derived.capital_state_total, masked)}` : " · buckets do not reconcile to a full value"}`)} onMouseLeave={tip.hide}>
        <rect x={(x / 100) * w} y={0} width={Math.max(1, (share / 100) * w - 2)} height={barH} rx={3} style={{ fill: color(key) }} />
        {(share / 100) * w > 74 && <text x={(x / 100) * w + 8} y={barH / 2} dy=".32em" style={{ ...INK, fill: "var(--surface)" }}>{label} {share.toFixed(0)}%</text>}
      </g>;
    })}
    <rect x={m.l} y={top} width={cw} height={ch} rx={4} style={{ fill: "var(--surface-sunken)", opacity: 0.6 }} />
    <line x1={X(0)} x2={X(0)} y1={top} y2={top + ch} style={{ stroke: "var(--border-strong)" }} />
    <line x1={m.l} x2={m.l + cw} y1={Y(0)} y2={Y(0)} style={{ stroke: "var(--border-strong)" }} />
    {quad.map(([key, x, y, anchor]) => <text key={key} x={x} y={y} textAnchor={anchor} style={{ ...AXIS, font: "600 10.5px var(--font-label)", fill: color(key), opacity: 0.9 }}>{STATES.find(([k]) => k === key)![1]}</text>)}
    <text x={m.l + cw} y={h - 3} textAnchor="end" style={AXIS}>profit →</text>
    <text x={m.l} y={h - 3} style={AXIS}>← loss</text>
    <text x={X(0) + 4} y={top + 10} style={AXIS}>↑ above avg</text>
    {[...points].sort((a, b) => weightOf(data, b.isin) - weightOf(data, a.isin)).map((p) => <circle key={p.isin} cx={X(p.x)} cy={Y(p.y)} r={3 + Math.sqrt(weightOf(data, p.isin) / mw) * 11}
      style={{ fill: color(p.state), fillOpacity: 0.3, stroke: color(p.state), strokeWidth: 1.5 }}
      onMouseMove={(e) => tip.show(e, `${symbolOf(data, p.isin)} · ${STATE_RULE[p.state] ?? p.state} · unrealised ${compactInr(p.e.unrealised ?? null, masked)} (${pct(p.x, 1, true)}) · last ${Number(p.e.latest_level).toFixed(2)} vs average ${Number(p.e.moving_average_50).toFixed(2)} (${pct(p.y, 1, true)}) · ${weightOf(data, p.isin).toFixed(1)}% of money${value.trend_window ? ` · ${value.trend_window.start} – ${value.trend_window.end}` : ""}`)} onMouseLeave={tip.hide} />)}
    {unknown.length > 0 && <g transform={`translate(0,${h + 6})`}>
      {unknown.map(([isin, s], i) => <g key={isin} transform={`translate(${i * 86},0)`} onMouseMove={(e) => tip.show(e, `${symbolOf(data, isin)}: ${explain(s.reason ?? "UNKNOWN", data)}`)} onMouseLeave={tip.hide}>
        <rect width={80} height={16} rx={8} style={{ fill: `url(#${hatch})` }} />
        <text x={40} y={8} dy=".32em" textAnchor="middle" style={INK2}>{symbolOf(data, isin).slice(0, 11)}</text>
      </g>)}
    </g>}
  </svg>}</div>;
}

/* ------------------------------------------------------------- sensitivity ------- */

function Chip({ text, tone, title, onTip }: { text: ReactNode; tone: string; title: string; onTip: ReturnType<typeof useTip> }) {
  return <span onMouseMove={(e) => onTip.show(e, title)} onMouseLeave={onTip.hide}
    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 28, height: 24, padding: "0 8px", borderRadius: 6, background: tone, font: "600 12px var(--font-label)", color: "var(--text-primary)" }}>{text}</span>;
}

/** Score range if part weights move ±20%, and the grade if every cut-off moves ±5. */
export function Sensitivity({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const ws = data.health.evidence?.weight_sensitivity;
  const cut = data.health.evidence?.grade_cutoff_sensitivity;
  if (!ws?.value || ws.value.minimum == null || ws.value.maximum == null) {
    return <Withheld height={140} reason={explainAll(ws?.reasons ?? ["MISSING_COMPONENT_SCORES"], data)} />;
  }
  const bands = data.health.bands.length ? data.health.bands : DEFAULT_BANDS;
  const X = (v: number) => 6 + (v / 100) * Math.max(1, w - 12);
  const { minimum: lo, maximum: hi, grades, relative_weight_change: change } = ws.value;
  const exact = num(data.health.score_exact);
  const shift = cut?.value ?? {};
  return <div ref={ref} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
    {w > 0 && <svg width={w} height={62} role="img" aria-label="Score range under weight changes">
      <Bands bands={bands} X={X} y={4} h={18} />
      <g onMouseMove={(e) => tip.show(e, `Each part's weight moved ±${pct(change, 0)} one at a time and renormalised: score ${lo.toFixed(2)} – ${hi.toFixed(2)} · a design check, not a confidence interval`)} onMouseLeave={tip.hide}>
        <rect x={X(lo) - 4} y={26} width={Math.max(3, X(hi) - X(lo)) + 8} height={22} style={{ fill: "transparent" }} />
        <rect x={X(lo)} y={32} width={Math.max(3, X(hi) - X(lo))} height={10} rx={5} style={{ fill: "var(--chart-1)" }} />
        {exact != null && <line x1={X(exact)} x2={X(exact)} y1={27} y2={47} style={{ stroke: "var(--text-primary)", strokeWidth: 2 }} />}
      </g>
      <text x={X(lo) - 6} y={37} dy=".32em" textAnchor="end" style={AXIS}>{lo.toFixed(1)}</text>
      <text x={X(hi) + 6} y={37} dy=".32em" style={AXIS}>{hi.toFixed(1)}</text>
      <text x={X(0)} y={60} style={AXIS}>0</text><text x={X(100)} y={60} textAnchor="end" style={AXIS}>100</text>
    </svg>}
    <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 12px", alignItems: "center", font: "500 11px var(--font-label)", color: "var(--text-secondary)" }}>
      <span>Weights ±{pct(change, 0)}</span>
      <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{grades.map((g) => <Chip key={g} text={g} tone="var(--chart-1-tint)" onTip={tip} title={`Grade ${g} is reachable under a ±${pct(change, 0)} weight change`} />)}</span>
      <span>Cut-offs ±5</span>
      <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{(["-5", "0", "5"] as const).map((k) => <Chip key={k} text={<>{shift[k] ?? "—"}<small style={{ marginLeft: 4, fontWeight: 500, color: "var(--text-tertiary)" }}>{k === "0" ? "now" : k === "5" ? "+5" : "−5"}</small></>} tone={k === "0" ? "var(--surface-hover)" : "var(--surface-sunken)"} onTip={tip} title={`Every grade cut-off moved ${k === "0" ? "0" : k === "5" ? "+5" : "−5"} points: grade ${shift[k] ?? "—"}`} />)}</span>
    </div>
  </div>;
}

/* ------------------------------------------------------------- quality profiles -- */

const PROFILES: [key: string, label: string, color: string, test: (s: string) => boolean, rule: string][] = [
  ["non_financial", "Non-financial", "var(--chart-3)", (s) => s === "non_financial", "tested on ROE, net margin, debt/equity and interest cover"],
  ["bank", "Bank", "var(--chart-1)", (s) => s === "bank", "tested on ROE, capital adequacy and gross NPA (a bank's debt is its deposits)"],
  ["unsupported", "NBFC / insurer", "var(--warning)", (s) => s.startsWith("UNSUPPORTED_FINANCIAL"), "no v1 test; never scored as a bank or a company"],
  ["pending", "Unclassified", "", (s) => !["non_financial", "bank"].includes(s) && !s.startsWith("UNSUPPORTED_FINANCIAL"), "no exchange basic-industry classification for this date"],
];

/** Which business-quality test applies (money share), and each scored holding's result. */
export function QualityProfiles({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const hatch = useHatch();
  const status = data.health.profile_status ?? {};
  const entries = Object.entries(status);
  if (!entries.length) return <Withheld height={110} reason="no holdings in scope" />;
  const raw = rawComponents(data).find((c) => c.key === "quality")?.evidence as { holdings?: Record<string, { profile: string; score: string; subscores: Record<string, string> }>; input_errors?: string[] } | undefined;
  const scored = raw?.holdings ?? {};
  const quality = data.health.components.find((c) => c.key === "quality");
  const groups = PROFILES.map(([key, label, color, test, rule]) => {
    const held = entries.filter(([, s]) => test(s)).map(([isin]) => isin);
    return { key, label, color, rule, held, weight: held.reduce((s, i) => s + weightOf(data, i), 0) };
  });
  const totalW = Math.max(1e-9, groups.reduce((s, g) => s + g.weight, 0));
  const unscored = entries.filter(([isin]) => !scored[isin]).sort((a, b) => weightOf(data, b[0]) - weightOf(data, a[0]));
  const why = (isin: string, s: string) => {
    const own = (raw?.input_errors ?? []).filter((e) => e.includes(isin));
    return own.length ? explainAll(own, data) : explain(s, data);
  };
  const mw = Math.max(1, ...data.positions.map((p) => p.weight ?? 0));
  const m = { l: 92, r: 44 }, iw = Math.max(1, w - m.l - m.r), X = (v: number) => m.l + (v / 100) * iw;
  const chipW = 86, perRow = Math.max(1, Math.floor(w / chipW)), chipRows = Math.ceil(unscored.length / perRow);
  const h = 100 + chipRows * 22;
  let x0 = 0;
  return <div ref={ref}>{w > 0 && <svg width={w} height={h} role="img" aria-label="Business-quality test and scores">
    <Hatch id={hatch} />
    {groups.map((g) => {
      const share = g.weight / totalW, x = x0;
      x0 += share;
      if (!g.held.length) return null;
      return <g key={g.key} onMouseMove={(e) => tip.show(e, `${g.label} · ${g.held.length} holdings · ${g.weight.toFixed(1)}% of money · ${g.rule}`)} onMouseLeave={tip.hide}>
        <rect x={x * w} y={0} width={Math.max(1, share * w - 2)} height={24} rx={3} style={{ fill: g.color || `url(#${hatch})` }} />
        {share * w > 100 && <text x={x * w + 8} y={12} dy=".32em" style={{ ...INK, fill: g.color ? "var(--surface)" : "var(--text-primary)" }}>{g.label} {Math.round(share * 100)}%</text>}
      </g>;
    })}
    <text y={54} dy=".32em" style={INK2}>Quality score</text>
    <rect x={m.l} y={48} width={iw} height={12} rx={6} style={{ fill: Object.keys(scored).length ? "var(--surface-sunken)" : `url(#${hatch})` }} />
    {[0, 50, 100].map((t) => <text key={t} x={X(t)} y={78} textAnchor="middle" style={AXIS}>{t}</text>)}
    {Object.entries(scored).map(([isin, q]) => <circle key={isin} cx={X(Number(q.score))} cy={54} r={2 + Math.sqrt(weightOf(data, isin) / mw) * 7}
      style={{ fill: q.profile === "bank" ? "var(--chart-1)" : "var(--chart-3)", fillOpacity: 0.3, stroke: q.profile === "bank" ? "var(--chart-1)" : "var(--chart-3)" }}
      onMouseMove={(e) => tip.show(e, `${symbolOf(data, isin)} · ${Number(q.score).toFixed(1)} · ${Object.entries(q.subscores).map(([k, s]) => `${k.replaceAll("_", " ")} ${Number(s).toFixed(0)}`).join(" · ")}`)} onMouseLeave={tip.hide} />)}
    {quality?.score != null && <rect x={X(quality.score) - 2} y={41} width={4} height={26} rx={2} style={{ fill: "var(--text-primary)" }} onMouseMove={(e) => tip.show(e, `Book: money-weighted ${quality.score!.toFixed(1)}`)} onMouseLeave={tip.hide} />}
    <text x={w - m.r + 6} y={54} dy=".32em" style={quality?.score != null ? FIG : AXIS}>{quality?.score != null ? quality.score.toFixed(0) : "—"}</text>
    {unscored.map(([isin, s], i) => <g key={isin} transform={`translate(${(i % perRow) * chipW},${92 + Math.floor(i / perRow) * 22})`} onMouseMove={(e) => tip.show(e, `${symbolOf(data, isin)} · ${weightOf(data, isin).toFixed(1)}% of money · not scored: ${why(isin, s)}`)} onMouseLeave={tip.hide}>
      <rect width={chipW - 6} height={16} rx={8} style={{ fill: s.startsWith("UNSUPPORTED") ? "var(--warning-tint)" : s === "non_financial" || s === "bank" ? "var(--surface-sunken)" : `url(#${hatch})` }} />
      <text x={(chipW - 6) / 2} y={8} dy=".32em" textAnchor="middle" style={INK2}>{symbolOf(data, isin).slice(0, 11)}</text>
    </g>)}
  </svg>}</div>;
}

/* ------------------------------------------------------------- history gaps ------ */

/** The one-year window as a timeline; each missing session is a red mark at its date. */
export function WindowGaps({ data }: { data: AnalyticsResponse }) {
  const [ref, w] = useWidth();
  const tip = useTip();
  const gaps = data.advisor?.scope.health_window_gaps ?? [];
  const errors = (data.advisor?.sections.diversification as { history_errors?: Record<string, string> } | undefined)?.history_errors ?? {};
  const one = data.advisor?.sections.performance.windows["1Y"];
  const cov = one?.coverage as { eligible_count?: number; total_count?: number } | undefined;
  const start = one?.period?.start ?? one?.period?.requested_start, end = one?.period?.end ?? one?.period?.as_of;
  if (!start || !end) return <Withheld height={70} reason={explainAll(one?.reasons, data) || "no one-year window"} />;
  const t0 = Date.parse(start), t1 = Date.parse(end), m = { l: 4, r: 4 }, iw = Math.max(1, w - m.l - m.r);
  const X = (d: string) => m.l + ((Date.parse(d) - t0) / (t1 - t0 || 1)) * iw;
  const errs = Object.entries(errors);
  return <div ref={ref}>{w > 0 && <svg width={w} height={errs.length ? 74 : 50} role="img" aria-label="Observed daily returns over the last year">
    <rect x={m.l} y={8} width={iw} height={16} rx={8} style={{ fill: gaps.length ? "var(--warning-tint)" : "var(--positive-tint)" }}
      onMouseMove={(e) => tip.show(e, `${cov?.eligible_count ?? "—"} of ${cov?.total_count ?? "—"} daily returns observed · ${start} – ${end}`)} onMouseLeave={tip.hide} />
    {gaps.map((g) => <rect key={g.start} x={X(g.start) - 3} y={2} width={Math.max(6, X(g.end) - X(g.start))} height={28} rx={3} style={{ fill: "var(--negative)" }}
      onMouseMove={(e) => tip.show(e, `No portfolio snapshot return ${g.start} → ${g.end}. One missing session withholds every one-year statistic.`)} onMouseLeave={tip.hide} />)}
    <text x={m.l} y={42} style={AXIS}>{start}</text>
    <text x={w / 2} y={42} textAnchor="middle" style={{ ...INK, fill: gaps.length ? "var(--negative)" : "var(--positive)" }}>{cov?.eligible_count ?? "—"}/{cov?.total_count ?? "—"}</text>
    <text x={w - m.r} y={42} textAnchor="end" style={AXIS}>{end}</text>
    {errs.map(([isin, e], i) => <g key={isin} transform={`translate(${i * 96},56)`} onMouseMove={(ev) => tip.show(ev, `${symbolOf(data, isin)}: price history rejected · ${explain(e, data)}`)} onMouseLeave={tip.hide}>
      <rect width={90} height={16} rx={8} style={{ fill: "var(--negative-tint)" }} />
      <text x={45} y={8} dy=".32em" textAnchor="middle" style={INK2}>{symbolOf(data, isin).slice(0, 12)}</text>
    </g>)}
  </svg>}</div>;
}
