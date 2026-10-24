"use client";

/**
 * Factor Score (review §5, comments 7–9). One view only.
 *
 * Four cards (percentile vs the stated universe), a per-stock bar chart for the selected
 * factor with tertile colours stated in the legend, holdings without inputs listed with
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

export function FactorTab({ data }: { data: AnalyticsResponse }) {
  const [factor, setFactor] = useState<FactorKey>("momentum");
  const f = data.factors;
  const rows = data.positions.map((p) => ({ symbol: p.symbol ?? p.isin, weight: p.weight ?? 0, score: p.factors?.[factor] ?? null }));
  const scored = rows.filter((r) => r.score != null).sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const unscored = rows.filter((r) => r.score == null);
  const current = f[factor];
  const reasons = (current?.reasons ?? []).map(reasonText).join("; ");
  return <Grid>
    {(Object.keys(FACTORS) as FactorKey[]).map((key) => {
      const s = f[key];
      return <Panel key={key} span={3} label={FACTORS[key].label} info={<Info title={FACTORS[key].label} calc={<>
        {FACTORS[key].calc} Portfolio score = value-weighted average of holding percentiles; holdings without inputs are left out, not scored as zero.
        {" "}Universe: {universeName(s?.universe)} ({s?.universe_size ?? 0} stocks).
      </>}>{FACTORS[key].short}</Info>}>
        <div style={{ font: "600 26px var(--font-figure)" }}>{s?.score != null ? <>{s.score.toFixed(0)}<span style={muted}>/100</span></> : <span style={{ color: "var(--text-secondary)", fontSize: 18 }}>Not scored</span>}</div>
        <div style={faint}>{s?.score != null ? `Percentile vs ${universeName(s.universe)} · ${s.coverage_pct.toFixed(0)}% of value scored` : (s?.reasons ?? []).map(reasonText).join("; ") || "No inputs"}</div>
        {s?.score != null && <div style={{ height: 6, borderRadius: 3, background: "var(--surface-sunken)" }}>
          <div style={{ width: `${s.score}%`, height: 6, borderRadius: 3, background: tertile(s.score) }} />
        </div>}
        <div style={{ ...faint, color: "var(--text-secondary)" }}>{FACTORS[key].short}</div>
      </Panel>;
    })}
    <Panel label={`${FACTORS[factor].label.replace(" Factor", "")} score by holding`} right={
      <SegmentedControl size="sm" value={factor} onChange={setFactor} options={(Object.keys(FACTORS) as FactorKey[]).map((k) => ({ value: k, label: FACTORS[k].label.replace(" Factor", "") }))} />}>
      <div style={{ display: "flex", gap: 12, ...faint }}>
        <span><b style={{ color: "var(--positive)" }}>●</b> ≥67</span><span><b style={{ color: "var(--warning)" }}>●</b> 34–66</span><span><b style={{ color: "var(--negative)" }}>●</b> ≤33</span>
        <span>Percentile vs {universeName(current?.universe)}</span>
      </div>
      {scored.length === 0
        ? <div style={muted}>No holding could be scored{reasons ? `: ${reasons}` : ""}.</div>
        : <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>{scored.map((r) => <div key={r.symbol} style={{ display: "grid", gridTemplateColumns: "110px 1fr 40px", alignItems: "center", gap: 8, font: "500 12px var(--font-label)" }}>
          <span>{r.symbol}</span>
          <span style={{ height: 12, borderRadius: 3, background: "var(--surface-sunken)" }}><span style={{ display: "block", width: `${r.score}%`, height: 12, borderRadius: 3, background: tertile(r.score!) }} /></span>
          <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{r.score!.toFixed(0)}</span>
        </div>)}</div>}
      {unscored.length > 0 && <div style={faint}>Not scored ({unscored.length}): {unscored.map((r) => r.symbol).join(", ")}{reasons ? ` — ${reasons}` : ""}.</div>}
    </Panel>
    <div style={{ ...faint, gridColumn: "1 / -1" }}>
      Compared with {universeName(current?.universe)} · As of {data.as_of} · Coverage {current?.coverage_pct?.toFixed(0) ?? 0}% of value for {FACTORS[factor].label.replace(" Factor", "").toLowerCase()}
      <Info title="Coverage">Each factor&apos;s coverage is the share of portfolio value with every input that factor needs. Prices: NSE/BSE bhavcopy. Fundamentals: vendor data as of the date shown on the Fundamentals tab.</Info>
    </div>
    <CoverageFooter data={data} />
  </Grid>;
}
