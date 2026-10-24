"use client";

/**
 * Performance (review §9, comments 17–20).
 *
 * Basic: ₹1,00,000 a year ago, portfolio vs Nifty 50 TRI (the server's Decimal figures,
 * one 1Y number everywhere), the difference worded from its sign, the percentage-point gap
 * from unrounded returns, then the written lines.
 * Advanced: Return / Volatility / Sharpe / Sortino / VaR cards for the chosen period; the
 * growth chart whose first date IS the title's date; your money vs the index as XIRR for
 * 6M (not annualised), 1Y and 3Y (annualised) and since your first investment; 1Y gain by
 * holding; holding returns across time with a reason for every empty cell.
 */

import { useState } from "react";
import type { AnalyticsResponse, CashFlowWindow, ContributionResponse } from "@trackr/core";
import { Select } from "@trackr/ui";
import { BasicInsights } from "../../widgets/BasicInsights";
import { AdvisorGrowth, type AdvisorPeriod } from "../AdvisorVisuals";
import { GainBars } from "../visual";
import { riskPeriods } from "./Risk";
import { CoverageFooter, Grid, INDEX, Info, Panel, Periods, Rs, Stat, YOU, faint, fmtFrac, fmtNum, fmtPct, muted, reasonText } from "./common";

const RISK_KEY: Record<string, string> = { "1M": "1m", "3M": "3m", "6M": "6m", "1Y": "1y", "3Y": "3y", "5Y": "5y", All: "available" };

export function PerformanceBasic({ data }: { data: AnalyticsResponse }) {
  const facts = data.basic?.facts ?? {};
  const one = data.basic?.tabs.performance?.candidates.find((c) => c.slot === "PF.ONE_LAKH");
  const r = data.risk["1y"];
  const gapPp = r?.cum_return != null && r?.bench_cum_return != null ? (r.cum_return - r.bench_cum_return) * 100 : null;
  return <Grid>
    {one ? <>
      <Panel span={6} label={<span style={{ color: YOU }}>Your portfolio</span>}>
        <div style={faint}>₹1,00,000 invested on {String(facts[one.facts.start!] ?? "")} would be</div>
        <div style={{ font: "600 26px var(--font-figure)", color: YOU }}><Rs v={String(facts[one.facts.yours!])} /></div>
        <div style={muted}>{fmtFrac(r?.cum_return, 1, true)} · time-weighted, cash flows neutralised</div>
      </Panel>
      <Panel span={6} label={<span style={{ color: INDEX }}>Nifty 50 TRI</span>}>
        <div style={faint}>The same ₹1,00,000 in the index would be</div>
        <div style={{ font: "600 26px var(--font-figure)", color: INDEX }}><Rs v={String(facts[one.facts.index!])} /></div>
        <div style={muted}>{fmtFrac(r?.bench_cum_return, 1, true)} · total-return index</div>
      </Panel>
      <div style={{ gridColumn: "1 / -1", padding: "10px 14px", borderRadius: 8, border: `1px solid ${one.direction === "less" ? "var(--negative)" : "var(--positive)"}`, font: "600 14px var(--font-body)", color: one.direction === "less" ? "var(--negative)" : "var(--positive)" }}>
        You would have <Rs v={String(facts[one.facts.gap!])} /> {one.direction === "less" ? "less" : "more"} than in the index
        <span style={{ ...faint, marginLeft: 8 }}>({fmtNum(gapPp, 1)} percentage points over the year)</span>
      </div>
    </> : <Panel label="₹1,00,000 a year ago"><div style={muted}>Not available: this portfolio does not have a full year of recorded history yet.</div></Panel>}
    <BasicInsights tab="performance" />
    <CoverageFooter data={data} />
  </Grid>;
}

