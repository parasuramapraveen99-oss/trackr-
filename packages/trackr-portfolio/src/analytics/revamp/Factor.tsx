"use client";

/**
 * Factor Score (review §5, comments 7–9). One view only.
 *
 * Four cards in a 2×2 grid (percentile vs the stated universe) beside a per-stock column
 * chart for the selected factor with tertile colours stated in the legend, holdings without inputs listed with
 * the reason, and the footer "Compared with Nifty 500 · As of … · Coverage ⓘ". No
 * Reported/Validated toggle: one source.
 */

import { useState } from "react";
import type { AnalyticsResponse } from "@trackr/core";
import { SegmentedControl } from "@trackr/ui";
import { CoverageFooter, Grid, Info, Panel, faint, muted, reasonText } from "./common";

const FACTORS = {
  momentum: {
    label: "Momentum Factor",
    short: "Measures recent price momentum relative to other stocks. Higher score = stronger momentum.",
    calc: "12-month price return skipping the most recent month, divided by the stock's volatility; ranked as a percentile against the universe.",
  },
  value: {
    label: "Value Factor",
    short: "Compares stock prices with earnings and book value. Higher score = greater relative value.",
    calc: "Earnings yield (E/P) and book yield (B/P), each ranked against the universe and averaged.",
  },
  quality: {
    label: "Quality Factor",
    short: "Measures profitability and debt levels. Higher score = stronger business quality.",
    calc: "Return on equity, net margin and debt/equity, ranked against the universe; banks use their own profile.",
  },
  low_volatility: {
    label: "Low Volatility Factor",
    short: "Measures historical price fluctuations. Higher score = lower historical volatility.",
    calc: "Annualised volatility of daily returns over 252 sessions, ranked in reverse against the universe.",
  },
} as const;
type FactorKey = keyof typeof FACTORS;

const tertile = (v: number) => (v >= 67 ? "var(--positive)" : v >= 34 ? "var(--warning)" : "var(--negative)");
const universeName = (u: string | undefined) => (u === "NIFTY500" ? "Nifty 500" : u ?? "the universe");
const short = (key: FactorKey) => FACTORS[key].label.replace(" Factor", "");

/** Per-holding percentile columns (review §5 mockup). Only real scores are drawn; with none,
 * the empty frame says why instead of showing placeholder bars. */
function Columns({ rows, empty }: { rows: { symbol: string; score: number }[]; empty: string }) {
  const W = 680, H = 220, top = 14, left = 34;
  const bw = rows.length ? (W - left) / rows.length : 0;
  const Y = (v: number) => top + (1 - v / 100) * H;
  return <svg viewBox={`0 0 ${W} ${H + top + 70}`} width="100%" role="img" aria-label="Factor percentile by holding">
    {[0, 33, 67, 100].map((t) => <g key={t}>
      <line x1={left} x2={W} y1={Y(t)} y2={Y(t)} style={{ stroke: "var(--border)" }} />
      <text x={left - 6} y={Y(t) + 4} textAnchor="end" style={{ fill: "var(--text-tertiary)", font: "500 10px var(--font-label)" }}>{t}</text>
    </g>)}
    {rows.length === 0 && <text x={(W + left) / 2} y={Y(50)} textAnchor="middle" style={{ fill: "var(--text-secondary)", font: "500 13px var(--font-label)" }}>{empty}</text>}
    {rows.map((r, i) => {
      const x = left + i * bw + 3, w = Math.max(2, bw - 6), c = tertile(r.score);
      return <g key={r.symbol}>
        <title>{`${r.symbol}: ${r.score.toFixed(0)}`}</title>
        <rect x={x} y={Y(r.score)} width={w} height={Math.max(0, Y(0) - Y(r.score))} rx={2} style={{ fill: c }} />
        <text x={x + w / 2} y={Y(r.score) - 4} textAnchor="middle" style={{ fill: c, font: "600 10px var(--font-label)" }}>{r.score.toFixed(0)}</text>
        <text transform={`translate(${x + w / 2},${Y(0) + 14}) rotate(-40)`} textAnchor="end" style={{ fill: "var(--text-secondary)", font: "500 10px var(--font-label)" }}>{r.symbol}</text>
      </g>;
    })}
  </svg>;
}