function xirrRow(label: string, w: CashFlowWindow | undefined, nonAnnual = false) {
  if (!w) return { label, missing: "History is shorter than this window" };
  const mine = nonAnnual ? w.period_return : w.xirr, bench = nonAnnual ? w.benchmark_period_return : w.benchmark_xirr;
  return { label, mine, bench, start: w.start, end: w.end, unit: nonAnnual ? "over the period" : "a year", coverage: w.coverage_pct,
    missing: mine == null ? "XIRR undefined for this window" : null, excluded: w.excluded };
}

function heat(v: number | null): string {
  if (v == null) return "transparent";
  const strength = Math.round(Math.min(1, Math.abs(v) / 100) ** 0.6 * 70);
  return `color-mix(in srgb, ${v >= 0 ? "var(--positive)" : "var(--negative)"} ${Math.max(6, strength)}%, transparent)`;
}

function HoldingReturns({ data }: { data: AnalyticsResponse }) {
  const cols = ["1w", "1m", "3m", "1y", "3y", "5y"];
  const reasons = data.revamp?.diversification.return_reasons ?? {};
  return <Panel label="Holding returns across time" info={<Info title="Holding returns" calc="End close ÷ start close − 1 over each calendar window, from official exchange closes within five days of each end. Hover a cell for its dates.">
    Shows each stock&apos;s cumulative price return over the stated period, adjusted for corporate actions and excluding cash dividends—not your personal investment return. Stronger green indicates larger gains; stronger red indicates larger losses. &lsquo;—&rsquo; means unavailable data; hover or tap to see why.</Info>}>
    <div style={muted}>Cumulative price returns · As of {data.as_of}</div>
    <div className="rv-scroll"><table className="rv-table">
      <thead><tr><th>Holding</th>{cols.map((c) => <th key={c} className="num">{c.toUpperCase()}</th>)}</tr></thead>
      <tbody>{data.positions.map((p) => <tr key={p.isin}><td>{p.symbol ?? p.isin}</td>{cols.map((c) => {
        const cell = p.return_windows?.[c];
        const v = cell?.return_pct ?? null;
        const why = reasons[p.isin]?.[c];
        return <td key={c} className="num" style={{ background: heat(v) }} title={v == null ? reasonText(why) : `${cell?.start} – ${cell?.end}`}>
          {v == null ? <span style={faint}>—</span> : fmtPct(v, 1, true)}</td>;
      })}</tr>)}</tbody>
    </table></div>
    <div style={{ display: "flex", alignItems: "center", gap: 8, ...faint }}>
      <span>−100%</span><span style={{ width: 160, height: 10, borderRadius: 5, background: "linear-gradient(90deg, var(--negative), transparent, var(--positive))" }} /><span>+100% or more</span>
      <span>· — unavailable (hover for the reason)</span>
    </div>
  </Panel>;
}

export function PerformanceAdvanced({ data, contribution, benchmark, setBenchmark }: {
  data: AnalyticsResponse; contribution: ContributionResponse | null; benchmark: string; setBenchmark: (b: string) => void;
}) {
  const options = riskPeriods(data);
  const [period, setPeriod] = useState<AdvisorPeriod>((options.find((o) => o.key === "1Y" && !o.disabled)?.key ?? options.find((o) => !o.disabled)?.key ?? "1Y") as AdvisorPeriod);
  const w = data.risk[RISK_KEY[period]!];
  const growth = data.advisor?.sections.performance.windows[period]?.value?.growth_series.value ?? [];
  const windows = data.performance.windows;
  const xrows = [xirrRow("6M", windows["6m"], true), xirrRow("1Y", windows["1y"]), xirrRow("3Y", windows["3y"]), xirrRow("Since first investment", windows.all)];
  // Exactly one year needs no annualising (cumulative = annual); beyond a year the
  // per-year rate is shown, and a missing one stays "—".
  const annual = period === "3Y" || period === "5Y" || period === "All";
  return <Grid>
    <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
      <Periods options={options} value={period} onChange={(k) => setPeriod(k as AdvisorPeriod)} />
      <label style={{ ...faint, display: "inline-flex", alignItems: "center", gap: 6 }}>Benchmark
        <Select size="sm" value={benchmark} onChange={setBenchmark} options={[{ value: "NIFTY50_TRI", label: "Nifty 50 TRI" }, { value: "NIFTY500_TRI", label: "Nifty 500 TRI" }]} /></label>
    </div>
    <Stat label={`Return · ${period}`} value={fmtFrac(annual ? w?.cagr : w?.cum_return, 1, true)} sub={<>{annual ? "a year (p.a.), time-weighted" : period === "1Y" ? "over exactly one year, time-weighted" : "over the period, time-weighted"} · index {fmtFrac(annual ? w?.bench_cagr : w?.bench_cum_return, 1, true)}</>}
      info={<Info title="Return" calc="Chained daily time-weighted returns (cash flows neutralised); annualised only for periods of a year or more.">What the portfolio itself returned, independent of when you added or withdrew money.</Info>} />
    <Stat label="Volatility" value={fmtFrac(w?.volatility, 1)} sub={`index ${fmtFrac(w?.bench_volatility, 1)}`} />
    <Stat label="Sharpe" span={2} value={fmtNum(w?.sharpe, 2)} sub={`index ${fmtNum(w?.bench_sharpe, 2)}`} />
    <Stat label="Sortino" span={2} value={fmtNum(w?.sortino, 2)} />
    <Stat label="Bad day (1 in 20)" span={2} value={w?.var_1d_95_value ? <Rs v={w.var_1d_95_value} /> : <span style={{ fontSize: 15, color: "var(--text-secondary)" }}>Not measured</span>} sub={w?.var_1d_95 != null ? fmtFrac(w.var_1d_95, 2) : `${w?.observations ?? 0} of 252 daily returns`} />
    <Panel span={8} label={growth.length ? `Value of ₹1,00,000 invested on ${growth[0]!.date}` : "Growth of ₹1,00,000"}
      right={<span style={{ display: "inline-flex", gap: 10, ...faint }}><span style={{ color: YOU }}>● Your portfolio</span><span style={{ color: INDEX }}>● {benchmark === "NIFTY500_TRI" ? "Nifty 500 TRI" : "Nifty 50 TRI"}</span></span>}>
      <AdvisorGrowth data={data} period={period} basis="recorded" />
    </Panel>
    <Panel span={4} label="Your money vs the index" info={<Info title="XIRR" calc="Your XIRR on your actual cash flows, against the XIRR of putting the same cash on the same dates into the index. 6M is the period's own money-weighted return (not annualised); 1Y and 3Y are a year (annualised).">
      The most honest comparison for an investor: same money, same dates.</Info>}>
      {xrows.map((r) => <div key={r.label} style={{ padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", font: "600 12px var(--font-label)" }}><span>{r.label}</span>
          {"start" in r && r.start && <span style={faint}>{r.start} – {r.end}</span>}</div>
        {r.missing ? <div style={faint}>{r.missing}</div> : <>
          <div style={{ display: "flex", gap: 14, font: "600 14px var(--font-figure)" }}>
            <span style={{ color: YOU }}>You {fmtFrac(r.mine, 1, true)}</span><span style={{ color: INDEX }}>Index {fmtFrac(r.bench, 1, true)}</span>
          </div>
          <div style={faint}>{r.unit}{r.mine != null && r.bench != null ? ` · ${fmtNum((Number(r.mine) - Number(r.bench)) * 100, 1)} pp` : ""}{(r.coverage ?? 100) < 100 ? ` · covers ${fmtPct(r.coverage, 0)} of value` : ""}</div>
        </>}
      </div>)}
    </Panel>
    <Panel label="What drove your return · 1Y gain by holding" info={<Info title="1Y gain by holding" calc="(end value − start value) − net money put in, per holding, over the trailing year. Sold holdings are included.">Which holdings added or took away the most rupees this year.</Info>}>
      {contribution ? <GainBars contribution={contribution} /> : <div style={muted}>Loading contributions…</div>}
    </Panel>
    <HoldingReturns data={data} />
    <CoverageFooter data={data} />
  </Grid>;
}