export function FactorTab({ data }: { data: AnalyticsResponse }) {
  const [factor, setFactor] = useState<FactorKey>("momentum");
  const f = data.factors;
  const rows = data.positions.map((p) => ({ symbol: p.symbol ?? p.isin, weight: p.weight ?? 0, score: p.factors?.[factor] ?? null }));
  const scored = rows.filter((r): r is typeof r & { score: number } => r.score != null).sort((a, b) => b.score - a.score);
  const unscored = rows.filter((r) => r.score == null);
  const current = f[factor];
  return <Grid>
    <div className="rv-factor-cards" style={{ gridColumn: "span 5", minWidth: 0, display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gridTemplateRows: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
      {(Object.keys(FACTORS) as FactorKey[]).map((key) => {
        const s = f[key];
        return <Panel key={key} span={1} label={short(key)} style={{ gridColumn: "auto", justifyContent: "space-between" }} info={<Info title={FACTORS[key].label} calc={<>
          {FACTORS[key].calc} Portfolio score = value-weighted average of holding percentiles; holdings without inputs are left out, not scored as zero.
          {" "}Universe: {universeName(s?.universe)} ({s?.universe_size ?? 0} stocks).
          {s?.score == null && (s?.reasons?.length ?? 0) > 0 && <> Not scored: {s!.reasons!.map(reasonText).join("; ")}.</>}
        </>}>{FACTORS[key].short}</Info>}>
          <div style={{ font: "400 32px var(--font-slip)", lineHeight: 1, color: s?.score != null ? tertile(s.score) : "var(--text-tertiary)" }}>
            {s?.score != null ? <>{s.score.toFixed(0)}<span style={{ ...muted, fontSize: 13 }}>/100</span></> : "—"}
          </div>
          <div style={{ height: 6, borderRadius: 3, background: "var(--surface-sunken)" }}>
            {s?.score != null && <div style={{ width: `${s.score}%`, height: 6, borderRadius: 3, background: tertile(s.score) }} />}
          </div>
          <div style={faint}>Percentile vs {universeName(s?.universe)}{s?.score != null && ` · ${s.coverage_pct.toFixed(0)}% of value scored`}</div>
          {s?.score == null && <div style={{ ...faint, color: "var(--warning)" }}>Not enough data to score yet</div>}
        </Panel>;
      })}
    </div>
    <Panel span={7} label={`${short(factor)} by holding`} right={
      <SegmentedControl size="sm" value={factor} onChange={setFactor} options={(Object.keys(FACTORS) as FactorKey[]).map((k) => ({ value: k, label: short(k) }))} />}>
      <Columns rows={scored} empty="Not enough data to score holdings yet" />
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", ...faint }}>
        <span><b style={{ color: "var(--positive)" }}>●</b> Top third (≥67)</span><span><b style={{ color: "var(--warning)" }}>●</b> Middle (34–66)</span><span><b style={{ color: "var(--negative)" }}>●</b> Bottom third (≤33)</span>
        <span>Percentile vs {universeName(current?.universe)}</span>
      </div>
      {unscored.length > 0 && scored.length > 0 && <div style={faint}>Not scored ({unscored.length}): {unscored.map((r) => r.symbol).join(", ")}.</div>}
    </Panel>
    <div style={{ ...faint, gridColumn: "1 / -1" }}>
      Compared with {universeName(current?.universe)} · As of {data.as_of} · Coverage {current?.coverage_pct?.toFixed(0) ?? 0}% of value for {short(factor).toLowerCase()}
      <Info title="Coverage">Each factor&apos;s coverage is the share of portfolio value with every input that factor needs. Prices: NSE/BSE bhavcopy. Fundamentals: vendor data as of the date shown on the Fundamentals tab.</Info>
    </div>
    <CoverageFooter data={data} />
  </Grid>;
}
